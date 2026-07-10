import json
import os

from services.choke_financial_calculation import calculate_choke_financials
from services.choke_standard_schema import build_standard_choke_costing_json
from services.external_component_agent import run_external_component_agent
from services.manufacturing_strategy import select_manufacturing_strategy
from services.plant_data import get_plant_data
from services.workspace_agent_client import trigger_workspace_agent


def _save_address(project_code, product_id, *parts):
    cleaned_parts = [str(part).strip("/\\") for part in parts if part]
    return "/".join([
        "data",
        "costing_runs",
        str(project_code),
        "products",
        str(product_id),
        *cleaned_parts,
    ])


def _planned_status(trigger_result, dry_run):
    if dry_run:
        return "planned"
    if trigger_result.get("status") == "accepted":
        return "triggered"
    return "missing"


def _agent_token():
    return os.getenv("WORKSPACE_AGENT_ACCESS_TOKEN") or os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")


def _demo_operations(product_id):
    source = (
        "BYD/Fuse choke known demo values"
        if str(product_id) == "316-5001"
        else "generic choke demo placeholder values pending real process decomposition"
    )
    return [
        {
            "operation_number": 10,
            "operation_name": "winding",
            "p_h": 847.42,
            "oee": 0.75,
            "operator_percent": 25,
            "generic_capex_eur": 0,
            "specific_capex_eur": 15000,
            "tooling_cost_eur": 2500,
            "tooling_life_pieces": 250000,
            "tooling_adder_per_piece_eur": 0.002,
            "demo_source": source,
        },
        {
            "operation_number": 20,
            "operation_name": "gluing_baking",
            "p_h": 800,
            "oee": 0.8,
            "operator_percent": 100,
            "generic_capex_eur": 11000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_pieces": 800000,
            "tooling_type": "lifetime warranty",
            "demo_source": source,
        },
        {
            "operation_number": 30,
            "operation_name": "testing",
            "p_h": 1200,
            "oee": 0.8,
            "operator_percent": 100,
            "generic_capex_eur": 3000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 1000,
            "tooling_life_pieces": 800000,
            "tooling_type": "lifetime warranty",
            "demo_source": source,
        },
        {
            "operation_number": 40,
            "operation_name": "inspection_packaging",
            "p_h": 2500,
            "oee": 0.8,
            "operator_percent": 100,
            "generic_capex_eur": 4000,
            "specific_capex_eur": 1000,
            "tooling_cost_eur": 0,
            "demo_source": source,
        },
    ]


def _build_external_component_calls(payload, production_plant, dry_run):
    project_code = payload.get("project_code")
    product_id = payload.get("product_id")
    annual_quantity = payload.get("annual_quantity")
    delivery_zone = payload.get("customer_delivery_zone")
    common = {
        "project_code": project_code,
        "product_id": product_id,
        "annual_quantity": annual_quantity,
        "destination_zone": delivery_zone,
        "production_plant": production_plant,
    }

    component_payloads = [
        {
            **common,
            "component_id": f"{product_id}-ferrite",
            "component_type": "ferrite",
            "component_definition": {
                "description": "Planned ferrite component placeholder from choke BOM",
                "source_note": "Actual component payload will come from BOM JSON when available.",
            },
            "save_address": _save_address(
                project_code,
                product_id,
                "component_costing",
                f"{product_id}-ferrite.json",
            ),
        },
        {
            **common,
            "component_id": f"{product_id}-wire",
            "component_type": "enameled_wire",
            "scope_note": "raw_material_only",
            "component_definition": {
                "description": "Planned enameled wire raw material placeholder from choke BOM",
                "scope_note": "raw_material_only",
                "source_note": "Actual component payload will come from BOM JSON when available.",
            },
            "save_address": _save_address(
                project_code,
                product_id,
                "component_costing",
                f"{product_id}-wire.json",
            ),
        },
    ]

    component_calls = []
    for component_payload in component_payloads:
        agent_result = run_external_component_agent(component_payload, dry_run=dry_run)
        component_calls.append({
            "status": "planned" if dry_run else agent_result.get("status"),
            "component_id": component_payload["component_id"],
            "component_type": component_payload["component_type"],
            "agent_id": "External Component Costing Agent",
            "save_address": component_payload["save_address"],
            "payload": component_payload,
            "agent_result": agent_result,
            "note": "Actual component payloads will come from BOM JSON when available.",
        })
    return component_calls


def _build_agent_trigger(agent_id, input_payload, save_address, dry_run, conversation_key, idempotency_key):
    input_text = json.dumps(
        {
            "save_address": save_address,
            "input": input_payload,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    return trigger_workspace_agent(
        agent_id=agent_id,
        access_token=_agent_token(),
        input_text=input_text,
        conversation_key=conversation_key,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )


def build_choke_workspace_orchestration(payload, dry_run=True):
    payload = payload or {}
    project_code = payload.get("project_code")
    product_line = payload.get("product_line")
    product = payload.get("product")
    product_id = payload.get("product_id")
    delivery_zone = payload.get("customer_delivery_zone")
    annual_quantity = payload.get("annual_quantity")

    manufacturing_strategy = select_manufacturing_strategy(
        product_line,
        product,
        delivery_zone,
    )
    plant_data = get_plant_data(manufacturing_strategy.get("production_plant"))

    bom_agent_id = os.getenv("CHOKE_BOM_AGENT_ID", "Choke BOM Analyzer")
    process_agent_id = os.getenv("PROCESS_DECOMPOSITION_AGENT_ID", "Process Decomposition Agent")
    most_agent_id = os.getenv("MOST_AGENT_ID", "MOST Assemblage")

    bom_save_address = _save_address(project_code, product_id, "bom", f"{product_id}-bom.json")
    bom_input = {
        "task": "Read the choke plan and create component list and BOM.",
        "project_code": project_code,
        "product_line": product_line,
        "product": product,
        "product_id": product_id,
        "drawing_reference": payload.get("drawing_reference"),
        "save_address": bom_save_address,
    }
    bom_trigger = _build_agent_trigger(
        bom_agent_id,
        bom_input,
        bom_save_address,
        dry_run,
        f"{project_code}:{product_id}:bom",
        f"{project_code}:{product_id}:bom:v1",
    )

    production_plant = plant_data.get("plant_name") or manufacturing_strategy.get("production_plant")
    component_calls = _build_external_component_calls(payload, production_plant, dry_run)

    process_save_address = _save_address(
        project_code,
        product_id,
        "process_decomposition",
        f"{product_id}-process.json",
    )
    process_input = {
        "task": "Create process decomposition for choke technical data.",
        "project_code": project_code,
        "product_line": product_line,
        "product": product,
        "product_id": product_id,
        "annual_quantity": annual_quantity,
        "drawing_reference": payload.get("drawing_reference"),
        "save_address": process_save_address,
    }
    process_trigger = _build_agent_trigger(
        process_agent_id,
        process_input,
        process_save_address,
        dry_run,
        f"{project_code}:{product_id}:process",
        f"{project_code}:{product_id}:process:v1",
    )

    demo_operations = _demo_operations(product_id)
    operation_calls = []
    for operation in demo_operations:
        operation_save_address = _save_address(
            project_code,
            product_id,
            "operations",
            f"{operation['operation_number']:02d}-{operation['operation_name']}.json",
        )
        operation_input = {
            "task": "Run MOST for one choke operation only.",
            "project_code": project_code,
            "product_id": product_id,
            "operation": operation,
            "save_address": operation_save_address,
        }
        operation_trigger = _build_agent_trigger(
            most_agent_id,
            operation_input,
            operation_save_address,
            dry_run,
            f"{project_code}:{product_id}:operation:{operation['operation_number']}",
            f"{project_code}:{product_id}:operation:{operation['operation_number']}:v1",
        )
        operation_calls.append({
            "status": _planned_status(operation_trigger, dry_run),
            "operation_number": operation["operation_number"],
            "operation_name": operation["operation_name"],
            "agent_id": most_agent_id,
            "save_address": operation_save_address,
            "trigger_result": operation_trigger,
            "data": operation,
        })

    financial_result = calculate_choke_financials(
        demo_operations,
        annual_quantity,
        plant_data,
    )
    financial_missing = list(financial_result.get("missing_inputs") or [])
    financial_missing.append("material_cost_per_piece from component agent outputs")

    missing_inputs = []
    missing_inputs.extend(manufacturing_strategy.get("missing_inputs") or [])
    missing_inputs.extend(plant_data.get("missing_inputs") or [])
    missing_inputs.extend(financial_result.get("missing_inputs") or [])

    preliminary_manufacturing_cost = None
    if financial_result.get("status") == "calculated":
        preliminary_manufacturing_cost = (
            financial_result.get("dl_cost_per_piece")
            + financial_result.get("voh_cost_per_piece")
        )

    agent_outputs = {
        "bom": {
            "status": _planned_status(bom_trigger, dry_run),
            "save_address": bom_save_address,
            "agent_id": bom_agent_id,
            "trigger_result": bom_trigger,
            "data": None,
        },
        "components": component_calls,
        "process_decomposition": {
            "status": _planned_status(process_trigger, dry_run),
            "save_address": process_save_address,
            "agent_id": process_agent_id,
            "trigger_result": process_trigger,
            "data": None,
        },
        "operations": operation_calls,
    }

    agent_outputs["planned_calls"] = [
        {
            "agent": bom_agent_id,
            "type": "bom",
            "save_address": bom_save_address,
            "status": agent_outputs["bom"]["status"],
        },
        *[
            {
                "agent": component["agent_id"],
                "type": "component",
                "name": component["component_type"],
                "save_address": component["save_address"],
                "status": component["status"],
            }
            for component in component_calls
        ],
        {
            "agent": process_agent_id,
            "type": "process_decomposition",
            "save_address": process_save_address,
            "status": agent_outputs["process_decomposition"]["status"],
        },
        *[
            {
                "agent": operation["agent_id"],
                "type": "most_operation",
                "name": operation["operation_name"],
                "save_address": operation["save_address"],
                "status": operation["status"],
            }
            for operation in operation_calls
        ],
    ]

    return build_standard_choke_costing_json(
        project={
            "project_code": project_code,
            "product_line": product_line,
            "product": product,
            "product_id": product_id,
            "customer_delivery_zone": delivery_zone,
            "annual_quantity": annual_quantity,
        },
        manufacturing_strategy=manufacturing_strategy,
        plant_data=plant_data,
        agent_outputs=agent_outputs,
        financial_calculation={
            "status": financial_result.get("status"),
            "currency": financial_result.get("currency"),
            "dl_cost_per_piece": financial_result.get("dl_cost_per_piece"),
            "voh_cost_per_piece": financial_result.get("voh_cost_per_piece"),
            "material_cost_per_piece": None,
            "preliminary_manufacturing_cost_per_piece": preliminary_manufacturing_cost,
            "added_value_cost_per_piece": financial_result.get("added_value_cost_per_piece"),
            "operations_calculation": financial_result.get("operations_calculation") or [],
            "missing_inputs": list(dict.fromkeys(financial_missing)),
        },
        missing_inputs=missing_inputs,
        next_steps=[
            "Wait for Choke BOM Analyzer JSON at the BOM save_address.",
            "Replace planned ferrite and wire placeholders with BOM-derived component payloads.",
            "Run External Component Costing Agent component by component.",
            "Run MOST operation by operation after process decomposition is confirmed.",
            "Use component cost JSONs to complete material cost per piece.",
        ],
    )
