"""
maf_harness.harness.harness
============================
编排器是"大脑" — 一个无状态的编排循环，由
Microsoft Foundry（FoundryChatClient）驱动。它具有以下特性：

  1. 无状态    ：所有状态都保存在会话日志中，而非编排器内。
  2. 解耦      ：仅通过 execute(name, input) → string 调用沙箱。
  3. 可恢复    ：wake(session_id) 从持久化日志中重新注入上下文。
  4. Foundry   ：唯一的 LLM 后端是 FoundryChatClient — 无 OpenAI SDK。

必需的环境变量：
    FOUNDRY_PROJECT_ENDPOINT   https://<hub>.services.ai.azure.com
    FOUNDRY_MODEL              gpt-5.4（任何 Foundry 已部署的模型）

认证（DefaultAzureCredential 优先级顺序）：
    1. FOUNDRY_API_KEY 环境变量  → AzureKeyCredential（开发 / CI）
    2. az login                  → AzureCliCredential（本地开发）
    3. Managed Identity          → 在 Azure 上自动生效（生产环境）
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


# ── Foundry 客户端工厂 ────────────────────────────────────────────────────────

def make_foundry_client(model: str | None = None) -> FoundryChatClient:
    """
    构建 FoundryChatClient。

    认证优先级：
      1. FOUNDRY_API_KEY 环境变量  → AzureKeyCredential（开发 / CI）
      2. DefaultAzureCredential    → az login / Managed Identity（生产环境）

    通过环境变量配置（如未显式传递）：
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


# ── 编排器配置 ─────────────────────────────────────────────────────────────

@dataclass
class HarnessConfig:
    """每个编排器的配置。"""
    agent_name:          str       = "ManagedAgent"
    model:               str       = ""          # 回退到 FOUNDRY_MODEL 环境变量
    max_iterations:      int       = 20
    context_window_size: int       = 30          # wake() 时重新加载的事件数
    skill_names:         list[str] = field(default_factory=lambda: [
                                        "research", "code_execution",
                                        "summarise", "orchestration",
                                    ])
    rate_limit_rpm:      int       = 60
    sandbox_timeout_sec: int       = 30


# ── 沙箱工具包装器 ─────────────────────────────────────────────────────────

def build_sandbox_tools(
    sandbox_mgr: SandboxManager,
    session_log: SessionLog,
    session_id:  str,
) -> list[Callable]:
    """
    将 sandbox.execute(name, input) 包装为带类型的 AF 工具函数。
    沙箱在首次工具调用时延迟创建 — 仅需推理的会话
    永远不会承担创建开销（TTFT 优化）。
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

    # ── 带类型的 AF 工具函数 ───────────────────────────────────────────────

    async def run_python(
        code: Annotated[str, Field(description="在隔离沙箱中执行的 Python 代码。")],
    ) -> str:
        """在沙箱中执行 Python 代码并返回标准输出。"""
        return await _exec("run_python", code)

    async def web_search(
        query: Annotated[str, Field(description="要在网上搜索的查询内容。")],
    ) -> str:
        """搜索网络并返回摘要结果。"""
        return await _exec("web_search", query)

    async def write_file(
        path:    Annotated[str, Field(description="目标文件路径。")],
        content: Annotated[str, Field(description="要写入的内容。")],
    ) -> str:
        """将内容写入沙箱文件系统中的文件。"""
        return await _exec("write_file", f"{path}::{content}")

    async def read_file(
        path: Annotated[str, Field(description="要读取的文件路径。")],
    ) -> str:
        """从沙箱文件系统中读取文件。"""
        return await _exec("read_file", path)

    async def get_session_context(
        last_n: Annotated[int, Field(description="要检索的最近事件数量。")] = 10,
    ) -> str:
        """从会话日志中检索最近事件作为上下文。"""
        events = await session_log.get_context_window(session_id, last_n=last_n)
        return "\n".join(f"[{e.kind.value}] {e.payload}" for e in events) or "(no events)"

    return [run_python, web_search, write_file, read_file, get_session_context]


# ── 代理编排器 — 大脑 ─────────────────────────────────────────────────────

class AgentHarness:
    """
    由 Microsoft Foundry 驱动的无状态编排器。

    生命周期：
        harness = AgentHarness(session_log, sandbox_mgr, config)
        await harness.start(session_id)        # 若会话已存在则执行 wake()
        response = await harness.run(message)  # 一次代理循环轮次
        await harness.shutdown()

    崩溃时：创建新的编排器，调用 start(同一个 session_id)。
    wake() 会重新注入完整的事件历史 — 无数据丢失。
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
        self._client     = client   # 注入预构建的客户端（测试 / 复用）
        self._agent:           Agent | None                 = None
        self._session_id:      str | None                   = None
        self._history_provider: InMemoryHistoryProvider | None = None

    # ── 启动 / 唤醒 ──────────────────────────────────────────────────────────

    async def start(self, session_id: str) -> None:
        """
        附加到一个会话。如果会话已有事件，则执行
        wake() — 编排器从持久化日志中重建上下文，不丢失任何历史记录。
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

    # ── 运行 ───────────────────────────────────────────────────────────────

    async def run(self, user_input: str) -> str:
        """通过 Foundry 执行一次代理循环轮次。"""
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
        """逐步返回从 Foundry 到达的响应文本块。"""
        if self._agent is None:
            raise RuntimeError("Harness not started.")
        async for chunk in self._agent.run(user_input, stream=True):
            if chunk.text:
                yield chunk.text

    async def shutdown(self) -> None:
        """向持久化日志发出 SESSION_END 事件。"""
        if self._session_id:
            await self.session_log.emit_event(
                self._session_id,
                SessionEvent(
                    kind=EventKind.SESSION_END,
                    session_id=self._session_id,
                    payload={"graceful": True},
                ),
            )
