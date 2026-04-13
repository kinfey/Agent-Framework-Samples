# 基于 Microsoft Agent Framework 与 Azure 实现云原生的 Anthropic Managed Agent 架构

> Anthropic 在 [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents) 中提出了一个核心命题：**Agent 系统的可靠性取决于组件之间的解耦程度，而非单一组件的复杂度**。本文将这套架构理论完整落地到 **Microsoft Agent Framework（MAF）+ Azure AI Foundry** 技术栈上，并通过可运行的代码验证每一个设计决策。

**内容结构**

| 章节 | 主题 | 对应 Anthropic 概念 |
|------|------|---------------------|
| 一 | 单体 Agent 的失败模式分析 | "Don't adopt a pet" |
| 二 | 三层解耦架构全景 | Session / Harness / Sandbox |
| 三 | Session：持久化事件日志 | `emitEvent` · `getEvents` · `wake` |
| 四 | Sandbox：可替换的执行环境 | `execute(name, input) → string` |
| 五 | Harness：无状态编排器 | 崩溃恢复 · `wake(sessionId)` |
| 六 | MAF 原生扩展：Skill · Middleware · Foundry | 超越三层的工程实践 |
| 七 | 多大脑多双手：并行编排 | "Many brains, many hands" |
| 八 | 延迟沙箱创建与 TTFT 优化 | p50 ↓60%、p95 ↓90% |
| 九 | Azure Functions 无状态托管 | Serverless 部署 |
| 十 | 开发到生产的接口稳定性 | "Opinionated about interfaces" |

---

## 一、单体 Agent 的失败模式分析

### 从 Anthropic 的教训说起

Anthropic 最初将所有 Agent 组件（会话状态、编排逻辑、代码执行环境）部署在同一个容器内。这种设计有一个显而易见的好处：文件操作是直接的系统调用，没有跨服务的序列化开销。但正如他们在文章中所述：

> "We'd adopted a pet. If a container failed, the session was lost. If a container was unresponsive, we had to nurse it back to health."

这个"宠物"隐喻精确描述了单体架构的四类失败模式：

| 失败模式 | 根因 | 后果 |
|----------|------|------|
| **状态丢失** | 会话日志与编排器共存 | 容器崩溃 = 完整上下文丢失 |
| **调试困难** | WebSocket 事件流无法区分故障来源 | 编排器 bug、网络丢包、容器宕机表现相同 |
| **安全越界** | LLM 生成的代码与凭据同处一个进程 | Prompt injection → 凭据泄露 |
| **网络耦合** | 编排器假设所有资源与它同域 | 客户 VPC 接入需要网络对等互联 |

**具体场景**：某工程师使用 Agent 排查线上故障，Agent 已分析了 200MB 日志、执行了 12 条诊断命令、定位到根因是数据库连接池泄漏。此时容器因 OOM 崩溃。由于会话状态内嵌在编排器进程中，30 分钟的排查进度全部归零 — 用户必须从头描述问题背景，Agent 必须重新执行全部诊断步骤。

这不是假想的极端情况。对于任何长时间运行的 Agent 任务（代码审查、数据分析、多步骤部署），单体架构都意味着**系统的可靠性等于最弱组件的可靠性**。操作系统设计已经回答过这个问题：**隔离是可靠性的前提**。

---

## 二、架构全景：三层解耦

Anthropic 的解决方案是将 Agent 虚拟化为三个独立接口，每个接口可以独立失败、独立替换：

```
┌──────────────────────────────────────────────────────────┐
│                    Session（会话日志）                      │
│            持久化、仅追加（append-only）事件存储              │
│                                                          │
│  create_session(task) → id                               │
│  emitEvent(id, event) → void                             │
│  getEvents(id, start?, end?) → events[]                  │
│  wake(id) → (session, events)                            │
└───────────────────────────┬──────────────────────────────┘
                            │  读写事件
┌───────────────────────────┼──────────────────────────────┐
│               Harness（编排器 / 大脑）                      │
│        无状态 LLM 循环 · 崩溃后通过 wake() 恢复              │
│                                                          │
│  start(session_id)  →  wake()  →  重建上下文               │
│  run(input)  →  Agent.run()  →  emit_event()             │
│  shutdown()  →  emit SESSION_END                         │
└───────────────────────────┬──────────────────────────────┘
                            │  execute(name, input) → string
┌───────────────────────────┼──────────────────────────────┐
│               Sandbox（沙箱 / 双手）                       │
│      隔离执行环境 · 可替换 · 凭据不可达                       │
│                                                          │
│  provision(resources) → sandbox_id                       │
│  execute(name, input) → string                           │
│  kill() → 标记为 dead，编排器创建替代品                      │
└──────────────────────────────────────────────────────────┘
```

这种分层的设计哲学与操作系统高度一致。Anthropic 在文章结尾处明确阐述了这个类比：

> "Operating systems have lasted decades by virtualizing the hardware into abstractions general enough for programs that didn't exist yet. The `read()` command is agnostic as to whether it's accessing a disk pack from the 1970s or a modern SSD."

MAF 提供了落地这套架构所需的全部原语：`Agent` 作为编排器核心、`Skill` 注入领域知识、`@agent_middleware` 处理横切关注点、`FoundryChatClient` 连接 Azure AI Foundry 模型端点。下面逐层展开实现。

---

## 三、第一层：Session — 持久化事件日志

### 设计原理

Anthropic 对 Session 的定位不仅是"存聊天记录"，而是一个**独立于编排器和沙箱的持久化事件源**：

> "The session log sits outside the harness. Nothing in the harness needs to survive a crash. When one fails, a new one can be rebooted with `wake(sessionId)`."

更重要的是，Anthropic 明确区分了 Session 与 LLM 的上下文窗口：

> "The session is not Claude's context window... `getEvents()` allows the brain to interrogate context by selecting positional slices of the event stream."

这意味着 Session 是一个**可查询的事件数据库**，而非仅仅是传递给 LLM 的消息列表。编排器可以从中按位置切片、按类型过滤、在崩溃后从任意事件点恢复。

### MAF 实现：`session/session_log.py`

```python
class EventKind(str, Enum):
    """11 种事件类型，覆盖 Agent 完整生命周期。"""
    SESSION_START   = "session_start"    #  会话创建
    USER_INPUT      = "user_input"       #  用户输入
    AGENT_RESPONSE  = "agent_response"   #  模型回复
    TOOL_CALL       = "tool_call"        #  工具调用请求
    TOOL_RESULT     = "tool_result"      #  工具执行结果
    HARNESS_WAKE    = "harness_wake"     #  编排器恢复
    HARNESS_CRASH   = "harness_crash"    #  编排器崩溃记录
    SANDBOX_SPAWN   = "sandbox_spawn"    #  沙箱创建
    SANDBOX_EXEC    = "sandbox_exec"     #  沙箱执行
    SESSION_END     = "session_end"      #  会话结束
    COMPACTION      = "compaction"       #  上下文压缩
```

事件模型是 Session 设计的基石。为什么选择结构化事件而非原始消息？因为 Agent 的行为远不止"对话"：

```python
@dataclass
class SessionEvent:
    kind:       EventKind           # 事件类型
    payload:    dict[str, Any]      # 业务数据（工具名、输入、输出等）
    timestamp:  float               # 精确到毫秒的时间戳
    event_id:   str                 # 全局唯一标识
    session_id: str                 # 所属会话
```

基于此，`SessionLog` 实现了 Anthropic 定义的四个核心接口：

```python
class SessionLog:
    """
    仅追加（append-only）持久化事件存储。
    会话独立于编排器和沙箱 — 编排器崩溃不影响会话完整性。
    """

    async def create_session(self, task: str, metadata: dict | None = None) -> str:
        """创建会话，发出 SESSION_START 事件，返回 session_id。"""
        session_id = str(uuid.uuid4())
        af_session = AgentSession(session_id=session_id)
        self._sessions[session_id] = af_session
        self._history[session_id]  = InMemoryHistoryProvider()  # AF 原生历史提供者
        await self.emit_event(session_id, SessionEvent(
            kind=EventKind.SESSION_START,
            session_id=session_id,
            payload={"task": task, "metadata": metadata or {}},
        ))
        return session_id

    async def emit_event(self, session_id: str, event: SessionEvent) -> None:
        """向日志追加事件 — 对应 Anthropic 的 emitEvent(id, event)。"""
        event.session_id = session_id
        async with self._lock:  # asyncio.Lock 保证并发安全
            self._store.setdefault(session_id, []).append(event)

    async def get_events(
        self, session_id: str,
        start: int = 0, end: int | None = None,
        kind_filter: list[EventKind] | None = None,
    ) -> list[SessionEvent]:
        """
        位置切片 + 类型过滤 — 对应 Anthropic 的 getEvents()。
        支持"从上次停止的位置继续"的恢复模式。
        """
        events = self._store.get(session_id, [])[start:end]
        if kind_filter:
            events = [e for e in events if e.kind in kind_filter]
        return events

    async def wake(self, session_id: str) -> tuple[AgentSession | None, list[SessionEvent]]:
        """
        编排器恢复入口 — 对应 Anthropic 的 wake(sessionId)。
        返回 (session_metadata, all_events)，新编排器据此重建上下文。
        """
        session = await self.get_session(session_id)
        events  = await self.get_events(session_id)
        await self.emit_event(session_id, SessionEvent(
            kind=EventKind.HARNESS_WAKE,
            session_id=session_id,
            payload={"resumed_at_event": len(events)},
        ))
        return session, events
```

**上下文工程接口**：Anthropic 指出："Any fetched events can also be transformed in the harness before being passed to Claude's context window."。`SessionLog` 提供了一个轻量级的上下文窗口方法，编排器据此实施滑动窗口策略：

```python
    async def get_context_window(self, session_id: str, last_n: int = 20) -> list[SessionEvent]:
        """最近 N 条事件 — 编排器用于构建 LLM 上下文窗口的基础数据。"""
        events = await self.get_events(session_id)
        return events[-last_n:]
```

**使用示例** — 以下代码展示了 Session 如何在实际对话中产生结构化的事件流：

```python
# demo: 演示 6 — 会话日志检查
sid = await session_log.create_session("Log inspection demo")
h   = _harness()
await h.start(sid)
for q in ["Hello!", "What is 1 + 1?", "Thanks, bye."]:
    await h.run(q)
await h.shutdown()

# 查询完整事件流
all_events = await session_log.get_events(sid)
# → [SESSION_START, HARNESS_WAKE, USER_INPUT, AGENT_RESPONSE, ... SESSION_END]

# 滑动窗口：最近 5 条
recent = await session_log.get_context_window(sid, last_n=5)

# 按类型过滤：只看 Agent 回复
responses = await session_log.get_events(sid, kind_filter=[EventKind.AGENT_RESPONSE])

# 位置切片：事件 1 到 4
sliced = await session_log.get_events(sid, start=1, end=4)
```

**生产路径**：开发环境使用 `InMemoryHistoryProvider`（内存字典）；生产环境替换为 Azure CosmosDB 或 Redis — 接口完全不变。

---

## 四、第二层：Sandbox — 可替换的执行环境

### 设计原理

Anthropic 对沙箱的定位极为精确 — 一个只有两个方法的接口：

> "The container became cattle. If the container died, the harness caught the failure as a tool-call error and passed it back to Claude. Claude could decide to retry, and a new container could be reinitialized with `provision({resources})`."

更关键的是安全边界：

> "A prompt injection only had to convince Claude to read its own environment. Once an attacker has those tokens, they can spawn fresh, unrestricted sessions. The structural fix was to make sure the tokens are never reachable from the sandbox."

### MAF 实现：`sandbox/sandbox.py`

**凭据隔离 — `VaultStore`**

这是整个沙箱设计中最重要的安全组件。Anthropic 使用两种模式确保凭据不进入沙箱：一是将凭据绑定到资源初始化阶段（如 Git clone 时注入 token），二是通过 Vault 代理（MCP Proxy）间接访问。我们的实现采用后者：

```python
class VaultStore:
    """
    安全凭据存储 — 令牌永远不进入沙箱进程。
    开发环境：内存字典。生产环境：Azure Key Vault。
    """
    def store(self, key: str, token: str) -> None:
        self._vault[key] = token

    def fetch(self, key: str) -> str | None:
        return self._vault.get(key)

    def revoke(self, key: str) -> None:
        self._vault.pop(key, None)
```

**场景说明**：假设 Agent 需要调用 GitHub API 创建 Pull Request。传统方式是将 `GITHUB_TOKEN` 注入环境变量 — LLM 生成的代码可以通过 `os.environ` 读取。使用 VaultStore，Token 存储在沙箱外部，工具函数通过 `vault.fetch("github_token")` 在受控路径下获取凭据，LLM 永远无法在沙箱内遍历或导出密钥。以下是安全边界的验证：

```python
# demo: 演示 7 — 安全边界验证
vault.store("foundry_key", "FoundryKey-XXXX")
vault.store("github_token", "ghp_XXXXXXXX")

sid = await sandbox_mgr.provision()
box = sandbox_mgr.get(sid)

# LLM 生成的代码尝试读取环境变量 → 不可见
r1 = await box.execute("run_python",
    "import os; print(os.environ.get('FOUNDRY_API_KEY', 'NOT_VISIBLE'))")
# → 'NOT_VISIBLE'

# 尝试调用未授权的工具 → 被拒绝
r2 = await box.execute("shell", "cat /etc/secrets")
# → '[SANDBOX DENIED] Tool 'shell' is not in the allowed list.'
```

**执行接口 — `Sandbox.execute()`**

```python
class Sandbox:
    async def execute(self, name: str, input_data: str) -> str:
        """
        execute(name, input) → string — 大脑与双手之间的唯一接口。
        编排器不知道也不需要知道 'name' 映射到容器、子进程还是远程服务。
        """
        if not self._alive:
            raise RuntimeError(f"Sandbox {self.sandbox_id} is dead.")

        # 权限检查：只允许授权的工具
        if self.resources.allowed_tools and name not in self.resources.allowed_tools:
            return f"[SANDBOX DENIED] Tool '{name}' is not in the allowed list."

        fn = self.registry.get(name)
        try:
            result = await asyncio.wait_for(
                fn(input_data, vault=self._vault),
                timeout=self.resources.timeout_sec,
            )
            return str(result)
        except asyncio.TimeoutError:
            self._alive = False  # 超时 → 标记为 dead → 编排器创建替代品
            raise RuntimeError(
                f"Sandbox {self.sandbox_id} timed out on '{name}'. "
                "Harness should provision a fresh sandbox."
            )
```

**沙箱管理器 — "牲畜模式"**

```python
class SandboxManager:
    async def provision(self, resources: SandboxResources | None = None) -> str:
        """
        创建新沙箱 — 对应 Anthropic 的 provision({resources})。
        由编排器按需调用，而非预先创建。
        """
        sandbox_id = str(uuid.uuid4())
        self._sandboxes[sandbox_id] = Sandbox(
            sandbox_id, resources or SandboxResources(),
            self._registry, self._vault,
        )
        return sandbox_id

    def reclaim(self, sandbox_id: str) -> None:
        """终止并丢弃 — 零感情，零留恋。"""
        self._sandboxes[sandbox_id].kill()
        del self._sandboxes[sandbox_id]
```

**内置工具注册表**：沙箱预注册了四类工具，每类对应一种 Agent 能力：

| 工具 | 能力 | 生产环境替换 |
|------|------|-------------|
| `run_python` | 在隔离子进程中执行 Python | Azure Container Instances |
| `web_search` | 网络搜索（开发环境为模拟） | Azure AI Search / Bing API |
| `read_file` | 读取文件 | Azure Blob Storage |
| `write_file` | 写入文件 | Azure Blob Storage |

运行时可通过 `sandbox_mgr.register_tool(name, fn)` 动态扩展 — 正如 Anthropic 所说："That interface supports any custom tool, any MCP server, and our own tools."

---

## 五、第三层：Harness — 无状态编排器

### 设计原理

编排器是 Agent 的"大脑" — 调用 LLM、解析工具调用、路由到沙箱、将结果写回 Session。Anthropic 的核心洞察是：**编排器必须无状态**。

> "The harness also became cattle. Because the session log sits outside the harness, nothing in the harness needs to survive a crash."

无状态的运维意义极为重大：不需要粘性会话（sticky sessions）、不需要亲和性路由（affinity routing）、不需要优雅下线（graceful draining）。任何编排器实例可以服务任何会话，负载均衡和自动伸缩变成了基础设施层面的透明操作。

### MAF 实现：`harness/harness.py`

**编排器生命周期**：`start()` → `run()` → `shutdown()`

```python
class AgentHarness:
    """
    由 Azure AI Foundry 驱动的无状态编排器。
    崩溃时：创建新实例，调用 start(同一个 session_id)，
    wake() 自动从持久化日志重建完整上下文。
    """

    def __init__(self, session_log, sandbox_mgr, config=None, client=None):
        self.session_log = session_log
        self.sandbox_mgr = sandbox_mgr
        self.config      = config or HarnessConfig()
        self._client     = client          # 可注入 FoundryChatClient
        self._agent      = None            # AF Agent 实例 — start() 时构建
        self._session_id = None

    async def start(self, session_id: str) -> None:
        """
        附加到会话。核心逻辑：
        1. wake() — 从 Session 获取完整事件历史
        2. 构建工具列表（沙箱工具 + 上下文查询工具）
        3. 注入技能提供者（领域知识）
        4. 挂载中间件栈（日志 → 安全 → 限流 → 可观测性）
        5. 组装 AF Agent
        """
        self._session_id = session_id
        _session, past_events = await self.session_log.wake(session_id)

        tools           = build_sandbox_tools(self.sandbox_mgr, self.session_log, session_id)
        skills_provider = build_skills_provider(self.config.skill_names)
        middleware = [
            make_session_logging_middleware(self.session_log, session_id),
            make_security_middleware(),
            make_rate_limit_middleware(self.config.rate_limit_rpm),
            make_observability_middleware(),
        ]

        client = self._client or make_foundry_client(model=self.config.model or None)

        self._agent = Agent(
            client=client,
            name=self.config.agent_name,
            instructions=self._build_system_prompt(past_events),
            tools=tools,
            context_providers=[skills_provider, self.session_log.get_history_provider(session_id)],
            middleware=middleware,
        )
```

注意 `_build_system_prompt()` 如何将最近 5 条事件注入系统提示 — 这实现了 Anthropic 所说的"context engineering in the harness"：

```python
    def _build_system_prompt(self, past_events: list) -> str:
        base = (
            "You are a Managed Agent running on Microsoft Foundry.\n\n"
            "Architecture:\n"
            "- Session state lives in a durable log OUTSIDE this context window.\n"
            "- Interact with the world via: run_python, web_search, write_file, read_file.\n"
            "- Call get_session_context() to inspect recent session events.\n"
            "- Credentials are NEVER available to you; the harness proxy handles auth.\n"
        )
        if past_events:
            recent = past_events[-5:]
            summary = "\n".join(
                f"  [{e.kind.value}] {str(e.payload)[:120]}" for e in recent
            )
            base += f"\n\nMost recent session events:\n{summary}\n"
        return base
```

**崩溃恢复演示** — 这是整个架构的核心验证场景：

```python
# demo: 演示 3 — 编排器崩溃恢复

# 编排器 #1 正常工作后崩溃
sid = await session_log.create_session("Crash recovery demo")
h1  = _harness("Harness-1")
await h1.start(sid)
r1  = await h1.run("List three benefits of async programming in Python.")
print(f"[H1] {r1}")
del h1  # 💥 模拟崩溃 — 编排器未经 shutdown 直接丢弃

# 验证：Session 不受影响
n_before = await session_log.event_count(sid)
print(f"[LOG] 崩溃后仍保留 {n_before} 条事件 ✓")

# 编排器 #2 从持久化日志恢复 — 零数据丢失
h2 = _harness("Harness-2")
await h2.start(sid)       # 内部调用 wake() → 重建完整上下文
r2 = await h2.run("Give a short Python async/await example based on what you found.")
print(f"[H2] 无缝继续: {r2}")
await h2.shutdown()

n_after = await session_log.event_count(sid)
print(f"[LOG] 最终: {n_after} 条事件（恢复后增加 {n_after - n_before} 条）")
```

关键点：`h2` 不知道 `h1` 曾经存在过。它只知道 Session 中有事件历史，`wake()` 将其注入上下文后，`h2` 可以"无缝继续"对话 — 就像操作系统恢复进程状态一样，用户不会感知到中断。

**Anthropic 三原则对照**：

| 原则 | 实现方式 |
|------|----------|
| **无状态** | `AgentHarness` 不持有聊天历史；所有状态通过 `session_log.emit_event()` 外部化 |
| **解耦** | 通过 `build_sandbox_tools()` 间接调用沙箱，走 `execute(name, input) → string` |
| **可恢复** | `start()` 内部调用 `wake()`，从日志重建；异常时自动发出 `HARNESS_CRASH` 事件 |

### Foundry 客户端工厂 — 零 OpenAI SDK 依赖

LLM 后端选择是一个架构决策。直接依赖 OpenAI SDK 意味着与特定供应商绑定 — 模型定价变更、API 版本迭代、区域合规限制都会直接影响系统。`FoundryChatClient` 通过 Azure AI Foundry 统一入口消除了这一耦合：

```python
def make_foundry_client(model: str | None = None) -> FoundryChatClient:
    """
    认证优先级（DefaultAzureCredential chain）：
      1. FOUNDRY_API_KEY 环境变量  → AzureKeyCredential（开发 / CI）
      2. az login                  → AzureCliCredential（本地开发）
      3. Managed Identity          → 自动生效（Azure 生产环境）
    """
    api_key  = os.getenv("FOUNDRY_API_KEY")
    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT")
    model    = model or os.getenv("FOUNDRY_MODEL", "gpt-5.4")

    if api_key:
        from azure.core.credentials import AzureKeyCredential
        credential = AzureKeyCredential(api_key)
    else:
        from azure.identity import DefaultAzureCredential
        credential = DefaultAzureCredential()

    return FoundryChatClient(
        project_endpoint=endpoint,
        model=model,
        credential=credential,
    )
```

本地开发只需 `az login`，生产环境自动使用 Managed Identity — 代码零改动。

---

## 六、超越三层：MAF 原生能力的扩展

Anthropic 的三层架构是骨架，但要让 Agent 系统在生产环境中可靠运行，还需要回答三个问题："大脑的领域知识从哪来？"、"日志/安全/限流等横切关注点如何处理？"、"如何确保编排器假设不会过时？"。MAF 的原生能力填补了这些空白。

### 6.1 Skill — 可组合的领域知识

Anthropic 在文章中承认了一个核心张力：

> "Harnesses encode assumptions about what Claude can't do on its own. However, those assumptions need to be frequently questioned because they can go stale as models improve."

他们举了一个例子：Claude Sonnet 4.5 对上下文窗口耗尽会产生"焦虑行为"（提前结束任务），因此编排器加入了上下文重置机制。但当同一编排器用于 Claude Opus 4.5 时，该行为已消失 — 重置逻辑变成了死代码。

MAF 的 `Skill` 提供了管理这种"可能过时的假设"的正式机制。技能定义了**大脑知道如何做什么**（工作流协议），与**双手实际做什么**（沙箱执行）正交：

```python
# skills/skills.py — 四种内置技能

research_skill = Skill(
    name="research",
    description="系统化研究：分解问题 → 搜索 → 交叉验证 → 综合回答",
    content="""
    ## Workflow
    1. Decompose the question into 3–5 focused sub-queries.
    2. Search each sub-query using the web_search tool.
    3. Evaluate source quality; prefer primary sources.
    4. Cross-validate claims across at least 2 independent results.
    5. Synthesise findings with confidence level: High / Medium / Low.
    ## Constraints
    - Never fabricate citations.
    - Flag conflicting evidence explicitly.
    """,
)

code_skill = Skill(
    name="code_execution",
    description="编写、测试、迭代 Python 代码。",
    content="...",
    scripts=[SkillScript(name="run_snippet", ...)],  # AF SkillScript 将技能绑定到工具
)

summarise_skill = Skill(
    name="summarise",
    description="上下文压缩：当接近上下文窗口限制时，将历史事件压缩为结构化摘要。",
    content="..."  # 保留最近 5 条事件原文，其余生成摘要 + 发出 COMPACTION 事件
)

orchestration_skill = Skill(
    name="orchestration",
    description="多代理编排：分解任务 → 委派给专家代理 → 聚合结果。",
    content="..."
)
```

技能通过 `SkillsProvider` 注入到 AF `Agent` 的 `context_providers` 中，模型新增能力时（例如未来模型自带研究能力），直接移除或替换对应 Skill 即可 — 不改编排器代码。

### 6.2 Middleware — 横切关注点的分层治理

Agent 系统与 Web 服务一样面临横切关注点的治理问题。MAF 的 `@agent_middleware` 装饰器允许在不修改 Agent 核心逻辑的前提下注入四层治理：

```python
# middleware/middleware.py — 中间件栈（按顺序执行）

# 1. 会话日志 — 每次轮次自动持久化
@agent_middleware
async def session_logging_mw(ctx: AgentContext, next):
    await session_log.emit_event(session_id, SessionEvent(kind=EventKind.USER_INPUT, ...))
    result = await next()
    await session_log.emit_event(session_id, SessionEvent(kind=EventKind.AGENT_RESPONSE, ...))
    return result

# 2. 安全 — 在凭据模式到达模型之前拦截
@agent_middleware
async def security_mw(ctx: AgentContext, next):
    blocked = ["sk-", "Bearer ", "AZURE_", "password=", "secret=", "api_key=", "FoundryKey"]
    ctx.messages = [m for m in ctx.messages if not any(p in str(m) for p in blocked)]
    return await next()

# 3. 限流 — 滑动窗口 RPM 限制
@agent_middleware
async def rate_limit_mw(ctx: AgentContext, next):
    # 60 秒内超过 max_rpm 次调用 → 抛出 RuntimeError
    ...
    return await next()

# 4. 可观测性 — TTFT（Time To First Token）追踪
@agent_middleware
async def observability_mw(ctx: AgentContext, next):
    start = time.perf_counter()
    result = await next()
    elapsed_ms = (time.perf_counter() - start) * 1000
    metrics.record_ttft(elapsed_ms)
    # 输出：[METRICS] run=5 latency=1200ms p50=800ms p95=2100ms
    return result
```

中间件的顺序至关重要：安全检查在限流之前（被拦截的消息不消耗限流配额），可观测性在最外层（测量完整的端到端延迟）。

### 6.3 五层工具包装 — 沙箱延迟创建

`build_sandbox_tools()` 是编排器与沙箱之间的桥梁，实现了五个关键功能：

```python
def build_sandbox_tools(sandbox_mgr, session_log, session_id) -> list[Callable]:
    _state = {"sandbox_id": None}  # 初始为空 — 延迟创建

    async def _ensure() -> str:
        """LazyInit：首次工具调用时才创建沙箱。"""
        if _state["sandbox_id"] is None:
            sid = await sandbox_mgr.provision(SandboxResources(
                allowed_tools=["run_python", "web_search", "read_file", "write_file"],
            ))
            _state["sandbox_id"] = sid
            await session_log.emit_event(session_id, SessionEvent(
                kind=EventKind.SANDBOX_SPAWN, ...))
        return _state["sandbox_id"]

    async def _exec(name: str, data: str) -> str:
        """带自动重试的执行：沙箱挂了 → reclaim → provision → retry。"""
        for attempt in range(2):
            try:
                sid = await _ensure()
                result = await sandbox_mgr.execute(sid, name, data)
                await session_log.emit_event(session_id, SessionEvent(
                    kind=EventKind.SANDBOX_EXEC, ...))
                return result
            except RuntimeError:
                sandbox_mgr.reclaim(_state["sandbox_id"])
                _state["sandbox_id"] = None
                if attempt == 1:
                    return f"[SANDBOX FAILED after retry]"
        return "[SANDBOX FAILED]"

    # 带类型注解的 AF 工具函数 — LLM 通过 function calling 选择
    async def run_python(code: Annotated[str, Field(description="...")]) -> str: ...
    async def web_search(query: Annotated[str, Field(description="...")]) -> str: ...
    async def get_session_context(last_n: Annotated[int, ...] = 10) -> str:
        """从会话日志中检索最近事件 — LLM 可主动查询上下文。"""
        events = await session_log.get_context_window(session_id, last_n=last_n)
        return "\n".join(f"[{e.kind.value}] {e.payload}" for e in events)

    return [run_python, web_search, write_file, read_file, get_session_context]
```

注意 `get_session_context` 工具：它让 LLM 可以主动查询 Session 事件流，而非被动等待编排器推送上下文 — 这正是 Anthropic 所说的"context as a programmable object"的实现。

---

## 七、多大脑、多双手 — 并行编排

### 从单体到集群

解耦带来的核心威力不是容错 — 而是**组合**。Anthropic 明确阐述了这一点：

> "Scaling to many brains just meant starting many stateless harnesses, and connecting them to hands only if needed."
>
> "Decoupling the brain from the hands makes each hand a tool: a name and input go in, and a string is returned. Because no hand is coupled to any brain, brains can pass hands to one another."

### MAF 实现：`orchestration/multi_agent.py`

**场景**：用户请求"分析三家竞品的技术栈、市场定位和定价策略，生成综合报告"。单一 Agent 串行执行需要 N 分钟。多大脑模式下：

```python
async def run_many_brains(tasks, session_log, sandbox_mgr) -> list[dict]:
    """
    为每个任务生成独立的无状态 Foundry 编排器，并发执行。
    每个编排器拥有独立的 session_id 和事件日志。
    沙箱仅在实际需要工具时创建 — 纯推理任务不消耗沙箱资源。
    """
    async def run_one(task: str) -> dict:
        sid     = await session_log.create_session(task)
        harness = AgentHarness(
            session_log=session_log,
            sandbox_mgr=sandbox_mgr,
            config=HarnessConfig(agent_name=f"Brain-{sid[:6]}"),
        )
        await harness.start(sid)
        try:
            response = await harness.run(task)
            return {"session_id": sid, "task": task, "response": response, "ok": True}
        except Exception as exc:
            return {"session_id": sid, "task": task, "error": str(exc), "ok": False}
        finally:
            await harness.shutdown()

    return list(await asyncio.gather(*[run_one(t) for t in tasks]))
```

**使用示例**：

```python
# demo: 演示 5 — 多大脑并行
tasks = [
    "What is the capital of Japan? One sentence only.",        # 纯推理 → 不创建沙箱
    "Calculate the sum of squares from 1 to 10 using Python.", # 需要 run_python → 创建沙箱
    "Give one fun fact about Microsoft Azure AI Foundry.",     # 纯推理 → 不创建沙箱
]
results = await run_many_brains(tasks, session_log, sandbox_mgr)
# 三个任务并行执行，总耗时 ≈ max(单个任务耗时)
```

### 专家代理 + 基于图的路由

更复杂的场景需要专家分工。使用 AF `WorkflowBuilder` 构建有向无环图（DAG）：

```
[classify_task] ──→ ResearchAgent  ─┐
                ──→ CodeAgent       ├──→ SummariseAgent ──→ END
                ──→ OrchestratorAgent─┘
```

每个专家 Agent 拥有独立的沙箱权限：

```python
# ResearchAgent — 只能使用 web_search
def make_research_agent(sandbox_mgr):
    async def web_search(query: Annotated[str, ...]) -> str:
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["web_search"]))
        result = await sandbox_mgr.execute(sid, "web_search", query)
        sandbox_mgr.reclaim(sid)  # 用完即弃
        return result
    return _agent("ResearchAgent", "...", tools=[web_search])

# CodeAgent — 只能使用 run_python
def make_code_agent(sandbox_mgr):
    async def run_python(code: Annotated[str, ...]) -> str:
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["run_python"]))
        result = await sandbox_mgr.execute(sid, "run_python", code)
        sandbox_mgr.reclaim(sid)
        return result
    return _agent("CodeAgent", "...", tools=[run_python])

# SummariseAgent — 纯推理，不需要沙箱
def make_summarise_agent():
    return _agent("SummariseAgent", "...")

# OrchestratorAgent — 通过委派工具"将双手传递给专家"
def make_orchestrator_agent(research, code, summarise):
    async def delegate_research(task: ...) -> str:
        return await research.run(task)
    async def delegate_code(task: ...) -> str:
        return await code.run(task)
    async def delegate_summarise(text: ...) -> str:
        return await summarise.run(f"Summarise:\n\n{text}")
    return _agent("OrchestratorAgent", "...",
                   tools=[delegate_research, delegate_code, delegate_summarise])
```

路由逻辑通过 `WorkflowBuilder` 声明式定义：

```python
builder = WorkflowBuilder(
    start_executor=FunctionExecutor(classify_task),
    checkpoint_storage=InMemoryCheckpointStorage(),  # 生产环境 → Azure Blob Storage
)
builder.add_executor("research",    research_agent)
builder.add_executor("code",        code_agent)
builder.add_executor("orchestrate", orchestrator)
builder.add_executor("summarise",   summarise_agent)

builder.add_switch_edge("classify_task", key="agent_type", cases={
    "research": "research", "code": "code", "orchestrate": "orchestrate"
})
builder.add_edge("research",    "summarise")
builder.add_edge("code",        "summarise")
builder.add_edge("orchestrate", "summarise")
```

`InMemoryCheckpointStorage` 确保工作流状态在编排器重启后可恢复 — 与 Session 的设计哲学一致。

---

## 八、延迟沙箱创建 — TTFT 优化

### 不需要工具的推理，不应为工具付出启动代价

这是 Anthropic 文章中最具实际价值的性能洞察：

> "Decoupling the brain from the hands means that containers are provisioned by the brain via a tool call only if they are needed. A session that didn't need a container right away didn't wait for one... Our p50 TTFT dropped roughly 60% and p95 dropped over 90%."

分析其背后逻辑：在所有 Agent 会话中,相当比例仅需要推理能力 — 回答问题、解释概念、制定计划。如果每次请求都预先创建沙箱（启动容器 → 分配内存 → 初始化工具注册表），这些纯推理请求就在为自己用不到的能力买单。

在我们的实现中，沙箱创建完全由 LLM 的工具调用触发：

```python
# harness/harness.py — build_sandbox_tools()
_state = {"sandbox_id": None}  # 初始为空

async def _ensure() -> str:
    if _state["sandbox_id"] is None:
        # 延迟创建！仅在 LLM 首次决定使用工具时触发
        sid = await sandbox_mgr.provision(SandboxResources(
            allowed_tools=["run_python", "web_search", "read_file", "write_file"],
        ))
        _state["sandbox_id"] = sid
        await session_log.emit_event(session_id, SessionEvent(
            kind=EventKind.SANDBOX_SPAWN, ...))
    return _state["sandbox_id"]
```

**实际效果对比**：

| 请求类型 | 耦合模式 | 解耦模式（延迟创建） |
|----------|----------|---------------------|
| "日本首都是什么？" | 等待沙箱启动 + 推理 | 仅推理（无沙箱开销） |
| "用 Python 计算斐波那契数列" | 等待沙箱启动 + 推理 + 执行 | 推理 + 首次工具调用时创建沙箱 + 执行 |
| 多轮纯推理对话 | 每轮都有沙箱空转开销 | 全程零沙箱资源消耗 |

---

## 九、Azure Functions 托管 — 无状态的天然载体

### Serverless 即"终极牲畜模式"

Anthropic 的核心主张 — "任何编排器实例都可以服务任何会话" — 翻译成基础设施语言就是**不需要固定服务器**。Azure Functions 的执行模型与此完美契合：每次 HTTP 请求分配一个函数实例，执行完毕后释放所有资源。

### MAF 实现：`hosting/azure_function_host.py`

```python
# 每次 HTTP 请求的完整生命周期
async def _handle_run_turn(session_id: str, body: dict) -> dict:
    user_input = body.get("input", "")
    # 1. 创建无状态编排器
    harness = AgentHarness(
        session_log=_session_log,    # 持久化 Session（进程级单例）
        sandbox_mgr=_sandbox_mgr,
        config=_config,
        client=_get_client(),        # Foundry 客户端（延迟初始化）
    )
    # 2. wake() — 从持久化日志重建上下文
    await harness.start(session_id)
    # 3. 执行一次 Agent 循环
    response = await harness.run(user_input)
    # 4. 优雅关闭（发出 SESSION_END 事件）
    await harness.shutdown()
    return {"session_id": session_id, "response": response,
            "event_count": await _session_log.event_count(session_id)}
```

**REST API 设计** — 五个端点覆盖完整的 Agent 生命周期管理：

| 方法 | 路径 | 描述 | 对应原语 |
|------|------|------|----------|
| `POST` | `/sessions` | 创建新会话 | `create_session(task)` |
| `POST` | `/sessions/{id}/run` | 执行一次 Agent 轮次 | `run(input)` |
| `GET`  | `/sessions/{id}/events` | 查询事件日志（支持分页） | `getEvents(start, end)` |
| `POST` | `/sessions/{id}/wake` | 唤醒编排器（崩溃恢复） | `wake(sessionId)` |
| `GET`  | `/health` | 健康检查 | — |

**关键特性**：
- **无粘性会话**：任何 Functions 实例服务任何 session_id — 路由完全基于请求参数
- **水平扩展**：增加 Functions 实例 = 增加并发处理能力
- **冷启动优化**：Foundry 客户端延迟初始化，避免环境变量未就绪时的启动失败

本地开发使用 FastAPI 镜像相同的 API：

```bash
uvicorn maf_harness.hosting.azure_function_host:local_app --reload
```

---

## 十、从开发到生产 — 接口不变，实现可换

Anthropic 在文章结尾总结了他们的设计哲学：

> "We're opinionated about the shape of these interfaces, not about what runs behind them."

这是检验架构质量的终极标准：**你能不能在不改一行业务代码的前提下，把系统从开发笔记本搬到 Azure 生产集群？**

以下是每个组件从开发到生产的替换路径 — 注意接口完全不变：

| 组件 | 开发环境 | Azure 生产环境 | 接口 |
|------|----------|----------------|------|
| LLM 客户端 | `FoundryChatClient` + `az login` | `FoundryChatClient` + Managed Identity | `FoundryChatClient` |
| 会话日志 | `InMemoryHistoryProvider`（内存字典） | Azure CosmosDB / Redis | `SessionLog.emit_event()` · `wake()` |
| 工作流检查点 | `InMemoryCheckpointStorage` | Azure Blob Storage | `CheckpointStorage` |
| 凭据存储 | `VaultStore`（内存字典） | Azure Key Vault | `vault.store()` · `vault.fetch()` |
| 沙箱执行 | `subprocess`（本地 Python 子进程） | Azure Container Instances | `execute(name, input) → string` |
| 可观测性 | `Metrics`（进程内计数器） | OpenTelemetry → Azure Monitor | `metrics.record_ttft()` |
| 托管 | `uvicorn` + FastAPI（本地） | Azure Functions（Serverless） | HTTP REST API |

每一行替换都是实现层面的变化。不触及编排逻辑、中间件栈、技能定义或任何业务代码。

---

## 总结：为尚未被构想的程序设计系统

Anthropic 的 Managed Agent 架构回答了一个操作系统设计中的经典命题 — **"how to design a system for programs as yet unthought of"**。他们的答案与 Unix 哲学一脉相承：将 Agent 的组件虚拟化为稳定的接口，让实现随模型能力的演进自由替换。

通过 Microsoft Agent Framework 和 Azure AI Foundry，本项目将这套理论变成了可运行、可验证、可部署的工程实践：

| Anthropic 概念 | MAF 实现 |
|----------------|----------|
| Session（持久化事件日志） | `SessionLog` + AF `InMemoryHistoryProvider` + 11 种 `EventKind` |
| Harness（无状态编排器） | `AgentHarness` + AF `Agent` + `FoundryChatClient` + `wake()` 恢复 |
| Sandbox（可替换执行环境） | `Sandbox` + `SandboxManager` + `VaultStore` + 延迟创建 |
| Many Brains（多大脑并行） | `run_many_brains()` + AF `WorkflowBuilder` + 专家代理路由 |
| Security Boundary（安全边界） | `VaultStore` 隔离 + `SecurityMiddleware` 拦截 + 工具权限白名单 |
| Context Engineering（上下文工程） | `get_context_window()` + `get_session_context` 工具 + `COMPACTION` 事件 |
| Serverless Hosting（无状态托管） | Azure Functions + REST API + 零粘性会话 |

**三个核心接口**构成了整个系统的骨架：

```python
session_id = await session_log.create_session(task)       # 创建记忆
result     = await sandbox.execute(name, input)            # 执行动作
session, events = await session_log.wake(session_id)       # 恢复状态
```

接口稳定，实现可换。大脑无状态，双手可替换。会话持久化，崩溃可恢复。

---

*项目代码：[maf_harness_managed_agent](https://github.com/maf_harness_managed_agent)*
*参考：[Anthropic — Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents) · [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) · [Azure AI Foundry](https://ai.azure.com)*
