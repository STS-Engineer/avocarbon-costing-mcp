import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

try:
    import server
    from services.choke_sequential_agent_workflow import start_real_choke_workflow
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"anyio", "fastapi", "mcp", "dotenv", "psycopg2", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise


PROJECT_CODE_PREFIX = "RFQ-WRITEBACK-MCP-TEST"
PRODUCT_ID = "316-5001"


def _assert_state(payload: Dict[str, Any], expected_status: str, label: str) -> Dict[str, Any]:
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        raise AssertionError(f"{label}: missing state in response: {payload}")
    actual = state.get("status")
    if actual != expected_status:
        raise AssertionError(f"{label}: expected {expected_status}, got {actual}")
    return state


def _write_customer_input(project_code: str, product_id: str) -> str:
    input_dir = ROOT_DIR / "data" / "customer_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / "__mcp_choke_writeback_test.json"
    payload = {
        "project_code": project_code,
        "customer": "MCP write-back test customer",
        "final_customer": "MCP write-back final customer",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": product_id,
        "workflow_product_id": product_id,
        "part_number": product_id,
        "drawing_reference": "mcp-writeback-test.pdf",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "CNY",
        "target_price": None,
        "sop_date": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.relative_to(ROOT_DIR).as_posix()


def _sample_bom(project_code: str, product_id: str) -> Dict[str, Any]:
    return {
        "quote_information": {
            "project_code": project_code,
            "product_id": product_id,
            "product_name": "Fuse choke",
            "part_number": product_id,
            "drawing_number": "MCP-WRITEBACK-DRW-001",
            "drawing_revision": "A",
            "drawing_status": "test_confirmed",
        },
        "technical_data": {
            "wire_diameter_mm": 1.18,
            "turns": 11,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5,
            "glue_required": True,
        },
        "components": [
            {
                "component_id": "ferrite_core",
                "component_type": "Ferrite Core",
                "description": "Ferrite core",
                "quantity_per_product": 1,
                "ferrite_diameter_mm": 5,
                "ferrite_length_mm": 16,
            },
            {
                "component_id": "magnet_wire",
                "component_type": "Magnet Wire",
                "description": "Copper enameled magnet wire",
                "quantity_per_product": 1,
                "wire_diameter_mm": 1.18,
                "turns": 11,
            },
            {
                "component_id": "glue",
                "component_type": "Glue",
                "description": "Adhesive for glued choke",
                "quantity_per_product": 1,
                "weight_kg": 0.00002,
            },
            {
                "component_id": "lead_tin_plating",
                "component_type": "Lead Tin Plating",
                "description": "Tin plating on lead ends",
                "quantity_per_product": 2,
                "tin_thickness_micron": 20,
            },
        ],
    }


def _component_output(component_id: str, delivered_cost: float) -> Dict[str, Any]:
    return {
        "component_id": component_id,
        "component_type": component_id,
        "output_classification": "External",
        "recommended_offer": {
            "origin": "mcp test fixture",
            "currency": "CNY",
            "supply_chain": {
                "delivered_cost": delivered_cost,
                "transportation_cost": 0.002,
                "custom_duty_cost": 0,
                "forwarder_cost": 0.001,
                "currency": "CNY",
            },
        },
        "warnings": ["MCP write-back test output; not commercially usable."],
    }


def _most_output(most_scope_id: str, component_id: str, operation_id: int, operation_name: str) -> Dict[str, Any]:
    return {
        "work_package_id": most_scope_id,
        "most_scope_id": most_scope_id,
        "component_id": component_id,
        "operation_id": operation_id,
        "operation_name": operation_name,
        "p_h": 800,
        "oee": 0.8,
        "operator_percent": 25,
        "parts_per_cycle": 1,
        "generic_capex_eur": 0,
        "specific_capex_eur": 1000,
        "tooling_cost_eur": 500,
        "tooling_life_pieces": 250000,
        "tooling_adder_per_piece_eur": 0.001,
        "source": "mcp_writeback_test",
    }


def _assert_file(path: Path) -> None:
    if not path.exists():
        raise AssertionError(f"Expected file does not exist: {path}")


def main() -> int:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    project_code = f"{PROJECT_CODE_PREFIX}-{run_id}"
    product_id = PRODUCT_ID
    input_file = _write_customer_input(project_code, product_id)

    print("CHOKE COSTING WRITE-BACK MCP TEST")
    print("=" * 78)
    print(f"project_code: {project_code}")
    print(f"product_id: {product_id}")

    print("Verifying existing list_database_tables tool")
    table_result = server.list_database_tables()
    if not table_result.get("success"):
        raise AssertionError(f"list_database_tables failed: {table_result}")
    print(f"  list_database_tables: ok ({table_result.get('count', 'unknown')} tables)")

    print("Starting workflow state")
    start_result = start_real_choke_workflow(input_file=input_file, dry_run=True)
    start_state = start_result.get("state") or {}
    if start_state.get("status") != "bom_triggered":
        raise AssertionError(f"Expected bom_triggered, got: {start_state}")
    print("  workflow status: bom_triggered")

    print("Calling save_bom_output")
    bom_response = server.save_bom_output(
        project_code=project_code,
        product_id=product_id,
        raw_json=_sample_bom(project_code, product_id),
    )
    bom_state = _assert_state(bom_response, "bom_received", "save_bom_output")
    print(f"  workflow status: {bom_state['status']}")

    print("Calling save_component_output for separate components")
    for component_id, cost in [
        ("ferrite_core", 0.129),
        ("magnet_wire", 0.333),
        ("glue", 0.002),
    ]:
        response = server.save_component_output(
            project_code=project_code,
            product_id=product_id,
            component_id=component_id,
            raw_json=_component_output(component_id, cost),
        )
        state = response.get("state") or {}
        if (state.get("components") or {}).get(component_id, {}).get("status") != "received":
            raise AssertionError(f"{component_id} was not received: {response}")
        print(f"  {component_id}: received")

    print("Calling save_most_output for separate MOST scopes")
    for most_scope_id, component_id, operation_id, operation_name in [
        ("magnet_wire_winding", "magnet_wire", 10, "winding"),
        ("glue_application_baking", "glue", 20, "glue_application_baking"),
        ("electrical_test", "finished_choke", 30, "electrical_test"),
    ]:
        response = server.save_most_output(
            project_code=project_code,
            product_id=product_id,
            most_scope_id=most_scope_id,
            raw_json=_most_output(most_scope_id, component_id, operation_id, operation_name),
        )
        state = response.get("state") or {}
        if (state.get("most") or {}).get(most_scope_id, {}).get("status") != "received":
            raise AssertionError(f"{most_scope_id} was not received: {response}")
        print(f"  {most_scope_id}: received")

    print("Calling get_choke_workflow_status")
    status = server.get_choke_workflow_status(project_code=project_code, product_id=product_id)
    if status.get("status") != "most_received":
        raise AssertionError(f"Expected most_received from get_choke_workflow_status, got: {status}")
    print(f"  status: {status.get('status')}")
    print(f"  missing_outputs: {status.get('missing_outputs')}")

    print("Checking saved files remain separate")
    run_dir = ROOT_DIR / "data" / "costing_runs" / project_code / product_id
    _assert_file(run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json")
    for component_id in ["ferrite_core", "magnet_wire", "glue"]:
        _assert_file(run_dir / "agent_outputs" / "components" / f"{component_id}.json")
    for most_scope_id in ["magnet_wire_winding", "glue_application_baking", "electrical_test"]:
        _assert_file(run_dir / "agent_outputs" / "most" / f"{most_scope_id}.json")
    print("  separate component and MOST JSON files: ok")

    print("Calling calculate_choke_from_saved_outputs")
    calculation = server.calculate_choke_from_saved_outputs(project_code=project_code, product_id=product_id)
    if calculation.get("success") is False:
        raise AssertionError(f"calculate_choke_from_saved_outputs failed: {calculation}")
    financial = calculation.get("financial_calculation") or {}
    if financial.get("manufacturing_cost_per_piece") is None:
        raise AssertionError(f"manufacturing_cost_per_piece missing: {financial}")
    print(f"  financial status: {financial.get('status')}")
    print(f"  transport_cost_per_piece: {financial.get('transport_cost_per_piece')}")
    print(f"  manufacturing_cost_per_piece: {financial.get('manufacturing_cost_per_piece')}")

    print()
    print("Choke Costing Write-Back MCP tools OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
