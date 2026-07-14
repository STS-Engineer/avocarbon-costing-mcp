import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import server
from services.choke_sequential_agent_workflow import (
    get_workflow_state,
    get_writeback_debug,
)


PROJECT_CODE = "RFQ-20260714-140229"
PRODUCT_ID = "UNKNOWN-PART-20260714-140229"


def sample_bom():
    return {
        "schema_version": "avocarbon_choke_bom_v1",
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "bom": [
            {
                "component_id": "ferrite_core",
                "component": "Ferrite Core",
                "category": "Magnetic Component",
                "quantity_per_product": 1,
            },
            {
                "component_id": "magnet_wire",
                "component": "Magnet Wire",
                "category": "Copper Wire",
                "quantity_per_product": 1,
            },
            {
                "component_id": "lead_tinning",
                "component": "Lead Tinning",
                "category": "Tinning",
                "quantity_per_product": 1,
            },
        ],
    }


def main():
    response = server.save_bom_output(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        raw_json=sample_bom(),
    )
    assert response.get("success") is True, response
    assert response.get("status") == "saved", response

    state = get_workflow_state(PROJECT_CODE, PRODUCT_ID)
    assert state.get("status") == "bom_received", state
    assert (state.get("bom") or {}).get("status") == "received", state
    assert "bom" not in (state.get("missing_outputs") or []), state

    debug = get_writeback_debug(PROJECT_CODE, PRODUCT_ID)
    assert debug.get("raw_bom_exists") is True, debug
    assert debug.get("normalized_bom_exists") is True, debug
    assert set(debug.get("component_ids") or []) == {
        "ferrite_core",
        "magnet_wire",
        "lead_tinning",
    }, debug

    print("LIVE EXISTING BOM WRITE-BACK TEST")
    print(json.dumps({
        "tool_response": {
            key: response.get(key)
            for key in [
                "success",
                "status",
                "tool",
                "project_code",
                "product_id",
                "workflow_state_path",
                "state_exists_before",
                "state_status_before",
                "state_status_after",
                "raw_bom_saved",
                "normalized_bom_saved",
                "component_ids",
            ]
        },
        "workflow_status": state.get("status"),
        "bom_status": (state.get("bom") or {}).get("status"),
        "missing_outputs": state.get("missing_outputs"),
        "writeback_debug": {
            "raw_bom_path": debug.get("raw_bom_path"),
            "raw_bom_exists": debug.get("raw_bom_exists"),
            "normalized_bom_path": debug.get("normalized_bom_path"),
            "normalized_bom_exists": debug.get("normalized_bom_exists"),
            "component_ids": debug.get("component_ids"),
            "latest_writeback_error": debug.get("latest_writeback_error"),
        },
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
