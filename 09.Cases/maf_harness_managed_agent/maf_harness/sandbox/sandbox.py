"""
maf_harness.sandbox.sandbox
============================
沙箱是"双手" — 一个隔离的执行环境，编排器通过单一接口调用：

    execute(name, input) → string
    provision(resources) → sandbox_id

来自 Anthropic 文章的关键设计决策：
  - 沙箱是"可替换的"：如果一个挂了，编排器会创建一个新的。
  - 凭据永远不进入沙箱；它们保存在 VaultStore 中。
  - 沙箱按需创建（而非预先创建） — 这使 Anthropic 的
    p50 TTFT 减少了约 60%，p95 减少了超过 90%。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


# ── 凭据保险库 ──────────────────────────────────────────────────────────────

class VaultStore:
    """
    安全凭据存储 — 令牌永远不进入沙箱。
    生产环境中使用 Azure Key Vault 作为后端。
    """

    def __init__(self) -> None:
        self._vault: dict[str, str] = {}

    def store(self, key: str, token: str) -> None:
        self._vault[key] = token

    def fetch(self, key: str) -> str | None:
        return self._vault.get(key)

    def revoke(self, key: str) -> None:
        self._vault.pop(key, None)


# ── 沙箱资源规格 ─────────────────────────────────────────────────────────────

@dataclass
class SandboxResources:
    cpu_cores:     float      = 1.0
    memory_mb:     int        = 512
    timeout_sec:   int        = 30
    env_vars:      dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str]  = field(default_factory=list)


# ── 工具注册表 ─────────────────────────────────────────────────────────────

class ToolRegistry:
    """沙箱内可用的命名可调用工具的注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Callable | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())


# ── 沙箱实例 ──────────────────────────────────────────────────────────────

class Sandbox:
    """
    单个沙箱实例。除活跃标志外无状态。
    标准接口：await sandbox.execute(name, input) → str
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
        execute(name, input) → string — 大脑↔双手的唯一接口。
        编排器不关心 'name' 映射到容器、子进程还是其他执行后端。
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
        """将沙箱标记为已终止 — 编排器将创建一个替代品。"""
        self._alive = False


# ── 沙箱管理器 ───────────────────────────────────────────────────────────────

class SandboxManager:
    """
    创建和跟踪沙箱实例。

    对应 Anthropic 的 provision({resources}) → sandbox_id 模式。
    沙箱仅在编排器实际需要执行时才创建；
    纯推理的会话永远不会承担创建开销。
    """

    def __init__(self, vault: VaultStore) -> None:
        self._vault     = vault
        self._registry  = ToolRegistry()
        self._sandboxes: dict[str, Sandbox] = {}
        self._register_builtin_tools()

    # ── 内置工具注册 ────────────────────────────────────────────────────────

    def _register_builtin_tools(self) -> None:

        async def run_python(code: str, **_) -> str:
            """在隔离的子进程中执行 Python 代码片段。"""
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
            """模拟网络搜索（生产环境替换为 Bing / Azure AI Search）。"""
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
            """负载格式: 'path::content'"""
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
        """在运行时注册自定义工具。"""
        self._registry.register(name, fn)

    # ── 创建 / 执行 / 回收 ─────────────────────────────────────────────────

    async def provision(self, resources: SandboxResources | None = None) -> str:
        """
        创建新沙箱。等价于 provision({resources})。
        由编排器延迟调用 — 仅在实际需要工具时才调用。
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
        """将 execute(name, input) → string 路由到指定沙箱。"""
        sandbox = self.get(sandbox_id)
        if sandbox is None or not sandbox.alive:
            raise RuntimeError(
                f"Sandbox {sandbox_id} not found or dead. Call provision() first."
            )
        return await sandbox.execute(name, input_data)

    def reclaim(self, sandbox_id: str) -> None:
        """终止并丢弃沙箱（可替换模式）。"""
        if sandbox_id in self._sandboxes:
            self._sandboxes[sandbox_id].kill()
            del self._sandboxes[sandbox_id]
