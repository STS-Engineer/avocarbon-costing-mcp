"""Component-level dimensional and logistics costing.

Fixes a regression where a raw BOM/technical quantity (e.g. a magnet wire's
developed length in millimetres, or a tin coating dimension) was multiplied
directly against a unit price or logistics rate expressed in an incompatible
unit (e.g. RMB/kg), producing material and transport costs that were two to
three orders of magnitude too high.

Centralizes:
  - pricing-quantity resolution (bom count vs. physical mass vs. physical
    length vs. a supplier's priced quantity) so one "quantity_per_product"
    field is never reused for incompatible concepts.
  - logistics rate-basis normalization for Olivier's transport rule:
        component_transport_cost_per_product = transport + customs + forwarder
    where each value is first converted to the same per-product basis as the
    real BOM pricing quantity.

Every computation either returns a fully resolved, unit-consistent cost, or a
structured "blocked" result naming exactly what is missing/incompatible.
Never silently multiplies incompatible units.
"""

import re
from typing import Any, Dict, List, Optional

from services.material_properties import derive_mass_g_from_cylindrical_wire


def _coerce_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _get_path(data: Any, path: List[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_value(data: Any, paths: List[List[str]]) -> Any:
    for path in paths:
        value = _get_path(data, path)
        if value not in (None, ""):
            return value
    return None


# ---------------------------------------------------------------------------
# Pricing-quantity resolution
# ---------------------------------------------------------------------------

def resolve_wire_pricing_quantity(bom_fields: Dict[str, Any], agent_raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Priority order for magnet-wire pricing quantity:
    1. explicit normalized BOM weight_kg_per_product
    2. confirmed External Component Agent weight, with an explicit kg unit
    3. derived weight from diameter_mm + developed_length_mm + copper density
    4. unresolved (caller must block)
    """
    bom_fields = bom_fields or {}
    agent_raw = agent_raw or {}

    explicit_kg = _coerce_number(bom_fields.get("weight_kg_per_product"))
    if explicit_kg is not None and explicit_kg > 0:
        return {
            "pricing_quantity": explicit_kg,
            "pricing_unit": "kg",
            "pricing_quantity_basis": "explicit_bom_weight_kg",
        }

    agent_weight = _coerce_number(_first_value(agent_raw, [
        ["technical_specification", "weight_kg"],
        ["recommended_offer", "weight_kg"],
        ["weight_kg"],
    ]))
    agent_weight_unit = _first_value(agent_raw, [
        ["technical_specification", "weight_unit"],
        ["recommended_offer", "weight_unit"],
        ["weight_unit"],
    ])
    if agent_weight is not None and agent_weight > 0 and str(agent_weight_unit or "kg").strip().lower() in {
        "kg", "kilogram", "kilograms",
    }:
        return {
            "pricing_quantity": agent_weight,
            "pricing_unit": "kg",
            "pricing_quantity_basis": "confirmed_agent_weight_kg",
        }

    diameter_mm = _coerce_number(bom_fields.get("diameter_mm"))
    length_mm = _coerce_number(bom_fields.get("physical_length_mm_per_product"))
    mass_g = derive_mass_g_from_cylindrical_wire(diameter_mm, length_mm, "copper")
    if mass_g is not None and mass_g > 0:
        return {
            "pricing_quantity": mass_g / 1000,
            "pricing_unit": "kg",
            "pricing_quantity_basis": "derived_from_diameter_length_density",
            "calculated_mass_g_per_product": mass_g,
        }

    return {"pricing_quantity": None, "pricing_unit": None, "pricing_quantity_basis": "unresolved"}


_WIRE_FAMILY_MARKERS = ("wire", "magnet_wire", "enameled_wire", "copper")


def resolve_component_pricing_quantity(
    component_id: str,
    material_family: Optional[str],
    bom_fields: Dict[str, Any],
    agent_raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the quantity (and its unit) that a component's unit price must
    be multiplied against. Distinguishes BOM piece count, physical mass, and
    physical length so none of them get silently conflated."""
    bom_fields = bom_fields or {}
    agent_raw = agent_raw or {}
    family_text = f"{material_family or ''} {component_id or ''}".lower()

    if any(marker in family_text for marker in _WIRE_FAMILY_MARKERS):
        return resolve_wire_pricing_quantity(bom_fields, agent_raw)

    explicit_kg = _coerce_number(bom_fields.get("weight_kg_per_product"))
    if explicit_kg is not None and explicit_kg > 0:
        return {"pricing_quantity": explicit_kg, "pricing_unit": "kg", "pricing_quantity_basis": "explicit_bom_weight_kg"}

    physical_mass_g = _coerce_number(bom_fields.get("physical_mass_g_per_product"))
    if physical_mass_g is not None and physical_mass_g > 0:
        return {
            "pricing_quantity": physical_mass_g / 1000,
            "pricing_unit": "kg",
            "pricing_quantity_basis": "explicit_bom_mass_g",
        }

    bom_count = _coerce_number(bom_fields.get("bom_count_per_product"))
    if bom_count is not None and bom_count > 0:
        return {"pricing_quantity": bom_count, "pricing_unit": "pc", "pricing_quantity_basis": "bom_count"}

    # Legacy fallback for BOM shapes that only provide quantity_per_product +
    # a unit string, with no dimensional field distinction at all.
    legacy_qty = _coerce_number(bom_fields.get("quantity_per_product"))
    legacy_unit = str(bom_fields.get("quantity_unit") or "").strip().lower()
    if legacy_qty is not None and legacy_qty > 0:
        if legacy_unit in {"kg", "kilogram", "kilograms"}:
            return {"pricing_quantity": legacy_qty, "pricing_unit": "kg", "pricing_quantity_basis": "legacy_quantity_as_kg"}
        if legacy_unit in {"", "pc", "pcs", "piece", "pieces", "u"}:
            return {"pricing_quantity": legacy_qty, "pricing_unit": "pc", "pricing_quantity_basis": "legacy_quantity_as_piece_count"}

    return {"pricing_quantity": None, "pricing_unit": None, "pricing_quantity_basis": "unresolved"}


# ---------------------------------------------------------------------------
# Unit-price resolution and basis normalization
# ---------------------------------------------------------------------------

def resolve_unit_price(agent_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The External Component Costing Agent's current contract exposes the
    priced value as `recommended_offer.price_in_reporting_currency` (legacy
    name, historically assumed "per piece" with no stated basis — the exact
    ambiguity that caused the wire/tin unit-mismatch bug). The new contract
    additionally provides an explicit `unit_price_basis` alongside it (or the
    newer `unit_price` field name outright). Both are read here; if neither
    carries an explicit basis, `unit_price_basis` comes back None and the
    caller blocks rather than assuming "per piece"."""
    agent_raw = agent_raw or {}
    unit_price = _coerce_number(_first_value(agent_raw, [
        ["recommended_offer", "unit_price"],
        ["recommended_offer", "supply_chain", "unit_price"],
        ["normalized_cost", "unit_price"],
        ["unit_price"],
        ["recommended_offer", "price_in_reporting_currency"],
    ]))
    currency = _first_value(agent_raw, [
        ["recommended_offer", "unit_price_currency"],
        ["recommended_offer", "currency"],
        ["normalized_cost", "currency"],
        ["currency"],
        ["recommended_offer", "supplier_currency"],
    ])
    basis = _first_value(agent_raw, [
        ["recommended_offer", "unit_price_basis"],
        ["recommended_offer", "pricing_unit"],
        ["normalized_cost", "unit_price_basis"],
        ["unit_price_basis"],
        ["pricing_unit"],
    ])
    return {"unit_price": unit_price, "unit_price_currency": currency, "unit_price_basis": basis}


_BASIS_UNIT_ALIASES = {
    "kg": "kg", "cny/kg": "kg", "rmb/kg": "kg", "eur/kg": "kg", "usd/kg": "kg", "per_kg": "kg",
    "pc": "pc", "pcs": "pc", "piece": "pc", "cny/pc": "pc", "rmb/pc": "pc", "eur/pc": "pc",
    "usd/pc": "pc", "per_piece": "pc", "u": "pc", "cny/pce": "pc", "rmb/pce": "pc",
    "m": "m", "meter": "m", "metre": "m", "cny/m": "m", "rmb/m": "m", "eur/m": "m", "usd/m": "m", "per_m": "m",
    "shipment": "shipment", "per_shipment": "shipment", "cny/shipment": "shipment", "rmb/shipment": "shipment",
    "percentage_of_component_value": "percentage", "percent_of_value": "percentage", "%": "percentage",
    "percentage": "percentage",
}


def normalize_unit_basis(unit_price_basis: Any) -> Optional[str]:
    """Map a free-text rate basis (e.g. "CNY/kg", "per_kg", "RMB/pc") to a
    canonical quantity kind: "kg" | "pc" | "m" | "shipment" | "percentage"."""
    if not unit_price_basis:
        return None
    text = str(unit_price_basis).strip().lower()
    if text in _BASIS_UNIT_ALIASES:
        return _BASIS_UNIT_ALIASES[text]
    for token, kind in _BASIS_UNIT_ALIASES.items():
        if text.endswith("/" + token):
            return kind
    if "%" in text or "percent" in text:
        return "percentage"
    if "shipment" in text:
        return "shipment"
    if "/kg" in text or text.endswith("kg"):
        return "kg"
    if "/m" in text or text.endswith(" m") or text.endswith("/meter") or text.endswith("/metre"):
        return "m"
    if "/pc" in text or "piece" in text or text.endswith("/u"):
        return "pc"
    return None


# ---------------------------------------------------------------------------
# Material cost
# ---------------------------------------------------------------------------

def compute_component_material_cost(
    component_id: str,
    pricing_quantity_info: Dict[str, Any],
    price_info: Dict[str, Any],
) -> Dict[str, Any]:
    pricing_quantity = pricing_quantity_info.get("pricing_quantity")
    pricing_unit = pricing_quantity_info.get("pricing_unit")
    unit_price = price_info.get("unit_price")
    currency = price_info.get("unit_price_currency")
    raw_basis = price_info.get("unit_price_basis")
    price_basis_kind = normalize_unit_basis(raw_basis)

    base = {
        "component_id": component_id,
        "physical_quantity": pricing_quantity,
        "physical_unit": pricing_unit,
        "pricing_unit": raw_basis,
    }
    if pricing_quantity is None or pricing_unit is None:
        return {**base, "status": "blocked", "reason": "technical_quantity_unit_unknown"}
    if unit_price is None:
        return {**base, "status": "blocked", "reason": "unit_price_missing"}
    if not currency:
        return {**base, "status": "blocked", "reason": "currency_missing"}
    if price_basis_kind is None:
        return {**base, "status": "blocked", "reason": "pricing_unit_unknown"}
    if price_basis_kind != pricing_unit:
        return {**base, "status": "blocked", "reason": "pricing_unit_mismatch"}

    material_cost_per_product = pricing_quantity * unit_price
    return {
        "status": "calculated",
        "component_id": component_id,
        "pricing_quantity": pricing_quantity,
        "pricing_unit": pricing_unit,
        "unit_price": unit_price,
        "unit_price_currency": currency,
        "material_cost_per_product": material_cost_per_product,
        "currency": currency,
    }


# ---------------------------------------------------------------------------
# Logistics (transport + customs + forwarder) — Olivier's component rule
# ---------------------------------------------------------------------------

_LOGISTICS_FIELD_NAMES = {
    "transport": ["transportation_cost", "transport_cost", "transportation_cost_per_piece"],
    "customs": ["custom_duty_cost", "customs_duty_cost", "duty_cost", "customs_cost_per_piece"],
    "forwarder": ["forwarder_cost", "forwarding_cost", "forwarder_cost_per_piece"],
}


def resolve_logistics_value(agent_raw: Optional[Dict[str, Any]], field_names: List[str]) -> Dict[str, Any]:
    """A logistics field may be a bare number (with sibling `<name>_currency`
    / `<name>_basis` fields) or a structured {value, currency, rate_basis}
    object. Both shapes are tolerated. Checked under `recommended_offer`
    directly (the current External Component Costing Agent contract),
    `recommended_offer.supply_chain` (a proposed structured shape), and a
    handful of other common containers."""
    agent_raw = agent_raw or {}
    value = None
    currency = None
    basis = None
    for name in field_names:
        for container in [
            ["recommended_offer", "supply_chain"],
            ["recommended_offer"],
            ["supply_chain"],
            ["normalized_cost"],
            [],
        ]:
            candidate = _get_path(agent_raw, container + [name])
            if isinstance(candidate, dict):
                v = _coerce_number(candidate.get("value"))
                if v is not None:
                    value, currency, basis = v, candidate.get("currency"), (candidate.get("rate_basis") or candidate.get("unit"))
                    break
            elif candidate not in (None, ""):
                v = _coerce_number(candidate)
                if v is not None:
                    value = v
                    currency = _get_path(agent_raw, container + [f"{name}_currency"])
                    basis = _get_path(agent_raw, container + [f"{name}_basis"]) or _get_path(agent_raw, container + [f"{name}_unit"])
                    break
        if value is not None:
            break
    if currency is None:
        currency = _first_value(agent_raw, [
            ["recommended_offer", "supply_chain", "currency"],
            ["recommended_offer", "currency"],
            ["supply_chain", "currency"],
            ["currency"],
        ])
    return {"value": value, "currency": currency, "rate_basis": basis}


def _normalize_percent_or_fraction(value: float) -> float:
    return value / 100 if value > 1 else value


def convert_logistics_value_to_per_product(
    value: float,
    basis_kind: str,
    pricing_quantity_info: Dict[str, Any],
    material_cost_per_product: Optional[float],
) -> "tuple[Optional[float], Optional[str]]":
    pricing_quantity = pricing_quantity_info.get("pricing_quantity")
    pricing_unit = pricing_quantity_info.get("pricing_unit")
    length_mm = pricing_quantity_info.get("physical_length_mm_per_product")

    if basis_kind == "percentage":
        if material_cost_per_product is None:
            return None, "material_cost_required_for_percentage_basis"
        return _normalize_percent_or_fraction(value) * material_cost_per_product, None
    if basis_kind == pricing_unit and pricing_quantity is not None:
        return value * pricing_quantity, None
    if basis_kind == "m" and length_mm is not None:
        return value * (length_mm / 1000), None
    return None, "logistics_rate_basis_incompatible_with_bom_quantity"


def compute_component_transport_cost(
    component_id: str,
    agent_raw: Optional[Dict[str, Any]],
    pricing_quantity_info: Dict[str, Any],
    material_cost_per_product: Optional[float] = None,
) -> Dict[str, Any]:
    """Olivier's rule: transport + customs duty + forwarder fee, each
    converted to the same per-product basis as the BOM pricing quantity,
    then summed. A field that is genuinely absent contributes 0 and does not
    block; a field that is present but whose unit can't be reconciled with
    the BOM quantity blocks with a structured error."""
    agent_raw = agent_raw or {}
    breakdown: Dict[str, Any] = {}
    total = 0.0

    for label, names in _LOGISTICS_FIELD_NAMES.items():
        resolved = resolve_logistics_value(agent_raw, names)
        value = resolved["value"]
        if value is None:
            breakdown[label] = {"value": 0.0, "currency": None, "rate_basis": None, "converted_value": 0.0}
            continue
        if not resolved["currency"]:
            return {
                "status": "blocked",
                "component_id": component_id,
                "reason": "currency_missing",
                "field": label,
            }
        basis_kind = normalize_unit_basis(resolved["rate_basis"])
        if basis_kind is None:
            return {
                "status": "blocked",
                "component_id": component_id,
                "reason": "logistics_rate_basis_unknown",
                "field": label,
                "physical_quantity": pricing_quantity_info.get("pricing_quantity"),
                "physical_unit": pricing_quantity_info.get("pricing_unit"),
                "pricing_unit": None,
            }
        converted, error = convert_logistics_value_to_per_product(
            value, basis_kind, pricing_quantity_info, material_cost_per_product,
        )
        if error:
            return {
                "status": "blocked",
                "component_id": component_id,
                "reason": error,
                "field": label,
                "physical_quantity": pricing_quantity_info.get("pricing_quantity"),
                "physical_unit": pricing_quantity_info.get("pricing_unit"),
                "pricing_unit": resolved["rate_basis"],
            }
        breakdown[label] = {
            "value": value,
            "currency": resolved["currency"],
            "rate_basis": resolved["rate_basis"],
            "converted_value": converted,
        }
        total += converted

    return {
        "status": "calculated",
        "component_id": component_id,
        "transport_cost_per_product": total,
        "logistics_breakdown": breakdown,
    }
