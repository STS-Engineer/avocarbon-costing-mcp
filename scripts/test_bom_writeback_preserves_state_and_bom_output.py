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
from services.choke_sequential_agent_workflow import (
    get_bom_output,
    get_workflow_state,
    save_bom_output,
    start_real_choke_workflow,
)
from services.project_data_paths import COSTING_RUNS_DIR, CUSTOMER_INPUT_DIR, portable_data_reference


PROJECT_CODE = "BOM-STATE-MERGE-TEST"
PRODUCT_ID = "MERGE-CHOKE-001"


def main():
    upload_dir = CUSTOMER_INPUT_DIR / "uploads" / PROJECT_CODE
    upload_dir.mkdir(parents=True, exist_ok=True)
    drawing_path = upload_dir / "merge-test-drawing.pdf"
    drawing_path.write_bytes(b"%PDF-1.4\n% state merge regression fixture\n")

    CUSTOMER_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = CUSTOMER_INPUT_DIR / "__bom_state_merge_test.json"
    input_path.write_text(json.dumps({
        "project_code": PROJECT_CODE,
        "customer": "State merge customer",
        "product_line": "Chokes",
        "product": "Rod choke",
        "product_id": PRODUCT_ID,
        "workflow_product_id": PRODUCT_ID,
        "part_number": PRODUCT_ID,
        "drawing_reference": drawing_path.name,
        "drawing_file_path": portable_data_reference(drawing_path),
        "drawing_file_url": "https://example.invalid/merge-test-drawing.pdf",
        "drawing_access_mode": "public_test_url",
        "customer_delivery_zone": "Europe",
        "annual_quantity": 100000,
        "currency": "EUR",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    started = start_real_choke_workflow(
        input_file=portable_data_reference(input_path),
        dry_run=True,
        request_base_url="http://127.0.0.1:8000/",
    )
    start_state = started["state"]
    assert start_state["input_file"], start_state
    assert start_state["drawing_file_path"], start_state
    assert start_state["drawing_file_url"], start_state
    assert start_state["customer_input"], start_state
    assert start_state["bom"]["trigger_result"], start_state["bom"]

    trigger_result_before = start_state["bom"]["trigger_result"]
    drawing_path_before = start_state["drawing_file_path"]
    drawing_url_before = start_state["drawing_file_url"]
    input_file_before = start_state["input_file"]

    saved = save_bom_output(PROJECT_CODE, PRODUCT_ID, {
        "quote_information": {
            "product_name": "Rod choke",
            "part_number": PRODUCT_ID,
            "drawing_number": "MERGE-DRAWING-001",
        },
        "bom": [
            {"component_id": "ferrite_core", "component_type": "Ferrite core", "quantity": 1},
            {"component_id": "magnet_wire", "component_type": "Magnet wire", "quantity": 1},
            {"component_id": "lead_tinning", "component_type": "Lead tinning", "quantity": 2},
        ],
    })
    assert saved["state_merge"]["existing_state_found"] is True, saved["state_merge"]

    state = get_workflow_state(PROJECT_CODE, PRODUCT_ID)
    assert state["status"] == "bom_received", state
    assert state["input_file"] == input_file_before, state
    assert state["drawing_file_path"] == drawing_path_before, state
    assert state["drawing_file_url"] == drawing_url_before, state
    assert state["customer_input"], state
    assert state["bom"]["trigger_result"] == trigger_result_before, state["bom"]
    assert state["bom"]["drawing_file_path"], state["bom"]
    assert state["bom"]["drawing_file_url"], state["bom"]

    expected_missing = {
        "component:ferrite_core",
        "component:magnet_wire",
        "component:lead_tinning",
    }
    assert expected_missing <= set(state["missing_outputs"]), state["missing_outputs"]

    output = get_bom_output(PROJECT_CODE, PRODUCT_ID)
    assert output["status"] == "found", output
    assert output["raw_bom_available"] is True, output
    assert output["normalized_bom_available"] is True, output
    assert {item["component_id"] for item in output["components"]} == {
        "ferrite_core", "magnet_wire", "lead_tinning",
    }, output["components"]
    assert output["paths"]["resolved_raw_bom_path"], output["paths"]
    assert output["paths"]["resolved_normalized_bom_path"], output["paths"]

    client = TestClient(app)
    response = client.get(f"/api/choke-workflow/bom-output/{PROJECT_CODE}/{PRODUCT_ID}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "found", response.text

    raw_path = Path(output["paths"]["resolved_raw_bom_path"]).resolve()
    run_dir = (COSTING_RUNS_DIR / PROJECT_CODE / PRODUCT_ID).resolve()
    assert run_dir in raw_path.parents, raw_path
    backup_path = raw_path.with_suffix(".json.normalized-only-test")
    raw_path.replace(backup_path)
    try:
        normalized_only = get_bom_output(PROJECT_CODE, PRODUCT_ID)
        assert normalized_only["status"] == "found", normalized_only
        assert normalized_only["raw_bom_available"] is False, normalized_only
        assert normalized_only["normalized_bom_available"] is True, normalized_only
        assert len(normalized_only["components"]) == 3, normalized_only["components"]
    finally:
        backup_path.replace(raw_path)

    print("PASS BOM write-back preserved workflow start context")
    print("PASS nested BOM trigger and drawing metadata were preserved")
    print("PASS saved BOM endpoint returned raw, normalized, components and paths")
    print("PASS normalized-only BOM remains readable when raw output is unavailable")
    print("PASS lead_tinning remains a required component output")


if __name__ == "__main__":
    main()
