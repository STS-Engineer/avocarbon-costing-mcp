from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers.choke_agent_integration_router import router as choke_agent_integration_router
from app.routers.choke_costing_ui_router import router as choke_costing_ui_router
from app.routers.choke_orchestrator_router import router as choke_orchestrator_router
from app.routers.choke_workflow_router import router as choke_workflow_router
from server import mcp


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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "AVOCarbon Costing API",
        "mcp_sse_endpoint": "/sse",
        "mcp_streamable_http_endpoint": "/mcp",
    }


@app.get("/api/health", tags=["Health"])
def api_health():
    return {
        "status": "ok",
        "service": "avocarbon-costing-backend",
    }
