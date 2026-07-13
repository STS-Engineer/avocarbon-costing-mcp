# AVOCarbon Costing Backend Architecture

## Purpose

This repository is the backend and MCP repository for AVOCarbon Costing. It contains the FastAPI HTTP API, the MCP server used by Workspace Agents, Choke workflow orchestration, agent-output write-back, costing calculations, master-data access, Azure Blob document handling, and temporary static validation pages.

The future production UI will be a React/Vite application in a separate repository. The HTML pages in `app/static/` are legacy validation tools and remain available during the transition.

## Runtime entrypoints

### FastAPI application

- Module: `app/main.py`
- Development command: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- OpenAPI UI: `GET /docs`
- OpenAPI document: `GET /openapi.json`
- API health: `GET /api/health`
- Combined FastAPI/MCP health: `GET /health`

`app/main.py` registers all HTTP routers and mounts the MCP streamable HTTP and SSE transports without replacing the MCP server.

### MCP server

- Module: `server.py`
- MCP name: `AVOCarbon Costing MCP`
- Preferred endpoint: `POST /mcp` (streamable HTTP)
- Compatibility endpoints: `GET /sse` and `/messages/`
- Existing Azure MCP URL: `https://mcp-costing.azurewebsites.net/mcp`

When `server.py` is run directly, its root and health routes describe the MCP service. When `app.main:app` is run, FastAPI keeps `/mcp`, `/sse`, and `/messages` available alongside the REST API.

## HTTP routers

### Sequential Choke workflow

Router: `app/routers/choke_workflow_router.py`

- `POST /api/choke-workflow/start`: load a saved customer input and trigger the BOM stage.
- `GET /api/choke-workflow/status/{project_code}/{product_id}`: read workflow state.
- `POST /api/choke-workflow/save-bom-output`: save the BOM Agent JSON.
- `POST /api/choke-workflow/trigger-components`: trigger one external costing run per BOM component.
- `POST /api/choke-workflow/save-component-output`: save one component costing JSON.
- `POST /api/choke-workflow/trigger-most`: trigger one MOST run per component-operation scope.
- `POST /api/choke-workflow/save-most-output`: save one MOST JSON.
- `POST /api/choke-workflow/calculate-from-real-outputs`: build the larger orchestration envelope from saved outputs.
- `POST /api/choke-workflow/calculate-final`: calculate the final preliminary Choke result from saved outputs.
- `GET /api/choke-workflow/final-result/{project_code}/{product_id}`: read the saved final calculation.

### Customer input and legacy UI

Router: `app/routers/choke_costing_ui_router.py`

- Lists and creates file-backed customer inputs.
- Accepts the drawing PDF as part of customer-input creation.
- Uploads the PDF to Azure Blob when configured and retains a local fallback.
- Serves the legacy `/choke-costing` page and uploaded local PDFs.
- Exposes saved orchestration results.

### Choke orchestration and demos

Router: `app/routers/choke_orchestrator_router.py`

This router exposes direct orchestration, manual-output calculation, agent triggering, saved runs, and non-production demo payloads. It remains useful for backend tests and technical demonstrations.

### Agent integration compatibility API

Router: `app/routers/choke_agent_integration_router.py`

This compatibility API writes BOM, component, and MOST outputs through `agent_writeback_service.py`. New React workflow work should use `/api/choke-workflow/*` as the primary contract.

## Core services

- `choke_sequential_agent_workflow.py`: authoritative staged workflow state, agent triggers, file write-back, output normalization, and final saved-output calculation.
- `choke_orchestrator.py`: unified `avocarbon_choke_costing_v1` orchestration envelope.
- `choke_process_decomposition.py`: component-operation work-package planning.
- `choke_financial_calculation.py`: material, DL, VOH, tooling, and preliminary costing formulas.
- `workspace_agent_client.py`: Workspace Agent trigger client and dry-run behavior.
- `costing_master_data_service.py`: KPI master database access with CSV fallbacks.
- `manufacturing_strategy.py` and `unit_table_service.py`: CSV fallback readers.
- `azure_blob_storage_service.py`: PDF upload and read-only SAS URL generation.
- `agent_writeback_service.py`: compatibility write-back and status service.

## Persistence

### Backend database

`DATABASE_URL` points to the application-owned costing database. It is used by MCP database tools and optional agent-output traceability. File-backed Choke workflow state currently remains under `data/costing_runs/`.

### Master database

`KPI_DB_FINAL_URL`, when configured, points to KPI master/reference data such as products, zones, units, manufacturing strategy, currencies, and factory cost parameters. CSV files remain the V1 fallback.

### Workflow files

Each staged run uses:

```text
data/costing_runs/{project_code}/{product_id}/
  workflow_state.json
  agent_outputs/bom/raw_bom_agent_output.json
  agent_outputs/components/{component_id}.json
  agent_outputs/most/{work_package_id}.json
  final_choke_costing_result.json
```

## Azure Blob role

Azure Blob is document transport, not the costing database. Drawing PDFs uploaded by the customer-input API are stored in the configured container and exposed to cloud Workspace Agents through time-limited read-only SAS URLs. Agent JSON outputs return through MCP/REST write-back tools and are not read from Blob storage.

## CORS and frontend separation

`FRONTEND_ORIGINS` is a comma-separated allowlist. It defaults locally to:

- `http://localhost:5173`
- `http://127.0.0.1:5173`

Wildcard origins are ignored. Production deployments must configure the explicit React frontend origin.

## Deployment note

The FastAPI application exposes REST, OpenAPI, legacy HTML, and MCP together. A deployment that starts `server.py` directly is MCP-only and does not expose FastAPI `/docs` or `/api/*` routes. The backend deployment intended for the React application must start `app.main:app`.

Azure App Service startup command:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep the existing ChatGPT MCP URL ending in `/mcp`. Configure `FRONTEND_ORIGINS` with the explicit deployed React origin. Because workflow state is currently file-backed, the Azure deployment must use writable persistent App Service storage and should remain on one instance until workflow state is moved to PostgreSQL or Blob storage.
