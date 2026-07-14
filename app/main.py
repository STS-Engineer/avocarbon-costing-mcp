from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.routers.choke_agent_integration_router import router as choke_agent_integration_router
from app.routers.choke_costing_ui_router import router as choke_costing_ui_router
from app.routers.choke_orchestrator_router import router as choke_orchestrator_router
from app.routers.choke_workflow_router import router as choke_workflow_router
from server import (
    health_check as mcp_health_check,
    mcp,
    root_info as mcp_root_info,
)
from services.project_data_paths import get_data_root, validate_data_root_configuration


logger = logging.getLogger(__name__)
DEFAULT_FRONTEND_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _frontend_origins():
    configured = os.getenv("FRONTEND_ORIGINS", ",".join(DEFAULT_FRONTEND_ORIGINS))
    origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    if "*" in origins:
        logger.warning("Ignoring wildcard FRONTEND_ORIGINS; configure explicit frontend origins.")
        origins = [origin for origin in origins if origin != "*"]
    return origins or list(DEFAULT_FRONTEND_ORIGINS)


mcp_sse_app = mcp.sse_app()
mcp_streamable_http_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage_status = validate_data_root_configuration()
    if storage_status["healthy"]:
        logger.info("CANONICAL_DATA_ROOT=%s", get_data_root())
    else:
        logger.critical("Invalid workflow storage configuration: %s", storage_status["errors"])
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="AVOCarbon Costing API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_frontend_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type", "Idempotency-Key"],
)
app.include_router(choke_orchestrator_router)
app.include_router(choke_costing_ui_router)
app.include_router(choke_agent_integration_router)
app.include_router(choke_workflow_router)

for route in mcp_sse_app.routes:
    if getattr(route, "path", None) in {"/sse", "/messages"}:
        app.router.routes.append(route)
for route in mcp_streamable_http_app.routes:
    if getattr(route, "path", None) == "/mcp":
        app.router.routes.append(route)


@app.get("/", include_in_schema=False)
async def root(request: Request):
    return await mcp_root_info(request)


@app.get("/health", include_in_schema=False)
async def health(request: Request):
    return await mcp_health_check(request)


@app.get("/api/health", tags=["Health"])
def api_health():
    storage_status = validate_data_root_configuration()
    payload = {
        "status": "ok" if storage_status["healthy"] else "unhealthy",
        "service": "avocarbon-costing-backend",
        **{key: storage_status[key] for key in (
            "git_commit",
            "data_root_raw",
            "data_root_resolved",
            "persistent_storage_enabled",
            "workflow_path_version",
            "process_id",
            "cwd",
            "startup_module",
        )},
    }
    if not storage_status["healthy"]:
        payload["storage_errors"] = storage_status["errors"]
        return JSONResponse(payload, status_code=503)
    return payload
