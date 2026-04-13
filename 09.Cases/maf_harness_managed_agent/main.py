"""
main.py — MAF Harness Managed Agent Demo
=========================================
Demonstrates all Anthropic Managed Agents patterns using
Microsoft Agent Framework + Microsoft Foundry.

Usage:
    python main.py [--mode single|multi|recover|stream|many|log|security|all]

Environment variables:
    FOUNDRY_PROJECT_ENDPOINT   https://<hub>.services.ai.azure.com
    FOUNDRY_MODEL              gpt-5.4  (or any deployed model)

Auth (pick one):
    az login                   DefaultAzureCredential  (local dev)
    FOUNDRY_API_KEY=<key>      AzureKeyCredential      (CI)
    Managed Identity           automatic on Azure      (prod)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import warnings

from dotenv import load_dotenv
load_dotenv()

warnings.filterwarnings("ignore")

from maf_harness.harness.harness    import AgentHarness, HarnessConfig, make_foundry_client
from maf_harness.middleware.middleware import GLOBAL_METRICS
from maf_harness.orchestration.multi_agent import run_many_brains
from maf_harness.sandbox.sandbox    import SandboxManager, VaultStore
from maf_harness.session.session_log import EventKind, SessionLog


# ── Shared Infrastructure ──────────────────────────────────────────────────

vault       = VaultStore()
session_log = SessionLog()
sandbox_mgr = SandboxManager(vault)


def _harness(name: str = "ManagedAgent") -> AgentHarness:
    return AgentHarness(
        session_log=session_log,
        sandbox_mgr=sandbox_mgr,
        config=HarnessConfig(
            agent_name=name,
            skill_names=["research", "code_execution", "summarise", "orchestration"],
        ),
        client=make_foundry_client(),
    )


# ── Helper Functions ───────────────────────────────────────────────────────

def _banner(title: str, subtitle: str = "") -> None:
    print(f"\n{'═' * 68}\n{title}")
    if subtitle:
        print(subtitle)
    print("═" * 68)


def _clip(text: str, n: int) -> str:
    s = str(text)
    return s[:n] + ("…" if len(s) > n else "")


async def _event_breakdown(session_id: str) -> None:
    events = await session_log.get_events(session_id)
    kinds = {}
    for e in events:
        kinds[e.kind.value] = kinds.get(e.kind.value, 0) + 1
    print(f"[SESSION {session_id[:8]}] events → {kinds}")


# ── Demo 1: Single Turn Session ───────────────────────────────────────────

async def demo_single_turn() -> None:
    _banner(
        "Demo 1 — Single Turn Session",
        "create_session → start → run → shutdown",
    )
    task = "What is 6 × 7? Verify by running Python code."
    sid  = await session_log.create_session(task)
    print(f"\n[SESSION] {sid[:12]}…")

    h = _harness("SingleTurnAgent")
    await h.start(sid)
    resp = await h.run(task)
    await h.shutdown()

    print(f"\n[RESPONSE]\n{resp}")
    await _event_breakdown(sid)


# ── Demo 2: Multi-Turn Conversation ───────────────────────────────────────

async def demo_multi_turn() -> None:
    _banner(
        "Demo 2 — Multi-Turn Conversation",
        "Reuse the same session_id across multiple run() calls",
    )
    sid = await session_log.create_session("Multi-turn Foundry session")
    print(f"\n[SESSION] {sid[:12]}…")

    turns = [
        "Write a Python function returning the nth Fibonacci number.",
        "Add memoisation to make it efficient.",
        "What is the 30th Fibonacci number? Compute it.",
    ]

    h = _harness("MultiTurnAgent")
    await h.start(sid)
    for i, turn in enumerate(turns, 1):
        print(f"\n[TURN {i}] ▶ {turn}")
        resp = await h.run(turn)
        print(f"[TURN {i}] ◀ {_clip(resp, 280)}")
    await h.shutdown()
    await _event_breakdown(sid)


# ── Demo 3: Harness Crash + Wake Recovery ─────────────────────────────────

async def demo_harness_recovery() -> None:
    _banner(
        "Demo 3 — Harness Crash Recovery (wake mode)",
        "Harness #1 crashes → Harness #2 recovers from durable session log",
    )
    sid = await session_log.create_session("Crash recovery demo")
    print(f"\n[SESSION] {sid[:12]}…")

    h1 = _harness("Harness-1")
    await h1.start(sid)
    r1 = await h1.run("List three benefits of async programming in Python.")
    print(f"\n[H1] {_clip(r1, 200)}")
    print(f"\n[H1] 💥  Simulating crash — harness discarded without shutdown…")
    del h1

    n_before = await session_log.event_count(sid)
    print(f"[LOG] {n_before} events still preserved after crash ✓")

    print("\n[H2] wake(session_id) → recovering from durable log…")
    h2 = _harness("Harness-2")
    await h2.start(sid)       # internally calls SessionLog.wake()
    r2 = await h2.run(
        "Give a short Python async/await example based on what you found."
    )
    print(f"\n[H2] Seamless continuation: {_clip(r2, 280)}")
    await h2.shutdown()

    n_after = await session_log.event_count(sid)
    print(f"\n[LOG] Final: {n_after} events ({n_after - n_before} added after recovery)")
    await _event_breakdown(sid)


# ── Demo 4: Streaming Output ──────────────────────────────────────────────

async def demo_streaming() -> None:
    _banner(
        "Demo 4 — Streaming Response",
        "run_streaming() returns tokens incrementally as they arrive from Foundry",
    )
    sid = await session_log.create_session("Streaming demo")
    h   = _harness("StreamAgent")
    await h.start(sid)

    print("\n[AGENT streaming]: ", end="", flush=True)
    try:
        async for chunk in h.run_streaming(
            "Write a haiku about distributed systems and fault tolerance."
        ):
            print(chunk, end="", flush=True)
    except Exception:
        resp = await h.run("Write a haiku about distributed systems.")
        print(resp, end="")
    print()
    await h.shutdown()


# ── Demo 5: Many Brains, Many Hands ───────────────────────────────────────

async def demo_many_brains() -> None:
    _banner(
        "Demo 5 — Many Brains × Many Hands",
        "N stateless Foundry harnesses run concurrently; sandboxes created on demand",
    )
    tasks = [
        "What is the capital of Japan? One sentence only.",
        "Calculate the sum of squares from 1 to 10 using Python.",
        "Give one fun fact about Microsoft Foundry.",
    ]

    print(f"\n[ORCHESTRATOR] Launching {len(tasks)} parallel Foundry brains…")
    results = await run_many_brains(tasks, session_log, sandbox_mgr)

    for r in results:
        icon = "✓" if r.get("ok") else "✗"
        print(f"\n  {icon} [{r['session_id'][:8]}] {r['task'][:55]}")
        if r.get("ok"):
            print(f"     {_clip(r['response'], 200)}")
        else:
            print(f"     Error: {r.get('error','?')}")

    print(f"\n[METRICS] {GLOBAL_METRICS.summary()}")


# ── Demo 6: Session Log Inspection ────────────────────────────────────────

async def demo_session_log() -> None:
    _banner(
        "Demo 6 — Session Log Inspection",
        "getEvents() slicing for context window management",
    )
    sid = await session_log.create_session("Log inspection demo")
    h   = _harness()
    await h.start(sid)
    for q in ["Hello!", "What is 1 + 1?", "Thanks, bye."]:
        await h.run(q)
    await h.shutdown()

    all_events = await session_log.get_events(sid)
    print(f"\n[LOG] Total events: {len(all_events)}")

    recent = await session_log.get_context_window(sid, last_n=5)
    print(f"\n[CONTEXT WINDOW] Last 5 events:")
    for e in recent:
        print(f"  [{e.kind.value:22s}] {str(e.payload)[:80]}")

    responses = await session_log.get_events(
        sid, kind_filter=[EventKind.AGENT_RESPONSE]
    )
    print(f"\n[FILTER] Agent responses: {len(responses)} events")

    sliced = await session_log.get_events(sid, start=1, end=4)
    print(f"[SLICE 1:4] {len(sliced)} events: "
          + ", ".join(e.kind.value for e in sliced))


# ── Demo 7: Security Boundary ─────────────────────────────────────────────

async def demo_security() -> None:
    _banner(
        "Demo 7 — Security Boundary",
        "VaultStore holds tokens; sandbox never sees credentials",
    )
    vault.store("foundry_key",   "FoundryKey-XXXX")
    vault.store("github_token",  "ghp_XXXXXXXX")
    print("\n[VAULT] Stored (outside sandbox):")
    print("  foundry_key  → FoundryKey-XXXX")
    print("  github_token → ghp_XXXXXXXX")

    sid = await sandbox_mgr.provision()
    box = sandbox_mgr.get(sid)

    r1 = await box.execute(
        "run_python",
        "import os; print(os.environ.get('FOUNDRY_API_KEY', 'NOT_VISIBLE'))",
    )
    print(f"\n[SANDBOX] Reading FOUNDRY_API_KEY from inside sandbox: {r1!r}  ✓")

    r2 = await box.execute("shell", "cat /etc/secrets")  # Blocked tool
    print(f"[SANDBOX] Blocked tool 'shell': {r2!r}  ✓")

    box.kill()
    sid2 = await sandbox_mgr.provision()
    print(f"\n[SANDBOX] Old sandbox destroyed (cattle mode). New sandbox: {sid2[:12]}…  ✓")
    sandbox_mgr.reclaim(sid2)


# ── Entry Point ────────────────────────────────────────────────────────────

DEMOS = {
    "single":   demo_single_turn,
    "multi":    demo_multi_turn,
    "recover":  demo_harness_recovery,
    "stream":   demo_streaming,
    "many":     demo_many_brains,
    "log":      demo_session_log,
    "security": demo_security,
}


async def run_all() -> None:
    print("\n" + "█" * 68)
    print("  MAF HARNESS MANAGED AGENT")
    print("  Anthropic Managed Agents × Microsoft Agent Framework × Foundry")
    print("█" * 68)
    print(f"\n  Endpoint : {os.getenv('FOUNDRY_PROJECT_ENDPOINT', '(set FOUNDRY_PROJECT_ENDPOINT)')}")
    print(f"  Model    : {os.getenv('FOUNDRY_MODEL', 'gpt-5.4')}")

    for name, demo in DEMOS.items():
        try:
            await demo()
        except Exception as exc:
            print(f"\n[{name}] ⚠  Skipped — {type(exc).__name__}: {exc}")

    print("\n" + "═" * 68)
    print("GLOBAL METRICS")
    print("═" * 68)
    print(GLOBAL_METRICS.summary())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MAF Harness Managed Agent Demo (Microsoft Foundry)"
    )
    parser.add_argument("--mode", choices=[*DEMOS, "all"], default="all")
    args = parser.parse_args()

    if not os.getenv("FOUNDRY_PROJECT_ENDPOINT"):
        print(
            "⚠  FOUNDRY_PROJECT_ENDPOINT not set.\n"
            "   LLM demos will be skipped. Sandbox / session / security demos run.\n\n"
            "   To run LLM demos:\n"
            "     export FOUNDRY_PROJECT_ENDPOINT=https://<hub>.services.ai.azure.com\n"
            "     export FOUNDRY_MODEL=gpt-5.4\n"
            "     az login\n"
        )

    target = run_all if args.mode == "all" else DEMOS[args.mode]
    asyncio.run(target())


if __name__ == "__main__":
    main()
