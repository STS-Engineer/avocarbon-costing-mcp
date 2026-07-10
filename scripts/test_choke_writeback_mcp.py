import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

try:
    import server
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"anyio", "mcp", "dotenv", "psycopg2", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise


PROJECT_CODE = "RFQ-WRITEBACK-MCP-TEST"
PRODUCT_ID = "316-5001"


def _assert_status(payload: Dict[str, Any], expected_status: str, label: str) -> Dict[str, Any]:
    state = payload.get("state") if isinstance(payload, dict) else None
    if not isinstance(state, dict):
        raise AssertionError(f"{label}: missing state in response: {payload}")
    actual = state.get("status")
    if actual != expected_status:
        raise AssertionError(f"{label}: expected {expected_status}, got {actual}")
    return state


def _sample_bom() -> Dict[str, Any]:
    return {
        "quote_information": {
            "project_code": PROJECT_CODE,
            "product_id": PRODUCT_ID,
            "product_name": "Fuse choke",
            "part_number": PRODUCT_ID,
            "drawing_number": "MCP-WRITEBACK-DRW-001",
            "drawing_revision": "A",
            "drawing_status": "test_confirmed",
        },
        "technical_data": {
            "wire_diameter_mm": 1.18,
            "turns": 11,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5,
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


def _most_output() -> Dict[str, Any]:
    return {
        "work_package_id": "wp_10_winding",
        "component_id": "magnet_wire",
        "operation_id": 10,
        "operation_name": "winding",
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


def main() -> int:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    project_code = f"{PROJECT_CODE}-{run_id}"
    product_id = PRODUCT_ID

    print("CHOKE COSTING WRITE-BACK MCP TEST")
    print("=" * 78)
    print(f"project_code: {project_code}")
    print(f"product_id: {product_id}")

    print("Calling save_bom_output")
    bom_response = server.save_bom_output(
        project_code=project_code,
        product_id=product_id,
        raw_json=_sample_bom(),
    )
    bom_state = _assert_status(bom_response, "bom_received", "save_bom_output")
    print(f"  workflow status: {bom_state['status']}")

    print("Calling save_component_output for ferrite")
    ferrite_response = server.save_component_output(
        project_code=project_code,
        product_id=product_id,
        component_id="ferrite_core",
        raw_json=_component_output("ferrite_core", 0.129),
    )
    ferrite_state = ferrite_response.get("state") or {}
    if (ferrite_state.get("components") or {}).get("ferrite_core", {}).get("status") != "received":
        raise AssertionError(f"ferrite_core was not received: {ferrite_response}")
    print("  ferrite_core: received")

    print("Calling save_component_output for wire")
    wire_response = server.save_component_output(
        project_code=project_code,
        product_id=product_id,
        component_id="magnet_wire",
        raw_json=_component_output("magnet_wire", 0.333),
    )
    component_state = _assert_status(wire_response, "components_received", "save_component_output")
    if (component_state.get("components") or {}).get("magnet_wire", {}).get("status") != "received":
        raise AssertionError(f"magnet_wire was not received: {wire_response}")
    print(f"  workflow status: {component_state['status']}")

    print("Calling save_most_output")
    most_response = server.save_most_output(
        project_code=project_code,
        product_id=product_id,
        work_package_id="wp_10_winding",
        raw_json=_most_output(),
    )
    most_state = _assert_status(most_response, "most_received", "save_most_output")
    if (most_state.get("most") or {}).get("wp_10_winding", {}).get("status") != "received":
        raise AssertionError(f"wp_10_winding was not received: {most_response}")
    print(f"  workflow status: {most_state['status']}")

    print("Calling get_workflow_status")
    status = server.get_workflow_status(project_code=project_code, product_id=product_id)
    if status.get("status") != "most_received":
        raise AssertionError(f"Expected most_received from get_workflow_status, got: {status}")
    print(f"  status: {status.get('status')}")
    print(f"  missing_outputs: {status.get('missing_outputs')}")
    print()
    print("Choke Costing Write-Back MCP tools OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
