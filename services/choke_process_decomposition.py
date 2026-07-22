"""Compatibility adapter for callers of the former Choke rule engine.

All routing decisions now live in services.choke_process_routing. This module
keeps the public function name used by older scripts without retaining an
independent routing catalog or defaulting uncertain products to glued/rod rules.
"""

from typing import Any, Dict

from services.choke_classification import classify_choke
from services.choke_process_routing import build_choke_process_route


def _minimal_normalized_bom(bom_json: Any) -> Dict[str, Any]:
    if isinstance(bom_json, dict) and isinstance(bom_json.get("components"), list):
        components = bom_json["components"]
    elif isinstance(bom_json, dict) and isinstance(bom_json.get("bom"), list):
        components = bom_json["bom"]
    else:
        components = []
    return {
        "status": "normalized",
        "components": components,
        "raw_bom": bom_json if isinstance(bom_json, dict) else {},
    }


def decompose_choke_process(bom_json: Any, customer_input: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _minimal_normalized_bom(bom_json)
    classification = classify_choke(customer_input or {}, normalized["raw_bom"])
    result = build_choke_process_route(customer_input or {}, normalized, classification)
    return {
        **result,
        "missing_rules": result.get("missing_inputs") or [],
    }

