import json
import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
TEMP_PARENT = ROOT_DIR / "data" / "_test_workflow_paths"
TEMP_PARENT.mkdir(parents=True, exist_ok=True)
TEST_DIR = TEMP_PARENT / f"no-parallel-{uuid.uuid4().hex}"
TEST_DIR.mkdir(parents=True, exist_ok=True)
DATA_ROOT = TEST_DIR / "canonical-data"
os.environ["DATA_ROOT"] = str(DATA_ROOT)

from fastapi.testclient import TestClient

import server
from app.main import app
from services.project_data_paths import CUSTOMER_INPUT_DIR, get_workflow_run_paths


PROJECT_CODE = "NO-PARALLEL-STATE"
PRODUCT_ID = "NO-PARALLEL-PRODUCT"


def main():
    CUSTOMER_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = CUSTOMER_INPUT_DIR / "no_parallel.json"
    input_path.write_text(json.dumps({
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "workflow_product_id": PRODUCT_ID,
        "product_line": "Chokes",
        "drawing_reference": "test.pdf",
        "drawing_file_path": "data/customer_inputs/uploads/no-parallel/test.pdf",
        "drawing_file_url": "https://example.com/test.pdf",
    }), encoding="utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/choke-workflow/start",
            json={"input_file": "data/customer_inputs/no_parallel.json", "dry_run": True},
        )
        assert response.status_code == 200, response.text

    original_cwd = Path.cwd()
    changed_cwd = TEST_DIR / "different-cwd"
    changed_cwd.mkdir()
    try:
        os.chdir(changed_cwd)
        saved = server.save_bom_output(PROJECT_CODE, PRODUCT_ID, {
            "bom": [
                {"component_id": "ferrite_core", "component": "Ferrite Core"},
                {"component_id": "magnet_wire", "component": "Magnet Wire"},
            ],
        })
    finally:
        os.chdir(original_cwd)

    assert saved.get("success") is True, saved
    state_path = get_workflow_run_paths(PROJECT_CODE, PRODUCT_ID)["workflow_state_path"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["input_file"] == "data/customer_inputs/no_parallel.json", state
    assert state["drawing_file_path"], state
    assert state["status"] == "bom_received", state
    state_files = list(TEST_DIR.rglob("workflow_state.json"))
    assert state_files == [state_path], state_files
    print("PASS write-back preserved start state and created no parallel state")
    print(state_path)
    shutil.rmtree(TEST_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
