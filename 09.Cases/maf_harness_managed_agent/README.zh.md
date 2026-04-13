# maf_harness_managed_agent

**Microsoft Agent Framework × Microsoft Foundry**  
实现 Anthropic 的 [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents)（扩展托管代理：将大脑与双手解耦）

---

## 项目结构

```
maf_harness_managed_agent/
│
├── maf_harness/                        ← Python 包
│   ├── session/
│   │   └── session_log.py              会话层：持久化仅追加事件日志
│   ├── sandbox/
│   │   └── sandbox.py                  沙箱层：execute() 接口、VaultStore
│   ├── harness/
│   │   └── harness.py                  编排层：无状态大脑、FoundryChatClient
│   ├── skills/
│   │   └── skills.py                   AF 技能：研究、编码、总结、编排
│   ├── middleware/
│   │   └── middleware.py               AF 中间件：日志、安全、限流、TTFT
│   ├── orchestration/
│   │   └── multi_agent.py              多大脑 × 多双手、WorkflowBuilder
│   └── hosting/
│       └── azure_function_host.py      Azure Functions HTTP 触发器 + FastAPI 本地开发
│
├── main.py                             示例入口（7 个演示）
├── requirements.txt
├── .env.example
└── local.settings.json                 Azure Functions 本地设置模板
```

---

## Anthropic 概念 → 实现映射

| Anthropic 概念 | 文件 | 关键符号 |
|---|---|---|
| **Session（会话）** — 持久化日志 | `session/session_log.py` | `SessionLog` |
| `emitEvent(id, event)` | | `SessionLog.emit_event()` |
| `getSession(id)` | | `SessionLog.get_session()` |
| `getEvents()` — 位置切片 | | `SessionLog.get_events(start, end, kind_filter)` |
| `wake(sessionId)` | | `SessionLog.wake()` |
| **Harness（编排器）** — 无状态大脑 | `harness/harness.py` | `AgentHarness` |
| Foundry LLM 客户端 | | `make_foundry_client()` → `FoundryChatClient` |
| **Sandbox（沙箱）** — 可替换的执行器 | `sandbox/sandbox.py` | `Sandbox`, `SandboxManager` |
| `execute(name, input) → string` | | `Sandbox.execute()` |
| `provision({resources})` | | `SandboxManager.provision()` |
| 凭据隔离在沙箱外部 | | `VaultStore` |
| **Skills（技能）** — 大脑的能力 | `skills/skills.py` | AF `Skill`, `SkillsProvider` |
| **Middleware（中间件）** | `middleware/middleware.py` | AF `@agent_middleware` |
| **Many brains（多大脑）** | `orchestration/multi_agent.py` | `run_many_brains()` |
| **Many hands（多双手）** | | 拥有隔离沙箱的专家代理 |
| **Hosting（托管）** | `hosting/azure_function_host.py` | Azure Functions + FastAPI |

---

## 安装与配置

### 1 — 安装依赖

```bash
pip install -r requirements.txt
```

### 2 — 配置环境

```bash
cp .env.example .env
# 编辑 .env：设置 FOUNDRY_PROJECT_ENDPOINT 和 FOUNDRY_MODEL
```

### 3 — 认证

```bash
# 本地开发
az login

# CI/CD — 改用环境变量
export FOUNDRY_API_KEY=<key>

# Azure 生产环境 — 托管标识（自动）
```

### 4 — 运行演示

```bash
python main.py                  # 全部 7 个演示
python main.py --mode single    # 单轮会话
python main.py --mode multi     # 多轮对话
python main.py --mode recover   # 编排器崩溃 + wake 恢复
python main.py --mode stream    # 从 Foundry 流式输出 token
python main.py --mode many      # 并行多大脑 × 并行多沙箱
python main.py --mode log       # 会话日志切片 + 过滤
python main.py --mode security  # 凭据边界验证
```

### 5 — 启动本地开发服务器

```bash
uvicorn maf_harness.hosting.azure_function_host:local_app --reload
```

---

## 核心模式

### Foundry 客户端（无 OpenAI 依赖）

```python
from maf_harness.harness.harness import make_foundry_client
from agent_framework.foundry import FoundryChatClient

# 认证：FOUNDRY_API_KEY → AzureKeyCredential | 否则使用 DefaultAzureCredential
client = make_foundry_client(model="gpt-5.4")
```

### 无状态编排器 — 崩溃恢复

```python
# 编排器 #1 工作后崩溃
h1 = AgentHarness(session_log, sandbox_mgr, client=make_foundry_client())
await h1.start(session_id)
await h1.run("Do some work")
del h1  # 💥 崩溃 — 会话日志不受影响

# 编排器 #2 从持久化日志中唤醒 — 零数据丢失
h2 = AgentHarness(session_log, sandbox_mgr, client=make_foundry_client())
await h2.start(session_id)   # 内部调用：SessionLog.wake(session_id)
await h2.run("Continue the work")
```

### 沙箱按需创建（改善 TTFT）

```python
# 沙箱不在会话启动时创建。
# 仅在代理实际调用工具时才延迟创建。
# 纯推理的会话无需承担容器启动开销。
```

### 多大脑并行执行

```python
results = await run_many_brains(
    tasks=["Research X", "Compute Y", "Summarise Z"],
    session_log=session_log,
    sandbox_mgr=sandbox_mgr,
)
# 每个任务：独立会话 + 独立无状态编排器 + 按需创建沙箱
```

### 会话日志作为外部上下文

```python
# 位置切片（getEvents）
events = await session_log.get_events(sid, start=10, end=20)

# 按事件类型过滤
tool_calls = await session_log.get_events(sid, kind_filter=[EventKind.TOOL_CALL])

# 上下文窗口（最近 N 条事件）
recent = await session_log.get_context_window(sid, last_n=30)
```

---

## Azure Functions API

| 方法 | 路径 | 描述 |
|---|---|---|
| `POST` | `/sessions` | 创建会话，返回 `session_id` |
| `POST` | `/sessions/{id}/run` | 执行一次代理轮次 |
| `GET`  | `/sessions/{id}/events` | 查询事件日志（支持 `start`、`end` 参数） |
| `POST` | `/sessions/{id}/wake` | 重新注入编排器元数据 |
| `GET`  | `/health` | 端点与模型健康检查 |

---

## 生产环境后端替换

| 组件 | 开发环境 | 生产环境 |
|---|---|---|
| LLM | `FoundryChatClient` | 相同 + 托管标识 |
| 会话日志 | `InMemoryHistoryProvider` | Azure CosmosDB / Redis |
| 检查点 | `InMemoryCheckpointStorage` | Azure Blob Storage |
| 密钥库 | `VaultStore`（字典） | Azure Key Vault |
| 沙箱 | subprocess | Azure Container Instances |
| 指标 | `Metrics`（进程内） | OpenTelemetry → Azure Monitor |

---

## 参考资料

- [Anthropic: Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
- [AF Python 文档](https://learn.microsoft.com/en-us/agent-framework/)
- [Azure AI Foundry](https://ai.azure.com)
