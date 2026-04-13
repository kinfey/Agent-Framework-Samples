# Implementing Cloud-Native Anthropic Managed Agent Architecture with Microsoft Agent Framework and Azure

> Anthropic proposed a core thesis in [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents): **the reliability of an Agent system depends on the degree of decoupling between components, not on the complexity of any single component**. This article implements that architectural theory end-to-end on the **Microsoft Agent Framework (MAF) + Azure AI Foundry** stack, validating every design decision with runnable code.

**Content Structure**

| Section | Topic | Corresponding Anthropic Concept |
|---------|-------|---------------------------------|
| 1 | Failure Mode Analysis of Monolithic Agents | "Don't adopt a pet" |
| 2 | Three-Layer Decoupled Architecture Overview | Session / Harness / Sandbox |
| 3 | Session: Durable Event Log | `emitEvent` · `getEvents` · `wake` |
| 4 | Sandbox: Disposable Execution Environment | `execute(name, input) → string` |
| 5 | Harness: Stateless Orchestrator | Crash recovery · `wake(sessionId)` |
| 6 | MAF-Native Extensions: Skill · Middleware · Foundry | Beyond the three layers |
| 7 | Many Brains, Many Hands: Parallel Orchestration | "Many brains, many hands" |
| 8 | Lazy Sandbox Provisioning and TTFT Optimization | p50 ↓60%, p95 ↓90% |
| 9 | Azure Functions Stateless Hosting | Serverless deployment |
| 10 | Interface Stability from Dev to Production | "Opinionated about interfaces" |

---

## 1. Failure Mode Analysis of Monolithic Agents

### Lessons from Anthropic

Anthropic initially deployed all Agent components — session state, orchestration logic, and code execution environment — in a single container. This design had one obvious benefit: file operations were direct syscalls with no cross-service serialization overhead. But as they described in their article:

> "We'd adopted a pet. If a container failed, the session was lost. If a container was unresponsive, we had to nurse it back to health."

This "pet" metaphor precisely describes four failure modes of monolithic architecture:

| Failure Mode | Root Cause | Consequence |
|-------------|------------|-------------|
| **State loss** | Session log co-located with the harness | Container crash = complete context loss |
| **Debugging difficulty** | WebSocket event stream can't differentiate failure sources | Harness bugs, packet drops, and container crashes all look the same |
| **Security boundary violation** | LLM-generated code shares a process with credentials | Prompt injection → credential exfiltration |
| **Network coupling** | Harness assumes all resources are co-located | Customer VPC access requires network peering |

**Concrete scenario**: An engineer uses an Agent to debug a production incident. The Agent has already analyzed 200MB of logs, executed 12 diagnostic commands, and pinpointed the root cause as a database connection pool leak. At this point, the container crashes due to OOM. Since session state is embedded in the harness process, 30 minutes of investigation progress is wiped to zero — the user must re-describe the problem context from scratch, and the Agent must re-execute every diagnostic step.

This isn't a hypothetical edge case. For any long-running Agent task (code review, data analysis, multi-step deployment), monolithic architecture means **the system's reliability equals the reliability of its weakest component**. Operating system design answered this question long ago: **isolation is a prerequisite for reliability**.

---

## 2. Architecture Overview: Three-Layer Decoupling

Anthropic's solution virtualizes the Agent into three independent interfaces, each capable of failing and being replaced independently:

```
┌──────────────────────────────────────────────────────────┐
│                    Session (Event Log)                   │
│            Durable, append-only event store              │
│                                                          │
│  create_session(task) → id                               │
│  emitEvent(id, event) → void                             │
│  getEvents(id, start?, end?) → events[]                  │
│  wake(id) → (session, events)                            │
└───────────────────────────┬──────────────────────────────┘
                            │  read/write events
┌───────────────────────────┼──────────────────────────────┐
│               Harness (Orchestrator / Brain)             │
│        Stateless LLM loop · recovers via wake()          │
│                                                          │
│  start(session_id)  →  wake()  →  rebuild context        │
│  run(input)  →  Agent.run()  →  emit_event()             │
│  shutdown()  →  emit SESSION_END                         │
└───────────────────────────┬──────────────────────────────┘
                            │  execute(name, input) → string
┌───────────────────────────┼──────────────────────────────┐
│               Sandbox (Hands)                            │
│      Isolated execution · disposable · credentials       │
│      unreachable                                         │
│                                                          │
│  provision(resources) → sandbox_id                       │
│  execute(name, input) → string                           │
│  kill() → mark dead, harness provisions replacement      │
└──────────────────────────────────────────────────────────┘
```

This layered design philosophy is deeply aligned with operating systems. Anthropic explicitly articulated this analogy near the end of their article:

> "Operating systems have lasted decades by virtualizing the hardware into abstractions general enough for programs that didn't exist yet. The `read()` command is agnostic as to whether it's accessing a disk pack from the 1970s or a modern SSD."

MAF provides all the primitives needed to implement this architecture: `Agent` as the orchestrator core, `Skill` for injecting domain knowledge, `@agent_middleware` for cross-cutting concerns, and `FoundryChatClient` for connecting to Azure AI Foundry model endpoints. Let's walk through each layer.

---

## 3. Layer One: Session — Durable Event Log

### Design Principles

Anthropic positions the Session not merely as "chat history storage" but as a **durable event source independent of both the harness and sandbox**:

> "The session log sits outside the harness. Nothing in the harness needs to survive a crash. When one fails, a new one can be rebooted with `wake(sessionId)`."

More importantly, Anthropic explicitly distinguishes the Session from the LLM's context window:

> "The session is not Claude's context window... `getEvents()` allows the brain to interrogate context by selecting positional slices of the event stream."

This means the Session is a **queryable event database**, not merely a message list passed to the LLM. The harness can slice by position, filter by type, and recover from any event point after a crash.

### MAF Implementation: `session/session_log.py`

```python
class EventKind(str, Enum):
    """11 event types covering the full Agent lifecycle."""
    SESSION_START   = "session_start"    #  Session created
    USER_INPUT      = "user_input"       #  User message
    AGENT_RESPONSE  = "agent_response"   #  Model response
    TOOL_CALL       = "tool_call"        #  Tool invocation request
    TOOL_RESULT     = "tool_result"      #  Tool execution result
    HARNESS_WAKE    = "harness_wake"     #  Harness recovery
    HARNESS_CRASH   = "harness_crash"    #  Harness crash record
    SANDBOX_SPAWN   = "sandbox_spawn"    #  Sandbox created
    SANDBOX_EXEC    = "sandbox_exec"     #  Sandbox execution
    SESSION_END     = "session_end"      #  Session ended
    COMPACTION      = "compaction"       #  Context compaction
```

The event model is the cornerstone of Session design. Why structured events instead of raw messages? Because an Agent's behavior goes far beyond "conversation":

```python
@dataclass
class SessionEvent:
    kind:       EventKind           # Event type
    payload:    dict[str, Any]      # Business data (tool name, input, output, etc.)
    timestamp:  float               # Millisecond-precision timestamp
    event_id:   str                 # Globally unique identifier
    session_id: str                 # Owning session
```

Building on this, `SessionLog` implements the four core interfaces defined by Anthropic:

```python
class SessionLog:
    """
    Append-only durable event store.
    Sessions are independent of the harness and sandbox —
    harness crashes do not affect session integrity.
    """

    async def create_session(self, task: str, metadata: dict | None = None) -> str:
        """Create a session, emit SESSION_START event, return session_id."""
        session_id = str(uuid.uuid4())
        af_session = AgentSession(session_id=session_id)
        self._sessions[session_id] = af_session
        self._history[session_id]  = InMemoryHistoryProvider()  # AF-native history provider
        await self.emit_event(session_id, SessionEvent(
            kind=EventKind.SESSION_START,
            session_id=session_id,
            payload={"task": task, "metadata": metadata or {}},
        ))
        return session_id

    async def emit_event(self, session_id: str, event: SessionEvent) -> None:
        """Append an event to the log — maps to Anthropic's emitEvent(id, event)."""
        event.session_id = session_id
        async with self._lock:  # asyncio.Lock ensures concurrency safety
            self._store.setdefault(session_id, []).append(event)

    async def get_events(
        self, session_id: str,
        start: int = 0, end: int | None = None,
        kind_filter: list[EventKind] | None = None,
    ) -> list[SessionEvent]:
        """
        Positional slicing + type filtering — maps to Anthropic's getEvents().
        Supports the "pick up where you left off" recovery pattern.
        """
        events = self._store.get(session_id, [])[start:end]
        if kind_filter:
            events = [e for e in events if e.kind in kind_filter]
        return events

    async def wake(self, session_id: str) -> tuple[AgentSession | None, list[SessionEvent]]:
        """
        Harness recovery entry point — maps to Anthropic's wake(sessionId).
        Returns (session_metadata, all_events) for the new harness to rebuild context.
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

**Context engineering interface**: Anthropic notes: "Any fetched events can also be transformed in the harness before being passed to Claude's context window." `SessionLog` provides a lightweight context window method that the harness uses to implement a sliding window strategy:

```python
    async def get_context_window(self, session_id: str, last_n: int = 20) -> list[SessionEvent]:
        """Last N events — base data for the harness to build the LLM context window."""
        events = await self.get_events(session_id)
        return events[-last_n:]
```

**Usage example** — the following code shows how a Session produces structured event streams during actual conversations:

```python
# demo: Demo 6 — Session log inspection
sid = await session_log.create_session("Log inspection demo")
h   = _harness()
await h.start(sid)
for q in ["Hello!", "What is 1 + 1?", "Thanks, bye."]:
    await h.run(q)
await h.shutdown()

# Query the full event stream
all_events = await session_log.get_events(sid)
# → [SESSION_START, HARNESS_WAKE, USER_INPUT, AGENT_RESPONSE, ... SESSION_END]

# Sliding window: last 5 events
recent = await session_log.get_context_window(sid, last_n=5)

# Filter by type: only Agent responses
responses = await session_log.get_events(sid, kind_filter=[EventKind.AGENT_RESPONSE])

# Positional slice: events 1 through 4
sliced = await session_log.get_events(sid, start=1, end=4)
```

**Production path**: Development uses `InMemoryHistoryProvider` (in-memory dict); production swaps to Azure CosmosDB or Redis — the interface remains completely unchanged.

---

## 4. Layer Two: Sandbox — Disposable Execution Environment

### Design Principles

Anthropic's positioning of the sandbox is surgically precise — an interface with just two methods:

> "The container became cattle. If the container died, the harness caught the failure as a tool-call error and passed it back to Claude. Claude could decide to retry, and a new container could be reinitialized with `provision({resources})`."

Even more critical is the security boundary:

> "A prompt injection only had to convince Claude to read its own environment. Once an attacker has those tokens, they can spawn fresh, unrestricted sessions. The structural fix was to make sure the tokens are never reachable from the sandbox."

### MAF Implementation: `sandbox/sandbox.py`

**Credential isolation — `VaultStore`**

This is the most important security component in the entire sandbox design. Anthropic uses two patterns to ensure credentials never enter the sandbox: binding credentials to the resource initialization phase (e.g., injecting tokens during Git clone), and indirect access through a Vault proxy (MCP Proxy). Our implementation adopts the latter:

```python
class VaultStore:
    """
    Secure credential store — tokens never enter the sandbox process.
    Development: in-memory dict. Production: Azure Key Vault.
    """
    def store(self, key: str, token: str) -> None:
        self._vault[key] = token

    def fetch(self, key: str) -> str | None:
        return self._vault.get(key)

    def revoke(self, key: str) -> None:
        self._vault.pop(key, None)
```

**Scenario walkthrough**: Suppose the Agent needs to call the GitHub API to create a Pull Request. The traditional approach injects `GITHUB_TOKEN` as an environment variable — LLM-generated code can read it via `os.environ`. With VaultStore, the token is stored outside the sandbox; tool functions retrieve credentials through the controlled path `vault.fetch("github_token")`, and the LLM can never enumerate or export keys from within the sandbox. Here's the security boundary verification:

```python
# demo: Demo 7 — Security boundary verification
vault.store("foundry_key", "FoundryKey-XXXX")
vault.store("github_token", "ghp_XXXXXXXX")

sid = await sandbox_mgr.provision()
box = sandbox_mgr.get(sid)

# LLM-generated code attempts to read environment variables → not visible
r1 = await box.execute("run_python",
    "import os; print(os.environ.get('FOUNDRY_API_KEY', 'NOT_VISIBLE'))")
# → 'NOT_VISIBLE'

# Attempt to call an unauthorized tool → denied
r2 = await box.execute("shell", "cat /etc/secrets")
# → '[SANDBOX DENIED] Tool 'shell' is not in the allowed list.'
```

**Execution interface — `Sandbox.execute()`**

```python
class Sandbox:
    async def execute(self, name: str, input_data: str) -> str:
        """
        execute(name, input) → string — the sole interface between brain and hands.
        The harness neither knows nor needs to know whether 'name' maps to
        a container, a subprocess, or a remote service.
        """
        if not self._alive:
            raise RuntimeError(f"Sandbox {self.sandbox_id} is dead.")

        # Permission check: only authorized tools allowed
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
            self._alive = False  # Timeout → mark dead → harness provisions replacement
            raise RuntimeError(
                f"Sandbox {self.sandbox_id} timed out on '{name}'. "
                "Harness should provision a fresh sandbox."
            )
```

**Sandbox manager — "cattle pattern"**

```python
class SandboxManager:
    async def provision(self, resources: SandboxResources | None = None) -> str:
        """
        Create a new sandbox — maps to Anthropic's provision({resources}).
        Invoked on-demand by the harness, not pre-created.
        """
        sandbox_id = str(uuid.uuid4())
        self._sandboxes[sandbox_id] = Sandbox(
            sandbox_id, resources or SandboxResources(),
            self._registry, self._vault,
        )
        return sandbox_id

    def reclaim(self, sandbox_id: str) -> None:
        """Terminate and discard — zero sentiment, zero attachment."""
        self._sandboxes[sandbox_id].kill()
        del self._sandboxes[sandbox_id]
```

**Built-in tool registry**: The sandbox pre-registers four tool categories, each mapping to an Agent capability:

| Tool | Capability | Production Replacement |
|------|-----------|----------------------|
| `run_python` | Execute Python in an isolated subprocess | Azure Container Instances |
| `web_search` | Web search (simulated in dev) | Azure AI Search / Bing API |
| `read_file` | Read files | Azure Blob Storage |
| `write_file` | Write files | Azure Blob Storage |

Tools can be dynamically extended at runtime via `sandbox_mgr.register_tool(name, fn)` — as Anthropic states: "That interface supports any custom tool, any MCP server, and our own tools."

---

## 5. Layer Three: Harness — Stateless Orchestrator

### Design Principles

The harness is the Agent's "brain" — it calls the LLM, parses tool invocations, routes to the sandbox, and writes results back to the Session. Anthropic's core insight: **the harness must be stateless**.

> "The harness also became cattle. Because the session log sits outside the harness, nothing in the harness needs to survive a crash."

The operational significance of statelessness is profound: no sticky sessions needed, no affinity routing required, no graceful draining necessary. Any harness instance can serve any session; load balancing and auto-scaling become transparent infrastructure-level operations.

### MAF Implementation: `harness/harness.py`

**Harness lifecycle**: `start()` → `run()` → `shutdown()`

```python
class AgentHarness:
    """
    Stateless orchestrator powered by Azure AI Foundry.
    On crash: create a new instance, call start(same session_id),
    and wake() automatically rebuilds full context from the durable log.
    """

    def __init__(self, session_log, sandbox_mgr, config=None, client=None):
        self.session_log = session_log
        self.sandbox_mgr = sandbox_mgr
        self.config      = config or HarnessConfig()
        self._client     = client          # Injectable FoundryChatClient
        self._agent      = None            # AF Agent instance — built during start()
        self._session_id = None

    async def start(self, session_id: str) -> None:
        """
        Attach to a session. Core logic:
        1. wake() — retrieve full event history from Session
        2. Build tool list (sandbox tools + context query tools)
        3. Inject skills provider (domain knowledge)
        4. Mount middleware stack (logging → security → rate limiting → observability)
        5. Assemble AF Agent
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

Note how `_build_system_prompt()` injects the most recent 5 events into the system prompt — this implements what Anthropic calls "context engineering in the harness":

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

**Crash recovery demo** — this is the core validation scenario for the entire architecture:

```python
# demo: Demo 3 — Harness crash recovery

# Harness #1 works normally then crashes
sid = await session_log.create_session("Crash recovery demo")
h1  = _harness("Harness-1")
await h1.start(sid)
r1  = await h1.run("List three benefits of async programming in Python.")
print(f"[H1] {r1}")
del h1  # 💥 Simulate crash — harness discarded without shutdown

# Verify: Session is unaffected
n_before = await session_log.event_count(sid)
print(f"[LOG] {n_before} events preserved after crash ✓")

# Harness #2 recovers from the durable log — zero data loss
h2 = _harness("Harness-2")
await h2.start(sid)       # Internally calls wake() → rebuilds full context
r2 = await h2.run("Give a short Python async/await example based on what you found.")
print(f"[H2] Seamless continuation: {r2}")
await h2.shutdown()

n_after = await session_log.event_count(sid)
print(f"[LOG] Final: {n_after} events ({n_after - n_before} added after recovery)")
```

The key point: `h2` has no knowledge that `h1` ever existed. It only knows that the Session contains event history; after `wake()` injects it into context, `h2` can "seamlessly continue" the conversation — like an OS restoring process state, where the user perceives no interruption.

**Anthropic's three principles in practice**:

| Principle | Implementation |
|-----------|---------------|
| **Stateless** | `AgentHarness` holds no chat history; all state externalized via `session_log.emit_event()` |
| **Decoupled** | Calls sandbox indirectly via `build_sandbox_tools()`, using `execute(name, input) → string` |
| **Recoverable** | `start()` internally calls `wake()` to rebuild from log; exceptions auto-emit `HARNESS_CRASH` events |

### Foundry Client Factory — Zero OpenAI SDK Dependency

LLM backend selection is an architectural decision. Directly depending on the OpenAI SDK means coupling to a specific vendor — model pricing changes, API version iterations, and regional compliance restrictions all directly impact the system. `FoundryChatClient` eliminates this coupling through Azure AI Foundry's unified entry point:

```python
def make_foundry_client(model: str | None = None) -> FoundryChatClient:
    """
    Auth priority (DefaultAzureCredential chain):
      1. FOUNDRY_API_KEY env var  → AzureKeyCredential (dev / CI)
      2. az login                 → AzureCliCredential (local dev)
      3. Managed Identity         → automatic on Azure (production)
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

Local development just needs `az login`; production automatically uses Managed Identity — zero code changes.

---

## 6. Beyond Three Layers: MAF-Native Extensions

Anthropic's three-layer architecture is the skeleton, but making an Agent system run reliably in production requires answering three additional questions: "Where does the brain's domain knowledge come from?", "How are cross-cutting concerns like logging, security, and rate limiting handled?", and "How do you ensure harness assumptions don't go stale?". MAF's native capabilities fill these gaps.

### 6.1 Skill — Composable Domain Knowledge

Anthropic acknowledged a core tension in their article:

> "Harnesses encode assumptions about what Claude can't do on its own. However, those assumptions need to be frequently questioned because they can go stale as models improve."

They gave an example: Claude Sonnet 4.5 exhibited "anxiety behavior" when approaching context window limits (prematurely ending tasks), so the harness added context resets. But when the same harness was used with Claude Opus 4.5, the behavior was gone — the reset logic had become dead code.

MAF's `Skill` provides a formal mechanism for managing these "potentially stale assumptions." Skills define **what the brain knows how to do** (workflow protocols), orthogonal to **what the hands actually do** (sandbox execution):

```python
# skills/skills.py — four built-in skills

research_skill = Skill(
    name="research",
    description="Systematic research: decompose → search → cross-validate → synthesize",
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
    description="Write, test, and iterate on Python code.",
    content="...",
    scripts=[SkillScript(name="run_snippet", ...)],  # AF SkillScript binds skills to tools
)

summarise_skill = Skill(
    name="summarise",
    description="Context compaction: compress historical events into structured summaries when approaching context window limits.",
    content="..."  # Preserve last 5 events verbatim, generate summary for rest + emit COMPACTION event
)

orchestration_skill = Skill(
    name="orchestration",
    description="Multi-agent orchestration: decompose tasks → delegate to specialist agents → aggregate results.",
    content="..."
)
```

Skills are injected into the AF `Agent`'s `context_providers` via `SkillsProvider`. When models gain new capabilities (e.g., future models with built-in research abilities), you simply remove or replace the corresponding Skill — no harness code changes needed.

### 6.2 Middleware — Layered Cross-Cutting Concern Governance

Agent systems face the same cross-cutting concern governance challenges as web services. MAF's `@agent_middleware` decorator allows injecting four governance layers without modifying the Agent's core logic:

```python
# middleware/middleware.py — middleware stack (executed in order)

# 1. Session logging — automatically persists each turn
@agent_middleware
async def session_logging_mw(ctx: AgentContext, next):
    await session_log.emit_event(session_id, SessionEvent(kind=EventKind.USER_INPUT, ...))
    result = await next()
    await session_log.emit_event(session_id, SessionEvent(kind=EventKind.AGENT_RESPONSE, ...))
    return result

# 2. Security — intercept credential patterns before they reach the model
@agent_middleware
async def security_mw(ctx: AgentContext, next):
    blocked = ["sk-", "Bearer ", "AZURE_", "password=", "secret=", "api_key=", "FoundryKey"]
    ctx.messages = [m for m in ctx.messages if not any(p in str(m) for p in blocked)]
    return await next()

# 3. Rate limiting — sliding window RPM limit
@agent_middleware
async def rate_limit_mw(ctx: AgentContext, next):
    # More than max_rpm calls within 60 seconds → raise RuntimeError
    ...
    return await next()

# 4. Observability — TTFT (Time To First Token) tracking
@agent_middleware
async def observability_mw(ctx: AgentContext, next):
    start = time.perf_counter()
    result = await next()
    elapsed_ms = (time.perf_counter() - start) * 1000
    metrics.record_ttft(elapsed_ms)
    # Output: [METRICS] run=5 latency=1200ms p50=800ms p95=2100ms
    return result
```

Middleware ordering matters: security checks run before rate limiting (intercepted messages don't consume rate limit quota), and observability wraps the outermost layer (measuring full end-to-end latency).

### 6.3 Five-Layer Tool Wrapping — Lazy Sandbox Creation

`build_sandbox_tools()` is the bridge between harness and sandbox, implementing five key capabilities:

```python
def build_sandbox_tools(sandbox_mgr, session_log, session_id) -> list[Callable]:
    _state = {"sandbox_id": None}  # Starts empty — lazy creation

    async def _ensure() -> str:
        """LazyInit: sandbox created only on the first tool call."""
        if _state["sandbox_id"] is None:
            sid = await sandbox_mgr.provision(SandboxResources(
                allowed_tools=["run_python", "web_search", "read_file", "write_file"],
            ))
            _state["sandbox_id"] = sid
            await session_log.emit_event(session_id, SessionEvent(
                kind=EventKind.SANDBOX_SPAWN, ...))
        return _state["sandbox_id"]

    async def _exec(name: str, data: str) -> str:
        """Execution with auto-retry: sandbox dies → reclaim → provision → retry."""
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

    # Typed AF tool functions — LLM selects via function calling
    async def run_python(code: Annotated[str, Field(description="...")]) -> str: ...
    async def web_search(query: Annotated[str, Field(description="...")]) -> str: ...
    async def get_session_context(last_n: Annotated[int, ...] = 10) -> str:
        """Retrieve recent events from the session log — LLM can proactively query context."""
        events = await session_log.get_context_window(session_id, last_n=last_n)
        return "\n".join(f"[{e.kind.value}] {e.payload}" for e in events)

    return [run_python, web_search, write_file, read_file, get_session_context]
```

Note the `get_session_context` tool: it allows the LLM to proactively query the Session event stream rather than passively waiting for the harness to push context — this is exactly the implementation of what Anthropic calls "context as a programmable object."

---

## 7. Many Brains, Many Hands — Parallel Orchestration

### From Monolith to Cluster

The core power of decoupling is not fault tolerance — it's **composition**. Anthropic explicitly articulated this:

> "Scaling to many brains just meant starting many stateless harnesses, and connecting them to hands only if needed."
>
> "Decoupling the brain from the hands makes each hand a tool: a name and input go in, and a string is returned. Because no hand is coupled to any brain, brains can pass hands to one another."

### MAF Implementation: `orchestration/multi_agent.py`

**Scenario**: A user requests "Analyze the tech stack, market positioning, and pricing strategy of three competitors, and generate a consolidated report." A single Agent executing serially takes N minutes. In many-brains mode:

```python
async def run_many_brains(tasks, session_log, sandbox_mgr) -> list[dict]:
    """
    Spawn an independent stateless Foundry harness for each task, executing concurrently.
    Each harness has its own session_id and event log entries.
    Sandboxes are only created when tools are actually needed —
    pure reasoning tasks consume zero sandbox resources.
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

**Usage example**:

```python
# demo: Demo 5 — Many brains in parallel
tasks = [
    "What is the capital of Japan? One sentence only.",        # Pure reasoning → no sandbox
    "Calculate the sum of squares from 1 to 10 using Python.", # Needs run_python → sandbox created
    "Give one fun fact about Microsoft Azure AI Foundry.",     # Pure reasoning → no sandbox
]
results = await run_many_brains(tasks, session_log, sandbox_mgr)
# Three tasks execute in parallel; total time ≈ max(individual task time)
```

### Specialist Agents + Graph-Based Routing

More complex scenarios require specialist division of labor. Using AF `WorkflowBuilder` to construct a directed acyclic graph (DAG):

```
[classify_task] ──→ ResearchAgent    ─┐
                ──→ CodeAgent         ├──→ SummariseAgent ──→ END
                ──→ OrchestratorAgent─┘
```

Each specialist Agent has its own isolated sandbox permissions:

```python
# ResearchAgent — can only use web_search
def make_research_agent(sandbox_mgr):
    async def web_search(query: Annotated[str, ...]) -> str:
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["web_search"]))
        result = await sandbox_mgr.execute(sid, "web_search", query)
        sandbox_mgr.reclaim(sid)  # Use and discard
        return result
    return _agent("ResearchAgent", "...", tools=[web_search])

# CodeAgent — can only use run_python
def make_code_agent(sandbox_mgr):
    async def run_python(code: Annotated[str, ...]) -> str:
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["run_python"]))
        result = await sandbox_mgr.execute(sid, "run_python", code)
        sandbox_mgr.reclaim(sid)
        return result
    return _agent("CodeAgent", "...", tools=[run_python])

# SummariseAgent — pure reasoning, no sandbox needed
def make_summarise_agent():
    return _agent("SummariseAgent", "...")

# OrchestratorAgent — "passes hands between specialists" via delegation tools
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

Routing logic is declaratively defined via `WorkflowBuilder`:

```python
builder = WorkflowBuilder(
    start_executor=FunctionExecutor(classify_task),
    checkpoint_storage=InMemoryCheckpointStorage(),  # Production → Azure Blob Storage
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

`InMemoryCheckpointStorage` ensures workflow state survives harness restarts — consistent with the Session design philosophy.

---

## 8. Lazy Sandbox Provisioning — TTFT Optimization

### Reasoning that doesn't need tools shouldn't pay the startup cost

This is the most practically valuable performance insight from Anthropic's article:

> "Decoupling the brain from the hands means that containers are provisioned by the brain via a tool call only if they are needed. A session that didn't need a container right away didn't wait for one... Our p50 TTFT dropped roughly 60% and p95 dropped over 90%."

The logic behind this: across all Agent sessions, a significant proportion only require reasoning — answering questions, explaining concepts, formulating plans. If every request pre-creates a sandbox (starting containers → allocating memory → initializing tool registry), these pure reasoning requests are paying for capabilities they'll never use.

In our implementation, sandbox creation is entirely triggered by the LLM's tool calls:

```python
# harness/harness.py — build_sandbox_tools()
_state = {"sandbox_id": None}  # Starts empty

async def _ensure() -> str:
    if _state["sandbox_id"] is None:
        # Lazy creation! Only triggered when the LLM first decides to use a tool
        sid = await sandbox_mgr.provision(SandboxResources(
            allowed_tools=["run_python", "web_search", "read_file", "write_file"],
        ))
        _state["sandbox_id"] = sid
        await session_log.emit_event(session_id, SessionEvent(
            kind=EventKind.SANDBOX_SPAWN, ...))
    return _state["sandbox_id"]
```

**Practical comparison**:

| Request Type | Coupled Mode | Decoupled Mode (Lazy Creation) |
|-------------|-------------|-------------------------------|
| "What is the capital of Japan?" | Wait for sandbox startup + reasoning | Reasoning only (zero sandbox overhead) |
| "Calculate Fibonacci using Python" | Wait for sandbox startup + reasoning + execution | Reasoning + sandbox created on first tool call + execution |
| Multi-turn pure reasoning dialogue | Sandbox idle overhead every turn | Zero sandbox resource consumption throughout |

---

## 9. Azure Functions Hosting — A Natural Fit for Statelessness

### Serverless as "Ultimate Cattle Pattern"

Anthropic's core claim — "any harness instance could serve any session" — translates to infrastructure terms: **no dedicated servers needed**. Azure Functions' execution model is a perfect match: each HTTP request gets a function instance, releasing all resources upon completion.

### MAF Implementation: `hosting/azure_function_host.py`

```python
# Full lifecycle of each HTTP request
async def _handle_run_turn(session_id: str, body: dict) -> dict:
    user_input = body.get("input", "")
    # 1. Create a stateless harness
    harness = AgentHarness(
        session_log=_session_log,    # Durable Session (process-level singleton)
        sandbox_mgr=_sandbox_mgr,
        config=_config,
        client=_get_client(),        # Foundry client (lazily initialized)
    )
    # 2. wake() — rebuild context from the durable log
    await harness.start(session_id)
    # 3. Execute one Agent loop iteration
    response = await harness.run(user_input)
    # 4. Graceful shutdown (emit SESSION_END event)
    await harness.shutdown()
    return {"session_id": session_id, "response": response,
            "event_count": await _session_log.event_count(session_id)}
```

**REST API design** — five endpoints covering complete Agent lifecycle management:

| Method | Path | Description | Corresponding Primitive |
|--------|------|-------------|------------------------|
| `POST` | `/sessions` | Create new session | `create_session(task)` |
| `POST` | `/sessions/{id}/run` | Execute one Agent turn | `run(input)` |
| `GET`  | `/sessions/{id}/events` | Query event log (with pagination) | `getEvents(start, end)` |
| `POST` | `/sessions/{id}/wake` | Rehydrate harness (crash recovery) | `wake(sessionId)` |
| `GET`  | `/health` | Health check | — |

**Key characteristics**:
- **No sticky sessions**: Any Functions instance serves any session_id — routing is purely request-parameter-based
- **Horizontal scaling**: More Functions instances = more concurrent processing capacity
- **Cold start optimization**: Foundry client is lazily initialized, avoiding startup failures when environment variables aren't yet available

Local development uses FastAPI mirroring the same API:

```bash
uvicorn maf_harness.hosting.azure_function_host:local_app --reload
```

---

## 10. From Dev to Production — Interfaces Stay, Implementations Swap

Anthropic summarized their design philosophy at the end of their article:

> "We're opinionated about the shape of these interfaces, not about what runs behind them."

This is the ultimate test of architectural quality: **can you move the system from a development laptop to an Azure production cluster without changing a single line of business code?**

Here is each component's replacement path from dev to production — note the interfaces remain completely unchanged:

| Component | Development | Azure Production | Interface |
|-----------|-------------|-----------------|-----------|
| LLM Client | `FoundryChatClient` + `az login` | `FoundryChatClient` + Managed Identity | `FoundryChatClient` |
| Session Log | `InMemoryHistoryProvider` (in-memory dict) | Azure CosmosDB / Redis | `SessionLog.emit_event()` · `wake()` |
| Workflow Checkpoints | `InMemoryCheckpointStorage` | Azure Blob Storage | `CheckpointStorage` |
| Credential Store | `VaultStore` (in-memory dict) | Azure Key Vault | `vault.store()` · `vault.fetch()` |
| Sandbox Execution | `subprocess` (local Python subprocess) | Azure Container Instances | `execute(name, input) → string` |
| Observability | `Metrics` (in-process counters) | OpenTelemetry → Azure Monitor | `metrics.record_ttft()` |
| Hosting | `uvicorn` + FastAPI (local) | Azure Functions (Serverless) | HTTP REST API |

Every substitution is an implementation-level change. It touches no orchestration logic, no middleware stack, no skill definitions, and no business code.

---

## Conclusion: Designing Systems for Programs Not Yet Imagined

Anthropic's Managed Agent architecture answers a classic proposition in operating system design — **"how to design a system for programs as yet unthought of."** Their answer is deeply aligned with Unix philosophy: virtualize Agent components into stable interfaces, letting implementations freely evolve as model capabilities advance.

Through Microsoft Agent Framework and Azure AI Foundry, this project turns that theory into runnable, verifiable, deployable engineering practice:

| Anthropic Concept | MAF Implementation |
|-------------------|--------------------|
| Session (Durable Event Log) | `SessionLog` + AF `InMemoryHistoryProvider` + 11 `EventKind` types |
| Harness (Stateless Orchestrator) | `AgentHarness` + AF `Agent` + `FoundryChatClient` + `wake()` recovery |
| Sandbox (Disposable Execution) | `Sandbox` + `SandboxManager` + `VaultStore` + lazy creation |
| Many Brains (Parallel Orchestration) | `run_many_brains()` + AF `WorkflowBuilder` + specialist agent routing |
| Security Boundary | `VaultStore` isolation + `SecurityMiddleware` interception + tool permission allowlist |
| Context Engineering | `get_context_window()` + `get_session_context` tool + `COMPACTION` events |
| Serverless Hosting | Azure Functions + REST API + zero sticky sessions |

**Three core interfaces** form the skeleton of the entire system:

```python
session_id = await session_log.create_session(task)       # Create memory
result     = await sandbox.execute(name, input)            # Execute action
session, events = await session_log.wake(session_id)       # Restore state
```

Stable interfaces, swappable implementations. Stateless brains, disposable hands. Durable sessions, crash-recoverable.

---

*Project code: [maf_harness_managed_agent](https://github.com/maf_harness_managed_agent)*
*References: [Anthropic — Scaling Managed Agents](https://www.anthropic.com/engineering/managed-agents) · [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) · [Azure AI Foundry](https://ai.azure.com)*
