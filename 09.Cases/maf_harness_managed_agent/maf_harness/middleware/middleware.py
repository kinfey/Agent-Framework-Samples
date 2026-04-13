"""
maf_harness.middleware.middleware
==================================
AF @agent_middleware middleware stack — intercepts cross-cutting concerns on every agent turn,
without modifying agent logic.

Middleware stack (applied in order):
    1. SessionLoggingMiddleware  — write every turn to durable session log
    2. SecurityMiddleware        — scrub credential patterns from context
    3. RateLimitMiddleware       — token bucket rate limiter (RPM)
    4. ObservabilityMiddleware   — TTFT latency + run metrics

AF API: @agent_middleware, AgentContext
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agent_framework import AgentContext, agent_middleware

if TYPE_CHECKING:
    from maf_harness.session.session_log import EventKind, SessionEvent, SessionLog


# ── 1. Session Logging ──────────────────────────────────────────────────────────

def make_session_logging_middleware(session_log, session_id: str):
    """Write every agent turn as structured events to durable session log."""

    @agent_middleware
    async def _mw(ctx: AgentContext, next):
        from maf_harness.session.session_log import EventKind, SessionEvent

        if ctx.messages:
            last = ctx.messages[-1]
            await session_log.emit_event(
                session_id,
                SessionEvent(
                    kind=EventKind.USER_INPUT,
                    session_id=session_id,
                    payload={"role": getattr(last, "role", "user"), "content": str(last)[:400]},
                ),
            )

        result = await next()

        response_text = ""
        if hasattr(result, "message") and result.message:
            response_text = str(result.message)[:400]

        await session_log.emit_event(
            session_id,
            SessionEvent(
                kind=EventKind.AGENT_RESPONSE,
                session_id=session_id,
                payload={"content": response_text},
            ),
        )
        return result

    return _mw


# ── 2. Security ─────────────────────────────────────────────────────────────────

def make_security_middleware(blocked_patterns: list[str] | None = None):
    """
    Enforce security boundary: scrub credential patterns before they reach model context.
    Credentials never appear in sandbox.
    """
    _blocked = blocked_patterns or [
        "sk-", "Bearer ", "AZURE_", "password=", "secret=",
        "token=", "api_key=", "FoundryKey",
    ]

    @agent_middleware
    async def _mw(ctx: AgentContext, next):
        safe = []
        for msg in ctx.messages:
            if any(p in str(msg) for p in _blocked):
                print(f"[SECURITY] Scrubbed credential patterns from message context.")
                continue
            safe.append(msg)
        ctx.messages = safe
        return await next()

    return _mw


# ── 3. Rate Limiting ────────────────────────────────────────────────────────────

def make_rate_limit_middleware(max_rpm: int = 60):
    """Simple sliding window rate limiter."""
    from collections import deque

    window: deque[float] = deque()

    @agent_middleware
    async def _mw(ctx: AgentContext, next):
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= max_rpm:
            raise RuntimeError(
                f"Rate limit: {max_rpm} requests/min exceeded. Please wait."
            )
        window.append(now)
        return await next()

    return _mw


# ── 4. Observability (TTFT Metrics) ───────────────────────────────────────────

class Metrics:
    """In-process metrics — replace with OpenTelemetry in production."""

    def __init__(self) -> None:
        self.samples:      list[float] = []
        self.total_runs:   int         = 0
        self.total_errors: int         = 0
        self.total_tokens: int         = 0

    def record_ttft(self, ms: float) -> None:
        self.samples.append(ms)

    @property
    def p50(self) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        return s[len(s) // 2]

    @property
    def p95(self) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        return s[int(len(s) * 0.95)]

    def summary(self) -> dict:
        return {
            "total_runs":   self.total_runs,
            "total_errors": self.total_errors,
            "p50_ttft_ms":  round(self.p50, 2),
            "p95_ttft_ms":  round(self.p95, 2),
            "total_tokens": self.total_tokens,
        }


GLOBAL_METRICS = Metrics()


def make_observability_middleware(metrics: Metrics | None = None):
    """
    Measure time-to-first-token (TTFT) latency on every agent run.
    Anthropic reduced p50 TTFT by ~60% and p95 by >90% by decoupling brain from sandbox.
    This middleware tracks that metric for your deployment.
    """
    m = metrics or GLOBAL_METRICS

    @agent_middleware
    async def _mw(ctx: AgentContext, next):
        start = time.perf_counter()
        m.total_runs += 1
        try:
            result = await next()
            elapsed_ms = (time.perf_counter() - start) * 1000
            m.record_ttft(elapsed_ms)
            if hasattr(result, "usage") and result.usage:
                m.total_tokens += getattr(result.usage, "total_tokens", 0)
            print(
                f"[METRICS] run={m.total_runs} "
                f"latency={elapsed_ms:.0f}ms "
                f"p50={m.p50:.0f}ms p95={m.p95:.0f}ms"
            )
            return result
        except Exception:
            m.total_errors += 1
            raise

    return _mw
