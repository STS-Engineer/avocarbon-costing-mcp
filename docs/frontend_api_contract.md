# React Frontend API Contract

## General conventions

- Local base URL: `http://127.0.0.1:8000`
- JSON endpoints use `Content-Type: application/json`.
- Customer input creation uses `multipart/form-data` because it includes the drawing PDF.
- The frontend should retain `input_file`, `project_code`, and `product_id` returned by the backend.
- An unknown workflow currently returns an initialized `created` state. The frontend should treat that as not started.
- Costing is asynchronous across agent stages. Poll status after triggering a stage.

## Health and API discovery

### Backend health

`GET /api/health`

Response:

```json
{
  "status": "ok",
  "service": "avocarbon-costing-backend"
}
```

FastAPI documentation is available at `GET /docs`; the machine-readable schema is at `GET /openapi.json`.

## Customer input and drawing

### List saved customer inputs

`GET /api/choke-costing/customer-inputs`

Response example:

```json
[
  {
    "id": "24003-CHO-00_316-5001",
    "file": "data/customer_inputs/24003-CHO-00_316-5001.json",
    "project_code": "24003-CHO-00",
    "product_id": "316-5001",
    "drawing_file_url": "https://storage.example/drawing.pdf?...",
    "technical_fields_extracted_from_bom": false
  }
]
```

### Create customer input and upload drawing PDF

`POST /api/choke-costing/customer-inputs/create`

Content type: `multipart/form-data`

Form example:

```text
customer=Zhejiang NBT
customer_delivery_zone=China South Pacific
annual_quantity=600000
currency=RMB
target_price=1.5
project_code=24003-CHO-00
final_customer=BYD
product=Fuse choke
product_id=316-5001
part_number=316-5001
sop_date=
drawing_pdf=<binary PDF>
```

Response example:

```json
{
  "status": "saved",
  "input_file": "data/customer_inputs/24003-CHO-00_316-5001.json",
  "customer_input": {
    "project_code": "24003-CHO-00",
    "product_id": "316-5001",
    "drawing_access_mode": "azure_blob_sas",
    "drawing_file_url": "https://storage.example/drawing.pdf?..."
  }
}
```

Notes: this is both the customer-input save endpoint and the drawing upload endpoint. The React form should require customer, delivery zone, annual quantity, currency, and PDF. The backend can generate temporary project/product identifiers when optional technical identifiers are absent.

## Sequential real-agent workflow

### Start the real agent chain

`POST /api/choke-workflow/start`

Request:

```json
{
  "input_file": "data/customer_inputs/24003-CHO-00_316-5001.json",
  "dry_run": false
}
```

Response example:

```json
{
  "message": "BOM Agent triggered first. Waiting for BOM output write-back.",
  "state": {
    "project_code": "24003-CHO-00",
    "product_id": "316-5001",
    "status": "bom_triggered",
    "current_step": "Step 1 BOM Agent",
    "missing_outputs": ["bom"]
  },
  "trigger_report": {
    "bom": {"status": "triggered"},
    "components_triggered": [],
    "most_triggered": []
  }
}
```

Notes: `dry_run: true` builds the trigger request without calling the Workspace Agent API.

### Get workflow status

`GET /api/choke-workflow/status/{project_code}/{product_id}`

Response example:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "status": "components_received",
  "current_step": "Step 3 MOST Agent",
  "bom": {"status": "received"},
  "components": {
    "ferrite_core": {"status": "received"}
  },
  "most": {},
  "missing_outputs": []
}
```

Poll this endpoint after each real trigger. The UI should render `status`, `current_step`, per-item statuses, errors, warnings, and `missing_outputs`.
For an unknown project/product pair, the current service returns a default state with `status: "created"`.

### Get saved BOM output

`GET /api/choke-workflow/bom-output/{project_code}/{product_id}`

Response example:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "status": "found",
  "raw_bom": {"bom": []},
  "normalized_bom": {"components": []},
  "components": [
    {
      "component_id": "ferrite_core",
      "component": "Ferrite core",
      "quantity_per_product": 1,
      "category": "ferrite"
    }
  ],
  "process_scopes_for_most": [],
  "points_to_confirm": []
}
```

Call this endpoint when workflow status reports `bom_received`. It reads both raw and normalized backend files and supports BOM lines under `bom`, `components`, `line_items`, or `bill_of_material`.

### Update commercial fields

`POST /api/choke-workflow/update-commercial-fields`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "customer": "Zhejiang NBT",
  "final_customer": "BYD",
  "customer_delivery_zone": "China South Pacific",
  "annual_quantity": 600000,
  "currency": "RMB",
  "target_price": 1.5,
  "sop_date": null
}
```

This updates both the saved customer-input JSON and active workflow context. Annual quantity, delivery zone, and currency are required before component costing, but not before BOM analysis.

### Save BOM output manually

`POST /api/choke-workflow/save-bom-output`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "raw_json": {
    "product_name": "Fuse choke",
    "part_number": "316-5001",
    "components": [
      {"component_id": "ferrite_core", "component_type": "ferrite", "quantity": 1}
    ]
  }
}
```

Response example:

```json
{
  "status": "saved",
  "normalized_bom": {"external_components": []},
  "state": {"status": "bom_received", "current_step": "Step 2 External Component Costing Agent"}
}
```

This endpoint is normally called by the BOM Agent MCP tool. Keep it available in the React UI as a manual fallback.

### Trigger component costing

`POST /api/choke-workflow/trigger-components`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "dry_run": false
}
```

Response example:

```json
{
  "status": "components_triggered",
  "component_triggers": [
    {
      "component_id": "ferrite_core",
      "status": "triggered",
      "save_path": "data/costing_runs/24003-CHO-00/316-5001/agent_outputs/components/ferrite_core.json"
    }
  ],
  "state": {"status": "components_triggering"}
}
```

The BOM must already be received. One agent call is created per external component.
If commercial inputs are incomplete, the endpoint returns HTTP 200 with `status: "blocked"`, a `missing_inputs` list, and the message `Complete commercial fields before external component costing.`

### Save one component output manually

`POST /api/choke-workflow/save-component-output`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "component_id": "ferrite_core",
  "raw_json": {
    "component_id": "ferrite_core",
    "delivered_cost_per_piece": 0.129,
    "transportation_cost": 0.005,
    "custom_duty_cost": 0,
    "forwarder_cost": 0.001,
    "currency": "CNY"
  }
}
```

Response example:

```json
{
  "status": "saved",
  "component_id": "ferrite_core",
  "state": {"status": "components_received", "missing_outputs": []}
}
```

Save each component separately. Do not combine multiple component results in one request.

### Trigger MOST

`POST /api/choke-workflow/trigger-most`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "dry_run": false
}
```

Response example:

```json
{
  "status": "most_triggered",
  "most_triggers": [
    {
      "work_package_id": "magnet_wire_winding",
      "operation_name": "Winding",
      "status": "triggered"
    }
  ],
  "state": {"status": "most_triggering"}
}
```

All required component outputs must be received first.

### Save one MOST output manually

`POST /api/choke-workflow/save-most-output`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "work_package_id": "magnet_wire_winding",
  "raw_json": {
    "work_package_id": "magnet_wire_winding",
    "operation_name": "Winding",
    "p_h": 1200,
    "oee": 80,
    "operator_percent": 100
  }
}
```

Response example:

```json
{
  "status": "saved",
  "work_package_id": "magnet_wire_winding",
  "state": {"status": "most_received", "current_step": "Step 4 Cost Calculation"}
}
```

Save each component-operation scope separately.

## Final calculation and result

### Calculate final result

`POST /api/choke-workflow/calculate-final`

Request:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001"
}
```

An optional `unit_data` object may be supplied for controlled tests. Production should normally use the unit data already selected in workflow state.

Response example:

```json
{
  "project_code": "24003-CHO-00",
  "product_id": "316-5001",
  "status": "calculated",
  "currency": "CNY",
  "material_cost_per_piece": 0.464,
  "transport_cost_per_piece": 0.0102,
  "dl_cost_per_piece": 0.0375,
  "voh_cost_per_piece": 0.045,
  "direct_cost_per_piece": 0.0927,
  "foh_percent_dc": 77,
  "foh_cost_per_piece": 0.071379,
  "fee_percent_dc": 56,
  "fee_cost_per_piece": 0.051912,
  "manufacturing_cost_per_piece": 0.215991,
  "missing_inputs": [],
  "warnings": []
}
```

If required saved outputs or plant data are absent, the endpoint returns `status: "blocked"` with `missing_inputs`; this is a business state, not necessarily an HTTP error.

### Get saved final result

`GET /api/choke-workflow/final-result/{project_code}/{product_id}`

Response: the same final calculation object returned by `calculate-final`.

Returns `404` until the final calculation has been run successfully or blocked result data has been saved.

### Get saved orchestration envelope

`GET /api/choke-costing/result/{project_code}/{product_id}`

Response: the saved `orchestration_result.json` envelope. This is broader than the dedicated final-result endpoint and is useful for diagnostics.

## Legacy and technical endpoints

- `POST /api/choke-costing/run` supports the legacy static UI modes `instant` and `trigger_agents`.
- `/api/choke-orchestrator/*` supports demos and direct orchestration tests.
- `/api/agent-writeback/*` is a compatibility surface for the older write-back service.
- `/choke-costing` is the legacy validation UI. The future production UI will be React/Vite in a separate repository.

The React frontend should build new workflow screens against `/api/choke-costing/customer-inputs/*` and `/api/choke-workflow/*`.
