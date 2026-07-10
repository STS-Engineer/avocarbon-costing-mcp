# Choke Agent Write-back Setup

This setup connects the Workspace Agents to the backend workflow so their final JSON is saved automatically. The choke workflow will stay blocked at `bom_triggered`, `components_triggering`, or `most_triggering` until the related write-back tool is called.

Azure Blob SAS is for reading the PDF drawing. `PUBLIC_BASE_URL` through ngrok or Azure App Service is for writing JSON results back to the backend.

## A. Public Backend URL Requirement

Workspace Agents cannot call `localhost` or `127.0.0.1`.

Expose the FastAPI backend through Azure App Service or a temporary tunnel such as ngrok, then use that HTTPS base URL in the action/OpenAPI configuration.

Example:

```text
PUBLIC_BASE_URL=https://xxxx.ngrok-free.app
```

The OpenAPI action schema is:

```text
docs/choke_agent_writeback_openapi.json
```

Replace the schema server URL with your public backend URL before importing it into the agent action/tool configuration.

## B. Agent Configuration

Add the backend write-back action/MCP/tool to these agents:

- Choke BOM Analyzer
- External Component Costing Agent
- Estimateur MOST Assemblage

The action exposes three tools:

- `save_bom_output`
- `save_component_output`
- `save_most_output`

Each agent should only call the tool that matches its role.

## C. Choke BOM Agent Instruction

Add this instruction to the Choke BOM Analyzer:

```text
At the end of your analysis, call save_bom_output with:
- project_code from input
- product_id from input
- raw_json = your full final BOM JSON
Do not only return the JSON in chat.
```

Required tool payload:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "raw_json": {}
}
```

## D. External Component Agent Instruction

Add this instruction to the External Component Costing Agent:

```text
At the end of component costing, call save_component_output with:
- project_code
- product_id
- component_id
- raw_json = your full final component costing JSON
```

Required tool payload:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "component_id": "ferrite_core",
  "raw_json": {}
}
```

## E. MOST Agent Instruction

Add this instruction to the Estimateur MOST Assemblage agent:

```text
At the end of MOST estimation, call save_most_output with:
- project_code
- product_id
- work_package_id
- raw_json = your full final MOST JSON
```

Required tool payload:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "work_package_id": "wp_10_winding",
  "raw_json": {}
}
```

## Verification

Run the local write-back flow test:

```powershell
python scripts/test_writeback_endpoints.py
```

Then compile the router and workflow service:

```powershell
python -m py_compile app/routers/choke_workflow_router.py services/choke_sequential_agent_workflow.py
```

Expected state progression:

```text
bom_triggered
-> bom_received
-> components_triggering
-> components_received
-> most_triggering
-> most_received
```
