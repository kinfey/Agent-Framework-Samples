# Managed-Style Hosted Agent (Microsoft Foundry + Anthropic Managed Agents principles)

This sample shows how to build a Microsoft Foundry **Hosted Agent** (Agent Framework + Azure AI AgentServer SDK) whose internal architecture follows the interface design described in Anthropic's ["Scaling Managed Agents: Decoupling the brain from the hands"](https://www.anthropic.com/engineering/managed-agents).

Project layout follows the official [foundry-samples hosted-agents/agent-framework](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/agent-framework) sample, so you can deploy it with `azd`/`ai agent` the same way.

## Why this design

Managed Agents virtualizes three interfaces so each can fail or be swapped independently:

| Interface | Meaning | This repo |
|-----------|---------|-----------|
| **Brain**   | The model + harness that drives the loop | `main.py` (Agent Framework `Agent`) |
| **Hands**   | Sandboxes/tools that actually execute work | `harness/sandbox.py` (`SandboxPool`) |
| **Session** | Durable append-only event log, external to the context window | `harness/session.py` (`SessionStore`) |

Plus a **credential vault** (`harness/vault.py`) so raw tokens are never reachable from the sandbox where generated code runs.

### Principles applied

1. **Don't adopt a pet.** Every `execute(name, input)` call provisions a fresh sandbox and retires it after one invocation. If a sandbox dies, the brain just sees a `ERROR:` string and can retry. No nursing required.
2. **Decouple brain from hands.** The model's *only* external-action contract is the single tool `execute(name, input_json)`. The harness doesn't care whether a hand is a local Python subprocess, a remote container, or an MCP server.
3. **Session ≠ context window.** Every meaningful event (`tool_call`, `tool_result`, `note`, …) is appended to a JSONL log. The model uses `get_events(start, end)` to re-read earlier slices instead of keeping everything in its active context. If the harness crashes, `SessionStore.wake(session_id)` replays the log.
4. **Credentials live outside the sandbox.** The model passes logical credential *names* (e.g. `"credential": "github"`); the vault injects the real token at call time and redacts it from tool output before the result returns to the model.
5. **Many brains, many hands.** Because `execute` is generic and the session is external, you can run many stateless harness replicas against the same session log, and point each one at a different set of hands.

## Tools the model sees

| Tool | Purpose |
|------|---------|
| `list_tools()` | Discover currently attached hands |
| `execute(name, input_json)` | Call any hand in a fresh sandbox |
| `get_events(start, end)` | Re-read a slice of the durable session log |
| `emit_note(note)` | Checkpoint intermediate reasoning to the log |

Built-in hands (swap for your own in `harness/sandbox.py`):

- `python_exec` — run a short Python snippet in an isolated subprocess
- `shell_exec` — run an argv command in an isolated subprocess
- `http_fetch` — HTTP GET with vault-injected auth headers

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env                  # then edit with your Foundry endpoint

azd auth login

azd ai agent init -m agent.yaml 

azd ai agent run
```

Agent listens on `http://localhost:8088` and speaks the OpenAI Responses protocol:

```bash
curl -sS -H "Content-Type: application/json" -X POST http://localhost:8088/responses \
  -d '{"input":"Use execute to run python that prints 2+2, then summarize.","stream":false}'
```

Look at `./sessions/<session-id>.jsonl` to see the durable log the model is writing to.

## Test cases

These cover the four model-facing tools (`list_tools` / `execute` / `get_events` / `emit_note`) and the three built-in hands. Run each one, then check the tail of `./sessions/<id>.jsonl` to see the full `tool_call → tool_result` chain.

### A. Capability discovery

```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call list_tools() and return the result verbatim.","stream":false}'
```
Expect: `["http_fetch","python_exec","shell_exec"]`.

### B. `python_exec`

**B1 — arithmetic**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to compute sum(i*i for i in range(1000)) and tell me the result.","stream":false}'
```
Expect `332833500`.

**B2 — stdlib access**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to print the current UTC ISO time and the Python version.","stream":false}'
```

**B3 — cattle-style retry on failure**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"First use python_exec to run 1/0 and observe the error, then retry with 1/1 and return the result.","stream":false}'
```
Session should contain one failing `tool_result` (`ERROR: sandbox ...`) followed by a successful one.

**B4 — timeout**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to run import time; time.sleep(30). If it times out, explain the failure.","stream":false}'
```
Expect `ERROR: sandbox ... python_exec timed out after 15s`.

### C. `shell_exec`

**C1 — argv list**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use shell_exec to run ls -la / and list only the first 5 lines.","stream":false}'
```

**C2 — reject string argv**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call shell_exec with argv=\"ls -la\" (a string). If the tool rejects it, tell me exactly what it said.","stream":false}'
```
Expect `ERROR: 'argv' must be a list of strings.`, then the model should retry with a list.

### D. `http_fetch` + vault

**D1 — public URL**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to GET https://api.github.com/zen and return the plain text.","stream":false}'
```

**D2 — credential injected by vault** (add `GITHUB_TOKEN=ghp_xxx` to `.env` and restart first)
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to call https://api.github.com/user with credential logical name \"github\". Do not echo any token in your reply.","stream":false}'
```
Expect a successful call; the raw token never appears in the reply (the vault redacts it to `***REDACTED***`).

**D3 — reject non-http scheme**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use http_fetch to GET file:///etc/passwd. If the tool rejects it, tell me exactly what it said.","stream":false}'
```
Expect `ERROR: 'url' must be an http(s) URL.`

### E. `emit_note` / `get_events` (session ≠ context window)

**E1 — write a note**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use emit_note to record: \"User prefers metric units; likes concise answers.\" Confirm the event index.","stream":false}'
```

**E2 — read back a slice**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Call get_events(0, -1) and return only the entries where type==\"note\".","stream":false}'
```

**E3 — checkpoint during a long task**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Do three steps: 1) use python_exec to compute 2**64; 2) use emit_note to record the result; 3) use get_events to read back the last note and repeat it.","stream":false}'
```

### F. Orchestration

**F1 — chained tools**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"First http_fetch https://api.github.com/zen, then use python_exec to uppercase the returned text and count its characters.","stream":false}'
```

**F2 — unknown tool, self-correct**
```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use execute(\"database_query\", {...}) to query the database. If it does not exist, fall back to list_tools and tell me what is actually available.","stream":false}'
```
Expect first: `ERROR: unknown tool 'database_query'`; the model then falls back to `list_tools`.

### G. Streaming

```bash
curl -N -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"Use python_exec to print 1 through 20, and explain the output incrementally.","stream":true}'
```

### Inspecting evidence

After any test, the newest session file shows the durable event chain:

```bash
ls -t sessions/ | head -1 | xargs -I {} jq -c . sessions/{}
```

Expect a sequence like `session_start → tool_call(execute) → tool_result → note → ...` — concrete evidence that brain, hands, and session are decoupled, sandboxes are cattle, and context lives outside the model's window.

## Deploy to Microsoft Foundry

Build for `linux/amd64` and deploy with the Foundry CLI the same way as the upstream sample:

```bash
docker build --platform=linux/amd64 -t managed-style-agent .
```

Then follow the hosted-agent deployment flow: <https://learn.microsoft.com/en-us/azure/ai-foundry/agents/concepts/hosted-agents?view=foundry&tabs=cli>.

