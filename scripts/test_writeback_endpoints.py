import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name == "fastapi" and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print("fastapi is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise

from app.main import app  # noqa: E402


PROJECT_CODE = f"RFQ-WRITEBACK-TEST-{uuid.uuid4().hex[:8]}"
PRODUCT_ID = "316-5001"


def _post(client: TestClient, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = client.post(path, json=payload)
    if response.status_code >= 400:
        raise AssertionError(f"POST {path} failed: {response.status_code} {response.text}")
    return response.json()


def _write_runtime_customer_input(project_code: str, product_id: str) -> str:
    input_dir = ROOT_DIR / "data" / "customer_inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / "__writeback_test_runtime.json"
    payload = {
        "project_code": project_code,
        "customer": "Write-back test customer",
        "final_customer": "Write-back test final customer",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": product_id,
        "workflow_product_id": product_id,
        "part_number": product_id,
        "drawing_reference": "writeback-test.pdf",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "CNY",
        "target_price": None,
        "sop_date": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.relative_to(ROOT_DIR).as_posix()


def _sample_bom(product_id: str) -> Dict[str, Any]:
    return {
        "quote_information": {
            "product_name": "Fuse choke",
            "part_number": product_id,
            "drawing_number": "DRW-WRITEBACK-001",
            "drawing_revision": "A",
            "drawing_status": "validated_for_test",
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
                "component_type": "Ferrite core",
                "description": "Ferrite core for fuse choke",
                "quantity_per_product": 1,
                "ferrite_diameter_mm": 5,
                "ferrite_length_mm": 16,
            },
            {
                "component_id": "magnet_wire",
                "component_type": "Magnet Wire",
                "description": "Copper enameled wire raw material",
                "quantity_per_product": 1,
                "wire_diameter_mm": 1.18,
                "turns": 11,
            },
            {
                "component_id": "tin_plating",
                "component_type": "Tin plating",
                "description": "Tin on lead ends",
                "quantity_per_product": 2,
                "tin_thickness_micron": 20,
            },
        ],
        "assumptions": ["Automated endpoint test BOM."],
    }


def _component_output(
    component_id: str,
    delivered_cost: float,
    transportation_cost: float,
    custom_duty_cost: float,
    forwarder_cost: float,
) -> Dict[str, Any]:
    return {
        "component_id": component_id,
        "component_type": component_id,
        "output_classification": "External",
        "recommended_offer": {
            "origin": "test fixture",
            "currency": "CNY",
            "supply_chain": {
                "delivered_cost": delivered_cost,
                "transportation_cost": transportation_cost,
                "custom_duty_cost": custom_duty_cost,
                "forwarder_cost": forwarder_cost,
                "currency": "CNY",
            },
        },
        "warnings": ["Endpoint write-back test output; not a supplier quotation."],
    }


def _most_output(trigger: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "work_package_id": trigger.get("work_package_id"),
        "component_id": trigger.get("component_id"),
        "operation_id": trigger.get("operation_id"),
        "operation_name": trigger.get("operation_name"),
        "p_h": 800,
        "oee": 0.8,
        "operator_percent": 25,
        "generic_capex_eur": 0,
        "specific_capex_eur": 1000,
        "tooling_cost_eur": 500,
        "tooling_life_pieces": 250000,
        "tooling_adder_per_piece_eur": 0.001,
        "source": "endpoint_writeback_test",
    }


def main() -> int:
    project_code = PROJECT_CODE
    product_id = PRODUCT_ID
    input_file = _write_runtime_customer_input(project_code, product_id)
    client = TestClient(app)

    print("Starting workflow")
    start_result = _post(client, "/api/choke-workflow/start", {
        "input_file": input_file,
        "dry_run": True,
    })
    start_state = start_result["state"]
    assert start_state["status"] == "bom_triggered", start_state["status"]
    assert start_state["missing_outputs"] == ["bom"], start_state["missing_outputs"]
    print(f"  status: {start_state['status']}")

    print("Saving BOM output")
    bom_result = _post(client, "/api/choke-workflow/save-bom-output", {
        "project_code": project_code,
        "product_id": product_id,
        "raw_json": _sample_bom(product_id),
    })
    bom_state = bom_result["state"]
    assert bom_state["status"] == "bom_received", bom_state["status"]
    assert bom_state["bom"]["status"] == "received", bom_state["bom"]
    print(f"  status: {bom_state['status']}")

    print("Triggering component costing")
    component_trigger_result = _post(client, "/api/choke-workflow/trigger-components", {
        "project_code": project_code,
        "product_id": product_id,
        "dry_run": True,
        "force": True,
    })
    component_triggers = component_trigger_result.get("component_triggers") or []
    component_ids = [item["component_id"] for item in component_triggers]
    assert "ferrite_core" in component_ids, component_ids
    assert "magnet_wire" in component_ids, component_ids
    print(f"  component calls: {', '.join(component_ids)}")

    print("Saving component outputs")
    component_samples = [
        ("ferrite_core", 0.129, 0.005, 0, 0.001),
        ("magnet_wire", 0.333, 0.003, 0, 0.001),
        ("lead_tinning", 0.004, 0.0001, 0, 0.0001),
    ]
    for component_id, delivered_cost, transportation_cost, custom_duty_cost, forwarder_cost in component_samples:
        component_result = _post(client, "/api/choke-workflow/save-component-output", {
            "project_code": project_code,
            "product_id": product_id,
            "component_id": component_id,
            "raw_json": _component_output(
                component_id,
                delivered_cost,
                transportation_cost,
                custom_duty_cost,
                forwarder_cost,
            ),
        })
        state = component_result["state"]
        assert state["components"][component_id]["status"] == "received", state["components"][component_id]
        print(f"  {component_id}: received")
    assert state["status"] == "components_received", state["status"]
    print(f"  status: {state['status']}")

    print("Triggering MOST operations")
    most_trigger_result = _post(client, "/api/choke-workflow/trigger-most", {
        "project_code": project_code,
        "product_id": product_id,
        "dry_run": True,
    })
    most_triggers = most_trigger_result.get("most_triggers") or []
    assert most_triggers, "No MOST work packages were generated."
    print(f"  MOST work packages: {', '.join(item['work_package_id'] for item in most_triggers)}")

    print("Saving MOST outputs")
    for trigger in most_triggers:
        work_package_id = trigger["work_package_id"]
        most_result = _post(client, "/api/choke-workflow/save-most-output", {
            "project_code": project_code,
            "product_id": product_id,
            "work_package_id": work_package_id,
            "raw_json": _most_output(trigger),
        })
        state = most_result["state"]
        assert state["most"][work_package_id]["status"] == "received", state["most"][work_package_id]
        print(f"  {work_package_id}: received")
    assert state["status"] == "most_received", state["status"]
    assert state["missing_outputs"] == [], state["missing_outputs"]
    print(f"  status: {state['status']}")

    print("Calculating final result")
    calculate_result = _post(client, "/api/choke-workflow/calculate-from-real-outputs", {
        "project_code": project_code,
        "product_id": product_id,
    })
    financial = calculate_result.get("financial_calculation") or {}
    assert financial.get("transport_cost_per_piece") is not None, financial
    assert financial.get("direct_cost_per_piece") is not None, financial
    assert financial.get("foh_cost_per_piece") is not None, financial
    assert financial.get("fee_cost_per_piece") is not None, financial
    assert financial.get("manufacturing_cost_per_piece") is not None, financial
    print(f"  financial status: {financial.get('status')}")
    print(f"  transport_cost_per_piece: {financial.get('transport_cost_per_piece')}")
    print(f"  direct_cost_per_piece: {financial.get('direct_cost_per_piece')}")
    print(f"  manufacturing_cost_per_piece: {financial.get('manufacturing_cost_per_piece')}")

    print("\nWrite-back endpoints OK")
    print(f"  project_code: {project_code}")
    print(f"  product_id: {product_id}")
    print(f"  workflow_state: data/costing_runs/{project_code}/{product_id}/workflow_state.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
