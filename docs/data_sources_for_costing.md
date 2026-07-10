# Data Sources for Costing

## Backend Database: avocarbon_costing

`DATABASE_URL` points to the application-owned costing database.

Purpose:
- Store app-owned costing data.
- Store orchestration runs.
- Store customer inputs.
- Store BOM JSON.
- Store component costing JSON.
- Store MOST operation JSON.
- Store calculation results.

This is the database the FastAPI/backend should write to for operational costing workflow records.

## KPI_DB_Final via MCP 21 06 26

MCP 21 06 26 points to `KPI_DB_Final`.

Purpose:
- Master/reference data.
- `product_line`
- `product`
- `zone`
- `unit`
- `manufacturing_strategy`
- Factory DL/VOH/currency parameters.

The MCP selector name `people` targets `KPI_DB_Final`. This database is not the same as `DATABASE_URL`.

## Current V1 Behavior

If a direct `KPI_DB_FINAL_URL` connection is available, the Python backend should read master data from `KPI_DB_Final`.

If `KPI_DB_FINAL_URL` is not available, the backend uses CSV fallback:
- Product Matrix CSV for manufacturing strategy.
- Unit Table CSV for factory currency, DL, VOH, FOH/FEE, tax, and open-hours parameters.

Agents can later use MCP 21 06 26 directly for master-data reads/writes from the Agent UI when available.

## Uploaded Drawing Access for Workspace Agents

Uploaded customer drawing PDFs are stored by the backend under:
- `data/customer_inputs/uploads/{project_code}/{filename}`

The FastAPI app exposes them through:
- `/api/choke-costing/files/{project_code}/{filename}`

When the backend runs locally, generated URLs use the request base URL, for example:
- `http://127.0.0.1:8000/api/choke-costing/files/...`

Cloud Workspace Agents cannot access `localhost` or `127.0.0.1` on your machine. The preferred V1 handoff is Azure Blob Storage with a short-lived read-only SAS URL:
- `AZURE_STORAGE_CONNECTION_STRING`
- `AZURE_STORAGE_CONTAINER_NAME=choke-rfq-documents`
- `AZURE_BLOB_SAS_EXPIRY_HOURS=24`

When Azure Blob is configured, uploaded PDFs are copied to:
- `choke-rfq/{project_code}/{filename}`

The BOM Agent receives `drawing_file_url` as the SAS URL and `drawing_access_mode=azure_blob_sas`.

If Azure Blob is not configured, for real Workspace Agent runs you can temporarily configure:
- `PUBLIC_BASE_URL=https://xxxx.ngrok-free.app`

For local meeting tests:
- Run `ngrok http 8000`
- Set `PUBLIC_BASE_URL` to the ngrok HTTPS URL
- Restart the FastAPI app

If `PUBLIC_BASE_URL` is missing, the backend still triggers the BOM Agent, but warns that the drawing URL is local and not reachable from the cloud unless file attachment support is added.

## Important Separation

Do not use `DATABASE_URL` as if it were `KPI_DB_Final`.

`DATABASE_URL` is the backend costing database.

`KPI_DB_FINAL_URL` or MCP 21 06 26 is the master/reference data source.
