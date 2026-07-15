import json
import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"bom-product-resolution-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(ROOT))

from services import choke_sequential_agent_workflow as workflow
from services.manufacturing_strategy import (
    get_canonical_products,
    select_manufacturing_strategy,
)
from services.project_data_paths import CUSTOMER_INPUT_DIR, atomic_write_json, portable_data_reference


PROJECT_CODE = "TEST-BOM-PRODUCT"
PRODUCT_ID = "UNKNOWN-PART-TEST-BOM-PRODUCT"


def sample_bom():
    return {
        "agent": "Choke BOM Analyzer",
        "source": {
            "drawing_title": "Choke / self avec fil émaillé + barre ferrite",
            "part_no": "316-5001",
        },
        "bom": [
            {
                "component_id": "ferrite_core",
                "component_type": "ferrite",
                "quantity": 1,
            },
            {
                "component_id": "magnet_wire",
                "component_type": "enameled wire",
                "quantity": 1,
            },
        ],
    }


def fake_unit_data(plant):
    if not plant:
        return {"status": "missing_unit_data", "plant": None}
    return {
        "status": "found",
        "plant": plant,
        "selling_currency": "RMB",
        "operating_currency": "RMB",
        "source": "test.unit",
    }


def main():
    canonical_products = get_canonical_products("Chokes")
    assert canonical_products == ["Rod choke", "Fuse chokes", "Torroid choke"], canonical_products
    actual_strategy = select_manufacturing_strategy(
        "Chokes", "Fuse chokes", "China South Pacific"
    )
    assert actual_strategy["status"] == "found", actual_strategy
    assert actual_strategy["production_plant"] == "Kunshan", actual_strategy

    customer_input = {
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "workflow_product_id": PRODUCT_ID,
        "product_line": "Chokes",
        "product": None,
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 60000,
        "currency": "RMB",
    }
    input_path = CUSTOMER_INPUT_DIR / f"{PROJECT_CODE}_{PRODUCT_ID}.json"
    atomic_write_json(input_path, customer_input)

    state = workflow._load_state(PROJECT_CODE, PRODUCT_ID)
    state.update({
        "input_file": portable_data_reference(input_path),
        "customer_input": customer_input,
        "status": "bom_triggered",
        "bom": {"status": "triggered"},
    })
    workflow._save_state(state)
    workflow.get_master_manufacturing_strategy = select_manufacturing_strategy
    workflow.get_master_unit_data = fake_unit_data

    extracted = workflow.extract_bom_technical_fields(sample_bom())
    assert extracted["canonical_product"] == "Fuse chokes", extracted
    assert extracted["part_number"] == "316-5001", extracted

    writeback = workflow.save_bom_output(PROJECT_CODE, PRODUCT_ID, sample_bom())
    assert writeback["success"] is True, writeback
    saved_state = workflow.get_workflow_state(PROJECT_CODE, PRODUCT_ID)
    assert saved_state["product_id"] == PRODUCT_ID, saved_state
    assert saved_state["customer_input"]["workflow_product_id"] == PRODUCT_ID, saved_state
    assert saved_state["customer_input"]["product"] == "Fuse chokes", saved_state
    assert saved_state["customer_input"]["product_name"] == "Fuse chokes", saved_state
    assert saved_state["production_plant"] == "Kunshan", saved_state
    assert saved_state["unit_data"]["plant"] == "Kunshan", saved_state

    saved_state["customer_input"]["product"] = None
    saved_state["customer_input"]["product_name"] = None
    workflow._save_state(saved_state)
    blocked = workflow.trigger_next_component_costing(
        PROJECT_CODE,
        PRODUCT_ID,
        dry_run=True,
    )
    assert blocked["status"] == "blocked", blocked
    assert blocked["missing_inputs"] == ["product"], blocked
    assert blocked["dependent_missing_inputs"] == ["production_plant"], blocked
    assert blocked["message"].startswith("Select a product first."), blocked

    unmapped = workflow.update_commercial_fields(
        PROJECT_CODE,
        PRODUCT_ID,
        {"product": "Unsupported choke"},
    )
    assert unmapped["status"] == "product_not_mapped", unmapped
    assert unmapped["manufacturing_strategy"]["product_received"] == "Unsupported choke", unmapped
    assert unmapped["manufacturing_strategy"]["available_product_candidates"] == canonical_products, unmapped

    update = workflow.update_commercial_fields(
        PROJECT_CODE,
        PRODUCT_ID,
        {"product": "Fuse choke"},
    )
    assert update["customer_input"]["product"] == "Fuse choke", update
    assert update["customer_input"]["product_name"] == "Fuse choke", update
    assert update["state"]["canonical_product"] == "Fuse chokes", update
    assert update["state"]["production_plant"] == "Kunshan", update
    assert update["state"]["product_id"] == PRODUCT_ID, update
    assert update["success"] is True, update
    assert update["saved_fields"]["product"] == "Fuse choke", update
    assert update["customer_input_path"], update
    assert update["workflow_state_path"], update
    persisted_input = json.loads(input_path.read_text(encoding="utf-8"))
    assert persisted_input["product"] == "Fuse choke", persisted_input
    assert persisted_input["product_name"] == "Fuse choke", persisted_input
    persisted_state = workflow.get_workflow_state(PROJECT_CODE, PRODUCT_ID)
    assert persisted_state["customer_input"]["product"] == "Fuse choke", persisted_state
    assert persisted_state["product"] == "Fuse choke", persisted_state

    triggered = workflow.trigger_next_component_costing(
        PROJECT_CODE,
        PRODUCT_ID,
        dry_run=True,
    )
    assert triggered["status"] == "component_agents_triggered", triggered
    assert triggered["component_triggers"], triggered
    assert triggered["state"]["product_id"] == PRODUCT_ID, triggered

    print("Canonical Chokes products:", ", ".join(canonical_products))
    print("Selected product for 316-5001: Fuse chokes")
    print("Production plant for China South Pacific: Kunshan")
    print("PASS: BOM product resolution, strategy refresh, Step 2 trigger, and product_id stability")


if __name__ == "__main__":
    try:
        main()
    finally:
        if TEST_ROOT.exists():
            shutil.rmtree(TEST_ROOT)
