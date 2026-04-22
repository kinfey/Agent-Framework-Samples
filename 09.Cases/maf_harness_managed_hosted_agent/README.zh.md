# 托管式 Hosted Agent(Microsoft Foundry + Anthropic Managed Agents 原则)

本示例演示如何构建一个 Microsoft Foundry **Hosted Agent**(基于 Agent Framework + Azure AI AgentServer SDK),其内部架构遵循 Anthropic 在 [《Scaling Managed Agents: Decoupling the brain from the hands》](https://www.anthropic.com/engineering/managed-agents) 中描述的接口设计。

## 为什么这样设计

Anthropic Managed Agents 将三个接口虚拟化,使它们可以各自独立地失败或替换:

| 接口 | 含义 | 本仓库 |
|------|------|--------|
| **Brain(大脑)**   | 驱动循环的模型 + harness | `main.py`(Agent Framework `Agent`) |
| **Hands(双手)**   | 真正执行工作的沙箱/工具 | `harness/sandbox.py`(`SandboxPool`) |
| **Session(会话)** | 外置于上下文窗口的、持久化的追加式事件日志 | `harness/session.py`(`SessionStore`) |

再加上一个 **credential vault(凭据保险库)**(`harness/vault.py`),确保原始 token 永远不会被运行生成代码的沙箱拿到。

### 应用的原则

1. **别把工具当宠物养。** 每次 `execute(name, input)` 调用都会开一个全新的沙箱,用完即弃。沙箱挂了,大脑只会看到一个 `ERROR:` 字符串,可以直接重试。无需护理。
2. **把大脑和双手解耦。** 模型对外的*唯一*行动契约就是那个工具 `execute(name, input_json)`。harness 并不关心一只手到底是本地 Python 子进程、远程容器,还是 MCP server。
3. **Session ≠ 上下文窗口。** 每个有意义的事件(`tool_call`、`tool_result`、`note` 等)都会被追加到一个 JSONL 日志里。模型通过 `get_events(start, end)` 重读旧片段,而不是把所有东西塞进活跃上下文。harness 崩溃时,`SessionStore.wake(session_id)` 可重放日志。
4. **凭据不在沙箱里。** 模型只传逻辑凭据*名*(如 `"credential": "github"`);vault 在调用时注入真实 token,并在结果返回给模型之前把 token 从输出里抹掉。
5. **多大脑、多双手。** 因为 `execute` 是通用的、session 是外置的,你可以跑多个无状态 harness 副本共享同一份 session 日志,并为每个副本配不同的手。

## 模型看到的工具

| 工具 | 用途 |
|------|------|
| `list_tools()` | 列出当前挂载的手 |
| `execute(name, input_json)` | 在全新沙箱里调用任一手 |
| `get_events(start, end)` | 回读持久会话日志的一个切片 |
| `emit_note(note)` | 把中间推理 checkpoint 到日志 |

内置的手(可在 `harness/sandbox.py` 里换成你自己的):

- `python_exec` —— 在隔离子进程里运行一小段 Python
- `shell_exec` —— 在隔离子进程里运行一个 argv 命令
- `http_fetch` —— HTTP GET,由 vault 注入鉴权头

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env                  # then edit with your Foundry endpoint

azd auth login

azd ai agent init -m agent.yaml 

azd ai agent run
```

Agent 监听 `http://localhost:8088`,使用 OpenAI Responses 协议:

```bash
curl -sS -H "Content-Type: application/json" -X POST http://localhost:8088/responses \
  -d '{"input":"Use execute to run python that prints 2+2, then summarize.","stream":false}'
```

查看 `./sessions/<session-id>.jsonl` 就能看到模型正在写入的持久日志。

## 测试用例

以下用例覆盖四个面向模型的工具(`list_tools` / `execute` / `get_events` / `emit_note`)和三只内置的手。逐个运行后,查看 `./sessions/<id>.jsonl` 的末尾,即可看到完整的 `tool_call → tool_result` 链路。

### A. 能力发现

```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call list_tools() and return the result verbatim.","stream":false}'
```
预期:`["http_fetch","python_exec","shell_exec"]`。

### B. `python_exec`

**B1 —— 算术**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to compute sum(i*i for i in range(1000)) and tell me the result.","stream":false}'
```
预期 `332833500`。

**B2 —— 标准库访问**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to print the current UTC ISO time and the Python version.","stream":false}'
```

**B3 —— 失败后"牛群式"重试**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"First use python_exec to run 1/0 and observe the error, then retry with 1/1 and return the result.","stream":false}'
```
Session 里应出现一条失败的 `tool_result`(`ERROR: sandbox ...`),紧跟一条成功的。

**B4 —— 超时**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to run import time; time.sleep(30). If it times out, explain the failure.","stream":false}'
```
预期 `ERROR: sandbox ... python_exec timed out after 15s`。

### C. `shell_exec`

**C1 —— argv 列表**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use shell_exec to run ls -la / and list only the first 5 lines.","stream":false}'
```

**C2 —— 拒绝字符串 argv**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call shell_exec with argv=\"ls -la\" (a string). If the tool rejects it, tell me exactly what it said.","stream":false}'
```
预期 `ERROR: 'argv' must be a list of strings.`,随后模型应改用列表重试。

### D. `http_fetch` + vault

**D1 —— 公开 URL**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to GET https://api.github.com/zen and return the plain text.","stream":false}'
```

**D2 —— vault 注入凭据**(先在 `.env` 里加 `GITHUB_TOKEN=ghp_xxx` 并重启)
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to call https://api.github.com/user with credential logical name \"github\". Do not echo any token in your reply.","stream":false}'
```
预期调用成功;原始 token 不会出现在回答里(vault 会将其抹成 `***REDACTED***`)。

**D3 —— 拒绝非 http 协议**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to GET file:///etc/passwd. If the tool rejects it, tell me exactly what it said.","stream":false}'
```
预期 `ERROR: 'url' must be an http(s) URL.`

### E. `emit_note` / `get_events`(session ≠上下文窗口)

**E1 —— 写入一条 note**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use emit_note to record: \"User prefers metric units; likes concise answers.\" Confirm the event index.","stream":false}'
```

**E2 —— 回读一个切片**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call get_events(0, -1) and return only the entries where type==\"note\".","stream":false}'
```

**E3 —— 长任务中 checkpoint**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Do three steps: 1) use python_exec to compute 2**64; 2) use emit_note to record the result; 3) use get_events to read back the last note and repeat it.","stream":false}'
```

### F. 编排

**F1 —— 链式工具**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"First http_fetch https://api.github.com/zen, then use python_exec to uppercase the returned text and count its characters.","stream":false}'
```

**F2 —— 未知工具自我纠正**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use execute(\"database_query\", {...}) to query the database. If it does not exist, fall back to list_tools and tell me what is actually available.","stream":false}'
```
预期先得到:`ERROR: unknown tool 'database_query'`,随后模型会回退到 `list_tools`。

### G. 流式

```bash
curl -N -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to print 1 through 20, and explain the output incrementally.","stream":true}'
```

### 证据检查

任一测试之后,最新的 session 文件会显示完整的持久事件链:

```bash
ls -t sessions/ | head -1 | xargs -I {} jq -c . sessions/{}
```

预期看到类似 `session_start → tool_call(execute) → tool_result → note → ...` 的序列 —— 这就是大脑、双手、会话彼此解耦、沙箱可随时替换、上下文位于模型窗口之外的具体证据。

## 部署到 Microsoft Foundry


随后遵循 hosted-agent 部署流程:<https://learn.microsoft.com/en-us/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry&tabs=cli>。

