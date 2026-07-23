import json
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional

from services.choke_classification import classify_choke, classification_trace
from services.choke_reference_profiles import find_reference_profile


OPERATION_CATALOG = {
    "incoming_component_handling": "Incoming component handling",
    "ferrite_preparation": "Ferrite preparation",
    "wire_winding": "Winding",
    "core_assembly": "Assembly core",
    "terminal_forming": "Terminal forming",
    "wire_stripping": "Wire stripping",
    "soldering_tinning": "Soldering / tinning",
    "glue_application": "Glue application",
    "curing_baking": "Curing / baking",
    "electrical_test": "Electrical test",
    "inductance_test": "Inductance test",
    "resistance_test": "Resistance test",
    "push_out_pull_test": "Push-out / pull test",
    "dimensional_inspection": "Dimensional inspection",
    "visual_inspection": "Visual inspection",
    "marking": "Marking",
    "packaging": "Packaging",
}

SUBTYPE_KNOWLEDGE = {
    "fuse_choke": {
        "candidate_operation_keys": ["wire_winding", "core_assembly", "soldering_tinning"],
        "validation_questions": [
            "Confirm the protection function and whether core assembly is a separate operation.",
            "Confirm whether terminal ends require internal soldering or tinning.",
        ],
    },
    "rod_choke": {
        "candidate_operation_keys": ["wire_winding", "terminal_forming", "glue_application"],
        "validation_questions": [
            "Confirm whether the wire is wound directly on the ferrite rod.",
            "Confirm the fixation method and terminal-forming requirements.",
        ],
    },
    "toroid_choke": {
        "candidate_operation_keys": ["ferrite_preparation", "wire_winding", "wire_stripping"],
        "validation_questions": [
            "Confirm the toroidal-core handling and winding method.",
            "Confirm insulation and wire-end preparation requirements.",
        ],
    },
    "unknown_choke": {
        "candidate_operation_keys": [],
        "validation_questions": [
            "Confirm whether this product is a Fuse Choke, Rod Choke, Toroid Choke, or another Choke variant."
        ],
    },
}

_COMPONENT_ALIASES = {
    "ferrite": {"ferrite_core", "ferrite", "core"},
    "wire": {"magnet_wire", "enameled_wire", "enamelled_wire", "wire"},
    "tin": {"lead_tinning", "tin", "solder", "solder_material"},
    "glue": {"glue", "adhesive", "epoxy"},
}


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKD", json.dumps(value, ensure_ascii=False, default=str)).encode(
        "ascii", "ignore"
    ).decode().lower()


def _number(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("value", value.get("quantity"))
    if value in (None, "") or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", ".")
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def _component_kind(component: Dict[str, Any]) -> Optional[str]:
    canonical_identity = " ".join(str(component.get(key) or "") for key in (
        "component_id", "category", "external_component_type", "component_type"
    )).lower()
    for kind, aliases in _COMPONENT_ALIASES.items():
        if any(alias in canonical_identity for alias in aliases):
            return kind
    description = str(component.get("component") or "").lower()
    for kind, aliases in _COMPONENT_ALIASES.items():
        if any(alias in description for alias in aliases):
            return kind
    return None


def _component_usable(component: Dict[str, Any]) -> bool:
    if component.get("excluded_not_required") is True:
        return False
    quantity = _number(component.get("quantity_per_product"))
    if quantity is None or quantity <= 0:
        return False
    definition = component.get("component_definition") or {}
    certainty = str(
        component.get("certainty")
        or component.get("status")
        or (definition.get("certainty") if isinstance(definition, dict) else "")
        or (definition.get("validation_status") if isinstance(definition, dict) else "")
        or ""
    ).lower().replace("-", "_").replace(" ", "_")
    return certainty not in {"not_confirmed", "unconfirmed", "to_confirm", "pending_confirmation"}


def _evidence(source_type: str, reference: str, text: str, confidence: str) -> Dict[str, str]:
    return {
        "source_type": source_type,
        "source_reference": reference,
        "evidence_text": text,
        "confidence": confidence,
    }


def _operation(
    operation_code: str,
    operation_key: str,
    reason: str,
    evidence: List[Dict[str, str]],
    status: str,
    assumptions: Optional[List[str]] = None,
    questions: Optional[List[str]] = None,
    components: Optional[List[Dict[str, Any]]] = None,
    rate_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    operation = {
        "operation_code": operation_code,
        "operation_name": OPERATION_CATALOG[operation_key],
        "operation_key": operation_key,
        "reason_selected": reason,
        "evidence": evidence,
        "status": status,
        "assumptions": assumptions or [],
        "confirmation_questions": questions or [],
        "component_ids": [item.get("component_id") for item in (components or []) if item.get("component_id")],
    }
    if rate_data:
        operation.update(rate_data)
    return operation


def _profile_operations(profile: Dict[str, Any], components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    source = profile["source"]
    operations = []
    keys = ["wire_winding", "core_assembly", "soldering_tinning", "packaging"]
    for item, key in zip(profile["validated_operations"], keys):
        evidence = [_evidence("historical_reference", source, f"Exact-part validated operation {item['operation_code']}", "confirmed")]
        operation = _operation(
            item["operation_code"], key, "Exact-part benchmark operation validated in the customer workbook.",
            evidence, "confirmed", components=components,
            rate_data={
                "p_h": item["p_h"],
                "oee": item["oee"],
                "operator_percent": item["operator_percent"],
                "rate_provenance": {
                    "source_type": "historical_reference",
                    "source_reference": source,
                    "confidence": "confirmed",
                    "exact_reference_match": True,
                },
            },
        )
        operation["operation_name"] = item["operation_name"]
        operations.append(operation)
    return operations


def _contains(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def build_choke_process_route(
    customer_input: Optional[Dict[str, Any]],
    normalized_bom: Optional[Dict[str, Any]],
    classification: Optional[Dict[str, Any]] = None,
    preliminary_policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    customer_input = customer_input or {}
    normalized_bom = normalized_bom or {"components": [], "raw_bom": {}}
    raw_bom = normalized_bom.get("raw_bom") or {}
    classification = classification or normalized_bom.get("choke_classification") or classify_choke(customer_input, raw_bom)
    trace = classification_trace(classification)
    subtype = trace["choke_subtype"]
    components = [item for item in normalized_bom.get("components") or [] if _component_usable(item)]
    by_kind: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in _COMPONENT_ALIASES}
    for component in components:
        kind = _component_kind(component)
        if kind:
            by_kind[kind].append(component)
    raw_text = _text({"bom": raw_bom, "customer_requirements": customer_input})
    part_number = classification.get("part_number") or customer_input.get("part_number")
    references = find_reference_profile(part_number, subtype, customer_input.get("customer"))
    exact_profile = references["exact_match"]
    subtype_knowledge = SUBTYPE_KNOWLEDGE[subtype]

    if subtype == "unknown_choke":
        return {
            "status": "blocked",
            **trace,
            "exact_historical_profile_match": None,
            "comparable_references": references["comparable_references"],
            "subtype_knowledge": subtype_knowledge,
            "operations": [],
            "work_packages": [],
            "required_work_package_ids": [],
            "excluded_operations": [],
            "assumptions": [],
            "blocking_questions": trace["unresolved_classification_questions"],
            "missing_inputs": ["choke_subtype_confirmation"],
            "blocked_reason": "unknown_choke_subtype",
        }

    if exact_profile:
        operations = _profile_operations(exact_profile, components)
        excluded = [
            {
                "operation_key": key,
                "operation_name": OPERATION_CATALOG.get(key, key.replace("_", " ").title()),
                "status": "excluded",
                "reason_selected": "Excluded by the exact 316-5001 validated benchmark unless new explicit evidence is supplied.",
                "evidence": [_evidence("historical_reference", exact_profile["source"], "Exact-part validated exclusion", "confirmed")],
                "assumptions": [],
                "confirmation_questions": [],
            }
            for key in exact_profile["validated_exclusions"]
        ]
    else:
        operations: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        sequence = 10

        def add(key: str, reason: str, evidence: List[Dict[str, str]], status: str = "confirmed",
                assumptions: Optional[List[str]] = None, questions: Optional[List[str]] = None,
                selected_components: Optional[List[Dict[str, Any]]] = None) -> None:
            nonlocal sequence
            operations.append(_operation(
                f"OP{sequence}", key, reason, evidence, status, assumptions, questions, selected_components,
            ))
            sequence += 10

        if by_kind["wire"] and by_kind["ferrite"]:
            add(
                "wire_winding",
                "Confirmed magnet wire and ferrite BOM lines support winding, without assuming station rate or automation.",
                [_evidence("bom", "normalized BOM", "Magnet wire and ferrite have positive confirmed quantities.", "high")],
                selected_components=by_kind["wire"] + by_kind["ferrite"],
            )

        assembly_terms = ["assembly core", "core assembly", "assemble core", "ferrite insertion", "insert ferrite"]
        if _contains(raw_text, assembly_terms):
            add("core_assembly", "Core assembly is explicitly described.", [
                _evidence("drawing", "BOM/drawing text", "Explicit core assembly or ferrite insertion requirement.", "high")
            ], selected_components=by_kind["ferrite"])
        elif subtype == "fuse_choke" and by_kind["ferrite"] and by_kind["wire"]:
            add(
                "core_assembly",
                "Fuse Choke knowledge suggests checking whether core assembly is a separate operation.",
                [_evidence("knowledge_rule", "fuse_choke:core_assembly_check", "Fuse Chokes commonly require core assembly confirmation.", "medium")],
                "needs_confirmation", questions=["Is core assembly a separate manufacturing operation for this design?"],
                selected_components=by_kind["ferrite"],
            )

        tin_internal_terms = ["auto solder", "automatic solder", "internal solder", "lead tinning", "tin the lead", "etamage", "etamer"]
        if by_kind["tin"] and _contains(raw_text, tin_internal_terms):
            add("soldering_tinning", "Tin/solder is applied by an explicitly described internal operation.", [
                _evidence("drawing", "BOM/drawing text", "Internal soldering or tinning requirement is explicit.", "high")
            ], selected_components=by_kind["tin"])
        elif by_kind["tin"]:
            add(
                "soldering_tinning", "Tin/solder material exists but internal application is not confirmed.",
                [_evidence("bom", "normalized BOM", "Tin or solder material has a positive confirmed quantity.", "medium")],
                "needs_confirmation", questions=["Is the tin/solder applied internally, and by which process?"],
                selected_components=by_kind["tin"],
            )

        if by_kind["glue"]:
            glue_text = _text(by_kind["glue"])
            add("glue_application", "Confirmed glue BOM line supports glue application.", [
                _evidence("bom", "normalized BOM", "Glue has a positive confirmed quantity.", "high")
            ], selected_components=by_kind["glue"])
            if _contains(raw_text + glue_text, ["cure", "curing", "bake", "baking", "oven", "polymerization"]):
                add("curing_baking", "A curing or baking requirement is explicit.", [
                    _evidence("drawing", "BOM/drawing text", "Glue curing/baking requirement is explicit.", "high")
                ], selected_components=by_kind["glue"])
            else:
                add(
                    "curing_baking", "Glue exists but its curing method is not confirmed.",
                    [_evidence("bom", "normalized BOM", "Glue presence alone does not establish curing.", "medium")],
                    "needs_confirmation", questions=["Does the glue require curing or baking, and under what conditions?"],
                    selected_components=by_kind["glue"],
                )

        explicit_operations = [
            ("incoming_component_handling", ["separate incoming handling", "incoming component handling"]),
            ("ferrite_preparation", ["ferrite preparation", "core preparation", "grind ferrite"]),
            ("terminal_forming", ["terminal forming", "lead forming", "bend terminal"]),
            ("wire_stripping", ["wire stripping", "strip enamel", "remove enamel"]),
            ("dimensional_inspection", ["dimensional inspection", "dimensional control"]),
            ("marking", ["marking operation", "laser marking", "product marking"]),
        ]
        for key, terms in explicit_operations:
            if _contains(raw_text, terms):
                add(key, f"{OPERATION_CATALOG[key]} is explicitly described.", [
                    _evidence("drawing", "BOM/drawing text", f"Explicit requirement for {OPERATION_CATALOG[key]}.", "high")
                ], selected_components=components)

        tests = [
            ("inductance_test", ["inductance test", "test inductance", "measure inductance"]),
            ("resistance_test", ["resistance test", "dc resistance", "dcr test"]),
            ("electrical_test", ["electrical test", "continuity test", "insulation test", "dielectric test"]),
            ("push_out_pull_test", ["push out", "push-out", "pull test", "pull-out"]),
        ]
        for key, terms in tests:
            explicit_flag = {
                "electrical_test": customer_input.get("electrical_test_required"),
                "inductance_test": customer_input.get("inductance_test_required"),
                "resistance_test": customer_input.get("resistance_test_required"),
                "push_out_pull_test": customer_input.get("push_out_test_required") or customer_input.get("pull_test_required"),
            }.get(key)
            if explicit_flag is True or _contains(raw_text, terms):
                add(key, f"{OPERATION_CATALOG[key]} is explicitly required.", [
                    _evidence("specification", "BOM/drawing text", f"Explicit requirement for {OPERATION_CATALOG[key]}.", "high")
                ], selected_components=components)

        if _contains(raw_text, ["visual inspection", "visual control"]):
            add("visual_inspection", "Visual inspection is explicitly required.", [
                _evidence("specification", "BOM/drawing text", "Explicit visual inspection requirement.", "high")
            ], selected_components=components)
        if _contains(raw_text, ["packaging requirement", "packing instruction", "inspection and packaging"]):
            add("packaging", "Packaging is explicitly required.", [
                _evidence("specification", "BOM/drawing text", "Explicit packaging requirement.", "high")
            ], selected_components=components)
        elif (preliminary_policy or {}).get("allow_packaging_assumption"):
            add(
                "packaging", "Controlled preliminary-costing policy permits a packaging assumption.",
                [_evidence("knowledge_rule", "preliminary_policy:packaging", "Preliminary packaging assumption permitted.", "medium")],
                "proposed", assumptions=["Preliminary packaging operation; customer packaging requirement is pending."],
                questions=["Confirm the customer packaging requirement."], selected_components=components,
            )

        selected_keys = {item["operation_key"] for item in operations}
        for key, name in OPERATION_CATALOG.items():
            if key not in selected_keys:
                excluded.append({
                    "operation_key": key,
                    "operation_name": name,
                    "status": "excluded",
                    "reason_selected": "No product-specific evidence currently supports this operation.",
                    "evidence": [],
                    "assumptions": [],
                    "confirmation_questions": [],
                })

    allow_proposed = bool((preliminary_policy or {}).get("allow_proposed_operations"))
    work_packages = []
    for operation in operations:
        eligible = operation["status"] == "confirmed" or (operation["status"] == "proposed" and allow_proposed)
        if not eligible:
            continue
        package = {
            **operation,
            "work_package_id": f"wp_{operation['operation_code'][2:]}_{operation['operation_key']}",
            "component_id": (operation.get("component_ids") or ["finished_choke"])[0],
            "component_type": "finished_choke_operation",
            "operation_id": operation["operation_code"],
            "operation_family": operation["operation_key"],
            "quantity_per_product": 1,
            "technical_inputs": {},
            "status": "pending",
            "blocking_reason": None,
        }
        work_packages.append(package)

    questions = list(trace["unresolved_classification_questions"])
    questions.extend(question for item in operations for question in item["confirmation_questions"])
    assumptions = [assumption for item in operations for assumption in item["assumptions"]]
    return {
        "status": "created" if work_packages else "blocked",
        **trace,
        "exact_historical_profile_match": references["exact_reference_id"],
        "historical_profile": exact_profile,
        "comparable_references": references["comparable_references"],
        "subtype_knowledge": subtype_knowledge,
        "operations": operations,
        "work_packages": work_packages,
        "required_work_package_ids": [item["work_package_id"] for item in work_packages],
        "blocked_work_package_ids": [],
        "excluded_operations": excluded,
        "assumptions": list(dict.fromkeys(assumptions)),
        "blocking_questions": list(dict.fromkeys(questions)),
        "missing_inputs": [] if work_packages else ["confirmed_process_operations"],
        "blocked_reason": None if work_packages else "no_confirmed_operations",
        "routing_source": "services.choke_process_routing",
    }
