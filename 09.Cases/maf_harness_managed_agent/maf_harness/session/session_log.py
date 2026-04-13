"""
maf_harness.session.session_log
================================
Durable, append-only event log — the "Session" layer in Anthropic's Managed Agent architecture.

Key interfaces (mapping to Anthropic article):
    create_session(task)            → session_id
    emit_event(session_id, event)   → append to log
    get_events(session_id, ...)     → positional/filtered slicing
    get_session(session_id)         → AF AgentSession metadata
    wake(session_id)                → (session, all_events) for harness recovery
    get_context_window(session_id)  → recent N events for context engineering

Underlying uses AF InMemoryHistoryProvider (replace with CosmosDB / Redis in production).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_framework import AgentSession, InMemoryHistoryProvider, Message, Role


# ── Event Model ───────────────────────────────────────────────────────────────

class EventKind(str, Enum):
    SESSION_START   = "session_start"
    USER_INPUT      = "user_input"
    AGENT_RESPONSE  = "agent_response"
    TOOL_CALL       = "tool_call"
    TOOL_RESULT     = "tool_result"
    HARNESS_WAKE    = "harness_wake"
    HARNESS_CRASH   = "harness_crash"
    SANDBOX_SPAWN   = "sandbox_spawn"
    SANDBOX_EXEC    = "sandbox_exec"
    SESSION_END     = "session_end"
    COMPACTION      = "compaction"


@dataclass
class SessionEvent:
    kind:       EventKind
    payload:    dict[str, Any]
    timestamp:  float = field(default_factory=time.time)
    event_id:   str   = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str   = ""

    def to_dict(self) -> dict:
        return {
            "event_id":   self.event_id,
            "session_id": self.session_id,
            "kind":       self.kind.value,
            "payload":    self.payload,
            "timestamp":  self.timestamp,
        }


# ── Session Log ───────────────────────────────────────────────────────────────

class SessionLog:
    """
    Append-only, durable event store.

    Sessions exist outside the harness and sandbox.
    If the harness crashes, the session is unaffected; a new harness
    calls wake(session_id) to recover from the last event.
    """

    def __init__(self) -> None:
        self._store:    dict[str, list[SessionEvent]]         = {}
        self._sessions: dict[str, AgentSession]               = {}
        self._history:  dict[str, InMemoryHistoryProvider]    = {}
        self._lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def create_session(
        self,
        task:     str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new session and emit SESSION_START event."""
        session_id = str(uuid.uuid4())
        af_session = AgentSession(session_id=session_id)

        async with self._lock:
            self._store[session_id]    = []
            self._sessions[session_id] = af_session
            self._history[session_id]  = InMemoryHistoryProvider()

        await self.emit_event(
            session_id,
            SessionEvent(
                kind=EventKind.SESSION_START,
                session_id=session_id,
                payload={"task": task, "metadata": metadata or {}},
            ),
        )
        return session_id

    async def emit_event(self, session_id: str, event: SessionEvent) -> None:
        """Append an event to the log. Equivalent to emitEvent(id, event)."""
        event.session_id = session_id
        async with self._lock:
            self._store.setdefault(session_id, []).append(event)

    async def get_events(
        self,
        session_id:  str,
        start:       int                  = 0,
        end:         int | None           = None,
        kind_filter: list[EventKind] | None = None,
    ) -> list[SessionEvent]:
        """
        Return positional slice of event stream.
        Equivalent to getEvents() — "resume from where you left off".
        """
        async with self._lock:
            events = self._store.get(session_id, [])
            sliced = events[start:end]
            if kind_filter:
                sliced = [e for e in sliced if e.kind in kind_filter]
            return list(sliced)

    async def get_session(self, session_id: str) -> AgentSession | None:
        """Retrieve session metadata. Equivalent to getSession(id)."""
        return self._sessions.get(session_id)

    async def wake(
        self,
        session_id: str,
    ) -> tuple[AgentSession | None, list[SessionEvent]]:
        """
        Rehydrate harness from durable session.
        Equivalent to wake(sessionId) → (session, event_log).
        """
        session = await self.get_session(session_id)
        events  = await self.get_events(session_id)
        await self.emit_event(
            session_id,
            SessionEvent(
                kind=EventKind.HARNESS_WAKE,
                session_id=session_id,
                payload={"resumed_at_event": len(events)},
            ),
        )
        return session, events

    # ── AF History Integration ────────────────────────────────────────────────────

    def get_history_provider(self, session_id: str) -> InMemoryHistoryProvider | None:
        return self._history.get(session_id)

    # ── Context Engineering ──────────────────────────────────────────────────────

    async def get_context_window(
        self,
        session_id: str,
        last_n:     int = 20,
    ) -> list[SessionEvent]:
        """Recent N events — lightweight context window based on session log."""
        events = await self.get_events(session_id)
        return events[-last_n:]

    async def event_count(self, session_id: str) -> int:
        async with self._lock:
            return len(self._store.get(session_id, []))

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())
