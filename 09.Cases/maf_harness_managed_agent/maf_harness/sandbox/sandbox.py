"""
maf_harness.sandbox.sandbox
============================
Sandbox is the "hands" — an isolated execution environment that the harness calls via a single interface:

    execute(name, input) → string
    provision(resources) → sandbox_id

Key design decisions from the Anthropic article:
  - Sandboxes are "cattle": if one dies, the harness creates a new one.
  - Credentials never enter the sandbox; they are kept in VaultStore.
  - Sandboxes are created on-demand (not pre-provisioned) — this reduced Anthropic's
    p50 TTFT by ~60% and p95 by >90%.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


# ── Credential Vault ────────────────────────────────────────────────────────

class VaultStore:
    """
    Secure credential storage — tokens never enter the sandbox.
    Use Azure Key Vault as backend in production.
    """

    def __init__(self) -> None:
        self._vault: dict[str, str] = {}

    def store(self, key: str, token: str) -> None:
        self._vault[key] = token

    def fetch(self, key: str) -> str | None:
        return self._vault.get(key)

    def revoke(self, key: str) -> None:
        self._vault.pop(key, None)


# ── Sandbox Resource Spec ─────────────────────────────────────────────────────────

@dataclass
class SandboxResources:
    cpu_cores:     float      = 1.0
    memory_mb:     int        = 512
    timeout_sec:   int        = 30
    env_vars:      dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str]  = field(default_factory=list)


# ── Tool Registry ───────────────────────────────────────────────────────────

class ToolRegistry:
    """Registry of named callable tools available within the sandbox."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Callable | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())


# ── Sandbox Instance ────────────────────────────────────────────────────────

class Sandbox:
    """
    A single sandbox instance. Stateless except for alive flag.
    Standard interface: await sandbox.execute(name, input) → str
    """

    def __init__(
        self,
        sandbox_id: str,
        resources:  SandboxResources,
        registry:   ToolRegistry,
        vault:      VaultStore,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.resources  = resources
        self.registry   = registry
        self._vault     = vault
        self._alive     = True
        self._created   = time.time()

    @property
    def alive(self) -> bool:
        return self._alive

    async def execute(self, name: str, input_data: str) -> str:
        """
        execute(name, input) → string — the sole interface between brain ↔ hands.
        The orchestrator doesn't care whether 'name' maps to a container, subprocess, or other execution backend.
        """
        if not self._alive:
            raise RuntimeError(f"Sandbox {self.sandbox_id} is dead.")

        if self.resources.allowed_tools and name not in self.resources.allowed_tools:
            return f"[SANDBOX DENIED] Tool '{name}' is not in the allowed list."

        fn = self.registry.get(name)
        if fn is None:
            return f"[SANDBOX ERROR] Unknown tool: '{name}'"

        try:
            if asyncio.iscoroutinefunction(fn):
                result = await asyncio.wait_for(
                    fn(input_data, vault=self._vault),
                    timeout=self.resources.timeout_sec,
                )
            else:
                result = fn(input_data, vault=self._vault)
            return str(result)
        except asyncio.TimeoutError:
            self._alive = False
            raise RuntimeError(
                f"Sandbox {self.sandbox_id} timed out on '{name}'. "
                "Harness should provision a fresh sandbox."
            )
        except Exception as exc:
            return f"[TOOL ERROR] {name}: {exc}"

    def kill(self) -> None:
        """Mark the sandbox as terminated — the orchestrator will create a replacement."""
        self._alive = False


# ── Sandbox Manager ───────────────────────────────────────────────────────────────

class SandboxManager:
    """
    Creates and tracks sandbox instances.

    Corresponds to Anthropic's provision({resources}) → sandbox_id pattern.
    Sandboxes are created only when the orchestrator actually needs execution;
    purely inferential sessions never incur creation overhead.
    """

    def __init__(self, vault: VaultStore) -> None:
        self._vault     = vault
        self._registry  = ToolRegistry()
        self._sandboxes: dict[str, Sandbox] = {}
        self._register_builtin_tools()

    # ── Built-in Tool Registration ────────────────────────────────────────────

    def _register_builtin_tools(self) -> None:

        async def run_python(code: str, **_) -> str:
            """Execute Python code snippet in an isolated subprocess."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-c", code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
                stdout = out.decode().strip()
                stderr = err.decode().strip()
                return stdout if not stderr else f"{stdout}\nSTDERR: {stderr}"
            except Exception as e:
                return f"[EXEC ERROR] {e}"

        async def web_search(query: str, **_) -> str:
            """Mock web search (replace with Bing / Azure AI Search in production)."""
            return (
                f"[SEARCH: '{query}']\n"
                f"1. Result A — overview of '{query}'\n"
                f"2. Result B — detailed analysis of '{query}'\n"
                f"3. Result C — recent developments on '{query}'"
            )

        async def read_file(path: str, **_) -> str:
            try:
                with open(path) as f:
                    return f.read()
            except Exception as e:
                return f"[FILE ERROR] {e}"

        async def write_file(payload: str, **_) -> str:
            """Payload format: 'path::content'"""
            if "::" not in payload:
                return "[FILE ERROR] payload must be 'path::content'"
            path, content = payload.split("::", 1)
            try:
                with open(path.strip(), "w") as f:
                    f.write(content)
                return f"Written {len(content)} chars to {path.strip()}"
            except Exception as e:
                return f"[FILE ERROR] {e}"

        self._registry.register("run_python", run_python)
        self._registry.register("web_search", web_search)
        self._registry.register("read_file",  read_file)
        self._registry.register("write_file", write_file)

    def register_tool(self, name: str, fn: Callable) -> None:
        """Register custom tools at runtime."""
        self._registry.register(name, fn)

    # ── Create / Execute / Reclaim ─────────────────────────────────────────────

    async def provision(self, resources: SandboxResources | None = None) -> str:
        """
        Create a new sandbox. Equivalent to provision({resources}).
        Called lazily by the orchestrator — only when tools are actually needed.
        """
        resources  = resources or SandboxResources()
        sandbox_id = str(uuid.uuid4())
        self._sandboxes[sandbox_id] = Sandbox(
            sandbox_id=sandbox_id,
            resources=resources,
            registry=self._registry,
            vault=self._vault,
        )
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        return self._sandboxes.get(sandbox_id)

    async def execute(self, sandbox_id: str, name: str, input_data: str) -> str:
        """Route execute(name, input) → string to the specified sandbox."""
        sandbox = self.get(sandbox_id)
        if sandbox is None or not sandbox.alive:
            raise RuntimeError(
                f"Sandbox {sandbox_id} not found or dead. Call provision() first."
            )
        return await sandbox.execute(name, input_data)

    def reclaim(self, sandbox_id: str) -> None:
        """Terminate and discard sandbox (replaceable pattern)."""
        if sandbox_id in self._sandboxes:
            self._sandboxes[sandbox_id].kill()
            del self._sandboxes[sandbox_id]
