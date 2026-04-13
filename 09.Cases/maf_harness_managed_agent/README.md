# maf_harness_managed_agent

**Microsoft Agent Framework × Azure AI Foundry**
Implementing Anthropic's [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents)

Blog post: [English](blog_en.md) | [中文](blog_zh.md)

---

## Project Structure

```
maf_harness_managed_agent/
│
├── maf_harness/                          ← Python package
│   ├── __init__.py
│   ├── session/
│   │   ├── __init__.py
│   │   └── session_log.py                Session layer: durable append-only event log
│   ├── sandbox/
│   │   ├── __init__.py
│   │   └── sandbox.py                    Sandbox layer: execute() interface, VaultStore
│   ├── harness/
│   │   ├── __init__.py
│   │   └── harness.py                    Harness layer: stateless brain, FoundryChatClient
│   ├── skills/
│   │   ├── __init__.py
│   │   └── skills.py                     AF Skills: research, code, summarise, orchestration
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── middleware.py                 AF Middleware: logging, security, rate-limit, TTFT
│   ├── orchestration/
│   │   ├── __init__.py
│   │   └── multi_agent.py                Many brains × many hands, WorkflowBuilder
│   └── hosting/
│       ├── __init__.py
│       └── azure_function_host.py        Azure Functions HTTP triggers + FastAPI local dev
│
├── main.py                               Demo entry point (7 demos)
├── requirements.txt                      Dependencies
├── .env.example                          Environment variables template
├── local.settings.json                   Azure Functions local settings
├── blog_en.md                            Technical blog (English)
├── blog_zh.md                            Technical blog (中文)
└── README.md
```

---

## Anthropic → Implementation Mapping

| Anthropic Concept | File | Key Symbol |
|---|---|---|
| **Session** — durable log | `session/session_log.py` | `SessionLog` |
| `emitEvent(id, event)` | | `SessionLog.emit_event()` |
| `getSession(id)` | | `SessionLog.get_session()` |
| `getEvents()` — positional slice | | `SessionLog.get_events(start, end, kind_filter)` |
| `wake(sessionId)` | | `SessionLog.wake()` |
| **Harness** — stateless brain | `harness/harness.py` | `AgentHarness` |
| Foundry LLM client | | `make_foundry_client()` → `FoundryChatClient` |
| **Sandbox** — cattle hands | `sandbox/sandbox.py` | `Sandbox`, `SandboxManager` |
| `execute(name, input) → string` | | `Sandbox.execute()` |
| `provision({resources})` | | `SandboxManager.provision()` |
| Credentials outside sandbox | | `VaultStore` |
| **Skills** — brain capabilities | `skills/skills.py` | AF `Skill`, `SkillsProvider` |
| **Middleware** | `middleware/middleware.py` | AF `@agent_middleware` |
| **Many brains** | `orchestration/multi_agent.py` | `run_many_brains()` |
| **Many hands** | | Specialist agents with isolated sandboxes |
| **Hosting** | `hosting/azure_function_host.py` | Azure Functions + FastAPI |

---

## Setup

### 1 — Install Dependencies

```bash
pip install -r requirements.txt
```

### 2 — Configure Environment

```bash
cp .env.example .env
# Edit .env: set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL
```

### 3 — Authenticate

```bash
# Option A: Local dev (recommended)
az login

# Option B: API key (CI/CD)
export FOUNDRY_API_KEY=<your-key>

# Option C: Azure production — Managed Identity (automatic, no config needed)
```

### 4 — Run Demos

```bash
python main.py                  # all 7 demos
python main.py --mode single    # single-turn session
python main.py --mode multi     # multi-turn conversation
python main.py --mode recover   # harness crash + wake recovery
python main.py --mode stream    # streaming tokens from Foundry
python main.py --mode many      # parallel brains × parallel sandboxes
python main.py --mode log       # session log slicing + filtering
python main.py --mode security  # credential boundary verification
```

---

## Azure Functions API

### Start Local Dev Server

```bash
# FastAPI local server (mirrors Azure Functions routes)
uvicorn maf_harness.hosting.azure_function_host:local_app --reload

# Or via Azure Functions Core Tools
func start
```

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/sessions` | Create session, returns `session_id` |
| `POST` | `/sessions/{id}/run` | Execute one agent turn |
| `GET`  | `/sessions/{id}/events` | Query event log (supports `start`, `end` params) |
| `POST` | `/sessions/{id}/wake` | Rehydrate harness from durable log |
| `GET`  | `/sessions` | List all sessions (FastAPI only) |
| `GET`  | `/health` | Endpoint + model health check |

### Testing with curl

**Health check**

```bash
curl http://localhost:8000/health
```

**Create a session**

```bash
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"task": "Help me analyze Python async patterns"}'
```

Response:

```json
{"session_id": "a1b2c3d4-...", "task": "Help me analyze Python async patterns"}
```

**Run an agent turn**

```bash
# Replace <session_id> with the value from the previous response
curl -X POST http://localhost:8000/sessions/<session_id>/run \
  -H "Content-Type: application/json" \
  -d '{"input": "What are the benefits of async/await in Python?"}'
```

Response:

```json
{
  "session_id": "<session_id>",
  "response": "...",
  "event_count": 5
}
```

**Multi-turn conversation**

```bash
# Continue the conversation with the same session_id
curl -X POST http://localhost:8000/sessions/<session_id>/run \
  -H "Content-Type: application/json" \
  -d '{"input": "Can you write a short example?"}'
```

**Query the event log**

```bash
# Get all events
curl http://localhost:8000/sessions/<session_id>/events

# Get events 2 through 5 (positional slice)
curl "http://localhost:8000/sessions/<session_id>/events?start=2&end=5"
```

**Crash recovery — wake from durable log**

```bash
# Simulate crash recovery: rehydrate a new harness from the session log
curl -X POST http://localhost:8000/sessions/<session_id>/wake
```

Response:

```json
{
  "session_id": "<session_id>",
  "event_count": 7,
  "resumed": true,
  "last_event": {"kind": "harness_wake", "...": "..."}
}
```

**List all sessions (FastAPI local server only)**

```bash
curl http://localhost:8000/sessions
```

### End-to-End curl Example

```bash
# 1. Create session
SESSION_ID=$(curl -s -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"task": "Code assistant"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

echo "Session: $SESSION_ID"

# 2. First turn
curl -s -X POST "http://localhost:8000/sessions/$SESSION_ID/run" \
  -H "Content-Type: application/json" \
  -d '{"input": "What is the capital of Japan?"}' | python3 -m json.tool

# 3. Second turn (multi-turn)
curl -s -X POST "http://localhost:8000/sessions/$SESSION_ID/run" \
  -H "Content-Type: application/json" \
  -d '{"input": "What about its population?"}' | python3 -m json.tool

# 4. Inspect event log
curl -s "http://localhost:8000/sessions/$SESSION_ID/events" | python3 -m json.tool

# 5. Simulate crash recovery
curl -s -X POST "http://localhost:8000/sessions/$SESSION_ID/wake" | python3 -m json.tool

# 6. Continue after recovery
curl -s -X POST "http://localhost:8000/sessions/$SESSION_ID/run" \
  -H "Content-Type: application/json" \
  -d '{"input": "Summarize what we discussed."}' | python3 -m json.tool
```

---

## Key Patterns

### Foundry Client (Zero OpenAI Dependency)

```python
from maf_harness.harness.harness import make_foundry_client

# Auth: FOUNDRY_API_KEY → AzureKeyCredential | else DefaultAzureCredential
client = make_foundry_client(model="gpt-5.4")
```

### Stateless Harness — Crash Recovery

```python
# Harness #1 works then crashes
h1 = AgentHarness(session_log, sandbox_mgr, client=make_foundry_client())
await h1.start(session_id)
await h1.run("Do some work")
del h1  # 💥 crash — session log unaffected

# Harness #2 wakes from the durable log — zero data loss
h2 = AgentHarness(session_log, sandbox_mgr, client=make_foundry_client())
await h2.start(session_id)   # internally: SessionLog.wake(session_id)
await h2.run("Continue the work")
```

### Lazy Sandbox Provisioning (TTFT Improvement)

```python
# Sandbox is NOT created at session start.
# Created lazily only when the agent actually calls a tool.
# Sessions that only reason never pay the container setup cost.
```

### Many Brains in Parallel

```python
results = await run_many_brains(
    tasks=["Research X", "Compute Y", "Summarise Z"],
    session_log=session_log,
    sandbox_mgr=sandbox_mgr,
)
# Each task: own session + own stateless harness + sandbox on demand
```

### Session Log as External Context

```python
# Positional slice (getEvents)
events = await session_log.get_events(sid, start=10, end=20)

# Filter by event type
tool_calls = await session_log.get_events(sid, kind_filter=[EventKind.TOOL_CALL])

# Context window (last N events)
recent = await session_log.get_context_window(sid, last_n=30)
```

---

## Production Backend Swap

| Component | Dev | Production |
|---|---|---|
| LLM | `FoundryChatClient` + `az login` | `FoundryChatClient` + Managed Identity |
| Session log | `InMemoryHistoryProvider` | Azure CosmosDB / Redis |
| Checkpoint | `InMemoryCheckpointStorage` | Azure Blob Storage |
| Vault | `VaultStore` (dict) | Azure Key Vault |
| Sandbox | subprocess | Azure Container Instances |
| Metrics | `Metrics` (in-process) | OpenTelemetry → Azure Monitor |
| Hosting | `uvicorn` + FastAPI | Azure Functions (Serverless) |

---

## References

- [Anthropic: Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)
- [Azure AI Foundry](https://ai.azure.com)
