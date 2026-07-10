# Choke Costing Write-Back MCP Agent Setup

## Purpose

The **Choke Costing Write-Back MCP** lets ChatGPT Workspace Agents save their final JSON outputs back to the Choke backend so the workflow can continue automatically.

Azure Blob SAS is only for reading the drawing PDF. The write-back MCP is for saving JSON outputs.

## Public Backend URL Requirement

Workspace Agents cannot write back to `localhost`.

Deploy the MCP/backend behind a public HTTPS URL using Azure App Service or ngrok, then configure that URL in the Workspace Agent tool/action setup.

Example:

```text
PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
MCP URL=https://xxxx.ngrok-free.app/sse
```

When running locally on port `8000`, the backend exposes:

```text
http://127.0.0.1:8000/sse
http://127.0.0.1:8000/mcp
```

For the ChatGPT custom MCP screen, paste the public SSE URL:

```text
https://xxxx.ngrok-free.app/sse
```

With the current `.env` value at the time of verification, the URL to paste is:

```text
https://8eeb-41-224-4-231.ngrok-free.app/sse
```

If ngrok restarts, this URL changes. Run `python scripts/check_mcp_endpoint.py` and paste the printed `public URL`.

Use `/mcp` only if your client explicitly supports streamable HTTP MCP. ChatGPT custom MCP screens commonly ask for an SSE URL, so `/sse` is the preferred endpoint.

You can verify the mounted endpoint paths with:

```powershell
python scripts/check_mcp_endpoint.py
```

The OpenAPI action fallback is available at:

```text
docs/choke_agent_writeback_openapi.json
```

## Required MCP Tools

### save_bom_output

Use with: **Choke BOM Analyzer**

Input:

```json
{
  "project_code": "string",
  "product_id": "string",
  "raw_json": {}
}
```

Behavior:

```text
Equivalent to POST /api/choke-workflow/save-bom-output
```

### save_component_output

Use with: **External Component Costing Agent**

Input:

```json
{
  "project_code": "string",
  "product_id": "string",
  "component_id": "string",
  "raw_json": {}
}
```

Behavior:

```text
Equivalent to POST /api/choke-workflow/save-component-output
```

### save_most_output

Use with: **Estimateur MOST Assemblage**

Input:

```json
{
  "project_code": "string",
  "product_id": "string",
  "work_package_id": "string",
  "raw_json": {}
}
```

Behavior:

```text
Equivalent to POST /api/choke-workflow/save-most-output
```

### get_workflow_status

Use with: orchestration/debug agents, or manually during setup validation.

Input:

```json
{
  "project_code": "string",
  "product_id": "string"
}
```

Behavior:

```text
Equivalent to GET /api/choke-workflow/status/{project_code}/{product_id}
```

## Exact Agent Instructions

### Choke BOM Analyzer

```text
At the end of your analysis, call save_bom_output with:
- project_code from input
- product_id from input
- raw_json = your full final BOM JSON

Do not only return the JSON in chat. The backend workflow will not continue until this tool is called.
```

### External Component Costing Agent

```text
At the end of component costing, call save_component_output with:
- project_code from input
- product_id from input
- component_id from input
- raw_json = your full final component costing JSON

Do not only return the JSON in chat. The backend workflow will not continue until this tool is called.
```

### Estimateur MOST Assemblage

```text
At the end of MOST estimation, call save_most_output with:
- project_code from input
- product_id from input
- work_package_id from input
- raw_json = your full final MOST JSON

Do not only return the JSON in chat. The backend workflow will not continue until this tool is called.
```

## Tool Descriptions for Agent Builder

Use this description on all write-back tools:

```text
The agent must call this tool at the end of its analysis. The backend workflow will not continue until this tool is called.
```

## Local Verification

Run:

```powershell
cd C:\Users\youssef.benamor\avocarbon-costing-system\mcp_server
python scripts/test_choke_writeback_mcp.py
```

Then compile the workflow router and service:

```powershell
python -m py_compile app/routers/choke_workflow_router.py services/choke_sequential_agent_workflow.py
```
