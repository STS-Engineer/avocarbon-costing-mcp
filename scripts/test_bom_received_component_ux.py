import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if exc.name in {"fastapi", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
    raise

from app.main import app
from services.choke_sequential_agent_workflow import save_bom_output, start_real_choke_workflow
from services.project_data_paths import CUSTOMER_INPUT_DIR, portable_data_reference


PROJECT_CODE = "BOM-UX-TEST"
PRODUCT_ID = "BOM-UX-PRODUCT"


def main():
    CUSTOMER_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    input_path = CUSTOMER_INPUT_DIR / "__bom_received_component_ux.json"
    input_path.write_text(json.dumps({
        "project_code": PROJECT_CODE,
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": PRODUCT_ID,
        "part_number": PRODUCT_ID,
        "drawing_reference": "test-drawing.pdf",
    }, indent=2), encoding="utf-8")

    start_real_choke_workflow(portable_data_reference(input_path), dry_run=True)
    save_bom_output(PROJECT_CODE, PRODUCT_ID, {
        "quote_information": {
            "product_name": "Fuse choke",
            "part_number": PRODUCT_ID,
            "drawing_number": "DRAWING-001",
        },
        "bom": [
            {"component_id": "ferrite_core", "component_type": "Ferrite Core", "quantity": 1},
            {"component_id": "magnet_wire", "component_type": "Magnet Wire", "quantity": 1},
            {"component_id": "lead_tinning", "component_type": "Lead tinning", "quantity": 1},
            {
                "component_id": "glue",
                "component_type": "Glue",
                "quantity": 1,
                "status": "to_confirm",
                "costing_relevance": True,
            },
        ],
        "points_to_confirm": ["Glue requirement"],
    })

    client = TestClient(app)
    response = client.get(f"/api/choke-workflow/bom-output/{PROJECT_CODE}/{PRODUCT_ID}")
    assert response.status_code == 200, response.text
    bom = response.json()
    assert bom["status"] == "found", bom
    assert {item["component_id"] for item in bom["components"]} == {
        "ferrite_core", "magnet_wire", "lead_tinning", "glue",
    }, bom["components"]

    blocked_response = client.post("/api/choke-workflow/trigger-components", json={
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "dry_run": True,
    })
    assert blocked_response.status_code == 200, blocked_response.text
    blocked = blocked_response.json()
    assert blocked["status"] == "blocked", blocked
    assert blocked["missing_inputs"] == [
        "annual_quantity", "customer_delivery_zone", "currency",
    ], blocked

    update_response = client.post("/api/choke-workflow/update-commercial-fields", json={
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "RMB",
    })
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["status"] == "updated", update_response.text

    trigger_response = client.post("/api/choke-workflow/trigger-components", json={
        "project_code": PROJECT_CODE,
        "product_id": PRODUCT_ID,
        "dry_run": True,
        "force": True,
    })
    assert trigger_response.status_code == 200, trigger_response.text
    triggered = trigger_response.json()
    assert triggered["status"] == "component_agents_triggered", triggered
    assert {item["component_id"] for item in triggered["component_triggers"]} == {
        "ferrite_core", "magnet_wire", "lead_tinning",
    }, triggered

    print("PASS saved BOM endpoint exposes bom[] components")
    print("PASS component trigger returns structured commercial-field block")
    print("PASS commercial update persists and enables supported component triggers")


if __name__ == "__main__":
    main()
