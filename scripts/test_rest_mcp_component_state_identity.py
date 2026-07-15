from fastapi.testclient import TestClient

from component_step2_test_support import cleanup, create_workflow, successful_raw, workflow
from app.main import app

try:
    project, product = create_workflow()
    mcp_result = workflow.save_component_output(project, product, "ferrite_core", successful_raw("ferrite_core"))
    client = TestClient(app)
    rest = client.get(f"/api/choke-workflow/component-output/{project}/{product}/ferrite_core")
    assert rest.status_code == 200, rest.text
    payload = rest.json()
    assert payload["normalized_component"]["component_id"] == "ferrite_core", payload
    status = client.get(f"/api/choke-workflow/status/{project}/{product}").json()
    assert status["components"]["ferrite_core"]["normalized_path"] == mcp_result["state"]["components"]["ferrite_core"]["normalized_path"], status
    print("PASS: REST and MCP/service write-back use the identical component state and paths")
finally:
    cleanup()
