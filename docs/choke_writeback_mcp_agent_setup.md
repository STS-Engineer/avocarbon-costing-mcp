# Choke Costing Write-Back Tools in MCP-KPI-Costing

## Purpose

Do **not** create a new MCP application.

Use the existing deployed MCP application:

```text
MCP-KPI-Costing
```

Azure MCP URL:

```text
https://mcp-costing.azurewebsites.net/mcp
```

These tools allow ChatGPT Workspace Agents to save their final JSON outputs back to the Choke backend so the workflow can continue automatically.

Azure Blob SAS is only for reading the drawing PDF. The write-back MCP is for saving JSON outputs.

## Agent Configuration

Add this same MCP application to:

- Choke BOM Analyzer
- External Component Costing Agent
- MOST Assemblage Agent

Use this MCP URL:

```text
https://mcp-costing.azurewebsites.net/mcp
```

Local development may expose:

```text
http://127.0.0.1:8000/mcp
http://127.0.0.1:8000/sse
```

Do not use localhost in ChatGPT Workspace Agents. Use the deployed Azure MCP URL.

## Required MCP Tools

### save_bom_output

Use with: **Choke BOM Analyzer**

The BOM Agent calls this once for the full choke drawing. The raw JSON must contain the full detected BOM and manufacturing requirements, including items such as:

- `ferrite_core`
- `magnet_wire`
- `tin_plating`
- `lead_tinning`
- `glue`
- `locking_element`
- `terminal_lead`
- `packaging_component`

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
Stores traceability in agent_json_records when available:
output_type = bom
object_id = bom
agent_name = choke_bom_agent
status = received
```

### save_component_output

Use with: **External Component Costing Agent**

The backend triggers this agent once per BOM component. Each run costs exactly one external component and calls this tool exactly once.

Do not make the External Component Costing Agent return all components in one JSON.

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
Stores traceability in agent_json_records when available:
output_type = component
object_id = component_id
agent_name = external_component_costing_agent
status = received
```

Supported component examples:

- `ferrite_core`
- `magnet_wire`
- `tin_plating`
- `lead_tinning`
- `glue`
- `locking_element`
- `terminal_lead`
- `packaging_component`

### save_most_output

Use with: **MOST Assemblage Agent**

The backend triggers MOST once per component/process scope. Each run estimates exactly one component/process scope and calls this tool exactly once.

Do not make the MOST Agent return all components/processes in one JSON.

Input:

```json
{
  "project_code": "string",
  "product_id": "string",
  "most_scope_id": "string",
  "raw_json": {}
}
```

Backward-compatible alias:

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
Stores traceability in agent_json_records when available:
output_type = most
object_id = most_scope_id or work_package_id
agent_name = most_assemblage_agent
status = received
```

Supported scope examples:

- `ferrite_core`
- `magnet_wire_winding`
- `glue_application_baking`
- `tin_plating_or_tinning`
- `locking_process`
- `electrical_test`
- `visual_inspection_packaging`

### get_choke_workflow_status

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

### calculate_choke_from_saved_outputs

Use after the BOM JSON, individual component JSONs, and individual MOST JSONs are saved.

Input:

```json
{
  "project_code": "string",
  "product_id": "string"
}
```

The calculation uses the saved individual JSON files and includes Olivier formulas:

```text
transport_cost_per_piece =
  sum for each component:
    BOM quantity * (
      transportation_cost
      + custom_duty_cost
      + forwarder_cost
    )

direct_cost_per_piece = DL + VOH + transport_cost_per_piece

foh_cost_per_piece = foh_percent_dc / 100 * direct_cost_per_piece

fee_cost_per_piece = fee_percent_dc / 100 * direct_cost_per_piece

manufacturing_cost_per_piece =
  direct_cost_per_piece + foh_cost_per_piece + fee_cost_per_piece
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

This run must contain exactly one component. Do not merge all components into one JSON.
Do not only return the JSON in chat. The backend workflow will not continue until this tool is called.
```

### MOST Assemblage Agent

```text
At the end of MOST estimation, call save_most_output with:
- project_code from input
- product_id from input
- most_scope_id from input, or work_package_id if that is the provided identifier
- raw_json = your full final MOST JSON

This run must contain exactly one component/process scope. Do not merge all operations into one JSON.
Do not only return the JSON in chat. The backend workflow will not continue until this tool is called.
```

## Tool Description for Agent Builder

Use this description on all write-back tools:

```text
The agent must call this tool at the end of its analysis. The backend workflow will not continue until this tool is called.
```

## Verification

Run:

```powershell
cd C:\Users\youssef.benamor\avocarbon-costing-system\mcp_server
python scripts/test_choke_writeback_mcp.py
python -m py_compile server.py app/main.py services/choke_sequential_agent_workflow.py services/choke_financial_calculation.py
```
