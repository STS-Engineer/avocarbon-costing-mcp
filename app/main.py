from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.choke_agent_integration_router import router as choke_agent_integration_router
from app.routers.choke_costing_ui_router import router as choke_costing_ui_router
from app.routers.choke_orchestrator_router import router as choke_orchestrator_router
from app.routers.choke_workflow_router import router as choke_workflow_router
from server import mcp


mcp_sse_app = mcp.sse_app()
mcp_streamable_http_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="AVOCarbon Costing API", lifespan=lifespan)
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
