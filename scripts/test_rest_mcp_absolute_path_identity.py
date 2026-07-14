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
TEST_DIR = TEMP_PARENT / f"path-identity-{uuid.uuid4().hex}"
TEST_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_ROOT"] = str(TEST_DIR / "canonical-data")

from fastapi.testclient import TestClient

import server
from app.main import app
from services.project_data_paths import CUSTOMER_INPUT_DIR


PROJECT_CODE = "PATH-IDENTITY-TEST"
PRODUCT_ID = "PATH-IDENTITY-PRODUCT"


def main():
    CUSTOMER_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = CUSTOMER_INPUT_DIR / "path_identity.json"
    input_path.write_text(json.dumps({
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "workflow_product_id": PRODUCT_ID,
        "product_line": "Chokes",
        "drawing_reference": "test.pdf",
        "drawing_file_path": "data/customer_inputs/uploads/test.pdf",
        "drawing_file_url": "https://example.com/test.pdf",
    }), encoding="utf-8")

    with TestClient(app) as client:
        start = client.post(
            "/api/choke-workflow/start",
            json={"input_file": "data/customer_inputs/path_identity.json", "dry_run": True},
        )
        assert start.status_code == 200, start.text
        start_payload = start.json()
        rest_start_path = start_payload["path_diagnostics"]["resolved_workflow_state_path"]

        status = client.get(f"/api/choke-workflow/status/{PROJECT_CODE}/{PRODUCT_ID}")
        assert status.status_code == 200, status.text
        rest_status_path = status.json()["canonical_workflow_state_path"]

    save = server.save_bom_output(PROJECT_CODE, PRODUCT_ID, {
        "bom": [{"component_id": "ferrite_core", "component": "Ferrite Core"}],
    })
    assert save.get("success") is True, save
    mcp_save_path = save["workflow_state_path"]
    mcp_status = server.get_choke_workflow_status(PROJECT_CODE, PRODUCT_ID)
    mcp_status_path = mcp_status["canonical_workflow_state_path"]

    compared = [rest_start_path, rest_status_path, mcp_save_path, mcp_status_path]
    assert len(set(compared)) == 1, compared
    assert all(Path(path).is_absolute() for path in compared), compared
    print("PASS REST/MCP workflow paths are identical and absolute")
    print(rest_start_path)
    shutil.rmtree(TEST_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
