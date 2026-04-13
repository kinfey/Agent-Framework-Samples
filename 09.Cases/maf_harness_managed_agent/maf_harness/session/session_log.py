"""
maf_harness.session.session_log
================================
持久化、仅追加事件日志 — Anthropic 托管代理架构中的"会话"层。

关键接口（对应 Anthropic 文章）：
    create_session(task)            → session_id
    emit_event(session_id, event)   → 追加到日志
    get_events(session_id, ...)     → 位置/过滤切片
    get_session(session_id)         → AF AgentSession 元数据
    wake(session_id)                → (session, all_events) 用于编排器恢复
    get_context_window(session_id)  → 最近 N 条事件，用于上下文工程

底层使用 AF InMemoryHistoryProvider（生产环境替换为 CosmosDB / Redis）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_framework import AgentSession, InMemoryHistoryProvider, Message, Role


# ── 事件模型 ───────────────────────────────────────────────────────────────────

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


# ── 会话日志 ───────────────────────────────────────────────────────────────────

class SessionLog:
    """
    仅追加、持久化事件存储。

    会话存在于编排器和沙箱之外。
    如果编排器崩溃，会话不受影响；新编排器
    调用 wake(session_id) 从最后一个事件处恢复。
    """

    def __init__(self) -> None:
        self._store:    dict[str, list[SessionEvent]]         = {}
        self._sessions: dict[str, AgentSession]               = {}
        self._history:  dict[str, InMemoryHistoryProvider]    = {}
        self._lock = asyncio.Lock()

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    async def create_session(
        self,
        task:     str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """创建新会话并发出 SESSION_START 事件。"""
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
        """向日志追加一个事件。等价于 emitEvent(id, event)。"""
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
        返回事件流的位置切片。
        等价于 getEvents() — "从上次停止的位置继续"。
        """
        async with self._lock:
            events = self._store.get(session_id, [])
            sliced = events[start:end]
            if kind_filter:
                sliced = [e for e in sliced if e.kind in kind_filter]
            return list(sliced)

    async def get_session(self, session_id: str) -> AgentSession | None:
        """检索会话元数据。等价于 getSession(id)。"""
        return self._sessions.get(session_id)

    async def wake(
        self,
        session_id: str,
    ) -> tuple[AgentSession | None, list[SessionEvent]]:
        """
        从持久化会话中重新注入编排器。
        等价于 wake(sessionId) → (session, event_log)。
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

    # ── AF 历史记录集成 ────────────────────────────────────────────────────────

    def get_history_provider(self, session_id: str) -> InMemoryHistoryProvider | None:
        return self._history.get(session_id)

    # ── 上下文工程 ───────────────────────────────────────────────────────────

    async def get_context_window(
        self,
        session_id: str,
        last_n:     int = 20,
    ) -> list[SessionEvent]:
        """最近 N 条事件 — 基于会话日志的轻量级上下文窗口。"""
        events = await self.get_events(session_id)
        return events[-last_n:]

    async def event_count(self, session_id: str) -> int:
        async with self._lock:
            return len(self._store.get(session_id, []))

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())
