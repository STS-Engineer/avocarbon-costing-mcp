import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

try:
    from services.choke_sequential_agent_workflow import (
        calculate_final_choke_costing_from_saved_outputs,
        save_bom_output,
        save_component_output,
        save_most_output,
        start_real_choke_workflow,
        trigger_most_operations,
    )
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"fastapi", "mcp", "dotenv", "psycopg2", "sqlalchemy"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise


def assert_close(actual: float, expected: float, label: str, tolerance: float = 1e-9) -> None:
    if actual is None or not math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def write_customer_input(project_code: str, product_id: str) -> str:
    path = ROOT_DIR / "data" / "customer_inputs" / "__final_calculation_test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_code": project_code,
        "customer": "Final calculation test customer",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": product_id,
        "workflow_product_id": product_id,
        "part_number": product_id,
        "drawing_reference": "final-calc-test.pdf",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "CNY",
        "target_price": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.relative_to(ROOT_DIR).as_posix()


def bom_json() -> Dict[str, Any]:
    return {
        "components": [
            {
                "component_id": "ferrite_core",
                "component_type": "Ferrite Core",
                "quantity_per_product": 1,
            },
            {
                "component_id": "magnet_wire",
                "component_type": "Magnet Wire",
                "quantity_per_product": 1,
            },
            {
                "component_id": "glue",
                "component_type": "Glue",
                "quantity_per_product": 1,
                "costing_route": "external_component_costing_agent",
            },
        ],
        "technical_data": {
            "wire_diameter_mm": 1.18,
            "turns": 11,
            "tin_thickness_micron": 20,
        },
    }


def component_json(component_id: str, delivered_cost: float, transportation: float, duty: float, forwarder: float) -> Dict[str, Any]:
    return {
        "component_id": component_id,
        "material_cost": delivered_cost,
        "recommended_offer": {
            "supply_chain": {
                "delivered_cost": delivered_cost,
                "transportation_cost": transportation,
                "custom_duty_cost": duty,
                "forwarder_cost": forwarder,
                "currency": "CNY",
            }
        },
        "currency": "CNY",
    }


def most_json(scope_id: str) -> Dict[str, Any]:
    return {
        "work_package_id": scope_id,
        "most_scope_id": scope_id,
        "component_id": scope_id,
        "operation_name": scope_id,
        "p_h": 800,
        "oee": 0.8,
        "operator_percent": 25,
        "parts_per_cycle": 1,
        "generic_capex_eur": 0,
        "specific_capex_eur": 0,
        "tooling_cost_eur": 0,
    }


def main() -> int:
    project_code = f"FINAL-CALC-TEST-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    product_id = "316-5001"
    input_file = write_customer_input(project_code, product_id)
    plant_unit_data = {
        "plant": "Test plant",
        "selling_currency": "CNY",
        "operating_currency": "CNY",
        "dl_rate_operating_per_hour": 32,
        "voh_rate_operating_per_hour": 9.6,
        "direct_labor_cost_per_hour": 32,
        "base_variable_overhead_cost_per_hour": 9.6,
        "foh_percent_dc": 77,
        "fee_percent_dc": 56,
        "open_hours_per_year": 6000,
    }

    print("CHOKE FINAL CALCULATION TEST")
    print("=" * 78)
    print(f"project_code: {project_code}")
    print(f"product_id: {product_id}")

    start_real_choke_workflow(input_file=input_file, dry_run=True)
    save_bom_output(project_code, product_id, bom_json())
    for component_id, delivered_cost, transportation, duty, forwarder in [
        ("ferrite_core", 0.129, 0.005, 0, 0.001),
        ("magnet_wire", 0.333, 0.003, 0, 0.001),
        ("glue", 0.002, 0.0001, 0, 0.0001),
    ]:
        save_component_output(
            project_code,
            product_id,
            component_id,
            component_json(component_id, delivered_cost, transportation, duty, forwarder),
        )
    most_plan = trigger_most_operations(project_code, product_id, dry_run=True)
    for scope_id in most_plan["process_decomposition"]["required_work_package_ids"]:
        save_most_output(project_code, product_id, scope_id, most_json(scope_id))

    result = calculate_final_choke_costing_from_saved_outputs(
        project_code,
        product_id,
        unit_data_override=plant_unit_data,
    )

    assert result["status"] == "calculated", result
    assert_close(result["material_cost_per_piece"], 0.129 + 0.333 + 0.002, "material_cost_per_piece")
    transport_by_component = {
        item["component_id"]: item["transport_cost_per_piece"]
        for item in result["transport_breakdown_by_component"]
    }
    assert_close(transport_by_component["ferrite_core"], 0.006, "ferrite transport")
    assert_close(transport_by_component["magnet_wire"], 0.004, "wire transport")
    assert_close(transport_by_component["glue"], 0.0002, "glue transport")
    assert_close(result["transport_cost_per_piece"], 0.0102, "total transport")

    expected_direct = (
        result["dl_cost_per_piece"]
        + result["voh_cost_per_piece"]
        + result["transport_cost_per_piece"]
    )
    assert_close(result["direct_cost_per_piece"], expected_direct, "direct_cost_per_piece")
    assert_close(result["foh_cost_per_piece"], 0.77 * expected_direct, "foh_cost_per_piece")
    assert_close(result["fee_cost_per_piece"], 0.56 * expected_direct, "fee_cost_per_piece")
    assert_close(
        result["manufacturing_cost_per_piece"],
        expected_direct + result["foh_cost_per_piece"] + result["fee_cost_per_piece"],
        "manufacturing_cost_per_piece",
    )

    print(f"material_cost_per_piece: {result['material_cost_per_piece']}")
    print(f"transport_cost_per_piece: {result['transport_cost_per_piece']}")
    print(f"dl_cost_per_piece: {result['dl_cost_per_piece']}")
    print(f"voh_cost_per_piece: {result['voh_cost_per_piece']}")
    print(f"direct_cost_per_piece: {result['direct_cost_per_piece']}")
    print(f"foh_cost_per_piece: {result['foh_cost_per_piece']}")
    print(f"fee_cost_per_piece: {result['fee_cost_per_piece']}")
    print(f"manufacturing_cost_per_piece: {result['manufacturing_cost_per_piece']}")
    print("Choke final calculation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
