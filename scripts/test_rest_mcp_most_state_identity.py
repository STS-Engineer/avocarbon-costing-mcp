from fastapi.testclient import TestClient

from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, successful_most, workflow
from app.main import app

try:
    project, product = create_most_ready_workflow()
    install_most_trigger_spy()
    triggered = workflow.trigger_most_operations(project, product, dry_run=True)
    work_package_id = triggered["process_decomposition"]["required_work_package_ids"][0]
    service_result = workflow.save_most_output(project, product, work_package_id, successful_most(work_package_id))
    client = TestClient(app)
    rest = client.get(f"/api/choke-workflow/most-output/{project}/{product}/{work_package_id}")
    assert rest.status_code == 200, rest.text
    payload = rest.json()
    assert payload["normalized_most"]["work_package_id"] == work_package_id, payload
    status = client.get(f"/api/choke-workflow/status/{project}/{product}").json()
    assert status["most"][work_package_id]["normalized_path"] == service_result["state"]["most"][work_package_id]["normalized_path"], status
    print("PASS: REST and MCP/service MOST write-back use identical state and paths")
finally:
    cleanup()
