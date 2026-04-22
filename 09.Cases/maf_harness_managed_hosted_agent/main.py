"""Managed-style hosted agent on Microsoft Agent Framework.

Design (per Anthropic's Managed Agents):

  ┌─────────┐   execute(name, input) -> string    ┌──────────┐
  │  Brain  │ ───────────────────────────────────▶│  Hands   │
  │ (model) │                                     │ (sandbox)│
  └────┬────┘                                     └──────────┘
       │ emit_event / get_events
       ▼
  ┌──────────────┐
  │   Session    │  durable, append-only, external to context window
  └──────────────┘

- Brain/hands/session are three independent interfaces.
- Sandboxes are cattle: provisioned lazily, retired after each call.
- Credentials live in a vault; the sandbox never sees raw tokens.
- Session is NOT the context window; the harness can re-read any slice.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Annotated, Any

from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity.aio import DefaultAzureCredential

from harness import SessionStore, SandboxPool, CredentialVault

# --- Configuration ----------------------------------------------------------
# Accept either FOUNDRY_PROJECT_ENDPOINT (used by agent_framework.foundry) or
# PROJECT_ENDPOINT (used by the older Foundry hosted-agent samples).
PROJECT_ENDPOINT = (
    os.getenv("FOUNDRY_PROJECT_ENDPOINT")
    or os.getenv("PROJECT_ENDPOINT")
    or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
)
MODEL_DEPLOYMENT_NAME = (
    os.getenv("FOUNDRY_MODEL")
    or os.getenv("MODEL_DEPLOYMENT_NAME")
    or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    or "gpt-4.1-mini"
)
SESSION_DIR = os.getenv("SESSION_DIR", "/tmp/sessions")

# Export the canonical names the agent_framework.foundry SDK reads from env,
# so FoundryChatClient's internal settings loader resolves successfully even
# when callers only set the Foundry hosted-agent style variables.
if PROJECT_ENDPOINT:
    os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", PROJECT_ENDPOINT)
os.environ.setdefault("FOUNDRY_MODEL", MODEL_DEPLOYMENT_NAME)

if not PROJECT_ENDPOINT:
    raise RuntimeError(
        "Set FOUNDRY_PROJECT_ENDPOINT (preferred) or PROJECT_ENDPOINT in your .env."
    )

# --- Singletons held by the harness (outside the sandbox boundary) ---------
SESSIONS = SessionStore(root_dir=SESSION_DIR)
VAULT = CredentialVault()
# Example: register any outbound credentials by logical name.
VAULT.register_env("github", "GITHUB_TOKEN")
SANDBOX = SandboxPool(vault=VAULT)

# A single "current" session id per process for this demo. A real deployment
# derives the session id from the incoming Responses API request header so
# many brains can share the same session log.
CURRENT_SESSION_ID = SESSIONS.create_session(
    session_id=os.getenv("SESSION_ID") or str(uuid.uuid4())
)


# --- Tools exposed to the model --------------------------------------------
# The ENTIRE contract between the brain and the hands is a single tool:
#   execute(name, input) -> string
# plus the session interrogation tools (get_events, emit_note). This keeps
# the harness unopinionated about the specific hands available today.

def execute(
    name: Annotated[str, "Tool name, e.g. 'python_exec', 'shell_exec', 'http_fetch'."],
    input_json: Annotated[str, "JSON-encoded arguments for the tool."],
) -> str:
    """Call any registered 'hand' in a fresh, cattle-style sandbox.

    Failures are returned as strings beginning with 'ERROR:' so you can
    decide whether to retry with different input or a different tool.
    """
    try:
        payload: dict[str, Any] = json.loads(input_json) if input_json else {}
        if not isinstance(payload, dict):
            return "ERROR: input_json must decode to a JSON object."
    except json.JSONDecodeError as e:
        return f"ERROR: invalid input_json: {e}"

    SESSIONS.emit_event(
        CURRENT_SESSION_ID,
        "tool_call",
        {"name": name, "input": VAULT.redact(payload)},
    )
    result = SANDBOX.execute(name, payload)
    SESSIONS.emit_event(
        CURRENT_SESSION_ID,
        "tool_result",
        {"name": name, "output": result[:2000]},
    )
    return result


def list_tools() -> str:
    """Return the names of every 'hand' you can pass to execute()."""
    return json.dumps(SANDBOX.list_tools())


def get_events(
    start: Annotated[int, "First event index to return (0-based)."] = 0,
    end: Annotated[int, "Exclusive end index; -1 means 'up to latest'."] = -1,
) -> str:
    """Fetch a positional slice of the durable session log.

    Use this to re-read earlier context on demand instead of carrying
    everything in your active context window.
    """
    stop = None if end < 0 else end
    events = SESSIONS.get_events(CURRENT_SESSION_ID, start=start, end=stop)
    return json.dumps([
        {"i": e.index, "type": e.type, "payload": e.payload, "ts": e.ts}
        for e in events
    ])


def emit_note(
    note: Annotated[str, "Free-form note to persist in the durable session log."],
) -> str:
    """Append a note to the session. Use this to checkpoint intermediate
    reasoning that may be useful to re-read later."""
    ev = SESSIONS.emit_event(CURRENT_SESSION_ID, "note", {"text": note})
    return f"ok (event #{ev.index})"


# --- Instructions -----------------------------------------------------------
INSTRUCTIONS = """You are a Managed-style hosted agent.

ARCHITECTURE YOU OPERATE IN
- Your brain is decoupled from your hands. You interact with every external
  capability through exactly one tool: `execute(name, input_json)`.
  Call `list_tools()` to discover which hands are currently attached.
- Each call to `execute` runs in a fresh, disposable sandbox. Do not assume
  state carries across calls. If a sandbox fails, retry with different
  input or a different tool.
- Your durable memory is the session log, not this context window. Use
  `emit_note(note)` to record important intermediate findings and
  `get_events(start, end)` to re-read any slice of the log on demand.
- Never ask the user for raw credentials. If a tool needs auth, pass a
  logical credential name (e.g. `"credential": "github"`) in the input;
  the vault injects the real token outside your reach.

OPERATING GUIDELINES
1. Plan briefly, act in small steps, and verify with tools.
2. Prefer `emit_note` over stuffing scratch work into replies.
3. When a tool returns a line starting with `ERROR:`, treat it as a
   recoverable sandbox failure and decide whether to retry.
4. Keep responses to the user concise; lean on the session log for depth.
"""


async def main() -> None:
    async with DefaultAzureCredential() as credential:
        client = FoundryChatClient(
            project_endpoint=PROJECT_ENDPOINT,
            model=MODEL_DEPLOYMENT_NAME,
            credential=credential,
            allow_preview=True,
        )
        agent = Agent(
            client,
            instructions=INSTRUCTIONS,
            name="ManagedStyleAgent",
            tools=[execute, list_tools, get_events, emit_note],
        )
        SESSIONS.emit_event(
            CURRENT_SESSION_ID,
            "session_start",
            {"agent": "ManagedStyleAgent", "model": MODEL_DEPLOYMENT_NAME},
        )
        print("Managed-style Agent running on http://localhost:8088")
        print(f"Session id: {CURRENT_SESSION_ID}  (log dir: {SESSION_DIR})")
        server = ResponsesHostServer(agent)
        await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
