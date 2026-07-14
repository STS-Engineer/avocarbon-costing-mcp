import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import (
    _save_state,
    get_workflow_state,
    save_bom_output,
)


def main():
    suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
    project_code = f"BOM-RECEIVED-PRECEDENCE-{suffix}"
    product_id = f"PART-{suffix}"
    trigger_failure = {
        "stage": "bom",
        "trigger_result": {
            "status": "failed",
            "http_status": 409,
            "error": "The workspace agent trigger is not currently available.",
        },
    }
    _save_state({
        "project_code": project_code,
        "product_id": product_id,
        "status": "bom_trigger_failed_retryable",
        "current_step": "Step 1 BOM Agent",
        "retry_available": True,
        "errors": [trigger_failure],
        "bom": {
            "status": "trigger_failed_retryable",
            "retryable": True,
            "trigger_result": trigger_failure["trigger_result"],
        },
        "components": {},
        "most": {},
        "customer_input": {},
    })

    result = save_bom_output(project_code, product_id, {
        "bom": [
            {"component_id": "ferrite_core", "component": "Ferrite core", "quantity": 1},
            {"component_id": "magnet_wire", "component": "Magnet wire", "quantity": 1},
            {"component_id": "lead_tinning", "component": "Lead tinning", "quantity": 1},
        ]
    })
    state = get_workflow_state(project_code, product_id)

    assert result["state"]["status"] == "bom_received"
    assert state["status"] == "bom_received"
    assert state["current_step"] == "Step 2 External Component Costing Agent"
    assert state["bom"]["status"] == "received"
    assert state["bom"]["retryable"] is False
    assert state["retry_available"] is False
    assert state["errors"] == []
    assert trigger_failure in state["historical_errors"]
    assert state["bom"]["trigger_result"]["resolved_by_writeback"] is True
    assert state["bom"]["trigger_result"]["effective_status"] == "received"
    assert state["missing_outputs"] == [
        "component:ferrite_core",
        "component:magnet_wire",
        "component:lead_tinning",
    ]
    print("PASS received BOM clears retryable failure and archives historical 409")


if __name__ == "__main__":
    main()
