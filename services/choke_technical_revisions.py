"""Deterministic technical revisions and revision-aware reconciliation.

This module intentionally contains no product, project, component, or operation
identifiers.  It derives authority from the accepted BOM and process payloads,
not from filenames or modification timestamps.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional


REVISION_SCHEMA_VERSION = "1.0"
_VOLATILE_KEYS = {
    "accepted_at",
    "created_at",
    "received_at",
    "saved_at",
    "updated_at",
    "revision",
    "technical_revision",
    "output_revision",
}

_UNIT_DEFINITIONS = {
    "pc": ("count", Decimal("1")),
    "kg": ("mass", Decimal("1")),
    "g": ("mass", Decimal("0.001")),
    "m": ("length", Decimal("1")),
    "mm": ("length", Decimal("0.001")),
    "l": ("volume", Decimal("1")),
    "ml": ("volume", Decimal("0.001")),
    "cm3": ("volume", Decimal("0.001")),
    "mm3": ("volume", Decimal("0.000001")),
}
_UNIT_ALIASES = {
    "pcs": "pc",
    "piece": "pc",
    "pieces": "pc",
    "unit": "pc",
    "units": "pc",
    "kilogram": "kg",
    "kilograms": "kg",
    "gram": "g",
    "grams": "g",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "millimeter": "mm",
    "millimeters": "mm",
    "millimetre": "mm",
    "millimetres": "mm",
    "liter": "l",
    "litre": "l",
    "liters": "l",
    "litres": "l",
}


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
        if not match:
            return None
        try:
            return Decimal(match.group(0).replace(",", "."))
        except InvalidOperation:
            return None


def normalize_unit(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower().replace("³", "3")
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    text = _UNIT_ALIASES.get(text, text)
    return text if text in _UNIT_DEFINITIONS else None


def _unit_from_value(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower().replace("³", "3")
    for pattern, unit in (
        (r"\b(?:pcs?|pieces?|pce|units?)\b", "pc"),
        (r"\bkg\b", "kg"),
        (r"\b(?:g|grams?|grammes?)\b", "g"),
        (r"\bmm3\b", "mm3"),
        (r"\bcm3\b", "cm3"),
        (r"\bml\b", "ml"),
        (r"\b(?:l|liters?|litres?)\b", "l"),
        (r"\bmm\b", "mm"),
        (r"\b(?:m|meters?|metres?)\b", "m"),
    ):
        if re.search(pattern, text):
            return unit
    return None


def convert_quantity(value: Any, source_unit: Any, target_unit: Any) -> Optional[Decimal]:
    source = normalize_unit(source_unit)
    target = normalize_unit(target_unit)
    amount = _decimal(value)
    if amount is None or source is None or target is None:
        return None
    source_dimension, source_factor = _UNIT_DEFINITIONS[source]
    target_dimension, target_factor = _UNIT_DEFINITIONS[target]
    if source_dimension != target_dimension:
        return None
    return amount * source_factor / target_factor


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")
    return value


def deterministic_revision(kind: str, payload: Any) -> str:
    encoded = json.dumps(
        _canonical(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{kind}-sha256:{hashlib.sha256(encoded).hexdigest()}"


def raw_bom_revision(raw_bom: Mapping[str, Any]) -> str:
    return deterministic_revision("raw-bom", raw_bom)


def component_input_projection(component: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "component_id": component.get("component_id"),
        "component": component.get("component"),
        "category": component.get("category"),
        "costing_route": component.get("costing_route"),
        "external_component_type": component.get("external_component_type"),
        "quantity_per_product": component.get("quantity_per_product"),
        "quantity_unit": component.get("quantity_unit"),
        "component_definition": component.get("component_definition") or {},
        "excluded_from_costing": component.get("excluded_from_costing") is True,
        "costing_relevance": component.get("costing_relevance"),
    }


def component_input_revision(component: Mapping[str, Any]) -> str:
    return deterministic_revision("component-input", component_input_projection(component))


def normalized_bom_projection(normalized_bom: Mapping[str, Any]) -> Dict[str, Any]:
    components = [
        component_input_projection(item)
        for item in normalized_bom.get("components") or []
        if isinstance(item, Mapping)
    ]
    components.sort(key=lambda item: str(item.get("component_id") or ""))
    return {
        "components": components,
        "choke_classification": normalized_bom.get("choke_classification") or {},
        "process_scopes_for_most": normalized_bom.get("process_scopes_for_most") or [],
    }


def normalized_bom_revision(normalized_bom: Mapping[str, Any]) -> str:
    return deterministic_revision("normalized-bom", normalized_bom_projection(normalized_bom))


def work_package_projection(work_package: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "work_package_id": work_package.get("work_package_id"),
        "operation_id": work_package.get("operation_id"),
        "operation_key": work_package.get("operation_key"),
        "operation_name": work_package.get("operation_name"),
        "status": work_package.get("status"),
        "blocking_reason": work_package.get("blocking_reason"),
        "component_ids": sorted(str(item) for item in work_package.get("component_ids") or []),
        "technical_inputs": work_package.get("technical_inputs") or {},
        "annual_quantity": work_package.get("annual_quantity"),
        "production_plant": work_package.get("production_plant"),
    }


def work_package_input_revision(
    work_package: Mapping[str, Any],
    source_bom_revision: Optional[str],
) -> str:
    dependency_revisions = work_package.get("source_component_revisions") or {}
    return deterministic_revision(
        "work-package-input",
        {
            "source_component_revisions": dependency_revisions,
            "source_bom_revision": (
                source_bom_revision
                if not work_package.get("component_ids")
                and not dependency_revisions
                else None
            ),
            "work_package": work_package_projection(work_package),
        },
    )


def process_projection(process: Mapping[str, Any]) -> Dict[str, Any]:
    packages = [
        work_package_projection(item)
        for item in process.get("work_packages") or []
        if isinstance(item, Mapping)
    ]
    packages.sort(key=lambda item: str(item.get("work_package_id") or ""))
    required = required_work_package_ids(process)
    return {
        "source_bom_revision": process.get("source_bom_revision"),
        "required_work_package_ids": required,
        "work_packages": packages,
    }


def process_revision(process: Mapping[str, Any]) -> str:
    return deterministic_revision("process", process_projection(process))


def output_revision(kind: str, output: Mapping[str, Any]) -> str:
    return deterministic_revision(kind, output)


def attach_bom_revisions(
    raw_bom: Mapping[str, Any],
    normalized_bom: Dict[str, Any],
) -> Dict[str, Any]:
    normalized = deepcopy(normalized_bom)
    raw_revision = raw_bom_revision(raw_bom)
    normalized_revision = normalized_bom_revision(normalized)
    normalized["revision_schema_version"] = REVISION_SCHEMA_VERSION
    normalized["source_raw_bom_revision"] = raw_revision
    normalized["technical_revision"] = normalized_revision
    for component in normalized.get("components") or []:
        if isinstance(component, dict):
            component["source_bom_revision"] = normalized_revision
            component["technical_revision"] = component_input_revision(component)
    return normalized


def attach_process_revisions(
    process: Dict[str, Any],
    source_bom_revision: str,
    normalized_bom: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    revised = deepcopy(process)
    revised["revision_schema_version"] = REVISION_SCHEMA_VERSION
    revised["source_bom_revision"] = source_bom_revision
    component_revisions = {
        str(item.get("component_id")): (
            item.get("technical_revision") or component_input_revision(item)
        )
        for item in (normalized_bom or {}).get("components") or []
        if isinstance(item, Mapping) and item.get("component_id")
    }
    for package in revised.get("work_packages") or []:
        if isinstance(package, dict):
            package["source_bom_revision"] = source_bom_revision
            package["source_component_revisions"] = {
                str(component_id): component_revisions[str(component_id)]
                for component_id in package.get("component_ids") or []
                if str(component_id) in component_revisions
            }
            package["technical_revision"] = work_package_input_revision(
                package, source_bom_revision
            )
    revised["technical_revision"] = process_revision(revised)
    for package in revised.get("work_packages") or []:
        if isinstance(package, dict):
            package["source_process_revision"] = revised["technical_revision"]
    return revised


def required_work_package_ids(process: Mapping[str, Any]) -> List[str]:
    explicit = [
        str(item)
        for item in process.get("required_work_package_ids") or []
        if item not in (None, "")
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    required = []
    for package in process.get("work_packages") or []:
        if not isinstance(package, Mapping):
            continue
        status = str(package.get("status") or "required").lower()
        if status not in {"blocked", "excluded", "optional", "not_required"}:
            identifier = package.get("work_package_id")
            if identifier:
                required.append(str(identifier))
    return list(dict.fromkeys(required))


def classify_component(component: Mapping[str, Any]) -> str:
    if component.get("excluded_from_costing") is True:
        return "excluded"
    route = str(component.get("costing_route") or "").strip().lower()
    if route in {"external_component_costing_agent", "external_component_costing"}:
        return "externally_costed"
    if route in {"internal_costing", "internal_manufacturing", "manufacturing"}:
        return "internally_manufactured"
    if route in {"consumable", "rule_based_consumable"}:
        return "consumable"
    if route in {"process_only", "most_only", "operation_only"}:
        return "process_only"
    category = str(component.get("category") or "").strip().lower()
    if category in {
        "externally_costed",
        "internally_manufactured",
        "consumable",
        "process_only",
        "excluded",
    }:
        return category
    return "unresolved"


def _containers(component: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield component
    for key in (
        "component_definition",
        "technical_specification",
        "specification",
        "calculation",
        "calculations",
        "consumption",
    ):
        value = component.get(key)
        if isinstance(value, Mapping):
            yield value


def _first(component: Mapping[str, Any], names: Iterable[str]) -> Any:
    for container in _containers(component):
        for name in names:
            value = container.get(name)
            if value not in (None, ""):
                return value
    return None


def quantity_candidates(component: Mapping[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    definitions = (
        ("count_per_product", ("count_per_product", "bom_count_per_product", "piece_count_per_product"), "pc"),
        ("mass_kg_per_product", ("mass_kg_per_product", "weight_kg_per_product"), "kg"),
        ("mass_g_per_product", (
            "mass_g_per_product", "physical_mass_g_per_product",
            "weight_g_per_product", "line_weight_g_per_product",
            "line_weight_g", "estimated_total_weight_g_per_product",
        ), "g"),
        ("length_m_per_product", (
            "length_m_per_product", "physical_length_m_per_product",
            "developed_length_m", "developed_length_m_per_product",
        ), "m"),
        ("length_mm_per_product", ("length_mm_per_product", "physical_length_mm_per_product", "developed_length_mm"), "mm"),
        ("volume_l_per_product", ("volume_l_per_product", "volume_l"), "l"),
        ("volume_ml_per_product", ("volume_ml_per_product", "volume_ml"), "ml"),
        ("volume_cm3_per_product", ("volume_cm3_per_product", "volume_cm3"), "cm3"),
        ("volume_mm3_per_product", ("volume_mm3_per_product", "volume_mm3"), "mm3"),
        ("operation_consumption", ("operation_consumption", "consumption_per_product"), None),
    )
    for source, names, fixed_unit in definitions:
        value = _first(component, names)
        unit = fixed_unit
        if isinstance(value, Mapping):
            unit = normalize_unit(value.get("unit")) or unit
            value = value.get("value") if value.get("value") is not None else value.get("quantity")
        amount = _decimal(value)
        unit = normalize_unit(unit or _first(component, ("operation_consumption_unit", "consumption_unit")))
        if amount is not None and unit:
            candidates.append({"value": amount, "unit": unit, "source": source})
    quantity = _first(component, ("quantity_per_product", "quantity_per_assembly", "quantity", "qty"))
    quantity_unit = _first(component, ("quantity_unit", "technical_quantity_unit", "unit"))
    if isinstance(quantity, Mapping):
        quantity_unit = quantity.get("unit") or quantity_unit
        quantity = quantity.get("value") if quantity.get("value") is not None else quantity.get("quantity")
    amount = _decimal(quantity)
    unit = normalize_unit(quantity_unit) or _unit_from_value(quantity)
    if amount is not None and unit:
        candidates.append({"value": amount, "unit": unit, "source": "quantity_per_product"})
    unique: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in candidates:
        unique[(format(item["value"], "f"), item["unit"])] = item
    return list(unique.values())


def resolve_technical_quantity(
    component: Mapping[str, Any],
    pricing_unit: Any,
    mode: str = "firm",
    approved_assumption_rules: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    target = normalize_unit(pricing_unit)
    if target is None:
        return {
            "resolution_status": "blocked",
            "quantity": None,
            "unit": None,
            "source": None,
            "formula": None,
            "assumptions": [],
            "confirmation_questions": ["Confirm the supplier pricing unit."],
        }
    candidates = quantity_candidates(component)
    for candidate in candidates:
        converted = convert_quantity(candidate["value"], candidate["unit"], target)
        if converted is not None:
            return {
                "resolution_status": "confirmed",
                "quantity": float(converted),
                "unit": f"{target}/product",
                "source": candidate["source"],
                "formula": (
                    None
                    if candidate["unit"] == target
                    else f"{candidate['value']} {candidate['unit']} converted to {target}"
                ),
                "assumptions": [],
                "confirmation_questions": [],
                "source_quantity": {
                    "value": float(candidate["value"]),
                    "unit": f"{candidate['unit']}/product",
                },
                "conversion": {
                    "method": "unit_conversion",
                    "source_unit": candidate["unit"],
                    "target_unit": target,
                },
            }

    # Configurable physics resolver: any cylindrical material can opt in by
    # providing length, diameter, and density. No component name is consulted.
    length = next(
        (item for item in candidates if _UNIT_DEFINITIONS[item["unit"]][0] == "length"),
        None,
    )
    diameter = _decimal(_first(component, ("diameter_mm", "wire_diameter_mm")))
    density = _decimal(_first(component, ("density_g_cm3", "material_density_g_cm3")))
    if (
        length
        and target in {"kg", "g"}
        and diameter is not None
        and density is not None
    ):
        length_mm = convert_quantity(length["value"], length["unit"], "mm")
        pi = Decimal("3.141592653589793238462643383")
        volume_mm3 = pi * diameter * diameter / Decimal("4") * length_mm
        mass_g = volume_mm3 / Decimal("1000") * density
        converted = convert_quantity(mass_g, "g", target)
        return {
            "resolution_status": "confirmed",
            "quantity": float(converted),
            "unit": f"{target}/product",
            "source": length["source"],
            "formula": "cylindrical volume x explicit material density",
            "assumptions": [],
            "confirmation_questions": [],
            "source_quantity": {
                "value": float(length["value"]),
                "unit": f"{length['unit']}/product",
            },
            "conversion": {
                "method": "cylindrical_length_diameter_density_to_mass",
                "diameter_mm": float(diameter),
                "density_g_cm3": float(density),
            },
        }

    if mode == "preliminary":
        for rule in approved_assumption_rules or []:
            if not isinstance(rule, Mapping) or rule.get("approved") is not True:
                continue
            unit = normalize_unit(rule.get("unit"))
            converted = convert_quantity(rule.get("value"), unit, target)
            if converted is not None:
                return {
                    "resolution_status": "estimated",
                    "quantity": float(converted),
                    "unit": f"{target}/product",
                    "source": rule.get("source") or "approved_assumption_rule",
                    "formula": rule.get("formula"),
                    "assumptions": [dict(rule)],
                    "confirmation_questions": list(rule.get("confirmation_questions") or []),
                }
    return {
        "resolution_status": "blocked",
        "quantity": None,
        "unit": None,
        "source": None,
        "formula": None,
        "assumptions": [],
        "confirmation_questions": [
            f"Confirm a technical quantity compatible with {target}/product."
        ],
    }


def _component_map(normalized_bom: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {
        str(item.get("component_id")): item
        for item in normalized_bom.get("components") or []
        if isinstance(item, Mapping) and item.get("component_id")
    }


def reconcile_component_outputs(
    normalized_bom: Mapping[str, Any],
    state: Mapping[str, Any],
    output_loader: Callable[[str], Optional[Mapping[str, Any]]],
) -> Dict[str, Any]:
    current_bom_revision = normalized_bom.get("technical_revision")
    valid, missing, stale, blocked, legacy, obsolete = [], [], [], [], [], []
    current = _component_map(normalized_bom)
    state_outputs = state.get("components") or {}
    for component_id, component in current.items():
        classification = classify_component(component)
        current_component_revision = (
            component.get("technical_revision") or component_input_revision(component)
        )
        base = {
            "component_id": component_id,
            "classification": classification,
            "source_bom_revision": current_bom_revision,
            "source_component_revision": current_component_revision,
        }
        if classification in {"excluded", "process_only", "internally_manufactured"}:
            blocked.append({**base, "status": "blocked", "status_reason": classification})
            continue
        if classification == "unresolved":
            blocked.append({**base, "status": "blocked", "status_reason": "costing_route_unresolved"})
            continue
        saved = output_loader(component_id)
        if not saved:
            missing.append({**base, "status": "missing", "status_reason": "output_not_found"})
            continue
        output_status = str(saved.get("analysis_status") or saved.get("status") or "").lower()
        if output_status == "blocked":
            blocked.append({**base, "status": "blocked", "status_reason": "saved_output_blocked"})
            continue
        saved_bom_revision = saved.get("source_bom_revision")
        saved_component_revision = saved.get("source_component_revision")
        if saved_bom_revision and saved_component_revision:
            if saved_component_revision == current_component_revision:
                valid.append({
                    **base,
                    "status": "valid",
                    "status_reason": (
                        "source_revisions_match"
                        if saved_bom_revision == current_bom_revision
                        else "component_input_unchanged_across_bom_revision"
                    ),
                    "saved_output_revision": saved.get("technical_revision") or saved.get("output_revision"),
                })
            else:
                stale.append({
                    **base,
                    "status": "stale",
                    "status_reason": "source_revision_mismatch",
                    "saved_source_bom_revision": saved_bom_revision,
                    "saved_source_component_revision": saved_component_revision,
                })
        else:
            stored_snapshot = saved.get("source_component_snapshot")
            if (
                isinstance(stored_snapshot, Mapping)
                and component_input_revision(stored_snapshot) == current_component_revision
            ):
                valid.append({
                    **base,
                    "status": "valid",
                    "status_reason": "legacy_inputs_deterministically_verified",
                    "legacy_compatibility_verified": True,
                })
            else:
                legacy.append({
                    **base,
                    "status": "legacy_unverified",
                    "status_reason": "source_revision_metadata_missing",
                })
    for component_id, entry in state_outputs.items():
        if component_id not in current and isinstance(entry, Mapping):
            obsolete.append({
                "component_id": component_id,
                "status": "obsolete_for_current_revision",
                "status_reason": "component_not_in_current_bom",
                "preservation_note": "Preserved for audit; excluded from the current technical revision.",
            })
    return {
        "current_bom_revision": current_bom_revision,
        "current_components": list(current),
        "valid_components": valid,
        "missing_components": missing,
        "stale_components": stale,
        "blocked_components": blocked,
        "legacy_unverified_components": legacy,
        "obsolete_components": obsolete,
    }


def reconcile_most_outputs(
    process: Mapping[str, Any],
    state: Mapping[str, Any],
    output_loader: Callable[[str], Optional[Mapping[str, Any]]],
) -> Dict[str, Any]:
    process_revision_value = process.get("technical_revision")
    required_ids = required_work_package_ids(process)
    package_map = {
        str(item.get("work_package_id")): item
        for item in process.get("work_packages") or []
        if isinstance(item, Mapping) and item.get("work_package_id")
    }
    valid, missing, stale, blocked, received_not_normalized, legacy, obsolete = (
        [], [], [], [], [], [], []
    )
    state_outputs = state.get("most") or {}
    for work_package_id in required_ids:
        package = package_map.get(work_package_id, {})
        base = {
            "work_package_id": work_package_id,
            "operation_key": package.get("operation_key") or package.get("operation_id"),
            "source_process_revision": process_revision_value,
            "affected_component_ids": list(package.get("component_ids") or []),
        }
        if str(package.get("status") or "").lower() == "blocked":
            blocked.append({
                **base,
                "status": "blocked",
                "status_reason": package.get("blocking_reason") or "work_package_blocked",
            })
            continue
        saved = output_loader(work_package_id)
        state_entry = state_outputs.get(work_package_id) or {}
        if not saved:
            if state_entry.get("status") == "received":
                received_not_normalized.append({
                    **base,
                    "status": "received_not_normalized",
                    "status_reason": "state_received_but_normalized_output_missing",
                })
            else:
                missing.append({**base, "status": "missing", "status_reason": "output_not_found"})
            continue
        saved_process_revision = saved.get("source_process_revision")
        saved_package_revision = saved.get("source_work_package_revision")
        current_package_revision = (
            package.get("technical_revision")
            or work_package_input_revision(package, process.get("source_bom_revision"))
        )
        if saved_process_revision and saved_package_revision:
            if saved_package_revision == current_package_revision:
                valid.append({
                    **base,
                    "status": "valid",
                    "status_reason": (
                        "source_revisions_match"
                        if saved_process_revision == process_revision_value
                        else "work_package_input_unchanged_across_process_revision"
                    ),
                    "saved_output_revision": saved.get("technical_revision") or saved.get("output_revision"),
                })
            else:
                stale.append({
                    **base,
                    "status": "stale",
                    "status_reason": "source_revision_mismatch",
                    "saved_source_process_revision": saved_process_revision,
                    "saved_source_work_package_revision": saved_package_revision,
                })
        else:
            stored_snapshot = saved.get("source_work_package_snapshot")
            if (
                isinstance(stored_snapshot, Mapping)
                and work_package_input_revision(
                    stored_snapshot, process.get("source_bom_revision")
                ) == current_package_revision
            ):
                valid.append({
                    **base,
                    "status": "valid",
                    "status_reason": "legacy_inputs_deterministically_verified",
                    "legacy_compatibility_verified": True,
                })
            else:
                legacy.append({
                    **base,
                    "status": "legacy_unverified",
                    "status_reason": "source_revision_metadata_missing",
                })
    for work_package_id, entry in state_outputs.items():
        if (
            work_package_id not in required_ids
            and isinstance(entry, Mapping)
            and work_package_id not in {
                "status", "lifecycle_status", "retryable", "failure_reason",
                "stale_callback_history",
            }
        ):
            obsolete.append({
                "work_package_id": work_package_id,
                "operation_key": entry.get("operation_key") or entry.get("operation_id"),
                "source_process_revision": process_revision_value,
                "saved_output_revision": entry.get("output_revision"),
                "status": "obsolete_for_current_revision",
                "status_reason": "work_package_not_required_by_current_process",
                "affected_component_ids": list(entry.get("component_ids") or []),
                "preservation_note": "Preserved for audit; excluded from the current technical revision.",
            })
    required_id_set = set(required_ids)
    obsolete = [
        item
        for item in obsolete
        if item.get("work_package_id") not in required_id_set
    ]
    return {
        "process_revision": process_revision_value,
        "required_work_packages": required_ids,
        "valid_work_packages": valid,
        "missing_work_packages": missing,
        "stale_work_packages": stale,
        "blocked_work_packages": blocked,
        "received_not_normalized_work_packages": received_not_normalized,
        "legacy_unverified_work_packages": legacy,
        "obsolete_work_packages": obsolete,
    }


def component_scheduler_eligibility(
    reconciliation: Mapping[str, Any],
) -> Dict[str, List[str]]:
    """Translate component reconciliation into explicit scheduler policy."""

    def component_ids(key: str) -> List[str]:
        return [
            str(item["component_id"])
            for item in reconciliation.get(key) or []
            if item.get("component_id")
        ]

    return {
        "reuse_ids": component_ids("valid_components"),
        "automatic_trigger_ids": list(dict.fromkeys(
            component_ids("missing_components")
            + component_ids("stale_components")
        )),
        "explicit_validation_or_regeneration_ids": component_ids(
            "legacy_unverified_components"
        ),
        "never_trigger_ids": list(dict.fromkeys(
            component_ids("obsolete_components")
            + component_ids("blocked_components")
        )),
    }


def most_scheduler_eligibility(
    reconciliation: Mapping[str, Any],
) -> Dict[str, List[str]]:
    """Translate MOST reconciliation into explicit scheduler policy."""

    def work_package_ids(key: str) -> List[str]:
        return [
            str(item["work_package_id"])
            for item in reconciliation.get(key) or []
            if item.get("work_package_id")
        ]

    return {
        "reuse_ids": work_package_ids("valid_work_packages"),
        "automatic_trigger_ids": list(dict.fromkeys(
            work_package_ids("missing_work_packages")
            + work_package_ids("stale_work_packages")
        )),
        "explicit_validation_or_regeneration_ids": list(dict.fromkeys(
            work_package_ids("legacy_unverified_work_packages")
            + work_package_ids("received_not_normalized_work_packages")
        )),
        "never_trigger_ids": list(dict.fromkeys(
            work_package_ids("obsolete_work_packages")
            + work_package_ids("blocked_work_packages")
        )),
    }


def revision_transition(
    previous_bom: Optional[Mapping[str, Any]],
    current_bom: Mapping[str, Any],
    previous_process: Optional[Mapping[str, Any]],
    current_process: Mapping[str, Any],
) -> Dict[str, Any]:
    previous_components = _component_map(previous_bom or {})
    current_components = _component_map(current_bom)
    previous_component_revisions = {
        key: value.get("technical_revision") or component_input_revision(value)
        for key, value in previous_components.items()
    }
    current_component_revisions = {
        key: value.get("technical_revision") or component_input_revision(value)
        for key, value in current_components.items()
    }
    added_components = sorted(set(current_components) - set(previous_components))
    removed_components = sorted(set(previous_components) - set(current_components))
    changed_components = sorted(
        key
        for key in set(current_components) & set(previous_components)
        if current_component_revisions[key] != previous_component_revisions[key]
    )
    unchanged_components = sorted(
        key
        for key in set(current_components) & set(previous_components)
        if current_component_revisions[key] == previous_component_revisions[key]
    )

    def package_revisions(process: Mapping[str, Any]) -> Dict[str, str]:
        return {
            str(item.get("work_package_id")): (
                item.get("technical_revision")
                or work_package_input_revision(item, process.get("source_bom_revision"))
            )
            for item in process.get("work_packages") or []
            if isinstance(item, Mapping) and item.get("work_package_id")
        }

    previous_packages = package_revisions(previous_process or {})
    current_packages = package_revisions(current_process)
    added_packages = sorted(set(current_packages) - set(previous_packages))
    removed_packages = sorted(set(previous_packages) - set(current_packages))
    changed_packages = sorted(
        key
        for key in set(current_packages) & set(previous_packages)
        if current_packages[key] != previous_packages[key]
    )
    unchanged_packages = sorted(
        key
        for key in set(current_packages) & set(previous_packages)
        if current_packages[key] == previous_packages[key]
    )
    return {
        "previous_bom_revision": (previous_bom or {}).get("technical_revision"),
        "current_bom_revision": current_bom.get("technical_revision"),
        "previous_process_revision": (previous_process or {}).get("technical_revision"),
        "current_process_revision": current_process.get("technical_revision"),
        "components": {
            "added": added_components,
            "removed": removed_components,
            "changed": changed_components,
            "unchanged": unchanged_components,
        },
        "work_packages": {
            "added": added_packages,
            "removed": removed_packages,
            "changed": changed_packages,
            "unchanged": unchanged_packages,
        },
    }
