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
BASE = TEMP_PARENT / f"split-repair-{uuid.uuid4().hex}"
BASE.mkdir(parents=True, exist_ok=True)
CANONICAL_ROOT = BASE / "canonical"
LEGACY_REST_ROOT = BASE / "legacy-rest"
LEGACY_MCP_ROOT = BASE / "legacy-mcp"
os.environ["DATA_ROOT"] = str(CANONICAL_ROOT)
os.environ["LEGACY_DATA_ROOTS"] = f"{LEGACY_REST_ROOT},{LEGACY_MCP_ROOT}"

from scripts.repair_split_workflow_state import repair_split_workflow_state
from services.project_data_paths import get_workflow_run_paths


PROJECT_CODE = "SPLIT-STATE-TEST"
PRODUCT_ID = "SPLIT-PRODUCT"


def _state_path(root):
    return root / "costing_runs" / PROJECT_CODE / PRODUCT_ID / "workflow_state.json"


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def main():
    rest_state = _state_path(LEGACY_REST_ROOT)
    mcp_state = _state_path(LEGACY_MCP_ROOT)
    _write(rest_state, {
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "status": "bom_triggered",
        "current_step": "Step 1 BOM Agent",
        "input_file": "data/customer_inputs/split.json",
        "drawing_file_path": "data/customer_inputs/uploads/split.pdf",
        "drawing_file_url": "https://example.com/split.pdf",
        "customer_input": {"customer": "Test"},
        "created_at": "2026-07-14T14:42:00+00:00",
        "bom": {"status": "triggered", "trigger_result": {"status": "accepted"}},
    })
    _write(mcp_state, {
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "status": "bom_received",
        "current_step": "Step 2 External Component Costing Agent",
        "input_file": None,
        "drawing_file_path": None,
        "writeback_created_state_without_start": True,
        "bom": {
            "status": "received",
            "save_path": "data/costing_runs/SPLIT-STATE-TEST/SPLIT-PRODUCT/agent_outputs/bom/raw_bom_agent_output.json",
            "normalized_path": "data/costing_runs/SPLIT-STATE-TEST/SPLIT-PRODUCT/bom_normalized.json",
            "received_at": "2026-07-14T14:45:00+00:00",
        },
        "missing_outputs": ["component:ferrite_core", "component:magnet_wire"],
    })
    _write(mcp_state.parent / "agent_outputs" / "bom" / "raw_bom_agent_output.json", {
        "bom": [
            {"component_id": "ferrite_core", "component": "Ferrite Core"},
            {"component_id": "magnet_wire", "component": "Magnet Wire"},
        ]
    })

    dry_run = repair_split_workflow_state(PROJECT_CODE, PRODUCT_ID, apply=False)
    assert len(dry_run["states_found"]) == 2, dry_run
    applied = repair_split_workflow_state(PROJECT_CODE, PRODUCT_ID, apply=True)
    canonical = get_workflow_run_paths(PROJECT_CODE, PRODUCT_ID)["workflow_state_path"]
    state = json.loads(canonical.read_text(encoding="utf-8"))
    assert state["status"] == "bom_received", state
    assert state["input_file"] == "data/customer_inputs/split.json", state
    assert state["drawing_file_path"], state
    assert state["bom"]["status"] == "received", state
    assert state["writeback_created_state_without_start"] is False, state
    state_files = list(BASE.rglob("workflow_state.json"))
    assert state_files == [canonical], state_files
    assert applied["final_status"] == "bom_received", applied
    print("PASS split workflow states merged into one canonical state")
    print(canonical)
    shutil.rmtree(BASE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
