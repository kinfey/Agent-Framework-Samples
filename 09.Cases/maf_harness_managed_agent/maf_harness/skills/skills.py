"""
maf_harness.skills.skills
==========================
Reusable domain capabilities encapsulated as Microsoft Agent Framework Skills.

Skills define what the brain knows how to do (instructions + structure).
Actual execution is done via sandbox.execute() — skills and sandbox
are orthogonal concerns.

AF API: Skill, SkillScript, SkillsProvider
"""

from __future__ import annotations

import textwrap
import warnings

from agent_framework import Skill, SkillScript, SkillsProvider


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


# ── Skill Definitions ─────────────────────────────────────────────────────────────

def make_research_skill() -> Skill:
    return Skill(
        name="research",
        description=(
            "Systematic research: decompose a question into sub-queries, "
            "retrieve evidence via web_search, cross-validate sources, and "
            "synthesise a structured answer with citations."
        ),
        content=_dedent("""
            # Research Skill

            ## Workflow
            1. Decompose the question into 3–5 focused sub-queries.
            2. Search each sub-query using the web_search tool.
            3. Evaluate source quality; prefer primary sources.
            4. Cross-validate claims across at least 2 independent results.
            5. Synthesise findings:
               - Executive summary (2–3 sentences)
               - Key findings (bulleted)
               - Confidence level: High / Medium / Low
               - Sources consulted

            ## Constraints
            - Never fabricate citations.
            - Flag conflicting evidence explicitly.
        """),
    )


def make_code_skill() -> Skill:
    return Skill(
        name="code_execution",
        description=(
            "Write, test, and iterate on Python code. "
            "Always test in the sandbox before presenting results."
        ),
        content=_dedent("""
            # Code Execution Skill

            ## Workflow
            1. Plan the solution in pseudocode first.
            2. Write clean, well-commented Python.
            3. Execute via the run_python sandbox tool.
            4. Inspect stdout / stderr; fix errors iteratively.
            5. Present the final verified code and its output.

            ## Best Practices
            - Use type hints and docstrings.
            - Handle exceptions gracefully.
            - Never hard-code credentials.
        """),
        scripts=[
            SkillScript(
                name="run_snippet",
                description="Execute a Python snippet in the sandbox.",
                function=lambda code: f"sandbox.execute('run_python', {code!r})",
            )
        ],
    )


def make_summarise_skill() -> Skill:
    return Skill(
        name="summarise",
        description=(
            "Condense long event logs or documents into structured summaries "
            "for context compaction. Preserves key decisions and outcomes."
        ),
        content=_dedent("""
            # Summarisation Skill

            ## Compaction protocol
            When the context window approaches its limit:
            1. Preserve the LAST 5 events verbatim.
            2. For earlier events write a structured summary:
               - Goal: what was the agent trying to do
               - Steps taken: tool calls and outcomes (1 line each)
               - Current state: files written, decisions made, open questions
            3. Emit a COMPACTION event to the session log.
            4. Replace the compacted segment in the context window.

            ## Quality bar
            A reader of the summary alone must be able to continue the task.
        """),
    )


def make_orchestration_skill() -> Skill:
    return Skill(
        name="orchestration",
        description=(
            "Orchestrate multiple sub-agents, delegate sub-tasks, and "
            "aggregate results.  Implements the 'many brains, many hands' pattern."
        ),
        content=_dedent("""
            # Orchestration Skill

            ## Delegation protocol
            1. Decompose the task into independent sub-tasks.
            2. Select the appropriate specialist agent for each sub-task.
            3. Spawn sub-agents via delegate_research / delegate_code / delegate_summarise.
            4. Collect results; check for contradictions.
            5. Aggregate into a coherent final answer.

            ## Routing rules
            - Research tasks     → ResearchAgent
            - Code / compute     → CodeAgent
            - Summarisation      → SummariseAgent
            - Complex / unknown  → OrchestratorAgent (fallback)

            ## Failure handling
            - Retry once with a rephrased task on failure.
            - Surface partial results and note gaps if retry also fails.
        """),
    )


# ── Skills Provider Factory ───────────────────────────────────────────────────────

_ALL: dict[str, callable] = {
    "research":       make_research_skill,
    "code_execution": make_code_skill,
    "summarise":      make_summarise_skill,
    "orchestration":  make_orchestration_skill,
}


def build_skills_provider(skill_names: list[str] | None = None) -> SkillsProvider:
    """
    Build AF SkillsProvider containing requested skills.
    Pass None to include all skills.
    """
    names    = skill_names or list(_ALL.keys())
    selected = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for name in names:
            if name in _ALL:
                selected.append(_ALL[name]())
    return SkillsProvider(selected)
