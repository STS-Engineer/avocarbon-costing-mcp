import json
import os
import shutil
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "data" / "test_runs" / f"component-step2-{uuid.uuid4().hex}"
os.environ["DATA_ROOT"] = str(TEST_ROOT)
sys.path.insert(0, str(ROOT))

from services import choke_sequential_agent_workflow as workflow


def sample_bom():
    return {
        "bom": [
            {"component_id": "ferrite_core", "component_name": "Ferrite core", "component_family": "ferrite", "quantity": 1, "costing_route": "external_component_costing_agent", "diameter_mm": 5, "length_mm": 16},
            {"component_id": "magnet_wire", "component_name": "Enameled copper wire", "component_family": "enameled_wire", "quantity": 1, "costing_route": "external_component_costing_agent", "diameter_mm": 1.18, "turns": 11},
            {"component_id": "lead_tinning", "component_name": "Lead tinning", "component_family": "tin", "quantity": 2, "costing_route": "external_component_costing_agent", "thickness_microns": 20},
            {"component_id": "glue", "component_name": "Glue", "component_family": "glue", "quantity": 1, "costing_route": "not_external_agent"},
        ]
    }


def fake_strategy(product_line, product, delivery_zone):
    return {"status": "found", "source": "test.matrix", "production_plant": "Kunshan", "product": product, "delivery_zone": delivery_zone}


def fake_unit(plant):
    return {"status": "found", "source": "test.unit", "plant": plant, "selling_currency": "RMB", "operating_currency": "RMB"}


def create_workflow(project_code=None, product_id="316-5001", customer_overrides=None):
    project_code = project_code or f"TEST-COMP-{uuid.uuid4().hex[:8]}"
    customer_input = {
        "project_code": project_code,
        "workflow_product_id": product_id,
        "product_id": product_id,
        "product_line": "Chokes",
        "product": "Fuse choke",
        "annual_quantity": 600000,
        "customer_delivery_zone": "China South Pacific",
        "currency": "RMB",
        "drawing_reference": "316-5001.pdf",
    }
    customer_input.update(customer_overrides or {})
    workflow.get_master_manufacturing_strategy = fake_strategy
    workflow.get_master_unit_data = fake_unit
    normalized = workflow.normalize_bom(sample_bom())
    workflow._write_json(workflow._bom_raw_path(project_code, product_id), sample_bom())
    workflow._write_json(workflow._bom_normalized_path(project_code, product_id), normalized)
    state = workflow._load_state(project_code, product_id)
    state.update({
        "status": "bom_received",
        "current_step": "Step 2 External Component Costing Agent",
        "customer_input": customer_input,
        "manufacturing_strategy": fake_strategy("Chokes", "Fuse choke", "China South Pacific"),
        "production_plant": "Kunshan",
        "unit_data": fake_unit("Kunshan"),
        "bom": {"status": "received", "normalized_path": workflow._relative(workflow._bom_normalized_path(project_code, product_id))},
        "components": {},
        "missing_outputs": ["component:ferrite_core", "component:magnet_wire", "component:lead_tinning"],
    })
    workflow._save_state(state)
    return project_code, product_id


def successful_raw(component_id):
    return {
        "component_id": component_id,
        "classification": "External",
        "analysis_status": "complete",
        "cost_basis": {"basis_status": "benchmark", "source": "test", "source_date": None, "confidence": "medium"},
        "recommended_offer": {"origin": "China", "incoterm": "FCA", "supplier_currency": "CNY", "delivered_cost_per_piece": 0.1},
        "commercially_usable": False,
    }


def install_trigger_spy():
    calls = []

    def trigger(agent_env, fallback_name, input_text, conversation_key, idempotency_key, dry_run):
        payload = json.loads(input_text)
        calls.append({"agent_env": agent_env, "payload": payload, "conversation_key": conversation_key, "idempotency_key": idempotency_key, "dry_run": dry_run})
        return {"status": "dry_run" if dry_run else "accepted", "http_status": None if dry_run else 202, "response": {"conversation_url": f"https://example.test/{payload['component_id']}"}}

    workflow._trigger = trigger
    return calls


def cleanup():
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
