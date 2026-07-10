# Costing Agent Write-Back MCP Setup

## Purpose

This backend now exposes a Costing Agent Write-Back MCP so ChatGPT Workspace Agents can save their final JSON outputs automatically.

Use it for:

- Choke BOM Analyzer output
- External Component Costing Agent output
- MOST Assemblage operation output
- Reading received/missing output status
- Calculating a Choke costing run from saved agent outputs

This MCP is separate from `@MCP 21 06 26`.

- `@MCP 21 06 26` targets `KPI_DB_Final` and is used for read-only master/reference data such as product lines, products, zones, units, and manufacturing strategy.
- Costing Agent Write-Back MCP is owned by this backend and is used to save agent outputs into `data/costing_runs/...`.

## MCP Server URL

When running locally:

```text
http://127.0.0.1:8000/mcp
```

Start the MCP server with:

```powershell
cd C:\Users\youssef.benamor\avocarbon-costing-system\mcp_server
.\.venv\Scripts\python.exe server.py
```

If the browser UI or FastAPI app is already running on `8000`, start the MCP server on another port and use that port:

```powershell
$env:MCP_PORT = "8001"
.\.venv\Scripts\python.exe server.py
```

```text
http://127.0.0.1:8001/mcp
```

The same write-back features are also exposed through normal FastAPI endpoints:

```text
POST /api/agent-writeback/save-bom
POST /api/agent-writeback/save-component
POST /api/agent-writeback/save-most
GET  /api/agent-writeback/status/{project_code}/{product_id}
POST /api/agent-writeback/calculate
```

## MCP Tools

### save_choke_bom_result

Use this tool to save your final JSON output. Always call this tool before finishing.

Input:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "agent_name": "Choke BOM Analyzer",
  "raw_json": {}
}
```

Writes:

```text
data/costing_runs/{project_code}/{product_id}/agent_outputs/bom/raw_bom_agent_output.json
```

### save_component_costing_result

Use this tool to save your final JSON output. Always call this tool before finishing.

Input:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "component_id": "316-5001-ferrite",
  "component_type": "ferrite",
  "agent_name": "External Component Costing Agent",
  "raw_json": {}
}
```

Writes:

```text
data/costing_runs/{project_code}/{product_id}/agent_outputs/components/{component_id}.json
```

### save_most_operation_result

Use this tool to save your final JSON output. Always call this tool before finishing.

Input:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "work_package_id": "wp_10_winding",
  "component_id": "wire",
  "operation_id": "10",
  "operation_name": "winding",
  "agent_name": "Estimateur MOST Assemblage",
  "raw_json": {}
}
```

Writes:

```text
data/costing_runs/{project_code}/{product_id}/agent_outputs/most/{work_package_id}.json
```

### get_costing_run_status

Reads:

```text
data/costing_runs/{project_code}/{product_id}/agent_outputs/status.json
```

Returns received BOM, component, and MOST outputs, plus missing planned outputs when an orchestration plan exists.

### calculate_choke_from_saved_agent_outputs

Loads saved write-back files and produces:

```text
data/costing_runs/{project_code}/{product_id}/orchestration_result_from_saved_agent_outputs.json
```

Input:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "input_file": "data/customer_inputs/byd_3165001.json"
}
```

## Agent Instruction Snippets

Add this to the Choke BOM Analyzer:

```text
At the end of your analysis, call save_choke_bom_result with your final JSON.
Use this tool to save your final JSON output. Always call this tool before finishing.
Do not only display the JSON in chat.
```

Add this to the External Component Costing Agent:

```text
At the end of your analysis, call save_component_costing_result with your final JSON.
Use this tool to save your final JSON output. Always call this tool before finishing.
Do not only display the JSON in chat.
```

Add this to the MOST Assemblage Agent:

```text
At the end of your analysis, call save_most_operation_result with your final JSON.
Use this tool to save your final JSON output. Always call this tool before finishing.
Do not only display the JSON in chat.
```

## Optional Database Write

The write-back tools are file-first. They save outputs even when the backend database is unavailable.

If `save_to_database` is set to `true` and `DATABASE_URL` is configured, the backend also upserts into:

```text
costing_agent_outputs
```

For the current meeting proof, file write-back is enough to demonstrate the loop:

```text
Backend triggers Agent -> Agent calls write-back MCP -> Backend reads saved JSON -> Backend calculates
```
