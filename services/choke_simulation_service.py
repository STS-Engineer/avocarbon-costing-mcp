"""Persistent, agent-independent Choke costing simulations.

This module deliberately keeps simulation inputs separate from the sequential
Workspace Agent workflow. Both paths share master data and calculation rules,
but a simulation can be completed entirely from validated manual or pasted
agent outputs.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from services.costing_master_data_service import (
    get_master_manufacturing_strategy,
    get_master_unit_data,
    get_product_catalog_from_db,
)
from services.project_data_paths import atomic_write_json, get_data_root


SCHEMA_VERSION = "1.0"
OUTPUT_TYPES = {"bom", "component_costing", "most_component", "most_final_assembly"}
OUTPUT_STATUSES = {"complete", "assumption_based", "blocked"}
GLUE_STATUSES = {
    "included_confirmed",
    "included_assumption",
    "excluded_not_required",
    "blocked_to_confirm",
}
PAYMENT_METHODS = {
    "customer_pays_at_order",
    "50_order_25_off_tool_25_ppap",
    "partially_paid_then_amortized",
    "fully_amortized_in_piece_price",
}


class SimulationError(ValueError):
    """A user-correctable simulation validation error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_part(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise SimulationError(f"{name} is invalid.")
    return text


def simulation_paths(simulation_id: str) -> Dict[str, Path]:
    sid = _safe_part(simulation_id, "simulation_id")
    root = (get_data_root() / "simulations" / sid).resolve()
    return {
        "root": root,
        "context": root / "context.json",
        "bom": root / "bom.json",
        "components": root / "components",
        "most": root / "most",
        "calculation_input": root / "calculation_input.json",
        "result": root / "calculation_result.json",
        "events": root / "events.jsonl",
    }


def _read(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _append_event(simulation_id: str, event: str, **details: Any) -> None:
    path = simulation_paths(simulation_id)["events"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": _now(), "event": event, "simulation_id": simulation_id, **details}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value).replace(" ", ""))
    return float(match.group(0).replace(",", ".")) if match else None


def normalize_percent(value: Any, field_name: str = "percentage") -> float | None:
    number = _number(value)
    if number is None:
        return None
    if number < 0:
        raise SimulationError(f"{field_name} cannot be negative.")
    decimal = number / 100 if number > 1 else number
    if decimal > 1:
        raise SimulationError(f"{field_name} must be between 0 and 100 percent.")
    return decimal


def _first(mapping: Dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value: Any = mapping
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value not in (None, ""):
            return value
    return None


def _list_from(raw: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    for key in keys:
        value = _first(raw, key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _commercial_context(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "product": context.get("product") or context.get("product_name"),
        "annual_quantity": _number(context.get("annual_quantity")),
        "destination_zone": context.get("destination_zone") or context.get("customer_delivery_zone"),
        "production_plant": context.get("production_plant"),
        "reporting_currency": context.get("reporting_currency") or context.get("currency"),
    }


def _base_envelope(raw: Dict[str, Any], output_type: str, context: Dict[str, Any]) -> Dict[str, Any]:
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "output_type": output_type,
        "project_code": raw.get("project_code") or context.get("project_code"),
        "product_id": raw.get("product_id") or context.get("product_id"),
        "scope_id": raw.get("scope_id"),
        "component_id": raw.get("component_id"),
        "component_name": raw.get("component_name"),
        "status": raw.get("status") if raw.get("status") in OUTPUT_STATUSES else "assumption_based",
        "source": {
            "agent": source.get("agent") or raw.get("agent") or "Manual Choke Simulation",
            "entry_mode": source.get("entry_mode") or raw.get("entry_mode") or "pasted_json",
            "analysis_date": source.get("analysis_date") or date.today().isoformat(),
        },
        "commercial_context": {
            **_commercial_context(context),
            **(raw.get("commercial_context") if isinstance(raw.get("commercial_context"), dict) else {}),
        },
        "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
        "assumptions": list(raw.get("assumptions") or []),
        "unconfirmed_values": list(raw.get("unconfirmed_values") or []),
        "required_confirmations": list(raw.get("required_confirmations") or []),
    }


def validate_envelope(envelope: Dict[str, Any], expected_type: str | None = None) -> Dict[str, Any]:
    errors: List[str] = []
    if envelope.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1.0")
    output_type = envelope.get("output_type")
    if output_type not in OUTPUT_TYPES:
        errors.append(f"output_type must be one of {sorted(OUTPUT_TYPES)}")
    if expected_type and output_type != expected_type:
        errors.append(f"output_type must be {expected_type}")
    for field in ("project_code", "product_id", "status", "source", "commercial_context", "data"):
        if envelope.get(field) in (None, ""):
            errors.append(f"{field} is required")
    if envelope.get("status") not in OUTPUT_STATUSES:
        errors.append(f"status must be one of {sorted(OUTPUT_STATUSES)}")
    if not isinstance(envelope.get("data"), dict):
        errors.append("data must be an object")
    return {"valid": not errors, "errors": errors, "envelope": envelope}


def normalize_bom(raw: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    envelope = _base_envelope(raw, "bom", context)
    source_data = envelope["data"] or raw
    lines = _list_from(source_data, "components", "bom", "line_items", "bill_of_material")
    if not lines:
        lines = _list_from(raw, "components", "bom", "line_items", "bill_of_material")
    normalized = []
    for index, line in enumerate(lines, 1):
        component_id = _first(line, "component_id", "component_code", "id", "code", "part_number")
        component_name = _first(line, "component_name", "component", "name", "description", "designation")
        quantity = _number(_first(line, "quantity_per_product", "quantity", "qty", "quantity_per_assembly"))
        normalized.append({
            **line,
            "component_id": str(component_id or f"component_{index}").strip(),
            "component_name": component_name or str(component_id or f"Component {index}"),
            "component_family": _first(line, "component_family", "family", "category"),
            "classification": line.get("classification") or "External",
            "quantity_per_product": quantity,
            "glue_status": line.get("glue_status"),
        })
    envelope["data"] = {
        **(source_data if isinstance(source_data, dict) else {}),
        "components": normalized,
    }
    if any(line["quantity_per_product"] is None for line in normalized):
        envelope["status"] = "blocked"
        envelope["required_confirmations"].append("Every BOM line requires quantity_per_product")
    return envelope


def normalize_component(raw: Dict[str, Any], context: Dict[str, Any], component_id: str) -> Dict[str, Any]:
    envelope = _base_envelope(raw, "component_costing", context)
    data = envelope["data"] or raw
    offer = data.get("recommended_offer") if isinstance(data.get("recommended_offer"), dict) else {}
    legacy_offer = raw.get("recommended_offer") if isinstance(raw.get("recommended_offer"), dict) else {}
    supply = legacy_offer.get("supply_chain") if isinstance(legacy_offer.get("supply_chain"), dict) else {}
    normalized_offer = {
        "supplier": _first(offer, "supplier") or _first(legacy_offer, "supplier", "supplier_name"),
        "origin": _first(offer, "origin") or _first(legacy_offer, "origin"),
        "incoterm": _first(offer, "incoterm") or _first(legacy_offer, "incoterm"),
        "supplier_currency": _first(offer, "supplier_currency") or _first(legacy_offer, "supplier_currency", "currency"),
        "fca_price_per_component": _number(_first(offer, "fca_price_per_component", "price_in_reporting_currency") or _first(legacy_offer, "fca_price_per_component", "unit_price")),
        "price_in_reporting_currency": _number(_first(offer, "price_in_reporting_currency") or _first(legacy_offer, "price_in_reporting_currency", "selling_price_per_unit")),
        "transportation_cost_per_component": _number(_first(offer, "transportation_cost_per_component") or _first(supply, "transportation_cost", "transport_cost") or raw.get("transportation_cost")),
        "customs_cost_per_component": _number(_first(offer, "customs_cost_per_component") or _first(supply, "custom_duty_cost", "customs_duty_cost") or raw.get("custom_duty_cost")),
        "forwarder_fee_per_component": _number(_first(offer, "forwarder_fee_per_component") or _first(supply, "forwarder_cost") or raw.get("forwarder_cost")),
        "capital_cost_per_component": _number(_first(offer, "capital_cost_per_component") or _first(supply, "capital_cost")),
        "cash_locked_per_component": _number(_first(offer, "cash_locked_per_component") or _first(supply, "cash_locked")),
        "delivered_cost_per_component": _number(_first(offer, "delivered_cost_per_component") or _first(legacy_offer, "delivered_cost_per_component", "delivered_cost", "material_cost") or raw.get("delivered_cost") or raw.get("material_cost")),
    }
    envelope["component_id"] = component_id
    envelope["component_name"] = envelope.get("component_name") or data.get("component_name") or raw.get("component_name") or component_id
    envelope["scope_id"] = envelope.get("scope_id") or component_id
    envelope["data"] = {
        "component_family": data.get("component_family") or raw.get("component_family") or raw.get("component_type"),
        "classification": "External",
        "quantity_per_product": _number(data.get("quantity_per_product") or raw.get("quantity_per_product")),
        "technical_specification": data.get("technical_specification") or raw.get("technical_specification") or {},
        "cost_basis": data.get("cost_basis") or raw.get("cost_basis") or {
            "basis_status": raw.get("basis_status") or "estimated",
            "source": raw.get("cost_source"),
            "source_date": raw.get("source_date"),
            "confidence": raw.get("confidence") or "low",
        },
        "recommended_offer": normalized_offer,
        "indexed_material_cost_per_component": _number(data.get("indexed_material_cost_per_component") or raw.get("indexed_material_cost_per_component")),
        "non_indexed_material_cost_per_component": _number(data.get("non_indexed_material_cost_per_component") or raw.get("non_indexed_material_cost_per_component")),
        "fx": list(data.get("fx") or raw.get("fx") or []),
        "material_indexation": list(data.get("material_indexation") or raw.get("material_indexation") or []),
        "productivity": list(data.get("productivity") or raw.get("productivity") or []),
        "commercially_usable": bool(data.get("commercially_usable") or raw.get("commercially_usable")),
    }
    return envelope


def normalize_most(raw: Dict[str, Any], context: Dict[str, Any], component_id: str) -> Dict[str, Any]:
    output_type = "most_final_assembly" if component_id == "final_assembly" else "most_component"
    envelope = _base_envelope(raw, output_type, context)
    data = envelope["data"] or raw
    operations = _list_from(data, "operations", "routing_operations", "most_operations")
    if not operations:
        operations = _list_from(raw, "operations", "routing_operations", "most_operations")
    envelope["component_id"] = component_id
    envelope["scope_id"] = envelope.get("scope_id") or raw.get("work_package_id") or component_id
    envelope["component_name"] = envelope.get("component_name") or raw.get("component_name") or component_id
    envelope["data"] = {
        "method": data.get("method") or raw.get("method") or "engineering_estimate",
        "operations": operations,
    }
    return envelope


def normalize_output(raw: Dict[str, Any], output_type: str, context: Dict[str, Any], identifier: str | None = None) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise SimulationError("Output JSON must be an object.")
    if output_type == "bom":
        result = normalize_bom(raw, context)
    elif output_type == "component_costing":
        result = normalize_component(raw, context, _safe_part(identifier, "component_id"))
    elif output_type in {"most_component", "most_final_assembly"}:
        component_id = "final_assembly" if output_type == "most_final_assembly" else _safe_part(identifier, "component_id")
        result = normalize_most(raw, context, component_id)
    else:
        raise SimulationError(f"Unsupported output_type: {output_type}")
    validation = validate_envelope(result, result["output_type"])
    if not validation["valid"]:
        raise SimulationError("; ".join(validation["errors"]))
    return result


def _resolve_context(context: Dict[str, Any]) -> Dict[str, Any]:
    resolved = dict(context)
    resolved["destination_zone"] = resolved.get("destination_zone") or resolved.get("customer_delivery_zone")
    resolved["reporting_currency"] = resolved.get("reporting_currency") or resolved.get("currency")
    strategy = get_master_manufacturing_strategy(
        resolved.get("product_line") or "Chokes",
        resolved.get("product"),
        resolved.get("destination_zone"),
    )
    if not resolved.get("production_plant") and strategy.get("status") == "found":
        resolved["production_plant"] = strategy.get("production_plant")
    unit = get_master_unit_data(resolved.get("production_plant"))
    override = resolved.get("plant_data_override") if isinstance(resolved.get("plant_data_override"), dict) else {}
    if override:
        unit = {**unit, **override, "source": "manual_override", "base_source": unit.get("source")}
    resolved["manufacturing_strategy"] = strategy
    resolved["unit_data"] = unit
    resolved["master_data_sources"] = {
        "manufacturing_strategy": strategy.get("source"),
        "unit_data": unit.get("source"),
    }
    return resolved


def create_simulation(context: Dict[str, Any]) -> Dict[str, Any]:
    simulation_id = context.get("simulation_id") or f"SIM-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    simulation_id = _safe_part(simulation_id, "simulation_id")
    paths = simulation_paths(simulation_id)
    if paths["context"].exists():
        raise SimulationError(f"Simulation {simulation_id} already exists.")
    prepared = _resolve_context({
        **context,
        "simulation_id": simulation_id,
        "project_code": context.get("project_code") or simulation_id,
        "product_line": context.get("product_line") or "Chokes",
        "created_at": _now(),
        "updated_at": _now(),
    })
    atomic_write_json(paths["context"], prepared)
    _append_event(simulation_id, "simulation_created")
    return get_simulation(simulation_id)


def update_context(simulation_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    paths = simulation_paths(simulation_id)
    current = _read(paths["context"])
    if not current:
        raise FileNotFoundError(f"Simulation {simulation_id} not found.")
    merged = _resolve_context({**current, **updates, "simulation_id": simulation_id, "updated_at": _now()})
    atomic_write_json(paths["context"], merged)
    _append_event(simulation_id, "context_updated", fields=sorted(updates.keys()))
    return get_simulation(simulation_id)


def get_simulation(simulation_id: str) -> Dict[str, Any]:
    paths = simulation_paths(simulation_id)
    context = _read(paths["context"])
    if not context:
        raise FileNotFoundError(f"Simulation {simulation_id} not found.")
    component_files = sorted(paths["components"].glob("*.json")) if paths["components"].exists() else []
    most_files = sorted(paths["most"].glob("*.json")) if paths["most"].exists() else []
    return {
        "simulation_id": simulation_id,
        "context": context,
        "bom": _read(paths["bom"]),
        "components": {path.stem: _read(path) for path in component_files},
        "most": {path.stem: _read(path) for path in most_files},
        "result": _read(paths["result"]),
        "storage_path": str(paths["root"]),
    }


def save_output(simulation_id: str, output_type: str, raw: Dict[str, Any], identifier: str | None = None, replace: bool = False) -> Dict[str, Any]:
    simulation = get_simulation(simulation_id)
    paths = simulation_paths(simulation_id)
    envelope = normalize_output(raw, output_type, simulation["context"], identifier)
    if output_type == "bom":
        destination = paths["bom"]
    elif output_type == "component_costing":
        destination = paths["components"] / f"{_safe_part(identifier, 'component_id')}.json"
    else:
        destination = paths["most"] / f"{_safe_part(identifier or 'final_assembly', 'component_id')}.json"
    if destination.exists() and not replace:
        raise SimulationError(f"{destination.stem} already exists. Set replace=true to replace it explicitly.")
    existed = destination.exists()
    atomic_write_json(destination, envelope)
    _append_event(simulation_id, "output_saved", output_type=envelope["output_type"], identifier=identifier, replaced=existed)
    return {"status": "saved", "path": str(destination), "normalized_output": envelope}


def validate_simulation(simulation_id: str) -> Dict[str, Any]:
    simulation = get_simulation(simulation_id)
    errors: List[str] = []
    warnings: List[str] = []
    context = simulation["context"]
    for field in ("product", "annual_quantity", "destination_zone", "production_plant", "reporting_currency"):
        if context.get(field) in (None, ""):
            errors.append(field)
    bom = simulation.get("bom")
    if not bom:
        errors.append("bom")
    else:
        for line in bom.get("data", {}).get("components", []):
            cid = line.get("component_id")
            quantity = _number(line.get("quantity_per_product"))
            if quantity is None or quantity < 0:
                errors.append(f"bom.quantity_per_product:{cid}")
            glue_status = line.get("glue_status") if "glue" in str(cid).lower() else None
            if glue_status and glue_status not in GLUE_STATUSES:
                errors.append(f"invalid glue_status:{glue_status}")
            if glue_status == "blocked_to_confirm":
                errors.append("glue_status")
            if glue_status == "excluded_not_required":
                continue
            if cid not in simulation["components"]:
                errors.append(f"component_output:{cid}")
        if not simulation["most"]:
            errors.append("most_outputs")
    unit = context.get("unit_data") or {}
    for field in ("dl_rate_operating_per_hour", "voh_rate_operating_per_hour", "foh_percent_dc", "fee_percent_dc", "open_hours_per_year"):
        if unit.get(field) in (None, ""):
            errors.append(f"plant_unit_data.{field}")
    if context.get("reporting_currency") and unit.get("selling_currency") and context["reporting_currency"] != unit["selling_currency"]:
        warnings.append("Reporting currency differs from plant selling currency; provide an explicit reporting FX conversion.")
    return {"valid": not errors, "blocking_errors": list(dict.fromkeys(errors)), "warnings": warnings}


def _operation_calculation(operation: Dict[str, Any], context: Dict[str, Any], unit: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    operation_id = operation.get("operation_id") or operation.get("operation_name") or "operation"
    errors: List[str] = []
    strokes = _number(operation.get("strokes_per_hour") or operation.get("p_h") or operation.get("pieces_per_hour"))
    pieces = _number(operation.get("pieces_per_stroke") or operation.get("parts_per_cycle"))
    oee = normalize_percent(operation.get("oee_percent") if operation.get("oee_percent") is not None else operation.get("oee"), f"{operation_id}.oee")
    operator = normalize_percent(operation.get("operator_percent"), f"{operation_id}.operator_percent")
    annual = _number(context.get("annual_quantity"))
    open_hours = _number(unit.get("open_hours_per_year"))
    dl_rate = _number(unit.get("dl_rate_operating_per_hour"))
    voh_rate = _number(unit.get("voh_rate_operating_per_hour"))
    operating = unit.get("operating_currency")
    selling = unit.get("selling_currency")
    fx = _number(context.get("fx_operating_to_selling_30d") or context.get("fx_operating_to_selling"))
    if operating == selling:
        fx = 1.0
    for name, value in (("strokes_per_hour", strokes), ("pieces_per_stroke", pieces), ("oee_percent", oee), ("operator_percent", operator), ("annual_quantity", annual), ("plant_opening_hours", open_hours), ("dl_rate", dl_rate), ("voh_rate", voh_rate), ("fx_operating_to_selling_30d", fx)):
        if value is None or (name not in {"operator_percent"} and value <= 0):
            errors.append(f"{operation_id}.{name}")
    if errors:
        return {"operation_id": operation_id, "status": "blocked", "blocking_errors": errors}, errors
    theoretical = strokes * pieces
    effective = theoretical * oee
    if effective <= 0:
        return {"operation_id": operation_id, "status": "blocked", "blocking_errors": [f"{operation_id}.effective_pieces_per_hour"]}, [f"{operation_id}.effective_pieces_per_hour"]
    machine_h_1000 = 1000 / effective
    dl_h_1000 = machine_h_1000 * operator
    dl_rate_selling = dl_rate / fx
    dl_piece = dl_h_1000 * dl_rate_selling / 1000
    yearly_hours = annual / effective
    occupancy = yearly_hours / open_hours * 1.1
    generic = _number(operation.get("generic_capex"))
    specific = _number(operation.get("specific_capex"))
    if generic is None:
        errors.append(f"{operation_id}.generic_capex")
    if specific is None:
        errors.append(f"{operation_id}.specific_capex")
    warranty = bool(operation.get("lifetime_warranty"))
    tooling = _number(operation.get("tooling_cost"))
    tooling_life = _number(operation.get("tool_lifetime_pieces") or operation.get("tooling_life_pieces"))
    if tooling is None:
        errors.append(f"{operation_id}.tooling_cost")
    if not warranty and tooling and not tooling_life:
        errors.append(f"{operation_id}.tool_lifetime_pieces")
    if errors:
        return {"operation_id": operation_id, "status": "blocked", "blocking_errors": errors}, errors
    generic_hour = generic * occupancy * 0.15 / yearly_hours
    specific_units = math.ceil(occupancy)
    specific_hour = specific * specific_units * 0.15 / yearly_hours
    tooling_piece = 0.0 if warranty else (tooling / tooling_life if tooling else 0.0)
    tooling_hour = tooling_piece * effective
    base_voh_selling = voh_rate / fx
    total_voh_hour = base_voh_selling + generic_hour + specific_hour + tooling_hour
    voh_piece = machine_h_1000 * total_voh_hour / 1000
    return {
        "operation_id": operation_id,
        "operation_name": operation.get("operation_name"),
        "status": "calculated",
        "theoretical_pieces_per_hour": theoretical,
        "effective_pieces_per_hour": effective,
        "oee_decimal": oee,
        "operator_percent_decimal": operator,
        "machine_hours_per_1000": machine_h_1000,
        "direct_labor_hours_per_1000": dl_h_1000,
        "direct_labor_hourly_rate_selling_currency": dl_rate_selling,
        "direct_labor_cost_per_piece": dl_piece,
        "yearly_production_hours": yearly_hours,
        "generic_occupancy_rate": occupancy,
        "allocated_generic_capex": generic * occupancy,
        "generic_capex_voh_per_hour": generic_hour,
        "specific_capacity_units": specific_units,
        "allocated_specific_capex": specific * specific_units,
        "specific_capex_voh_per_hour": specific_hour,
        "tooling_consumption_per_piece": tooling_piece,
        "tooling_cost_per_hour": tooling_hour,
        "base_voh_hourly_rate_selling_currency": base_voh_selling,
        "total_voh_hourly_rate": total_voh_hour,
        "voh_cost_per_piece": voh_piece,
    }, []


def _commercial_adders(context: Dict[str, Any], warnings: List[str], errors: List[str]) -> Tuple[float, float, Dict[str, Any]]:
    commercial = context.get("commercial_adjustments") if isinstance(context.get("commercial_adjustments"), dict) else {}
    details: Dict[str, Any] = {}
    adders = []
    for name in ("tooling", "specific_capex"):
        item = commercial.get(name) if isinstance(commercial.get(name), dict) else {}
        payment = item.get("payment_method")
        if payment and payment not in PAYMENT_METHODS:
            errors.append(f"commercial_adjustments.{name}.payment_method")
        margin = normalize_percent(item.get("margin_percent", 10), f"{name}.margin_percent") or 0.0
        investment = _number(item.get("company_investment")) or 0.0
        customer_payment = _number(item.get("customer_payment")) or 0.0
        amortization = _number(item.get("amortization_quantity"))
        amount_to_amortize = max(0.0, investment * (1 + margin) - customer_payment)
        adder = 0.0
        if payment in {"partially_paid_then_amortized", "fully_amortized_in_piece_price"} or amount_to_amortize:
            if not amortization:
                errors.append(f"commercial_adjustments.{name}.amortization_quantity")
            else:
                adder = amount_to_amortize / amortization
        quote = max(0.0, investment * (1 + margin))
        details[name] = {**item, "margin_decimal": margin, "quotation": quote, "price_adder_per_piece": adder}
        adders.append(adder)
    return adders[0], adders[1], details


def _yearly_prices(context: Dict[str, Any], costs: Dict[str, float], initial: float, warnings: List[str]) -> List[Dict[str, Any]]:
    commercial = context.get("commercial") if isinstance(context.get("commercial"), dict) else {}
    productivity = commercial.get("productivity") if isinstance(commercial.get("productivity"), dict) else {}
    perimeter = productivity.get("perimeter") or "added_value"
    limits = {"added_value": 0.05, "added_value_plus_non_indexed_material": 0.03, "full_price": 0.02}
    rates = productivity.get("yearly_rates") or {}
    material_rates = commercial.get("material_indexation_by_year") or {}
    plant_rates = commercial.get("plant_indexation_by_year") or {}
    years = ["SOP", "SOP+1", "SOP+2", "SOP+3"]
    rows = [{"year": "SOP", "price": initial, "price_before_productivity": initial, "productivity_amount": 0.0, "material_indexation_amount": 0.0, "plant_indexation_amount": 0.0}]
    previous = initial
    added_value = costs["direct_cost"] + costs["foh"] + costs["fees"]
    material_share = costs["material"] / initial if initial else 0
    for year in years[1:]:
        rate = normalize_percent(rates.get(year, 0), f"productivity.{year}") or 0.0
        if rate > limits.get(perimeter, 0.05):
            warnings.append(f"{year} productivity {rate:.1%} exceeds {perimeter} guideline {limits.get(perimeter, .05):.1%}; override reason: {productivity.get('override_reason') or 'missing'}")
        if perimeter == "full_price" and material_share >= 0.4:
            warnings.append("Full-price productivity selected while material content is at least 40%.")
        material_index_rate = normalize_percent(material_rates.get(year, 0), f"material_indexation.{year}") or 0.0
        plant_index_rate = normalize_percent(plant_rates.get(year, 0), f"plant_indexation.{year}") or 0.0
        material_amount = costs["indexed_material"] * material_index_rate
        plant_amount = added_value * plant_index_rate
        before = previous + material_amount + plant_amount
        if perimeter == "added_value_plus_non_indexed_material":
            base = added_value + costs["non_indexed_material"]
        elif perimeter == "full_price":
            base = before
        else:
            base = added_value
        productivity_amount = base * rate
        final = before - productivity_amount
        rows.append({
            "year": year,
            "price_before_productivity": before,
            "productivity_perimeter": perimeter,
            "productivity_base": base,
            "productivity_rate": rate,
            "productivity_amount": productivity_amount,
            "material_indexation_amount": material_amount,
            "plant_indexation_amount": plant_amount,
            "price": final,
        })
        previous = final
    return rows


def calculate_simulation(simulation_id: str) -> Dict[str, Any]:
    simulation = get_simulation(simulation_id)
    context = simulation["context"]
    bom = simulation.get("bom")
    errors: List[str] = []
    warnings: List[str] = []
    component_details: List[Dict[str, Any]] = []
    operation_details: List[Dict[str, Any]] = []
    material = transport = indexed = non_indexed = 0.0

    if not bom:
        errors.append("bom")
        bom_lines = []
    else:
        bom_lines = bom.get("data", {}).get("components", [])
    for line in bom_lines:
        cid = str(line.get("component_id") or "")
        qty = _number(line.get("quantity_per_product"))
        glue_status = line.get("glue_status") if "glue" in cid.lower() or "glue" in str(line.get("component_name", "")).lower() else None
        if glue_status == "excluded_not_required":
            component_details.append({"component_id": cid, "quantity_per_product": qty, "status": "excluded_not_required"})
            continue
        if glue_status == "blocked_to_confirm":
            errors.append("glue_status")
            continue
        if qty is None:
            errors.append(f"quantity_per_product:{cid}")
            continue
        output = simulation["components"].get(cid)
        if not output:
            errors.append(f"component_output:{cid}")
            continue
        offer = output.get("data", {}).get("recommended_offer", {})
        required_cost_fields = ("delivered_cost_per_component", "transportation_cost_per_component", "customs_cost_per_component", "forwarder_fee_per_component")
        missing = [name for name in required_cost_fields if offer.get(name) is None]
        if missing:
            errors.extend(f"{cid}.{name}" for name in missing)
            continue
        delivered = _number(offer["delivered_cost_per_component"])
        transport_unit = sum(_number(offer[name]) or 0 for name in required_cost_fields[1:])
        extended_material = qty * delivered
        logistics = qty * transport_unit
        indexed_unit = _number(output.get("data", {}).get("indexed_material_cost_per_component")) or 0.0
        nonindexed_unit = _number(output.get("data", {}).get("non_indexed_material_cost_per_component"))
        if nonindexed_unit is None:
            nonindexed_unit = max(0.0, delivered - indexed_unit)
        material += extended_material
        transport += logistics
        indexed += qty * indexed_unit
        non_indexed += qty * nonindexed_unit
        if glue_status == "included_assumption":
            warnings.append("Glue is included using an explicit assumption-based cost.")
        component_details.append({
            "component_id": cid,
            "component_name": line.get("component_name"),
            "quantity_per_product": qty,
            "delivered_cost_per_component": delivered,
            "extended_material_cost": extended_material,
            "transportation_contribution": qty * (_number(offer["transportation_cost_per_component"]) or 0),
            "customs_contribution": qty * (_number(offer["customs_cost_per_component"]) or 0),
            "forwarder_contribution": qty * (_number(offer["forwarder_fee_per_component"]) or 0),
            "logistics_contribution": logistics,
            "basis_status": output.get("data", {}).get("cost_basis", {}).get("basis_status"),
            "commercially_usable": output.get("data", {}).get("commercially_usable", False),
        })

    unit = context.get("unit_data") or {}
    for output in simulation["most"].values():
        for operation in output.get("data", {}).get("operations", []):
            detail, operation_errors = _operation_calculation(operation, context, unit)
            detail["component_id"] = output.get("component_id")
            detail["scope_id"] = output.get("scope_id")
            operation_details.append(detail)
            errors.extend(operation_errors)
    if not simulation["most"]:
        errors.append("most_outputs")
    dl = sum(item.get("direct_labor_cost_per_piece", 0) for item in operation_details if item.get("status") == "calculated")
    voh = sum(item.get("voh_cost_per_piece", 0) for item in operation_details if item.get("status") == "calculated")
    foh_decimal = normalize_percent(unit.get("foh_percent_dc"), "foh_percent_dc")
    fee_decimal = normalize_percent(unit.get("fee_percent_dc"), "fee_percent_dc")
    if foh_decimal is None:
        errors.append("plant_unit_data.foh_percent_dc")
    if fee_decimal is None:
        errors.append("plant_unit_data.fee_percent_dc")
    direct = dl + voh + transport
    foh = direct * (foh_decimal or 0)
    fees = direct * (fee_decimal or 0)
    tooling_adder, capex_adder, quotations = _commercial_adders(context, warnings, errors)
    manufacturing = material + direct + foh + fees
    margin = _number((context.get("commercial") or {}).get("margin_per_piece")) or 0.0
    adjustment = _number((context.get("commercial") or {}).get("price_adjustment_per_piece")) or 0.0
    initial = manufacturing + tooling_adder + capex_adder + margin + adjustment
    costs = {
        "material": material,
        "indexed_material": indexed,
        "non_indexed_material": non_indexed,
        "transport": transport,
        "direct_labor": dl,
        "voh": voh,
        "direct_cost": direct,
        "foh": foh,
        "fees": fees,
        "manufacturing_cost": manufacturing,
        "tooling_adder": tooling_adder,
        "specific_capex_adder": capex_adder,
    }
    yearly = _yearly_prices(context, costs, initial, warnings)
    target = _number(context.get("target_price"))
    assumptions = []
    for output in [bom, *simulation["components"].values(), *simulation["most"].values()]:
        if output:
            assumptions.extend(output.get("assumptions") or [])
    status = "blocked" if errors else ("assumption_based" if assumptions or warnings else "complete")
    result = {
        "schema_version": SCHEMA_VERSION,
        "simulation_id": simulation_id,
        "project_code": context.get("project_code"),
        "product_id": context.get("product_id"),
        "currency": context.get("reporting_currency") or unit.get("selling_currency"),
        "plant": context.get("production_plant"),
        "annual_quantity": _number(context.get("annual_quantity")),
        "cost_breakdown": costs,
        "initial_selling_price": None if errors else initial,
        "target_price": target,
        "margin_to_target": None if errors or target is None else target - initial,
        "yearly_prices": yearly if not errors else [],
        "calculation_details": {
            "components": component_details,
            "operations": operation_details,
            "plant_data": unit,
            "fx": [{"type": "30_day_average", "operating_to_selling": context.get("fx_operating_to_selling_30d") or context.get("fx_operating_to_selling"), "source": context.get("fx_source")}],
            "commercial_quotations": quotations,
        },
        "assumptions": list(dict.fromkeys(str(item) for item in assumptions)),
        "warnings": list(dict.fromkeys(warnings)),
        "blocking_errors": list(dict.fromkeys(errors)),
        "calculation_status": status,
        "pending_validation": {
            "roce": {"formula_status": "pending_validation"},
            "npv": {"formula_status": "pending_validation"},
            "working_capital": {"formula_status": "pending_validation"},
            "ebitda": {"formula_status": "pending_validation"},
            "operating_income": {"formula_status": "pending_validation"},
            "taxes": {"formula_status": "pending_validation"},
            "cash_generation": {"formula_status": "pending_validation"},
        },
    }
    paths = simulation_paths(simulation_id)
    atomic_write_json(paths["calculation_input"], {"context": context, "bom": bom, "components": simulation["components"], "most": simulation["most"]})
    atomic_write_json(paths["result"], result)
    _append_event(simulation_id, "simulation_calculated", status=status, blocking_errors=result["blocking_errors"])
    return result


def get_result(simulation_id: str) -> Dict[str, Any]:
    path = simulation_paths(simulation_id)["result"]
    result = _read(path)
    if not result:
        raise FileNotFoundError(f"Simulation result {simulation_id} not found.")
    return result


def get_simulation_master_data() -> Dict[str, Any]:
    from services.manufacturing_strategy import load_product_matrix

    catalog = get_product_catalog_from_db()
    matrix = load_product_matrix()
    products = sorted({row.get("product") for row in matrix if row.get("product")})
    zones = sorted({zone for row in matrix for zone in (row.get("zones") or {}) if zone})
    plants = sorted({plant for row in matrix for plant in (row.get("zones") or {}).values() if plant})
    return {
        "catalog": catalog,
        "product_options": products,
        "destination_zone_options": zones,
        "production_plant_options": plants,
        "sources": {
            "catalog": catalog.get("source"),
            "strategy_options": "csv.product_matrix",
        },
    }


def create_from_workflow(project_code: str, product_id: str) -> Dict[str, Any]:
    from services.project_data_paths import get_workflow_run_paths

    workflow_paths = get_workflow_run_paths(project_code, product_id)
    state = _read(workflow_paths["workflow_state_path"])
    if not state:
        raise FileNotFoundError(f"Workflow {project_code}/{product_id} not found.")
    context = {**(state.get("customer_input") or {}), "project_code": project_code, "product_id": product_id}
    simulation = create_simulation(context)
    sid = simulation["simulation_id"]
    raw_bom = _read(workflow_paths["normalized_bom_path"]) or _read(workflow_paths["raw_bom_path"])
    if raw_bom:
        save_output(sid, "bom", raw_bom)
    if workflow_paths["components_dir"].exists():
        for path in workflow_paths["components_dir"].glob("*.json"):
            save_output(sid, "component_costing", _read(path), path.stem)
    if workflow_paths["most_dir"].exists():
        for path in workflow_paths["most_dir"].glob("*.json"):
            save_output(sid, "most_final_assembly" if path.stem == "final_assembly" else "most_component", _read(path), path.stem)
    _append_event(sid, "workflow_imported", workflow=f"{project_code}/{product_id}")
    return get_simulation(sid)
