"""
maf_harness.middleware.middleware
==================================
AF @agent_middleware 中间件栈 — 拦截每次代理轮次的横切关注点，
不修改代理逻辑。

中间件栈（按顺序应用）：
    1. SessionLoggingMiddleware  — 将每次轮次写入持久化会话日志
    2. SecurityMiddleware        — 从上下文中清除凭据模式
    3. RateLimitMiddleware       — 令牌桶限流器（RPM）
    4. ObservabilityMiddleware   — TTFT 延迟 + 运行指标

AF API: @agent_middleware, AgentContext
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agent_framework import AgentContext, agent_middleware

if TYPE_CHECKING:
    from maf_harness.session.session_log import EventKind, SessionEvent, SessionLog


# ── 1. 会话日志记录 ────────────────────────────────────────────────────────

def make_session_logging_middleware(session_log, session_id: str):
    """将每次代理轮次作为结构化事件写入持久化会话日志。"""

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


# ── 2. 安全 ───────────────────────────────────────────────────────────────

def make_security_middleware(blocked_patterns: list[str] | None = None):
    """
    强制安全边界：在凭据模式到达模型上下文之前将其清除。
    凭据绝不能出现在沙箱中。
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
                print(f"[SECURITY] 已从消息上下文中清除凭据模式。")
                continue
            safe.append(msg)
        ctx.messages = safe
        return await next()

    return _mw


# ── 3. 限流 ──────────────────────────────────────────────────────────────

def make_rate_limit_middleware(max_rpm: int = 60):
    """简单的滑动窗口限流器。"""
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


# ── 4. 可观测性（TTFT 指标）──────────────────────────────────────────────

class Metrics:
    """进程内指标 — 生产环境替换为 OpenTelemetry。"""

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
    测量每次代理运行的首 token 延迟（TTFT）。
    Anthropic 通过将大脑与沙箱解耦，使 p50 TTFT 减少约 60%，p95 减少超过 90%。
    此中间件为您的部署跟踪该数值。
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
