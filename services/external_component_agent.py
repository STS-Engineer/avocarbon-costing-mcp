import json
import os
import re
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
AGENT_CONFIG_PATH = BASE_DIR / "agents" / "external_component_costing_agent.yaml"

PROMPT_ROUTING = {
    "stamped_part": "Stamping prompt V2",
    "plastic_part": "Injection prompt V2",
    "electronic_component": "Electronic prompt V2",
    "ferrite_component": "Ferrite prompt V1",
    "enameled_wire": "Enameled wire prompt V1",
    "spring": "Spring prompt V1",
}

FORBIDDEN_FAMILIES = {
    "complete_choke",
    "full_product",
    "assembly",
    "internal_component",
}


def normalize_text(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def payload_text(component_payload):
    return json.dumps(component_payload or {}, ensure_ascii=False, default=str).lower()


def has_value(value):
    return value not in [None, "", [], {}]


def get_scope_note(component_payload):
    payload = component_payload or {}
    definition = payload.get("component_definition") or {}
    if isinstance(definition, dict):
        return payload.get("scope_note") or definition.get("scope_note") or definition.get("scope")
    return payload.get("scope_note")


def classify_component_family(component_payload):
    payload = component_payload or {}
    component_type = normalize_text(payload.get("component_type"))
    definition_text = payload_text(payload.get("component_definition"))
    scope_note = normalize_text(get_scope_note(payload))
    combined = " ".join([component_type, definition_text, scope_note])

    if payload.get("is_internal") is True or "internal" in combined:
        return "internal_component"
    if any(keyword in combined for keyword in ["complete_choke", "full_choke", "rod_choke"]):
        return "complete_choke"
    if component_type in ["choke", "rod_choke"]:
        return "complete_choke"
    if any(keyword in combined for keyword in ["full_product", "complete_product"]):
        return "full_product"
    if component_type in ["assembly", "assy"] or "assembly" in combined:
        return "assembly"

    if any(keyword in combined for keyword in ["stamped", "stamping", "busbar", "metal_stamp"]):
        return "stamped_part"
    if any(keyword in combined for keyword in ["plastic", "injection", "bushing"]):
        return "plastic_part"
    if any(keyword in combined for keyword in ["electronic", "electronics", "capacitor", "pth", "pcb"]):
        return "electronic_component"
    if "ferrite" in combined or component_type == "core":
        return "ferrite_component"
    if any(keyword in combined for keyword in ["enameled_wire", "enamelled_wire", "magnet_wire"]):
        return "enameled_wire"
    if component_type == "wire" and "raw_material" in scope_note:
        return "enameled_wire"
    if "spring" in combined:
        return "spring"

    return "unknown"


def validate_component_payload(component_payload):
    payload = component_payload or {}
    classified_family = classify_component_family(payload)
    missing_inputs = []
    blocking_reasons = []

    if classified_family in FORBIDDEN_FAMILIES:
        blocking_reasons.append(
            f"{classified_family} is outside External Component Costing Agent scope"
        )

    if not has_value(payload.get("annual_quantity")):
        missing_inputs.append("annual_quantity")

    if not has_value(payload.get("destination_zone")) and not has_value(payload.get("production_plant")):
        missing_inputs.append("destination_zone or production_plant")

    if not has_value(payload.get("component_definition")):
        missing_inputs.append("component_definition")

    if not has_value(payload.get("save_address")):
        missing_inputs.append("save_address")

    if classified_family == "unknown":
        blocking_reasons.append("component family could not be classified")

    status = "blocked" if missing_inputs or blocking_reasons else "ready_for_agent_call"

    return {
        "status": status,
        "classified_family": classified_family,
        "missing_inputs": missing_inputs,
        "blocking_reasons": blocking_reasons,
    }


def shortest_missing_information_request(validation_result):
    missing_inputs = validation_result.get("missing_inputs") or []
    blocking_reasons = validation_result.get("blocking_reasons") or []
    needed = missing_inputs or blocking_reasons
    return {
        "status": "blocked",
        "missing_information_request": ", ".join(needed[:3]),
    }


def build_agent_prompt(component_payload):
    payload = component_payload or {}
    validation = validate_component_payload(payload)
    classified_family = validation["classified_family"]
    selected_prompt_file = PROMPT_ROUTING.get(classified_family)

    if validation["status"] == "blocked":
        return json.dumps(
            shortest_missing_information_request(validation),
            ensure_ascii=False,
        )

    raw_material_only_clause = ""
    if classified_family == "enameled_wire" and normalize_text(get_scope_note(payload)) == "raw_material_only":
        raw_material_only_clause = (
            "\nFor this enameled wire request, cost raw material only. "
            "Explicitly exclude winding, forming, tooling, fixture and added value."
        )

    return f"""You are External Component Costing Agent.
Role: automotive costing expert focused on external industrial component cost evaluation.
Scope: external industrial components only. Do not cost internal components, complete chokes, full products or assemblies.
Selected component family: {classified_family}
Selected prompt file: {selected_prompt_file}
Classification field in output must always be: External
Actual sourcing origin must only be placed in recommended_offer.origin.
Use AVOCarbon Purchasing data.xlsx as internal purchasing benchmark when available.
Prefer local-for-local sourcing when credible.
Do not present the result as a supplier quotation.
Do not present unconfirmed values as commercially usable.
Return JSON only.
Save the resulting JSON to this backend address: {payload.get("save_address")}{raw_material_only_clause}

Component payload:
{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}
"""


def run_external_component_agent(component_payload, dry_run=True):
    validation = validate_component_payload(component_payload)
    classified_family = validation["classified_family"]
    selected_prompt_file = PROMPT_ROUTING.get(classified_family)
    save_address = (component_payload or {}).get("save_address")
    prompt_to_send = build_agent_prompt(component_payload)

    result = {
        "status": validation["status"],
        "validation": validation,
        "classified_family": classified_family,
        "selected_prompt_file": selected_prompt_file,
        "save_address": save_address,
        "prompt_to_send": prompt_to_send,
        "agent_config_path": str(AGENT_CONFIG_PATH),
    }

    if dry_run:
        return result

    if not os.getenv("OPENAI_API_KEY"):
        return {
            **result,
            "status": "blocked",
            "reason": "OPENAI_API_KEY is not available",
        }

    try:
        return {
            **result,
            "status": "ready_for_api_call",
            "call_structure": {
                "model": os.getenv("EXTERNAL_COMPONENT_AGENT_MODEL", "gpt-4.1"),
                "messages": [
                    {
                        "role": "system",
                        "content": "Return JSON only for external component costing.",
                    },
                    {
                        "role": "user",
                        "content": prompt_to_send,
                    },
                ],
            },
            "note": "API call is prepared but not executed in V1.",
        }
    except Exception as exc:
        return {
            **result,
            "status": "blocked",
            "reason": f"Failed to prepare API call: {exc}",
        }
