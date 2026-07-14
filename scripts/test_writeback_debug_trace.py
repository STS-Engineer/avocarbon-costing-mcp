import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"fastapi", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise

from app.main import app
from app.routers import choke_costing_ui_router
from services.project_data_paths import COSTING_RUNS_DIR


PROJECT_CODE = "WRITEBACK-DEBUG-TRACE"
PRODUCT_ID = "UNKNOWN-PART-DEBUG-TRACE"


def main():
    choke_costing_ui_router.is_azure_blob_configured = lambda: False
    client = TestClient(app)
    create_response = client.post(
        "/api/choke-costing/customer-inputs/create",
        data={
            "project_code": PROJECT_CODE,
            "product_line": "Chokes",
            "product_id": PRODUCT_ID,
        },
        files={"drawing_pdf": ("debug-trace.pdf", b"%PDF-1.4\n% debug trace\n", "application/pdf")},
    )
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()

    start_response = client.post("/api/choke-workflow/start", json={
        "input_file": created["input_file"],
        "dry_run": True,
    })
    assert start_response.status_code == 200, start_response.text
    assert start_response.json()["state"]["status"] == "bom_triggered", start_response.text

    save_response = client.post("/api/choke-workflow/save-bom-output", json={
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "raw_json": {
            "quote_information": {
                "product_name": "Debug trace choke",
                "part_number": "DEBUG-TRACE-001",
                "drawing_number": "DEBUG-DRAWING-001",
            },
            "bom": [
                {"component_id": "ferrite_core", "component_type": "Ferrite core", "quantity": 1},
                {"component_id": "magnet_wire", "component_type": "Magnet wire", "quantity": 1},
                {"component_id": "lead_tinning", "component_type": "Lead tinning", "quantity": 2},
            ],
        },
    })
    assert save_response.status_code == 200, save_response.text
    saved = save_response.json()
    assert saved["success"] is True, saved
    assert saved["tool"] == "save_bom_output", saved
    assert saved["state_exists_before_save"] is True, saved
    assert saved["state_status_before_save"] == "bom_triggered", saved
    assert saved["state_status_after_save"] == "bom_received", saved
    assert saved["raw_bom_saved"] is True, saved
    assert saved["normalized_bom_saved"] is True, saved

    status_response = client.get(f"/api/choke-workflow/status/{PROJECT_CODE}/{PRODUCT_ID}")
    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["status"] == "bom_received", status_response.text

    debug_response = client.get(f"/api/choke-workflow/debug/{PROJECT_CODE}/{PRODUCT_ID}")
    assert debug_response.status_code == 200, debug_response.text
    debug = debug_response.json()
    assert debug["workflow_state_exists"] is True, debug
    assert debug["raw_bom_exists"] is True, debug
    assert debug["normalized_bom_exists"] is True, debug
    assert set(debug["normalized_component_ids"]) == {
        "ferrite_core", "magnet_wire", "lead_tinning",
    }, debug

    event_path = COSTING_RUNS_DIR / PROJECT_CODE / PRODUCT_ID / "workflow_events.jsonl"
    assert event_path.exists(), event_path
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    event_names = {event["event"] for event in events}
    assert {
        "customer_input_saved",
        "workflow_started",
        "bom_agent_triggered",
        "save_bom_output_called",
        "save_bom_output_completed",
        "get_status_called",
        "get_debug_called",
    } <= event_names, event_names

    missing_response = client.get("/api/choke-workflow/status/NO-SUCH-WORKFLOW/NO-SUCH-PRODUCT")
    assert missing_response.status_code == 200, missing_response.text
    assert missing_response.json()["status"] == "not_found", missing_response.text

    print("PASS customer input, start and write-back share one workflow state")
    print("PASS save_bom_output returned full debug trace")
    print("PASS debug endpoint found raw and normalized BOM outputs")
    print("PASS workflow event log contains all required events")
    print("PASS unknown status returns not_found instead of synthetic created state")


if __name__ == "__main__":
    main()
