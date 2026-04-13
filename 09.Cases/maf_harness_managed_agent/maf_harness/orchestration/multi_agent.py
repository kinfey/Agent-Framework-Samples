"""
maf_harness.orchestration.multi_agent
======================================
实现 Anthropic 的"多大脑、多双手"模式。

  - 专家代理：ResearchAgent、CodeAgent、SummariseAgent
  - OrchestratorAgent 将任务委派给专家并聚合结果
  - 通过 AF WorkflowBuilder 实现基于图的路由
  - run_many_brains()：N 个无状态 Foundry 编排器并行运行

所有 LLM 调用使用 FoundryChatClient — 无 OpenAI 依赖。
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Annotated

from pydantic import Field

from agent_framework import (
    Agent,
    FunctionExecutor,
    InMemoryCheckpointStorage,
    WorkflowBuilder,
)
from agent_framework.foundry import FoundryChatClient

from maf_harness.harness.harness import make_foundry_client
from maf_harness.sandbox.sandbox import SandboxManager, SandboxResources
from maf_harness.session.session_log import EventKind, SessionEvent, SessionLog


# ── 共享客户端工厂 ─────────────────────────────────────────────────────────

def _client() -> FoundryChatClient:
    return make_foundry_client()


def _agent(name: str, instructions: str, tools: list | None = None) -> Agent:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Agent(
            client=_client(),
            name=name,
            instructions=instructions,
            tools=tools or [],
        )


# ── 专家代理 ─────────────────────────────────────────────────────────────

def make_research_agent(sandbox_mgr: SandboxManager) -> Agent:
    """研究大脑 — 仅使用 web_search 沙箱工具。"""

    async def web_search(
        query: Annotated[str, Field(description="搜索查询")],
    ) -> str:
        """在网上搜索信息。"""
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["web_search"]))
        result = await sandbox_mgr.execute(sid, "web_search", query)
        sandbox_mgr.reclaim(sid)
        return result

    return _agent(
        name="ResearchAgent",
        instructions=(
            "You are a research specialist on Microsoft Foundry.\n"
            "Use web_search to gather factual information. Always cite sources.\n"
            "Return structured, concise research summaries."
        ),
        tools=[web_search],
    )


def make_code_agent(sandbox_mgr: SandboxManager) -> Agent:
    """代码大脑 — 仅使用 run_python 沙箱工具。"""

    async def run_python(
        code: Annotated[str, Field(description="要执行的 Python 代码")],
    ) -> str:
        """执行 Python 代码并返回输出。"""
        sid = await sandbox_mgr.provision(SandboxResources(allowed_tools=["run_python"]))
        result = await sandbox_mgr.execute(sid, "run_python", code)
        sandbox_mgr.reclaim(sid)
        return result

    return _agent(
        name="CodeAgent",
        instructions=(
            "You are a code specialist on Microsoft Foundry.\n"
            "Write clean, tested Python. Always run code before returning.\n"
            "Return the final code + verified output."
        ),
        tools=[run_python],
    )


def make_summarise_agent() -> Agent:
    """总结大脑 — 纯推理，不需要沙箱。"""
    return _agent(
        name="SummariseAgent",
        instructions=(
            "You are a summarisation specialist on Microsoft Foundry.\n"
            "Condense information into clear, structured summaries.\n"
            "Use bullet points and headers. Preserve key facts; discard filler."
        ),
    )


def make_orchestrator_agent(
    research_agent:  Agent,
    code_agent:      Agent,
    summarise_agent: Agent,
) -> Agent:
    """
    编排器大脑。
    实现"大脑可以将双手传递给彼此"。
    """

    async def delegate_research(
        task: Annotated[str, Field(description="要委派的研究任务")],
    ) -> str:
        """将研究任务委派给 ResearchAgent。"""
        return await research_agent.run(task)

    async def delegate_code(
        task: Annotated[str, Field(description="要委派的编码任务")],
    ) -> str:
        """将编码任务委派给 CodeAgent。"""
        return await code_agent.run(task)

    async def delegate_summarise(
        text: Annotated[str, Field(description="要总结的文本")],
    ) -> str:
        """将总结任务委派给 SummariseAgent。"""
        return await summarise_agent.run(f"Summarise:\n\n{text}")

    return _agent(
        name="OrchestratorAgent",
        instructions=(
            "You are the orchestrator on Microsoft Foundry.\n"
            "Decompose tasks and delegate to specialists:\n"
            "  - delegate_research() for factual lookups\n"
            "  - delegate_code()     for computation / data processing\n"
            "  - delegate_summarise() to condense long results\n"
            "Aggregate outputs into a coherent final answer."
        ),
        tools=[delegate_research, delegate_code, delegate_summarise],
    )


# ── 基于图的工作流（AF WorkflowBuilder）─────────────────────────────────────

def build_multi_agent_workflow(
    session_log: SessionLog,
    session_id:  str,
    sandbox_mgr: SandboxManager,
):
    """
    构建 AF 工作流图：

        [classify] → research   ─┐
                   → code        ├→ summarise → END
                   → orchestrate ─┘

    InMemoryCheckpointStorage 在编排器重启后保留工作流状态。
    """
    research_agent  = make_research_agent(sandbox_mgr)
    code_agent      = make_code_agent(sandbox_mgr)
    summarise_agent = make_summarise_agent()
    orchestrator    = make_orchestrator_agent(research_agent, code_agent, summarise_agent)

    async def classify_task(task: str) -> dict:
        """将任务路由到适当的专家代理。"""
        t = task.lower()
        if any(k in t for k in ["code", "calculate", "compute", "script", "python"]):
            agent_type = "code"
        elif any(k in t for k in ["research", "find", "search", "what is", "who is"]):
            agent_type = "research"
        else:
            agent_type = "orchestrate"

        await session_log.emit_event(
            session_id,
            SessionEvent(
                kind=EventKind.TOOL_CALL,
                session_id=session_id,
                payload={"router": "classify", "routed_to": agent_type, "task": task[:200]},
            ),
        )
        return {"task": task, "agent_type": agent_type}

    builder = WorkflowBuilder(
        start_executor=FunctionExecutor(classify_task),
        checkpoint_storage=InMemoryCheckpointStorage(),
        name="ManagedAgentsWorkflow",
    )
    builder.add_executor("research",    research_agent)
    builder.add_executor("code",        code_agent)
    builder.add_executor("orchestrate", orchestrator)
    builder.add_executor("summarise",   summarise_agent)

    builder.add_switch_edge(
        source="classify_task",
        key="agent_type",
        cases={
            "research":    "research",
            "code":        "code",
            "orchestrate": "orchestrate",
        },
    )
    builder.add_edge("research",    "summarise")
    builder.add_edge("code",        "summarise")
    builder.add_edge("orchestrate", "summarise")

    return builder.build()


# ── 多大脑并行启动器 ─────────────────────────────────────────────────────────

async def run_many_brains(
    tasks:       list[str],
    session_log: SessionLog,
    sandbox_mgr: SandboxManager,
) -> list[dict]:
    """
    为每个任务生成一个无状态的 Foundry 编排器，并发运行。

    每个编排器：
      - 拥有自己的 session_id 和事件日志条目
      - 仅在实际执行工具时才创建沙箱
      - 任务完成后丢弃（无状态 / 可替换）

    这重现了 Anthropic 在并行工作负载中改善 p50 TTFT 的效果。
    """
    from maf_harness.harness.harness import AgentHarness, HarnessConfig

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
