"""Sandbox pool ("hands").

Managed-agent principles:
- Cattle, not pets: each sandbox is stateless and disposable. Provisioned on
  first demand (lazy), torn down on failure, re-provisioned on retry.
- Generic interface: `execute(name, input) -> str`. The brain doesn't know
  whether the hand is a Python REPL, a shell, an HTTP fetcher, or a
  remote container.
- Security boundary: the sandbox never sees raw credentials. The vault
  (held by the harness) injects auth at call time via a proxy.
"""
from __future__ import annotations

import io
import json
import contextlib
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .vault import CredentialVault


class SandboxError(Exception):
    """Raised when a sandbox invocation fails. The brain catches this as a
    normal tool-call error and may decide to retry with a fresh sandbox."""


@dataclass
class _Sandbox:
    kind: str
    sandbox_id: str
    created_at: float


ToolFn = Callable[[dict[str, Any], CredentialVault], str]


class SandboxPool:
    """Lazy, cattle-style sandbox pool with a generic execute() interface."""

    def __init__(self, vault: CredentialVault, max_output_chars: int = 8000) -> None:
        self._vault = vault
        self._max_output = max_output_chars
        self._tools: dict[str, ToolFn] = {}
        self._active: dict[str, _Sandbox] = {}
        self._register_builtins()

    # ---------- registration ----------
    def register(self, name: str, fn: ToolFn) -> None:
        self._tools[name] = fn

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    # ---------- provisioning (cattle) ----------
    def provision(self, kind: str) -> str:
        sid = f"{kind}-{int(time.time() * 1000)}"
        self._active[sid] = _Sandbox(kind=kind, sandbox_id=sid, created_at=time.time())
        return sid

    def retire(self, sandbox_id: str) -> None:
        self._active.pop(sandbox_id, None)

    # ---------- the ONE interface the brain calls ----------
    def execute(self, name: str, input: dict[str, Any]) -> str:
        """execute(name, input) -> string.

        This is the only contract between the brain and the hands.
        Failures are returned as errors (not raised up the harness) so the
        brain can decide whether to retry on a fresh sandbox.
        """
        if name not in self._tools:
            return f"ERROR: unknown tool '{name}'. Available: {self.list_tools()}"

        sandbox_id = self.provision(kind=name)
        try:
            out = self._tools[name](input or {}, self._vault)
            out = self._vault.redact(out)
            if len(out) > self._max_output:
                out = out[: self._max_output] + f"\n...[truncated {len(out) - self._max_output} chars]"
            return out
        except Exception as e:  # sandbox failed -> becomes a tool error
            return f"ERROR: sandbox '{sandbox_id}' failed: {type(e).__name__}: {e}"
        finally:
            # Cattle: always retire the sandbox after a single call.
            self.retire(sandbox_id)

    # ---------- built-in hands ----------
    def _register_builtins(self) -> None:
        self.register("python_exec", _python_exec)
        self.register("shell_exec", _shell_exec)
        self.register("http_fetch", _http_fetch)


# --- Built-in tool implementations ------------------------------------------

def _python_exec(input: dict[str, Any], vault: CredentialVault) -> str:
    """Run a short snippet of Python in an isolated sandbox process.

    Input: {"code": "print(1+1)"}
    """
    code = str(input.get("code", "")).strip()
    if not code:
        return "ERROR: 'code' is required."

    # Use sys.executable so this works in conda/venv envs where `python`
    # may not be on PATH (only `python3` is).
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},  # no secrets leaked
        )
    except subprocess.TimeoutExpired:
        raise SandboxError("python_exec timed out after 15s")

    out = proc.stdout or ""
    err = proc.stderr or ""
    if proc.returncode != 0:
        return f"exit={proc.returncode}\nstdout:\n{out}\nstderr:\n{err}"
    return out if out else "(no output)"


def _shell_exec(input: dict[str, Any], vault: CredentialVault) -> str:
    """Run a shell command in an isolated subprocess (no shell metachars)."""
    argv = input.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return "ERROR: 'argv' must be a list of strings."
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    except FileNotFoundError:
        raise SandboxError(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise SandboxError("shell_exec timed out after 15s")
    return f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def _http_fetch(input: dict[str, Any], vault: CredentialVault) -> str:
    """HTTP GET via a vault proxy.

    Input: {"url": "...", "credential": "optional-logical-name"}
    The model never sees the token; the vault injects it here.
    """
    url = input.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return "ERROR: 'url' must be an http(s) URL."

    headers: dict[str, str] = {"User-Agent": "managed-style-agent/1.0"}
    cred_name = input.get("credential")
    if isinstance(cred_name, str) and vault.has(cred_name):
        headers.update(vault.build_auth_headers(cred_name))

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            body = resp.read(64 * 1024).decode("utf-8", errors="replace")
            return f"status={resp.status}\n{body}"
    except Exception as e:
        raise SandboxError(f"http_fetch failed: {e}")
