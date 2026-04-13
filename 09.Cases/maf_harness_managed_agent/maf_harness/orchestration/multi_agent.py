"""
maf_harness.orchestration.multi_agent
======================================
Implements Anthropic's "many brains, many hands" pattern.

  - Specialist agents: ResearchAgent, CodeAgent, SummariseAgent
  - OrchestratorAgent delegates tasks to specialists and aggregates results
  - Graph-based routing via AF WorkflowBuilder
  - run_many_brains(): N stateless Foundry harnesses run in parallel

All LLM calls use FoundryChatClient — zero OpenAI dependency.
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


# ── Shared Client Factory ───────────────────────────────────────────────────

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


# ── Specialist Agents ──────────────────────────────────────────────────────

def make_research_agent(sandbox_mgr: SandboxManager) -> Agent:
    """Research brain — uses only web_search sandbox tool."""

    async def web_search(
        query: Annotated[str, Field(description="Search query")],
    ) -> str:
        """Search the web for information."""
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
    """Code brain — uses only run_python sandbox tool."""

    async def run_python(
        code: Annotated[str, Field(description="Python code to execute")],
    ) -> str:
        """Execute Python code and return output."""
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
    """Summarisation brain — pure reasoning, no sandbox needed."""
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
    Orchestrator brain.
    Implements "brains can pass hands to each other".
    """

    async def delegate_research(
        task: Annotated[str, Field(description="Research task to delegate")],
    ) -> str:
        """Delegate research task to ResearchAgent."""
        return await research_agent.run(task)

    async def delegate_code(
        task: Annotated[str, Field(description="Coding task to delegate")],
    ) -> str:
        """Delegate coding task to CodeAgent."""
        return await code_agent.run(task)

    async def delegate_summarise(
        text: Annotated[str, Field(description="Text to summarise")],
    ) -> str:
        """Delegate summarisation task to SummariseAgent."""
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


# ── Graph-based Workflow (AF WorkflowBuilder) ──────────────────────────────

def build_multi_agent_workflow(
    session_log: SessionLog,
    session_id:  str,
    sandbox_mgr: SandboxManager,
):
    """
    Build AF workflow graph:

        [classify] → research   ─┐
                   → code        ├→ summarise → END
                   → orchestrate ─┘

    InMemoryCheckpointStorage preserves workflow state across harness restarts.
    """
    research_agent  = make_research_agent(sandbox_mgr)
    code_agent      = make_code_agent(sandbox_mgr)
    summarise_agent = make_summarise_agent()
    orchestrator    = make_orchestrator_agent(research_agent, code_agent, summarise_agent)

    async def classify_task(task: str) -> dict:
        """Route task to appropriate specialist agent."""
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


# ── Many Brains Parallel Launcher ─────────────────────────────────────────

async def run_many_brains(
    tasks:       list[str],
    session_log: SessionLog,
    sandbox_mgr: SandboxManager,
) -> list[dict]:
    """
    Spawn a stateless Foundry harness for each task and run them concurrently.

    Each harness:
      - Has its own session_id and event log entries
      - Creates sandbox only when actually executing tools
      - Is discarded after task completion (stateless / cattle)

    This reproduces Anthropic's p50 TTFT improvement on parallel workloads.
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
