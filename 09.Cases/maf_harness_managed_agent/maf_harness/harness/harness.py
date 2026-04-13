"""
maf_harness.harness.harness
============================
The harness is the "brain" — a stateless orchestration loop,
driven by Microsoft Foundry (FoundryChatClient). It has the following characteristics:

  1. Stateless   : All state is stored in the session log, not in the harness.
  2. Decoupled   : Calls sandboxes only via execute(name, input) → string.
  3. Recoverable : wake(session_id) rehydrates context from durable log.
  4. Foundry     : The only LLM backend is FoundryChatClient — zero OpenAI SDK.

Required environment variables:
    FOUNDRY_PROJECT_ENDPOINT   https://<hub>.services.ai.azure.com
    FOUNDRY_MODEL              gpt-5.4 (any Foundry deployed model)

Authentication (DefaultAzureCredential precedence):
    1. FOUNDRY_API_KEY env var  → AzureKeyCredential (dev / CI)
    2. az login                 → AzureCliCredential (local dev)
    3. Managed Identity         → automatic on Azure (production)
"""

from __future__ import annotations

import asyncio
import os
import warnings
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable

from pydantic import Field

from agent_framework import (
    Agent,
    InMemoryHistoryProvider,
    SkillsProvider,
)
from agent_framework.foundry import FoundryChatClient

from maf_harness.middleware.middleware import (
    GLOBAL_METRICS,
    make_observability_middleware,
    make_rate_limit_middleware,
    make_security_middleware,
    make_session_logging_middleware,
)
from maf_harness.sandbox.sandbox import SandboxManager, SandboxResources
from maf_harness.session.session_log import EventKind, SessionEvent, SessionLog
from maf_harness.skills.skills import build_skills_provider


# ── Foundry Client Factory ──────────────────────────────────────────────────

def make_foundry_client(model: str | None = None) -> FoundryChatClient:
    """
    Build FoundryChatClient.

    Authentication priority:
      1. FOUNDRY_API_KEY env var  → AzureKeyCredential (dev / CI)
      2. DefaultAzureCredential   → az login / Managed Identity (production)

    Configured via environment variables (if not explicitly passed):
      FOUNDRY_PROJECT_ENDPOINT
      FOUNDRY_MODEL
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


# ── Harness Configuration ───────────────────────────────────────────────────────

@dataclass
class HarnessConfig:
    """Configuration per harness."""
    agent_name:          str       = "ManagedAgent"
    model:               str       = ""          # Falls back to FOUNDRY_MODEL env var
    max_iterations:      int       = 20
    context_window_size: int       = 30          # Events reloaded on wake()
    skill_names:         list[str] = field(default_factory=lambda: [
                                        "research", "code_execution",
                                        "summarise", "orchestration",
                                    ])
    rate_limit_rpm:      int       = 60
    sandbox_timeout_sec: int       = 30


# ── Sandbox Tool Wrappers ───────────────────────────────────────────────────────

def build_sandbox_tools(
    sandbox_mgr: SandboxManager,
    session_log: SessionLog,
    session_id:  str,
) -> list[Callable]:
    """
    Wraps sandbox.execute(name, input) as typed AF tool functions.
    Sandboxes are lazily created on first tool call — reasoning-only sessions
    never incur creation overhead (TTFT optimization).
    """
    _state: dict[str, str | None] = {"sandbox_id": None}

    async def _ensure() -> str:
        if _state["sandbox_id"] is None:
            sid = await sandbox_mgr.provision(SandboxResources(
                allowed_tools=["run_python", "web_search", "read_file", "write_file"],
            ))
            _state["sandbox_id"] = sid
            await session_log.emit_event(
                session_id,
                SessionEvent(
                    kind=EventKind.SANDBOX_SPAWN,
                    session_id=session_id,
                    payload={"sandbox_id": sid},
                ),
            )
        return _state["sandbox_id"]

    async def _exec(name: str, data: str) -> str:
        for attempt in range(2):
            try:
                sid    = await _ensure()
                result = await sandbox_mgr.execute(sid, name, data)
                await session_log.emit_event(
                    session_id,
                    SessionEvent(
                        kind=EventKind.SANDBOX_EXEC,
                        session_id=session_id,
                        payload={"tool": name, "input": data[:200], "result": result[:500]},
                    ),
                )
                return result
            except RuntimeError as exc:
                if _state["sandbox_id"]:
                    sandbox_mgr.reclaim(_state["sandbox_id"])
                    _state["sandbox_id"] = None
                if attempt == 1:
                    return f"[SANDBOX FAILED after retry] {exc}"
        return "[SANDBOX FAILED]"

    # ── Typed AF Tool Functions ───────────────────────────────────────────────

    async def run_python(
        code: Annotated[str, Field(description="Python code to execute in isolated sandbox.")],
    ) -> str:
        """Execute Python code in sandbox and return stdout."""
        return await _exec("run_python", code)

    async def web_search(
        query: Annotated[str, Field(description="Query to search on the web.")],
    ) -> str:
        """Search the web and return summarized results."""
        return await _exec("web_search", query)

    async def write_file(
        path:    Annotated[str, Field(description="Target file path.")],
        content: Annotated[str, Field(description="Content to write.")],
    ) -> str:
        """Write content to a file in sandbox filesystem."""
        return await _exec("write_file", f"{path}::{content}")

    async def read_file(
        path: Annotated[str, Field(description="File path to read.")],
    ) -> str:
        """Read a file from sandbox filesystem."""
        return await _exec("read_file", path)

    async def get_session_context(
        last_n: Annotated[int, Field(description="Number of recent events to retrieve.")] = 10,
    ) -> str:
        """Retrieve recent events from session log for context."""
        events = await session_log.get_context_window(session_id, last_n=last_n)
        return "\n".join(f"[{e.kind.value}] {e.payload}" for e in events) or "(no events)"

    return [run_python, web_search, write_file, read_file, get_session_context]


# ── Agent Harness — Brain ─────────────────────────────────────────────────────

class AgentHarness:
    """
    Stateless harness driven by Microsoft Foundry.

    Lifecycle:
        harness = AgentHarness(...)
        await harness.start(session_id)        # wake() if session exists
        response = await harness.run(message)  # one agent loop turn
        await harness.shutdown()

    On crash: create a new harness, call start(same session_id).
    wake() rehydrates full event history — zero data loss.
    """

    def __init__(
        self,
        session_log: SessionLog,
        sandbox_mgr: SandboxManager,
        config:      HarnessConfig | None      = None,
        client:      FoundryChatClient | None  = None,
    ) -> None:
        self.session_log = session_log
        self.sandbox_mgr = sandbox_mgr
        self.config      = config or HarnessConfig()
        self._client     = client   # Inject pre-built client (testing / reuse)
        self._agent:           Agent | None                 = None
        self._session_id:      str | None                   = None
        self._history_provider: InMemoryHistoryProvider | None = None

    # ── Start / Wake ──────────────────────────────────────────────────────────────

    async def start(self, session_id: str) -> None:
        """
        Attach to a session. If session has existing events, perform
        wake() — harness rebuilds context from durable log without losing any history.
        """
        self._session_id = session_id

        _session, past_events = await self.session_log.wake(session_id)
        verb = "Resuming" if len(past_events) > 1 else "Starting"
        print(f"[HARNESS] {verb} session {session_id[:8]}… ({len(past_events)} past events)")

        self._history_provider = self.session_log.get_history_provider(session_id)

        tools           = build_sandbox_tools(self.sandbox_mgr, self.session_log, session_id)
        skills_provider = build_skills_provider(self.config.skill_names)

        middleware = [
            make_session_logging_middleware(self.session_log, session_id),
            make_security_middleware(),
            make_rate_limit_middleware(self.config.rate_limit_rpm),
            make_observability_middleware(),
        ]

        client = self._client or make_foundry_client(model=self.config.model or None)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._agent = Agent(
                client=client,
                name=self.config.agent_name,
                instructions=self._build_system_prompt(past_events),
                tools=tools,
                context_providers=[skills_provider, self._history_provider],
                middleware=middleware,
            )

    def _build_system_prompt(self, past_events: list) -> str:
        base = (
            "You are a Managed Agent running on Microsoft Foundry.\n\n"
            "Architecture:\n"
            "- Session state lives in a durable log OUTSIDE this context window.\n"
            "- Interact with the world via: run_python, web_search, write_file, read_file.\n"
            "- Call get_session_context() to inspect recent session events.\n"
            "- Credentials are NEVER available to you; the harness proxy handles auth.\n\n"
            "Guidelines:\n"
            "- Think step-by-step before using any tool.\n"
            "- Use web_search to ground factual claims.\n"
            "- Use run_python to verify numerical or data-processing results.\n"
            "- If approaching context limits, call get_session_context() and continue.\n"
            "- Be concise and actionable.\n"
        )
        if past_events:
            recent  = past_events[-5:]
            summary = "\n".join(
                f"  [{e.kind.value}] {str(e.payload)[:120]}" for e in recent
            )
            base += f"\n\nMost recent session events:\n{summary}\n"
        return base

    # ── Execution ───────────────────────────────────────────────────────────────

    async def run(self, user_input: str) -> str:
        """Execute one agent loop iteration via Foundry."""
        if self._agent is None or self._session_id is None:
            raise RuntimeError("Harness not started. Call start(session_id) first.")
        try:
            result = await self._agent.run(user_input)
            await self.session_log.emit_event(
                self._session_id,
                SessionEvent(
                    kind=EventKind.AGENT_RESPONSE,
                    session_id=self._session_id,
                    payload={"response": str(result)[:500]},
                ),
            )
            return str(result)
        except Exception as exc:
            await self.session_log.emit_event(
                self._session_id,
                SessionEvent(
                    kind=EventKind.HARNESS_CRASH,
                    session_id=self._session_id,
                    payload={"error": str(exc)},
                ),
            )
            raise

    async def run_streaming(self, user_input: str):
        """Yield response text chunks as they arrive from Foundry."""
        if self._agent is None:
            raise RuntimeError("Harness not started.")
        async for chunk in self._agent.run(user_input, stream=True):
            if chunk.text:
                yield chunk.text

    async def shutdown(self) -> None:
        """Emit SESSION_END event to durable log."""
        if self._session_id:
            await self.session_log.emit_event(
                self._session_id,
                SessionEvent(
                    kind=EventKind.SESSION_END,
                    session_id=self._session_id,
                    payload={"graceful": True},
                ),
            )
