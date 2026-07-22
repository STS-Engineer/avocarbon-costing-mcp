def build_standard_choke_costing_json(
    project=None,
    manufacturing_strategy=None,
    plant_data=None,
    agent_outputs=None,
    financial_calculation=None,
    missing_inputs=None,
    next_steps=None,
):
    envelope = {
        "schema_version": "choke_costing_orchestration_v1",
        "project": {
            "project_code": "",
            "product_line": "",
            "product": "",
            "product_id": "",
            "customer_delivery_zone": "",
            "annual_quantity": 0,
            "choke_family": "choke",
            "choke_subtype": "unknown_choke",
            "raw_detected_product_name": None,
            "classification_evidence": [],
            "classification_source": "unresolved",
            "classification_confidence": "low",
            "unresolved_classification_questions": [],
        },
        "manufacturing_strategy": {},
        "plant_data": {},
        "agent_outputs": {
            "bom": {
                "status": "missing",
                "save_address": "",
                "agent_id": "",
                "data": None,
            },
            "components": [],
            "process_decomposition": {
                "status": "missing",
                "save_address": "",
                "data": None,
            },
            "operations": [],
        },
        "financial_calculation": {
            "status": "not_started",
            "currency": "",
            "dl_cost_per_piece": None,
            "voh_cost_per_piece": None,
            "transport_cost_per_piece": None,
            "transport_breakdown_by_component": [],
            "direct_cost_per_piece": None,
            "foh_percent_dc": None,
            "foh_cost_per_piece": None,
            "fee_percent_dc": None,
            "fee_cost_per_piece": None,
            "manufacturing_cost_per_piece": None,
            "material_cost_per_piece": None,
            "preliminary_manufacturing_cost_per_piece": None,
            "operations_calculation": [],
            "missing_inputs": [],
        },
        "missing_inputs": [],
        "next_steps": [],
    }

    if project:
        envelope["project"].update(project)
    if manufacturing_strategy:
        envelope["manufacturing_strategy"] = manufacturing_strategy
    if plant_data:
        envelope["plant_data"] = plant_data
    if agent_outputs:
        envelope["agent_outputs"].update(agent_outputs)
    if financial_calculation:
        envelope["financial_calculation"].update(financial_calculation)
    if missing_inputs:
        envelope["missing_inputs"] = list(dict.fromkeys(missing_inputs))
    if next_steps:
        envelope["next_steps"] = next_steps

    return envelope


def build_standard_choke_envelope(
    project=None,
    manufacturing_strategy=None,
    unit_data=None,
    agent_orchestration=None,
    bom=None,
    components=None,
    most_work_packages=None,
    financial_calculation=None,
    missing_inputs=None,
    next_steps=None,
):
    envelope = {
        "schema_version": "avocarbon_choke_costing_v1",
        "project": {
            "project_code": "",
            "customer": "",
            "final_customer": "",
            "product_line": "",
            "product": "",
            "product_id": "",
            "part_number": "",
            "drawing_reference": "",
            "customer_delivery_zone": "",
            "annual_quantity": 0,
            "target_price": None,
            "target_price_currency": None,
            "choke_family": "choke",
            "choke_subtype": "unknown_choke",
            "raw_detected_product_name": None,
            "classification_evidence": [],
            "classification_source": "unresolved",
            "classification_confidence": "low",
            "unresolved_classification_questions": [],
        },
        "manufacturing_strategy": {},
        "unit_data": {},
        "agent_orchestration": {
            "mode": "dry_run",
            "bom_agent": {
                "agent_id": "",
                "status": "",
                "conversation_key": "",
                "idempotency_key": "",
                "save_address": "",
            },
            "component_agent_calls": [],
            "most_agent_calls": [],
        },
        "bom": {
            "status": "missing",
            "save_address": "",
            "agent_raw_output": None,
            "normalized_components": [],
        },
        "components": [],
        "most_work_packages": [],
        "financial_calculation": {
            "status": "not_started",
            "currency": "",
            "material_cost_per_piece": None,
            "dl_cost_per_piece": None,
            "voh_cost_per_piece": None,
            "transport_cost_per_piece": None,
            "transport_breakdown_by_component": [],
            "tooling_adder_per_piece": None,
            "direct_cost_per_piece": None,
            "preliminary_direct_cost_per_piece": None,
            "foh_percent_dc": None,
            "foh_cost_per_piece": None,
            "fee_percent_dc": None,
            "fee_cost_per_piece": None,
            "manufacturing_cost_per_piece": None,
            "preliminary_manufacturing_cost_per_piece": None,
            "work_package_calculation": [],
            "missing_inputs": [],
        },
        "missing_inputs": [],
        "next_steps": [],
    }

    if project:
        envelope["project"].update(project)
    if manufacturing_strategy:
        envelope["manufacturing_strategy"] = manufacturing_strategy
    if unit_data:
        envelope["unit_data"] = unit_data
    if agent_orchestration:
        envelope["agent_orchestration"].update(agent_orchestration)
    if bom:
        envelope["bom"].update(bom)
    if components is not None:
        envelope["components"] = components
    if most_work_packages is not None:
        envelope["most_work_packages"] = most_work_packages
    if financial_calculation:
        envelope["financial_calculation"].update(financial_calculation)
    if missing_inputs:
        envelope["missing_inputs"] = list(dict.fromkeys(missing_inputs))
    if next_steps:
        envelope["next_steps"] = next_steps

    return envelope
