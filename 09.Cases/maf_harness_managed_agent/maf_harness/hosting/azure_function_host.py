"""
maf_harness.hosting.azure_function_host
========================================
将托管代理编排器托管为 Azure Functions HTTP 触发器。
LLM 后端：Microsoft Foundry（FoundryChatClient）— 无 OpenAI 依赖。

端点：
    POST /sessions                   → 创建会话，返回 session_id
    POST /sessions/{id}/run          → 执行一次代理轮次
    GET  /sessions/{id}/events       → 查询会话事件日志
    POST /sessions/{id}/wake         → 从会话日志中重新注入编排器
    GET  /health                     → 端点与模型健康检查

任何 Azure Functions 实例都可以服务任何会话 — 路由完全基于
session_id。无需粘性会话。通过增加实例即可水平扩展。

local.settings.json:
    {
      "Values": {
        "FOUNDRY_PROJECT_ENDPOINT": "https://<hub>.services.ai.azure.com",
        "FOUNDRY_MODEL":            "gpt-5.4",
        "AGENT_NAME":               "ManagedAgent",
        "MAX_ITERATIONS":           "20"
      }
    }

本地开发（FastAPI）：
    uvicorn maf_harness.hosting.azure_function_host:local_app --reload
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv
load_dotenv()

try:
    import azure.functions as func
    _AZURE = True
except ImportError:
    _AZURE = False

from maf_harness.harness.harness import AgentHarness, HarnessConfig, make_foundry_client
from maf_harness.sandbox.sandbox import SandboxManager, VaultStore
from maf_harness.session.session_log import SessionLog


# ── 单例基础设施（每个冷启动容器）───────────────────────────────────────────

_vault       = VaultStore()
_session_log = SessionLog()
_sandbox_mgr = SandboxManager(_vault)

_config = HarnessConfig(
    agent_name=os.getenv("AGENT_NAME", "ManagedAgent"),
    model=os.getenv("FOUNDRY_MODEL", "gpt-5.4"),
    max_iterations=int(os.getenv("MAX_ITERATIONS", "20")),
)

# 延迟构建，首次请求时创建 — 避免环境变量尚未注入时的冷启动失败
# （如测试环境中模块导入阶段）。
_foundry_client = None


def _get_client():
    global _foundry_client
    if _foundry_client is None:
        _foundry_client = make_foundry_client(model=_config.model)
    return _foundry_client


# ── 处理逻辑（Azure Functions 和 FastAPI 共用）────────────────────────────

async def _handle_create_session(body: dict) -> dict:
    task       = body.get("task", "")
    metadata   = body.get("metadata", {})
    session_id = await _session_log.create_session(task, metadata)
    return {"session_id": session_id, "task": task}


async def _handle_run_turn(session_id: str, body: dict) -> dict:
    user_input = body.get("input", "")
    if not user_input:
        return {"error": "Missing 'input' field."}

    # 每次请求创建新的无状态编排器 — 从持久化会话日志中唤醒
    harness = AgentHarness(
        session_log=_session_log,
        sandbox_mgr=_sandbox_mgr,
        config=_config,
        client=_get_client(),
    )
    await harness.start(session_id)
    response = await harness.run(user_input)
    await harness.shutdown()

    return {
        "session_id":  session_id,
        "response":    response,
        "event_count": await _session_log.event_count(session_id),
    }


async def _handle_get_events(session_id: str, params: dict) -> dict:
    start  = int(params.get("start", 0))
    end    = int(params.get("end", -1)) or None
    events = await _session_log.get_events(session_id, start=start, end=end)
    return {
        "session_id": session_id,
        "events":     [e.to_dict() for e in events],
        "count":      len(events),
    }


async def _handle_wake(session_id: str) -> dict:
    session, events = await _session_log.wake(session_id)
    if session is None:
        return {"error": f"Session {session_id} not found."}
    return {
        "session_id":  session_id,
        "event_count": len(events),
        "resumed":     True,
        "last_event":  events[-1].to_dict() if events else None,
    }


# ── Azure Functions HTTP 触发器 ─────────────────────────────────────────────

if _AZURE:
    app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

    @app.route(route="sessions", methods=["POST"])
    async def create_session(req: func.HttpRequest) -> func.HttpResponse:
        result = await _handle_create_session(req.get_json())
        return func.HttpResponse(
            json.dumps(result), status_code=200, mimetype="application/json"
        )

    @app.route(route="sessions/{session_id}/run", methods=["POST"])
    async def run_turn(req: func.HttpRequest) -> func.HttpResponse:
        sid = req.route_params.get("session_id", "")
        try:
            result = await _handle_run_turn(sid, req.get_json())
            return func.HttpResponse(
                json.dumps(result), status_code=200, mimetype="application/json"
            )
        except Exception as exc:
            return func.HttpResponse(
                json.dumps({"error": str(exc)}),
                status_code=500, mimetype="application/json",
            )

    @app.route(route="sessions/{session_id}/events", methods=["GET"])
    async def get_events(req: func.HttpRequest) -> func.HttpResponse:
        sid    = req.route_params.get("session_id", "")
        result = await _handle_get_events(sid, dict(req.params))
        return func.HttpResponse(
            json.dumps(result, default=str),
            status_code=200, mimetype="application/json",
        )

    @app.route(route="sessions/{session_id}/wake", methods=["POST"])
    async def wake_session(req: func.HttpRequest) -> func.HttpResponse:
        sid    = req.route_params.get("session_id", "")
        result = await _handle_wake(sid)
        return func.HttpResponse(
            json.dumps(result, default=str),
            status_code=200, mimetype="application/json",
        )


# ── 本地 FastAPI 开发服务器 ──────────────────────────────────────────────────

def create_local_app():
    """
    镜像 Azure Functions 路由，用于本地开发。

    用法：
        uvicorn maf_harness.hosting.azure_function_host:local_app --reload

    环境变量：
        export FOUNDRY_PROJECT_ENDPOINT=https://...
        export FOUNDRY_MODEL=gpt-5.4
        az login
    """
    try:
        from fastapi import FastAPI
    except ImportError:
        raise RuntimeError("pip install fastapi uvicorn  for local dev server.")

    app = FastAPI(
        title="MAF Harness Managed Agent — Microsoft Foundry",
        description="Anthropic Managed Agents on Microsoft Agent Framework + Foundry.",
        version="1.0.0",
    )

    @app.post("/sessions")
    async def create_session(body: dict) -> dict:
        return await _handle_create_session(body)

    @app.post("/sessions/{session_id}/run")
    async def run_turn(session_id: str, body: dict) -> dict:
        return await _handle_run_turn(session_id, body)

    @app.get("/sessions/{session_id}/events")
    async def get_events(session_id: str, start: int = 0, end: int = None) -> dict:
        return await _handle_get_events(session_id, {"start": start, "end": end})

    @app.post("/sessions/{session_id}/wake")
    async def wake(session_id: str) -> dict:
        return await _handle_wake(session_id)

    @app.get("/sessions")
    async def list_sessions() -> dict:
        return {"sessions": _session_log.list_sessions()}

    @app.get("/health")
    async def health() -> dict:
        return {
            "status":   "ok",
            "backend":  "Microsoft Foundry",
            "endpoint": os.getenv("FOUNDRY_PROJECT_ENDPOINT", "(not set)"),
            "model":    os.getenv("FOUNDRY_MODEL", "gpt-5.4"),
            "sessions": len(_session_log.list_sessions()),
        }

    return app


try:
    local_app = create_local_app()
except Exception:
    local_app = None
