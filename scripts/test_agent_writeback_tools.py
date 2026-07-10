import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.agent_writeback_service import (
    calculate_choke_from_saved_agent_outputs,
    get_costing_run_status,
    save_choke_bom_result,
    save_component_costing_result,
    save_most_operation_result,
)


PROJECT_CODE = "24003-CHO-00"
PRODUCT_ID = "316-5001"
INPUT_FILE = "data/customer_inputs/byd_3165001.json"


def print_section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main():
    fake_bom = {
        "status": "agent_test_output",
        "technical_data": {
            "wire_diameter_mm": 1.25,
            "total_turns": 14,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5.2,
            "ferrite_length_mm": 20.5,
            "glue_requirement": "to_confirm",
        },
        "components": [
            {
                "component_id": "316-5001-ferrite",
                "component_type": "ferrite",
                "quantity_per_product": 1,
                "costing_route": "external_component_costing_agent",
                "bom_definition": {
                    "diameter_mm": 5.2,
                    "length_mm": 20.5,
                    "material_family": "to_confirm",
                },
            },
            {
                "component_id": "316-5001-wire",
                "component_type": "enameled_wire",
                "quantity_per_product": 1,
                "costing_route": "external_component_costing_agent",
                "bom_definition": {
                    "wire_diameter_mm": 1.25,
                    "turns": 14,
                    "scope_note": "raw enameled wire only",
                },
            },
            {
                "component_id": "316-5001-tin",
                "component_type": "tin",
                "quantity_per_product": 2,
                "costing_route": "material_price_lookup",
                "bom_definition": {
                    "tin_thickness_micron": 20,
                },
            },
        ],
    }

    fake_ferrite = {
        "component_id": "316-5001-ferrite",
        "component_type": "ferrite",
        "status": "test_preliminary",
        "recommended_offer": {
            "origin": "China",
            "reporting_currency": "CNY",
            "supply_chain": {
                "delivered_cost": 0.129,
            },
        },
    }

    fake_wire = {
        "component_id": "316-5001-wire",
        "component_type": "enameled_wire",
        "status": "test_preliminary",
        "normalized_cost": {
            "currency": "CNY",
            "delivered_cost_per_piece": 0.333,
            "material_cost_per_piece": 0.328,
            "commercially_usable": False,
        },
    }

    fake_most_winding = {
        "work_package_id": "wp_10_winding",
        "component_id": "wire",
        "operation_id": "10",
        "operation_name": "winding",
        "p_h": 847.42,
        "oee": 0.75,
        "operator_percent": 25,
        "generic_capex_eur": 0,
        "specific_capex_eur": 15000,
        "tooling_cost_eur": 2500,
        "tooling_life_pieces": 250000,
        "tooling_adder_per_piece_eur": 0.002,
    }

    print_section("SAVE FAKE BOM OUTPUT")
    print(json.dumps(save_choke_bom_result(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        agent_name="Choke BOM Analyzer",
        raw_json=fake_bom,
    ), indent=2))

    print_section("SAVE FAKE FERRITE COMPONENT OUTPUT")
    print(json.dumps(save_component_costing_result(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        component_id="316-5001-ferrite",
        component_type="ferrite",
        agent_name="External Component Costing Agent",
        raw_json=fake_ferrite,
    ), indent=2))

    print_section("SAVE FAKE WIRE COMPONENT OUTPUT")
    print(json.dumps(save_component_costing_result(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        component_id="316-5001-wire",
        component_type="enameled_wire",
        agent_name="External Component Costing Agent",
        raw_json=fake_wire,
    ), indent=2))

    print_section("SAVE FAKE MOST WINDING OUTPUT")
    print(json.dumps(save_most_operation_result(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        work_package_id="wp_10_winding",
        component_id="wire",
        operation_id="10",
        operation_name="winding",
        agent_name="Estimateur MOST Assemblage",
        raw_json=fake_most_winding,
    ), indent=2))

    print_section("READ STATUS")
    status = get_costing_run_status(PROJECT_CODE, PRODUCT_ID)
    print(json.dumps(status, indent=2))

    print_section("CALCULATE FROM SAVED OUTPUTS")
    envelope = calculate_choke_from_saved_agent_outputs(
        project_code=PROJECT_CODE,
        product_id=PRODUCT_ID,
        input_file=INPUT_FILE,
    )
    financial = envelope.get("financial_calculation") or {}
    print(f"schema_version: {envelope.get('schema_version')}")
    print(f"project_code: {envelope.get('project', {}).get('project_code')}")
    print(f"material_cost_per_piece: {financial.get('material_cost_per_piece')}")
    print(f"dl_cost_per_piece: {financial.get('dl_cost_per_piece')}")
    print(f"voh_cost_per_piece: {financial.get('voh_cost_per_piece')}")
    print(f"status: {financial.get('status')}")
    print("missing_inputs:")
    for item in financial.get("missing_inputs") or []:
        print(f"- {item}")
    print("saved_result:")
    print(envelope.get("orchestration_result_from_saved_agent_outputs_path"))

    result_path = Path(envelope.get("orchestration_result_from_saved_agent_outputs_path", ""))
    if not result_path.exists():
        raise SystemExit("Expected calculated result file was not created.")


if __name__ == "__main__":
    main()
