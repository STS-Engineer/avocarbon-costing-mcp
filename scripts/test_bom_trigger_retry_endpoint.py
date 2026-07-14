import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"fastapi", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise

import services.choke_sequential_agent_workflow as workflow
from app.main import app
from bom_trigger_retry_test_support import accepted_202, event_names, retryable_409, unique_identity, write_customer_input


def main():
    os.environ["WORKSPACE_AGENT_TRIGGER_MAX_ATTEMPTS"] = "3"
    os.environ["WORKSPACE_AGENT_TRIGGER_BACKOFF_SECONDS"] = "0,0,0"
    project_code, product_id = unique_identity("BOM-RETRY-ENDPOINT")
    input_file = write_customer_input(project_code, product_id)
    original_trigger = workflow._trigger
    workflow._trigger = lambda *args, **kwargs: retryable_409()
    try:
        initial = workflow.start_real_choke_workflow(input_file, dry_run=False)
        assert initial["state"]["status"] == "bom_trigger_failed_retryable", initial
        workflow._trigger = lambda *args, **kwargs: accepted_202()
        response = TestClient(app).post("/api/choke-workflow/retry-bom", json={
            "project_code": project_code,
            "product_id": product_id,
        })
    finally:
        workflow._trigger = original_trigger

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "bom_triggered", payload
    assert payload["bom"]["status"] == "triggered", payload
    assert payload["trigger_attempts"][0]["http_status"] == 202, payload
    events = event_names(project_code, product_id)
    assert "retry_bom_requested" in events, events
    assert "bom_trigger_accepted" in events, events
    print("PASS POST /api/choke-workflow/retry-bom accepted a previously failed workflow")


if __name__ == "__main__":
    main()
