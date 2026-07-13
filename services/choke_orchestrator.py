import json
import os
from pathlib import Path

from services.choke_demo_outputs import (
    get_demo_bom_output_3165001,
    get_demo_component_cost_outputs_3165001,
    get_demo_glue_cost,
    get_demo_tin_cost,
)
from services.choke_financial_calculation import (
    apply_olivier_direct_foh_fee,
    calculate_dl_voh,
    calculate_transport_cost_from_components,
)
from services.choke_process_decomposition import decompose_choke_process
from services.choke_standard_schema import build_standard_choke_envelope
from services.costing_master_data_service import (
    get_master_manufacturing_strategy,
    get_master_unit_data,
)
from services.customer_input_schema import normalize_customer_input
from services.project_data_paths import resolve_data_reference
from services.workspace_agent_client import trigger_workspace_agent


BASE_DIR = Path(__file__).resolve().parents[1]


def _save_address(project_code, product_id, filename):
    return "/".join([
        "data",
        "costing_runs",
        str(project_code),
        str(product_id),
        filename.strip("/\\"),
    ])


def _absolute_save_path(save_address):
    return resolve_data_reference(save_address)


def _write_json(save_address, data):
    path = _absolute_save_path(save_address)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(path)


def _parse_jsonish(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _agent_id(env_name, fallback):
    return os.getenv(env_name) or fallback


def _trigger_or_plan(
    agent_id,
    input_text,
    conversation_key,
    idempotency_key,
    dry_run,
    trigger_agents,
):
    return trigger_workspace_agent(
        agent_id=agent_id,
        access_token=os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN"),
        input_text=input_text,
        conversation_key=conversation_key,
        idempotency_key=idempotency_key,
        dry_run=dry_run or not trigger_agents,
    )


def _trigger_status(trigger_result, dry_run, trigger_agents):
    if dry_run or not trigger_agents:
        return "planned"
    if trigger_result.get("status") == "accepted":
        return "triggered"
    return "missing"


def _json_input_text(instructions, payload, save_address):
    return json.dumps(
        {
            "instructions": instructions,
            "save_address": save_address,
            "payload": payload,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _demo_bom(customer_input):
    return {
        "status": "demo_normalized_bom",
        "technical_data": {
            "wire_diameter_mm": 1.25,
            "total_turns": 14,
            "tin_thickness_micron": 20,
            "ferrite_diameter_mm": 5,
            "glue_requirement": "doubt",
            "left_direction_changes": 0,
            "right_direction_changes": 0,
        },
        "components": [
            {
                "component_id": "ferrite",
                "component_type": "ferrite_component",
                "quantity_per_product": 1,
                "costing_route": "external_component_costing_agent",
                "bom_definition": {
                    "description": "Ferrite core from demo BOM",
                    "diameter_mm": 5,
                    "source": "demo_override 316-5001",
                },
            },
            {
                "component_id": "wire",
                "component_type": "enameled_wire",
                "quantity_per_product": 1,
                "costing_route": "external_component_costing_agent",
                "bom_definition": {
                    "description": "Enameled wire raw material from demo BOM",
                    "wire_diameter_mm": 1.25,
                    "turns": 14,
                    "scope_note": "raw_material_only",
                    "source": "demo_override 316-5001",
                },
            },
            {
                "component_id": "tin",
                "component_type": "tin",
                "quantity_per_product": 1,
                "costing_route": "material_price_lookup",
                "bom_definition": {
                    "description": "Tin material lookup pending",
                    "tin_thickness_micron": 20,
                },
            },
            {
                "component_id": "glue",
                "component_type": "glue",
                "quantity_per_product": None,
                "costing_route": "rule_based_or_pending",
                "bom_definition": {
                    "description": "Glue requirement uncertain; process decomposition uses glued by default.",
                },
            },
        ],
        "customer_input_reference": {
            "project_code": customer_input.get("project_code"),
            "product_id": customer_input.get("product_id"),
        },
    }


def _extract_components_from_bom(bom_json):
    bom = _parse_jsonish(bom_json, {})
    components = bom.get("components") or bom.get("normalized_components") or bom.get("bom") or []
    if isinstance(components, dict):
        components = components.get("components") or components.get("lines") or []
    normalized = []
    for index, component in enumerate(components if isinstance(components, list) else [], start=1):
        if not isinstance(component, dict):
            continue
        component_type = (
            component.get("component_type")
            or component.get("material_type")
            or component.get("family")
            or component.get("type")
        )
        component_id = (
            component.get("component_id")
            or component.get("component_code")
            or component.get("id")
            or str(component_type or f"component_{index}").lower().replace(" ", "_")
        )
        route = component.get("costing_route")
        if not route:
            if str(component_type or "").lower() in ["ferrite", "ferrite_component", "wire", "enameled_wire"]:
                route = "external_component_costing_agent"
            elif str(component_type or "").lower() == "tin":
                route = "material_price_lookup"
            else:
                route = "pending"
        normalized.append({
            "component_id": component_id,
            "component_type": component_type,
            "quantity_per_product": component.get("quantity_per_product") or component.get("quantity"),
            "costing_route": route,
            "bom_definition": component.get("bom_definition") or component,
        })
    return normalized


def _normalized_component_entry(component, save_address=None, raw_output=None):
    return {
        "component_id": component.get("component_id"),
        "component_type": component.get("component_type"),
        "quantity_per_product": component.get("quantity_per_product"),
        "bom_definition": component.get("bom_definition") or {},
        "costing_status": "planned" if save_address else "missing",
        "costing_save_address": save_address or "",
        "agent_raw_output": raw_output,
        "normalized_cost": {
            "currency": "",
            "material_cost_per_piece": None,
            "delivered_cost_per_piece": None,
            "tooling_cost": None,
            "commercially_usable": False,
            "missing_inputs": [],
        },
    }


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _normalize_component_cost_outputs(component_entries, component_cost_outputs):
    outputs = _parse_jsonish(component_cost_outputs, []) if component_cost_outputs is not None else []
    if isinstance(outputs, dict):
        outputs = outputs.get("components") or outputs.get("component_outputs") or [outputs]
    by_id = {}
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict):
            continue
        component_id = (
            output.get("component_id")
            or output.get("component_reference")
            or output.get("component_code")
        )
        if component_id:
            by_id[str(component_id)] = output

    material_cost = 0.0
    missing = []
    normalized = []
    for entry in component_entries:
        raw = by_id.get(str(entry["component_id"]))
        if raw:
            normalized_cost = raw.get("normalized_cost") or {}
            recommended_offer = raw.get("recommended_offer") if isinstance(raw.get("recommended_offer"), dict) else {}
            supply_chain = (
                recommended_offer.get("supply_chain")
                if isinstance(recommended_offer.get("supply_chain"), dict)
                else {}
            )
            cost = _first_present(
                normalized_cost.get("delivered_cost_per_piece"),
                normalized_cost.get("material_cost_per_piece"),
                supply_chain.get("delivered_cost"),
                recommended_offer.get("delivered_cost"),
                raw.get("delivered_cost_per_piece"),
                raw.get("material_cost_per_piece"),
                raw.get("cost_per_piece"),
            )
            try:
                cost = float(cost)
            except (TypeError, ValueError):
                cost = None
            if cost is not None:
                material_cost += cost
            else:
                missing.append(f"{entry['component_id']} material cost")
            entry = {
                **entry,
                "costing_status": "available",
                "agent_raw_output": raw,
                "normalized_cost": {
                    "currency": (
                        normalized_cost.get("currency")
                        or supply_chain.get("currency")
                        or recommended_offer.get("currency")
                        or raw.get("currency")
                        or ""
                    ),
                    "material_cost_per_piece": normalized_cost.get("material_cost_per_piece") or cost,
                    "delivered_cost_per_piece": normalized_cost.get("delivered_cost_per_piece") or cost,
                    "tooling_cost": normalized_cost.get("tooling_cost") or raw.get("tooling_cost"),
                    "commercially_usable": bool(normalized_cost.get("commercially_usable")),
                    "missing_inputs": normalized_cost.get("missing_inputs") or [],
                },
            }
        elif entry.get("costing_status") == "planned" and str(entry.get("component_type")).lower() in [
            "ferrite_component",
            "enameled_wire",
            "wire",
            "ferrite",
        ]:
            missing.append(f"{entry['component_id']} component costing output")
        normalized.append(entry)
    return normalized, (material_cost if material_cost > 0 else None), missing


def _build_material_cost_breakdown(component_entries):
    breakdown = []
    for entry in component_entries:
        normalized_cost = entry.get("normalized_cost") or {}
        raw = entry.get("agent_raw_output") or {}
        delivered_cost = _first_present(
            normalized_cost.get("delivered_cost_per_piece"),
            normalized_cost.get("material_cost_per_piece"),
        )
        if delivered_cost is None and isinstance(raw.get("recommended_offer"), dict):
            delivered_cost = (
                raw["recommended_offer"].get("supply_chain", {}).get("delivered_cost")
                or raw["recommended_offer"].get("selling_price_converted_per_unit")
            )
        try:
            delivered_cost = float(delivered_cost)
        except (TypeError, ValueError):
            delivered_cost = None
        breakdown.append({
            "component_id": entry.get("component_id"),
            "delivered_cost_per_piece": delivered_cost,
            "currency": normalized_cost.get("currency") or raw.get("recommended_offer", {}).get("reporting_currency") or "",
            "status": raw.get("status") or entry.get("costing_status"),
        })
    return breakdown


def _build_bom_agent_call(customer_input, save_address, dry_run, trigger_agents):
    agent_id = _agent_id("CHATGPT_CHOKE_BOM_AGENT_ID", "Choke BOM Analyzer")
    instructions = [
        "This starts from customer input.",
        "Do not calculate final price.",
        "Create structured BOM with ferrite, wire, tin, glue.",
        "Include component IDs and costing_route.",
        "Save or prepare JSON at save_address.",
        "Return JSON only.",
    ]
    input_text = _json_input_text(instructions, customer_input, save_address)
    trigger = _trigger_or_plan(
        agent_id,
        input_text,
        f"{customer_input['project_code']}:{customer_input['product_id']}:bom",
        f"{customer_input['project_code']}:{customer_input['product_id']}:bom:v1",
        dry_run,
        trigger_agents,
    )
    return {
        "agent_id": trigger.get("agent_id") or agent_id,
        "status": _trigger_status(trigger, dry_run, trigger_agents),
        "conversation_key": trigger.get("conversation_key"),
        "idempotency_key": trigger.get("idempotency_key"),
        "save_address": save_address,
        "input_text": input_text,
        "trigger_result": trigger,
    }


def _build_component_agent_call(customer_input, unit_data, component, save_address, dry_run, trigger_agents):
    agent_id = _agent_id("CHATGPT_EXTERNAL_COMPONENT_AGENT_ID", "External Component Costing Agent")
    payload = {
        "project_code": customer_input["project_code"],
        "product_id": customer_input["product_id"],
        "component_id": component["component_id"],
        "component_type": component["component_type"],
        "component_definition": component.get("bom_definition") or {},
        "annual_quantity": customer_input["annual_quantity"],
        "destination_zone": customer_input["customer_delivery_zone"],
        "production_plant": unit_data.get("plant"),
        "reporting_currency": unit_data.get("selling_currency"),
        "save_address": save_address,
    }
    instructions = [
        "This is one external component only.",
        "Do not cost complete choke.",
        "Use production plant, annual quantity and destination.",
        "Use SharePoint purchasing history if available.",
        "Return JSON only according to costing-output-spec.",
        "Save or prepare JSON at save_address.",
    ]
    input_text = _json_input_text(instructions, payload, save_address)
    trigger = _trigger_or_plan(
        agent_id,
        input_text,
        f"{customer_input['project_code']}:{customer_input['product_id']}:component:{component['component_id']}",
        f"{customer_input['project_code']}:{customer_input['product_id']}:component:{component['component_id']}:v1",
        dry_run,
        trigger_agents,
    )
    return {
        "agent_id": trigger.get("agent_id") or agent_id,
        "status": _trigger_status(trigger, dry_run, trigger_agents),
        "component_id": component["component_id"],
        "component_type": component["component_type"],
        "save_address": save_address,
        "input_payload": payload,
        "input_text": input_text,
        "trigger_result": trigger,
    }


def _build_most_agent_call(customer_input, unit_data, work_package, save_address, dry_run, trigger_agents):
    agent_id = _agent_id("CHATGPT_MOST_AGENT_ID", "MOST Assemblage")
    payload = {
        "project_code": customer_input["project_code"],
        "product_id": customer_input["product_id"],
        "component_id": work_package["component_id"],
        "operation_id": work_package["operation_id"],
        "operation_name": work_package["operation_name"],
        "technical_inputs": work_package,
        "annual_quantity": customer_input["annual_quantity"],
        "plant": unit_data.get("plant"),
        "save_address": save_address,
    }
    instructions = [
        "This is one component-operation work package only.",
        "Do not process full product.",
        "Do not read SharePoint for Choke workflow.",
        "Use provided technical payload.",
        "Return JSON only according to most_cycle_output_template.",
        "Save or prepare JSON at save_address.",
    ]
    input_text = _json_input_text(instructions, payload, save_address)
    trigger = _trigger_or_plan(
        agent_id,
        input_text,
        f"{customer_input['project_code']}:{customer_input['product_id']}:most:{work_package['work_package_id']}",
        f"{customer_input['project_code']}:{customer_input['product_id']}:most:{work_package['work_package_id']}:v1",
        dry_run,
        trigger_agents,
    )
    return {
        "agent_id": trigger.get("agent_id") or agent_id,
        "status": _trigger_status(trigger, dry_run, trigger_agents),
        "work_package_id": work_package["work_package_id"],
        "component_id": work_package["component_id"],
        "operation_id": work_package["operation_id"],
        "operation_name": work_package["operation_name"],
        "save_address": save_address,
        "input_payload": payload,
        "input_text": input_text,
        "trigger_result": trigger,
    }


def _financial_result(
    material_cost,
    material_missing,
    dl_voh_result,
    unit_data,
    material_cost_breakdown=None,
    component_entries=None,
    force_status=None,
):
    missing_inputs = list(material_missing or [])
    missing_inputs.extend(dl_voh_result.get("missing_inputs") or [])
    transport_result = calculate_transport_cost_from_components(component_entries or [])
    missing_inputs.extend(transport_result.get("missing_inputs") or [])
    olivier_costs = apply_olivier_direct_foh_fee(dl_voh_result, unit_data, transport_result)
    dl = dl_voh_result.get("dl_cost_per_piece")
    voh = dl_voh_result.get("voh_cost_per_piece")
    tooling = dl_voh_result.get("tooling_adder_per_piece")
    direct_cost = olivier_costs.get("direct_cost_per_piece")
    manufacturing_cost = olivier_costs.get("manufacturing_cost_per_piece")

    return {
        "status": force_status or ("blocked" if missing_inputs else "calculated"),
        "currency": dl_voh_result.get("currency"),
        "material_cost_per_piece": material_cost,
        "dl_cost_per_piece": dl,
        "voh_cost_per_piece": voh,
        "transport_cost_per_piece": olivier_costs.get("transport_cost_per_piece"),
        "transport_breakdown_by_component": olivier_costs.get("transport_breakdown_by_component"),
        "tooling_adder_per_piece": tooling,
        "direct_cost_per_piece": direct_cost,
        "preliminary_direct_cost_per_piece": direct_cost,
        "foh_percent_dc": olivier_costs.get("foh_percent_dc"),
        "foh_cost_per_piece": olivier_costs.get("foh_cost_per_piece"),
        "fee_percent_dc": olivier_costs.get("fee_percent_dc"),
        "fee_cost_per_piece": olivier_costs.get("fee_cost_per_piece"),
        "manufacturing_cost_per_piece": manufacturing_cost,
        "preliminary_manufacturing_cost_per_piece": manufacturing_cost,
        "work_package_calculation": dl_voh_result.get("work_package_calculation") or [],
        "material_cost_breakdown": material_cost_breakdown or [],
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
        "assumptions": dl_voh_result.get("assumptions") or [],
    }


def run_choke_orchestration(
    customer_input,
    dry_run=True,
    trigger_agents=False,
    trigger_bom=True,
    trigger_components=True,
    trigger_most=True,
    bom_json=None,
    component_cost_outputs=None,
    most_outputs=None,
    demo_override=True,
    full_demo_mode=False,
):
    validation = normalize_customer_input(customer_input)
    normalized_input = validation["customer_input"]
    missing_inputs = list(validation.get("missing_inputs") or [])

    manufacturing_strategy = get_master_manufacturing_strategy(
        normalized_input.get("product_line"),
        normalized_input.get("product"),
        normalized_input.get("customer_delivery_zone"),
    )
    missing_inputs.extend(manufacturing_strategy.get("missing_inputs") or [])
    unit_data = get_master_unit_data(manufacturing_strategy.get("production_plant"))
    missing_inputs.extend(unit_data.get("missing_inputs") or [])

    project_code = normalized_input.get("project_code")
    product_id = normalized_input.get("product_id") or normalized_input.get("part_number")
    bom_save_address = _save_address(project_code, product_id, "bom.json")
    bom_agent = _build_bom_agent_call(
        normalized_input,
        bom_save_address,
        dry_run or not trigger_bom,
        trigger_agents and trigger_bom,
    )
    if not trigger_bom:
        bom_agent["status"] = "skipped_by_request"
        if isinstance(bom_agent.get("trigger_result"), dict):
            bom_agent["trigger_result"]["status"] = "skipped_by_request"

    raw_bom = _parse_jsonish(bom_json, None)
    bom_status = "available" if raw_bom else bom_agent["status"]
    demo_bom_used = False
    if full_demo_mode and product_id == "316-5001":
        raw_bom = get_demo_bom_output_3165001(normalized_input)
        bom_status = "agent_sample_output"
        demo_bom_used = True
        if component_cost_outputs is None:
            component_cost_outputs = [
                *get_demo_component_cost_outputs_3165001(),
                get_demo_tin_cost(),
                get_demo_glue_cost(),
            ]
    elif raw_bom is None and demo_override and product_id == "316-5001":
        raw_bom = _demo_bom(normalized_input)
        bom_status = "available"
    normalized_components = _extract_components_from_bom(raw_bom) if raw_bom else []

    component_agent_calls = []
    component_entries = []
    for component in normalized_components:
        save_address = ""
        if component.get("costing_route") == "external_component_costing_agent":
            save_address = _save_address(
                project_code,
                product_id,
                f"components/{component['component_id']}.json",
            )
            component_agent_calls.append(_build_component_agent_call(
                normalized_input,
                unit_data,
                component,
                save_address,
                dry_run or not trigger_components,
                trigger_agents and trigger_components,
            ))
            if not trigger_components:
                component_agent_calls[-1]["status"] = "skipped_by_request"
                if isinstance(component_agent_calls[-1].get("trigger_result"), dict):
                    component_agent_calls[-1]["trigger_result"]["status"] = "skipped_by_request"
        component_entries.append(_normalized_component_entry(
            component,
            save_address=save_address,
        ))

    component_entries, material_cost, material_missing = _normalize_component_cost_outputs(
        component_entries,
        component_cost_outputs,
    )
    material_cost_breakdown = _build_material_cost_breakdown(component_entries)
    if full_demo_mode:
        material_cost = sum(
            item["delivered_cost_per_piece"] or 0
            for item in material_cost_breakdown
        )
        material_missing = []

    process_bom = raw_bom
    if process_bom is None and demo_override:
        process_bom = _demo_bom(normalized_input)
    process_result = decompose_choke_process(process_bom or {}, normalized_input)
    missing_inputs.extend(process_result.get("missing_rules") or [])

    most_work_packages = []
    most_agent_calls = []
    for work_package in process_result.get("work_packages") or []:
        save_address = _save_address(
            project_code,
            product_id,
            f"most/{work_package['work_package_id']}.json",
        )
        work_package = {**work_package, "save_address": save_address}
        most_agent_calls.append(_build_most_agent_call(
            normalized_input,
            unit_data,
            work_package,
            save_address,
            dry_run or not trigger_most,
            trigger_agents and trigger_most,
        ))
        if not trigger_most:
            most_agent_calls[-1]["status"] = "skipped_by_request"
            if isinstance(most_agent_calls[-1].get("trigger_result"), dict):
                most_agent_calls[-1]["trigger_result"]["status"] = "skipped_by_request"
        most_work_packages.append({
            "work_package_id": work_package["work_package_id"],
            "component_id": work_package["component_id"],
            "component_type": work_package["component_type"],
            "operation_id": work_package["operation_id"],
            "operation_name": work_package["operation_name"],
            "operation_definition": work_package,
            "most_status": "planned",
            "most_save_address": save_address,
            "agent_raw_output": None,
            "normalized_operation": {
                "p_h": work_package.get("p_h"),
                "oee": work_package.get("oee"),
                "parts_per_cycle": work_package.get("parts_per_cycle") or 1,
                "operator_percent": work_package.get("operator_percent"),
                "generic_capex_eur": work_package.get("generic_capex_eur"),
                "specific_capex_eur": work_package.get("specific_capex_eur"),
                "tooling_cost_eur": work_package.get("tooling_cost_eur"),
                "tooling_life_pieces": work_package.get("tooling_life_pieces"),
                "tooling_type": work_package.get("tooling_type"),
                "tooling_adder_per_piece_eur": work_package.get("tooling_adder_per_piece_eur"),
            },
        })

    dl_voh_source = _parse_jsonish(most_outputs, None) if most_outputs is not None else most_work_packages
    dl_voh_result = calculate_dl_voh(
        dl_voh_source,
        unit_data,
        normalized_input.get("annual_quantity"),
    )
    full_demo_confirmations = []
    if full_demo_mode:
        full_demo_confirmations = [
            "ferrite commercial confirmation",
            "wire commercial confirmation",
            "glue requirement confirmation",
            "tin price confirmation",
        ]
    financial = _financial_result(
        material_cost,
        material_missing + full_demo_confirmations,
        dl_voh_result,
        unit_data,
        material_cost_breakdown=material_cost_breakdown,
        component_entries=component_entries,
        force_status="calculated_preliminary_demo" if full_demo_mode else None,
    )

    if full_demo_mode:
        mode = "full_demo_mode"
    elif trigger_agents:
        mode = "workspace_trigger_mode"
    else:
        mode = "dry_run" if dry_run else "manual_outputs"
    envelope = build_standard_choke_envelope(
        project={
            "project_code": normalized_input.get("project_code"),
            "customer": normalized_input.get("customer"),
            "final_customer": normalized_input.get("final_customer"),
            "product_line": normalized_input.get("product_line"),
            "product": normalized_input.get("product"),
            "product_id": normalized_input.get("product_id"),
            "part_number": normalized_input.get("part_number"),
            "drawing_reference": normalized_input.get("drawing_reference"),
            "customer_delivery_zone": normalized_input.get("customer_delivery_zone"),
            "annual_quantity": normalized_input.get("annual_quantity"),
            "target_price": normalized_input.get("target_price"),
            "target_price_currency": normalized_input.get("currency"),
        },
        manufacturing_strategy=manufacturing_strategy,
        unit_data=unit_data,
        agent_orchestration={
            "mode": mode,
            "bom_agent": bom_agent,
            "component_agent_calls": component_agent_calls,
            "most_agent_calls": most_agent_calls,
        },
        bom={
            "status": bom_status,
            "save_address": bom_save_address,
            "agent_raw_output": raw_bom if (bom_json or demo_bom_used) else None,
            "normalized_components": normalized_components,
            "process_decomposition": process_result,
        },
        components=component_entries,
        most_work_packages=most_work_packages,
        financial_calculation=financial,
        missing_inputs=missing_inputs + financial.get("missing_inputs", []),
        next_steps=[
            "Do not use demo preliminary values commercially.",
            "Load BOM JSON from save_address when Choke BOM Agent output is available.",
            "Run External Component Costing Agent component by component.",
            "Run MOST Assemblage component-operation by component-operation.",
            "Load saved component and MOST JSON outputs, then recalculate material cost, DL, VOH and tooling adder.",
            "Add FOH/FEE/P&L/cash flow when commercial workflow is ready.",
        ],
    )

    result_save_address = _save_address(project_code, product_id, "orchestration_result.json")
    envelope["orchestration_result_save_address"] = result_save_address
    envelope["orchestration_result_absolute_path"] = _write_json(result_save_address, envelope)
    return envelope
