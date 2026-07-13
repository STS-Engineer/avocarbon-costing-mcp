import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.choke_sequential_agent_workflow import (
    calculate_from_real_outputs,
    save_bom_output,
    save_component_output,
    save_most_output,
    start_real_choke_workflow,
    trigger_most_operations,
    trigger_next_component_costing,
)


INPUT_FILE = "data/customer_inputs/byd_3165001.json"
PROJECT_CODE = "24003-CHO-00"
PRODUCT_ID = "316-5001"


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def assert_condition(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    section("START WORKFLOW: BOM ONLY")
    started = start_real_choke_workflow(INPUT_FILE, dry_run=True)
    state = started["state"]
    print(json.dumps({
        "status": state["status"],
        "bom": state["bom"],
        "components": state["components"],
        "most": state["most"],
    }, indent=2))
    assert_condition(state["status"] == "bom_triggered", "workflow should start at bom_triggered")
    assert_condition(state["bom"]["status"] == "triggered", "BOM should be triggered")
    assert_condition(state["components"] == {}, "components must not be triggered before BOM output")
    assert_condition(state["most"] == {}, "MOST must not be triggered before BOM output")

    fake_bom = {
        "status": "real_bom_agent_test_output",
        "technical_data": {
            "wire_diameter_mm": 1.25,
            "total_turns": 14,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5.2,
        },
        "components": [
            {
                "component_id": "ferrite_core",
                "component_type": "Ferrite Core",
                "quantity_per_product": 1,
                "diameter_mm": 5.2,
                "length_mm": 20.5,
                "material_family": "to_confirm",
            },
            {
                "component_id": "magnet_wire",
                "component_type": "Magnet Wire",
                "quantity_per_product": 1,
                "wire_diameter_mm": 1.25,
                "turns": 14,
                "scope_note": "raw enameled copper wire only",
            },
            {
                "component_id": "lead_tin_plating",
                "component_type": "Lead tin plating",
                "quantity_per_product": 2,
                "tin_thickness_micron": 20,
            },
        ],
    }

    section("SAVE FAKE BOM OUTPUT")
    bom_saved = save_bom_output(PROJECT_CODE, PRODUCT_ID, fake_bom)
    normalized = bom_saved["normalized_bom"]
    print(json.dumps({
        "state_status": bom_saved["state"]["status"],
        "external_components": [
            item["component_id"] for item in normalized["external_components"]
        ],
    }, indent=2))
    assert_condition(bom_saved["state"]["status"] == "bom_received", "BOM state should be received")
    assert_condition(
        {item["component_id"] for item in normalized["external_components"]} == {"ferrite_core", "magnet_wire", "lead_tin_plating"},
        "external component calls must be derived from saved BOM",
    )

    section("TRIGGER COMPONENT COSTING FROM SAVED BOM")
    components_triggered = trigger_next_component_costing(PROJECT_CODE, PRODUCT_ID, dry_run=True)
    component_keys = set(components_triggered["state"]["components"].keys())
    print(json.dumps(components_triggered["component_triggers"], indent=2))
    assert_condition(
        component_keys == {"ferrite_core", "magnet_wire", "lead_tin_plating"},
        "should trigger ferrite_core, magnet_wire and lead_tin_plating",
    )
    assert_condition("ferrite" not in component_keys and "wire" not in component_keys, "must not use old fallback component IDs")

    section("SAVE FAKE COMPONENT OUTPUTS")
    save_component_output(PROJECT_CODE, PRODUCT_ID, "ferrite_core", {
        "component_id": "ferrite_core",
        "component_type": "ferrite",
        "recommended_offer": {
            "reporting_currency": "CNY",
            "supply_chain": {"delivered_cost": 0.129},
        },
    })
    save_component_output(PROJECT_CODE, PRODUCT_ID, "magnet_wire", {
        "component_id": "magnet_wire",
        "component_type": "enameled_wire",
        "normalized_cost": {
            "currency": "CNY",
            "delivered_cost_per_piece": 0.333,
            "material_cost_per_piece": 0.328,
        },
    })
    component_saved = save_component_output(PROJECT_CODE, PRODUCT_ID, "lead_tin_plating", {
        "component_id": "lead_tin_plating",
        "component_type": "tin",
        "normalized_cost": {
            "currency": "CNY",
            "delivered_cost_per_piece": 0.001,
            "material_cost_per_piece": 0.001,
        },
    })
    print(json.dumps({
        "state_status": component_saved["state"]["status"],
        "missing_outputs": component_saved["state"]["missing_outputs"],
    }, indent=2))
    assert_condition(component_saved["state"]["status"] == "components_received", "components should be received")

    section("TRIGGER MOST FROM REAL BOM AND COMPONENT STAGE")
    most_triggered = trigger_most_operations(PROJECT_CODE, PRODUCT_ID, dry_run=True)
    most_keys = set(most_triggered["state"]["most"].keys())
    print(json.dumps({
        "most_keys": sorted(most_keys),
        "status": most_triggered["state"]["status"],
    }, indent=2))
    assert_condition("wp_10_winding" in most_keys, "MOST winding work package should be created")

    section("SAVE FAKE MOST OUTPUTS")
    most_state = most_triggered["state"]
    for work_package_id, info in most_state["most"].items():
        save_most_output(PROJECT_CODE, PRODUCT_ID, work_package_id, {
            "work_package_id": work_package_id,
            "component_id": info.get("component_id"),
            "operation_id": info.get("operation_id"),
            "operation_name": info.get("operation_name"),
            "p_h": 847.42 if work_package_id == "wp_10_winding" else 900,
            "oee": 0.8 if work_package_id != "wp_10_winding" else 0.75,
            "operator_percent": 25 if work_package_id == "wp_10_winding" else 100,
            "generic_capex_eur": 0,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_adder_per_piece_eur": 0.002 if work_package_id == "wp_10_winding" else 0,
        })

    section("CALCULATE FINAL ENVELOPE")
    envelope = calculate_from_real_outputs(PROJECT_CODE, PRODUCT_ID)
    financial = envelope.get("financial_calculation") or {}
    print(json.dumps({
        "schema_version": envelope.get("schema_version"),
        "financial_status": financial.get("status"),
        "material_cost_per_piece": financial.get("material_cost_per_piece"),
        "dl_cost_per_piece": financial.get("dl_cost_per_piece"),
        "voh_cost_per_piece": financial.get("voh_cost_per_piece"),
        "direct_cost": financial.get("preliminary_direct_cost_per_piece"),
        "output_path": envelope.get("orchestration_result_real_agent_chain_path"),
        "missing_inputs": envelope.get("missing_inputs"),
    }, indent=2))
    assert_condition(envelope.get("schema_version") == "avocarbon_choke_costing_v1", "standard envelope expected")
    assert_condition(financial.get("material_cost_per_piece") is not None, "material cost should be calculated from saved outputs")
    assert_condition(envelope.get("orchestration_result_real_agent_chain_path"), "real-chain result path should be saved")


if __name__ == "__main__":
    main()
