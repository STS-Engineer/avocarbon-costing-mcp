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
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any, Dict, List, Optional

from services.material_properties import derive_mass_g_from_cylindrical_wire
from services.currency_service import convert_currency, normalize_currency_code


getcontext().prec = 28

MONEY_INTERMEDIATE_PRECISION = 12
MONEY_UNIT_DISPLAY_DECIMALS = 6
MONEY_PER_PRODUCT_DISPLAY_DECIMALS = 6
DELIVERED_COST_RECONCILIATION_TOLERANCE = Decimal("0.000001")


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _decimal_float(value: Optional[Decimal]) -> Optional[float]:
    return float(value) if value is not None else None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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


def _normalized_quantity_unit(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s*/\s*(?:product|piece|assembly|produit)$", "", text)
    aliases = {
        "pc": "pc", "pcs": "pc", "piece": "pc", "pieces": "pc", "pce": "pc", "u": "pc",
        "kg": "kg", "kilogram": "kg", "kilograms": "kg",
        "g": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
        "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
        "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    }
    return aliases.get(text)


def _currency_from_monetary_unit(value: Any) -> Optional[str]:
    direct = normalize_currency_code(value)
    if direct:
        return direct
    match = re.search(r"\b([A-Za-z]{3})\b", str(value or ""))
    return normalize_currency_code(match.group(1)) if match else None


def _unit_from_text(value: Any) -> Optional[str]:
    text = str(value or "").lower()
    for pattern, unit in (
        (r"\b(?:pcs?|pieces?|pce|unit(?:s)?)\b", "pc"),
        (r"\bkg\b", "kg"),
        (r"\b(?:g|gram(?:me)?s?)\b", "g"),
        (r"\bmm\b", "mm"),
        (r"\b(?:m|met(?:er|re)s?)\b", "m"),
    ):
        if re.search(pattern, text):
            return unit
    return None


def extract_bom_dimensional_fields(component_id: str, component: Dict[str, Any]) -> Dict[str, Any]:
    """Extract controlled quantity paths from one raw BOM line."""
    component = component or {}
    containers = [component]
    for name in ("technical_specification", "specification", "calculations", "calculation", "component_definition"):
        nested = component.get(name)
        if isinstance(nested, dict):
            containers.append(nested)

    def value(*names: str) -> Any:
        for container in containers:
            for name in names:
                candidate = container.get(name)
                if candidate not in (None, ""):
                    return candidate
        return None

    quantity_raw = value("quantity_per_product", "quantity_per_assembly", "quantity", "qty", "quantite")
    quantity_value = quantity_raw
    quantity_unit = value("quantity_unit", "unit", "unite_quantite", "technical_quantity_unit")
    if isinstance(quantity_raw, dict):
        quantity_value = quantity_raw.get("value") or quantity_raw.get("quantity")
        quantity_unit = quantity_unit or quantity_raw.get("unit")
    quantity_unit = quantity_unit or _unit_from_text(quantity_value)

    weight_kg = _coerce_number(value("weight_kg_per_product", "weight_kg", "part_weight_kg", "mass_kg_per_product"))
    mass_g = _coerce_number(value("mass_g_per_product", "physical_mass_g_per_product", "weight_g_per_product"))
    raw_weight_value = value("weight_per_piece", "poids_par_piece", "mass_per_product")
    raw_weight_unit = _normalized_quantity_unit(value("weight_unit", "unite_poids", "mass_unit"))
    if isinstance(raw_weight_value, dict):
        raw_weight_unit = raw_weight_unit or _normalized_quantity_unit(raw_weight_value.get("unit"))
        raw_weight_value = raw_weight_value.get("value") or raw_weight_value.get("quantity")
    raw_weight = _coerce_number(raw_weight_value)
    raw_weight_unit = raw_weight_unit or _unit_from_text(raw_weight_value)
    if (
        component_id == "glue"
        and re.search(r"confirm|indicati|hypoth", str(raw_weight_value or ""), re.IGNORECASE)
    ):
        raw_weight = None
        raw_weight_unit = None
    raw_quantity = _coerce_number(quantity_value)
    per_item_multiplier = 1.0
    if raw_quantity and raw_quantity > 0 and component_id in {"lead_tinning", "glue"}:
        per_item_multiplier = raw_quantity
    if re.search(
        r"(?:per product|par (?:piece|produit)|total)",
        str(raw_weight_value or ""),
        re.IGNORECASE,
    ):
        per_item_multiplier = 1.0
    if weight_kg is None and raw_weight is not None and raw_weight_unit == "kg":
        weight_kg = raw_weight * per_item_multiplier
    if mass_g is None and raw_weight is not None and raw_weight_unit == "g":
        mass_g = raw_weight * per_item_multiplier

    length_mm = _coerce_number(value(
        "developed_length_mm", "wire_length_mm", "total_length_mm", "physical_length_mm_per_product",
    ))
    length_m = _coerce_number(value("developed_length_m", "wire_length_m", "physical_length_m_per_product"))
    normalized_quantity_unit = _normalized_quantity_unit(quantity_unit)
    quantity = _coerce_number(quantity_value)
    if length_mm is None and length_m is not None:
        length_mm = length_m * 1000
    if normalized_quantity_unit == "g" and mass_g is None:
        mass_g = quantity
    elif normalized_quantity_unit == "kg" and weight_kg is None:
        weight_kg = quantity
    elif normalized_quantity_unit == "m" and length_mm is None and quantity is not None:
        length_mm = quantity * 1000
    elif normalized_quantity_unit == "mm" and length_mm is None:
        length_mm = quantity

    bom_count = _coerce_number(value("bom_count_per_product", "piece_count_per_product"))
    if bom_count is None and normalized_quantity_unit == "pc":
        bom_count = quantity
    if bom_count is None and component_id == "ferrite_core" and quantity is not None and not normalized_quantity_unit:
        bom_count = quantity

    diameter = _coerce_number(value("diameter_mm", "wire_diameter_mm", "wire_diameter", "diametre_mm"))
    if diameter is None:
        diameter_text = " ".join(str(value(name) or "") for name in (
            "product_designation", "produit_designation", "designation", "specification",
        ))
        match = re.search(r"(?:[Øø]|(?:dia(?:meter|metre)?\.?))\s*(\d+(?:[.,]\d+)?)\s*mm", diameter_text, re.IGNORECASE)
        if match:
            diameter = _coerce_number(match.group(1).replace(",", "."))

    normalized = {
        "weight_kg_per_product": weight_kg,
        "physical_mass_g_per_product": mass_g,
        "physical_length_mm_per_product": length_mm,
        "diameter_mm": diameter,
        "bom_count_per_product": bom_count,
        "quantity_per_product": quantity,
        "quantity_unit": normalized_quantity_unit,
        "source_quantity_value": quantity_raw,
        "source_weight_value": raw_weight_value,
        "mass_is_estimated": bool(
            re.search(r"estim|indicati", str(raw_weight_value or ""), re.IGNORECASE)
        ),
        "mass_is_conditional": bool(
            re.search(r"confirm|indicati|si\b", str(raw_weight_value or ""), re.IGNORECASE)
        ),
    }
    return normalized


def resolve_annual_purchasing_quantity(
    component_id: str,
    material_family: Optional[str],
    bom_fields: Dict[str, Any],
    annual_product_quantity: Any,
) -> Dict[str, Any]:
    """Resolve supplier-facing annual demand without conflating dimensions."""
    annual_products = _coerce_number(annual_product_quantity)
    family_text = f"{material_family or ''} {component_id or ''}".lower()
    if annual_products is None or annual_products <= 0:
        return {
            "annual_product_quantity": annual_products,
            "purchasing_quantity_per_product": None,
            "annual_purchasing_quantity": None,
            "annual_purchasing_unit": None,
            "status": "blocked",
            "reason": "annual_product_quantity_missing",
        }

    expected_unit = None
    if component_id == "ferrite_core" or "ferrite" in family_text:
        expected_unit = "pc"
    elif component_id in {"magnet_wire", "lead_tinning", "glue"} or any(
        marker in family_text for marker in ("wire", "tin", "solder", "glue", "adhesive")
    ):
        expected_unit = "kg"

    synthetic_offer = {"recommended_offer": {"pricing_unit": expected_unit}} if expected_unit else {}
    quantity = resolve_component_pricing_quantity(
        component_id, material_family, bom_fields, synthetic_offer,
    )
    per_product = quantity.get("pricing_quantity")
    unit = quantity.get("pricing_unit")
    if per_product is None or not unit:
        return {
            "annual_product_quantity": annual_products,
            "purchasing_quantity_per_product": None,
            "annual_purchasing_quantity": None,
            "annual_purchasing_unit": expected_unit,
            "status": "blocked",
            "reason": "purchasing_quantity_unresolved",
        }
    return {
        "annual_product_quantity": annual_products,
        "purchasing_quantity_per_product": per_product,
        "annual_purchasing_quantity": annual_products * per_product,
        "annual_purchasing_unit": unit,
        "purchasing_quantity_basis": quantity.get("pricing_quantity_basis"),
        "technical_length_m_per_product": quantity.get("technical_length_m_per_product"),
        "status": "resolved",
    }


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
            "pricing_quantity": _decimal_float(_decimal(mass_g) / Decimal("1000")),
            "pricing_unit": "kg",
            "pricing_quantity_basis": "derived_from_diameter_length_density",
            "calculated_mass_g_per_product": mass_g,
        }

    return {"pricing_quantity": None, "pricing_unit": None, "pricing_quantity_basis": "unresolved"}


_WIRE_FAMILY_MARKERS = ("wire", "magnet_wire", "enameled_wire", "copper")


def _resolve_component_pricing_quantity_legacy(
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
            "pricing_quantity": _decimal_float(
                _decimal(physical_mass_g) / Decimal("1000")
            ),
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


def resolve_component_pricing_quantity(
    component_id: str,
    material_family: Optional[str],
    bom_fields: Dict[str, Any],
    agent_raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve a BOM quantity compatible with the supplier's explicit unit."""
    bom_fields = bom_fields or {}
    agent_raw = agent_raw or {}
    family_text = f"{material_family or ''} {component_id or ''}".lower()
    offer = resolve_component_offer(agent_raw)
    offer_unit = offer.get("pricing_unit") or normalize_unit_basis(offer.get("pricing_basis"))

    normalized_component = (
        agent_raw.get("normalized_component")
        if isinstance(agent_raw.get("normalized_component"), dict)
        else agent_raw
    )
    explicit_quantity = _coerce_number(normalized_component.get("technical_quantity"))
    explicit_unit = _normalized_quantity_unit(normalized_component.get("technical_quantity_unit"))
    if (
        explicit_quantity is not None
        and explicit_quantity > 0
        and explicit_unit
        and (offer_unit is None or explicit_unit == offer_unit)
    ):
        return {
            "pricing_quantity": explicit_quantity,
            "pricing_unit": explicit_unit,
            "pricing_quantity_basis": "explicit_normalized_component_quantity",
            "source_quantity": normalized_component.get("source_quantity"),
            "conversion": normalized_component.get("conversion"),
        }

    kg = _coerce_number(bom_fields.get("weight_kg_per_product"))
    mass_g = _coerce_number(bom_fields.get("physical_mass_g_per_product"))
    length_mm = _coerce_number(bom_fields.get("physical_length_mm_per_product"))
    count = _coerce_number(bom_fields.get("bom_count_per_product"))
    if kg is None and mass_g is not None:
        kg = _decimal_float(_decimal(mass_g) / Decimal("1000"))
    if mass_g is None and kg is not None:
        mass_g = kg * 1000

    is_wire = any(marker in family_text for marker in _WIRE_FAMILY_MARKERS)
    technical_specification = (
        agent_raw.get("technical_specification")
        if isinstance(agent_raw.get("technical_specification"), dict)
        else {}
    )
    if component_id == "glue":
        confirmed_glue_g = _coerce_number(
            technical_specification.get("confirmed_mass_per_piece_g")
        )
        if confirmed_glue_g is not None and confirmed_glue_g > 0:
            mass_g = confirmed_glue_g
            kg = _decimal_float(_decimal(confirmed_glue_g) / Decimal("1000"))
    elif is_wire and kg is None:
        estimated_wire_g = _coerce_number(_first_value(technical_specification, [
            ["estimated_mass_per_piece_g"],
            ["mass_g_per_product"],
            ["weight_g_per_product"],
        ]))
        if estimated_wire_g is not None and estimated_wire_g > 0:
            mass_g = estimated_wire_g
            kg = _decimal_float(_decimal(estimated_wire_g) / Decimal("1000"))
    elif component_id in {"lead_tinning", "lead_tin_plating", "tin_plating"} and kg is None:
        indicative_tin_kg = _coerce_number(
            technical_specification.get("indicative_tin_mass_per_product_kg")
        )
        indicative_tin_g = _coerce_number(
            technical_specification.get("indicative_tin_mass_per_product_g")
        )
        if indicative_tin_kg is not None and indicative_tin_kg > 0:
            kg = indicative_tin_kg
            mass_g = indicative_tin_kg * 1000
        elif indicative_tin_g is not None and indicative_tin_g > 0:
            mass_g = indicative_tin_g
            kg = _decimal_float(_decimal(indicative_tin_g) / Decimal("1000"))

    if is_wire and kg is None:
        diameter_mm = _coerce_number(bom_fields.get("diameter_mm"))
        derived_mass_g = derive_mass_g_from_cylindrical_wire(diameter_mm, length_mm, "copper")
        if derived_mass_g is not None and derived_mass_g > 0:
            mass_g = derived_mass_g
            kg = _decimal_float(_decimal(derived_mass_g) / Decimal("1000"))

    candidates = {
        "kg": (kg, "explicit_or_derived_mass_kg"),
        "g": (mass_g, "explicit_or_derived_mass_g"),
        "m": ((length_mm / 1000) if length_mm is not None else None, "explicit_length_m"),
        "mm": (length_mm, "explicit_length_mm"),
        "pc": (count, "bom_count"),
    }
    if offer_unit in candidates:
        quantity, basis = candidates[offer_unit]
        if quantity is not None and quantity > 0:
            result = {
                "pricing_quantity": quantity,
                "pricing_unit": offer_unit,
                "pricing_quantity_basis": basis,
                "technical_length_m_per_product": (length_mm / 1000) if length_mm is not None else None,
                "technical_mass_kg_per_product": kg,
            }
            if (
                is_wire
                and kg is not None
                and bom_fields.get("mass_is_estimated")
            ):
                result["pricing_quantity_basis"] = "estimated_bom_mass"
                result["warnings"] = ["Final wire cut length and mass remain unconfirmed."]
            elif is_wire and kg is not None and bom_fields.get("weight_kg_per_product") in (None, ""):
                if _coerce_number(technical_specification.get("estimated_mass_per_piece_g")):
                    result["pricing_quantity_basis"] = "estimated_bom_mass"
                    result["warnings"] = ["Final wire cut length and mass remain unconfirmed."]
                else:
                    result["pricing_quantity_basis"] = "derived_from_diameter_length_density"
            if component_id in {"lead_tinning", "lead_tin_plating", "tin_plating"}:
                result["pricing_quantity_basis"] = "indicative_conditional"
                result["warnings"] = [
                    "Tinning requirement and effective tinned length remain unconfirmed."
                ]
            return result
        return {"pricing_quantity": None, "pricing_unit": None, "pricing_quantity_basis": "incompatible_or_missing_conversion_input"}

    # A missing supplier unit never gets inferred from price magnitude. The
    # technical quantity is still resolved for diagnostics where unambiguous.
    if is_wire:
        return resolve_wire_pricing_quantity(bom_fields, agent_raw)
    if kg is not None and kg > 0:
        return {"pricing_quantity": kg, "pricing_unit": "kg", "pricing_quantity_basis": "explicit_bom_mass"}
    if count is not None and count > 0:
        return {"pricing_quantity": count, "pricing_unit": "pc", "pricing_quantity_basis": "bom_count"}
    return {"pricing_quantity": None, "pricing_unit": None, "pricing_quantity_basis": "unresolved"}


# ---------------------------------------------------------------------------
# Unit-price resolution and basis normalization
# ---------------------------------------------------------------------------

def _resolve_unit_price_legacy(agent_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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


def resolve_component_offer(agent_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve one selected offer using controlled source and field priority."""
    agent_raw = agent_raw or {}
    sources = [
        ("recommended_offer", agent_raw.get("recommended_offer")),
        ("selected_offer", agent_raw.get("selected_offer")),
        ("offer", agent_raw.get("offer")),
        ("root", agent_raw),
    ]

    def offer_value(*keys: str) -> Any:
        for _, candidate in sources:
            if not isinstance(candidate, dict):
                continue
            value = _first_value(candidate, [[key] for key in keys])
            if value not in (None, ""):
                return value
        return None

    unit_price_raw = offer_value(
        "unit_price", "delivered_cost_per_unit", "delivered_cost_per_piece",
        "delivered_cost", "price_in_reporting_currency", "selling_price_per_unit",
    )
    source_path = next((
        name for name, candidate in sources
        if isinstance(candidate, dict) and unit_price_raw in candidate.values()
    ), "root")
    currency = normalize_currency_code(offer_value(
        "currency", "unit_price_currency", "price_currency", "offer_currency",
        "supplier_currency", "purchasing_currency", "reporting_currency",
    ))
    pricing_basis = offer_value("pricing_basis", "unit_price_basis", "price_basis", "rate_basis")
    pricing_unit = _normalized_quantity_unit(offer_value("pricing_unit", "unit_price_unit"))
    basis_unit = normalize_unit_basis(pricing_basis)
    if pricing_unit is None and basis_unit in {"pc", "kg", "g", "m", "mm"}:
        pricing_unit = basis_unit
    if pricing_basis in (None, "") and currency and pricing_unit:
        pricing_basis = f"{currency}/{pricing_unit}"

    normalized = {
        "supplier": offer_value("supplier", "supplier_name"),
        "unit_price": _coerce_number(unit_price_raw),
        "delivered_cost_per_unit": _coerce_number(offer_value(
            "delivered_cost_per_unit", "delivered_cost_per_piece", "delivered_cost",
        )),
        "currency": currency,
        "pricing_unit": pricing_unit,
        "pricing_basis": pricing_basis,
        "incoterm": offer_value("incoterm"),
        "origin": offer_value("origin"),
        "source_path": source_path,
        "converted_to_project_currency": False,
        "original_unit_price": _coerce_number(offer_value("original_unit_price")),
        "original_currency": normalize_currency_code(offer_value("original_currency")),
        "conversion_rate": _coerce_number(offer_value("conversion_rate")),
        "conversion_rate_date": offer_value("conversion_rate_date"),
        "converted_unit_price": _coerce_number(offer_value("converted_unit_price")),
        "converted_currency": normalize_currency_code(offer_value("converted_currency")),
    }
    supply_chain = (
        agent_raw.get("recommended_offer", {}).get("supply_chain")
        if isinstance(agent_raw.get("recommended_offer"), dict)
        and isinstance(agent_raw.get("recommended_offer", {}).get("supply_chain"), dict)
        else {}
    )
    delivered_value = _coerce_number(
        offer_value("delivered_cost_per_unit", "delivered_cost_per_piece", "delivered_cost")
    )
    if delivered_value is None:
        delivered_value = _coerce_number(supply_chain.get("delivered_cost"))
    delivered_currency_raw = (
        offer_value("delivered_cost_currency")
        or supply_chain.get("delivered_cost_currency")
    )
    delivered_currency = _currency_from_monetary_unit(delivered_currency_raw)
    delivered_currency_inherited = False
    if delivered_value is not None and not delivered_currency and currency:
        delivered_currency = currency
        delivered_currency_inherited = True
    delivered_basis = (
        offer_value("delivered_cost_basis", "delivered_cost_per_unit_basis")
        or supply_chain.get("delivered_cost_basis")
        or delivered_currency_raw
        or pricing_unit
    )
    normalized.update({
        "delivered_cost_per_unit": delivered_value,
        "delivered_cost_currency": delivered_currency,
        "delivered_cost_basis": delivered_basis,
        "delivered_currency_inherited_from_offer": delivered_currency_inherited,
    })
    normalized["transport"] = resolve_logistics_value(agent_raw, _LOGISTICS_FIELD_NAMES["transport"])
    normalized["customs"] = resolve_logistics_value(agent_raw, _LOGISTICS_FIELD_NAMES["customs"])
    normalized["forwarder_fee"] = resolve_logistics_value(agent_raw, _LOGISTICS_FIELD_NAMES["forwarder"])
    return normalized


def resolve_unit_price(
    agent_raw: Optional[Dict[str, Any]],
    target_currency: Any = None,
) -> Dict[str, Any]:
    offer = resolve_component_offer(agent_raw)
    target = normalize_currency_code(target_currency)
    converted_currency = normalize_currency_code(offer.get("converted_currency"))
    converted_unit_price = _coerce_number(offer.get("converted_unit_price"))
    offer_currency = normalize_currency_code(offer.get("currency"))
    offer_unit_price = _coerce_number(offer.get("unit_price"))
    use_converted = bool(
        target
        and converted_currency == target
        and converted_unit_price is not None
        and (offer_unit_price is None or offer_currency != target)
    )
    if use_converted:
        unit_price = converted_unit_price
        currency = converted_currency
        fx = {
            "status": "already_converted",
            "source_currency": offer.get("original_currency") or offer.get("currency"),
            "destination_currency": converted_currency,
            "rate": offer.get("conversion_rate"),
            "rate_date": offer.get("conversion_rate_date"),
            "rate_source": "normalized_offer_conversion",
            "original_unit_price": offer.get("original_unit_price") or offer.get("unit_price"),
            "converted_unit_price": converted_unit_price,
        }
    else:
        unit_price = offer_unit_price
        currency = offer_currency
        fx = None
    return {
        "unit_price": unit_price,
        "unit_price_currency": currency,
        # Cost arithmetic consumes the canonical unit. Keep the descriptive
        # supplier basis separately so prose can never overwrite "kg"/"pc".
        "unit_price_basis": offer.get("pricing_unit") or offer.get("pricing_basis"),
        "pricing_basis_description": offer.get("pricing_basis"),
        "normalized_offer": offer,
        "fx": fx,
        "calculation_source": (
            "normalized_offer.converted_unit_price"
            if use_converted else "normalized_offer.unit_price"
        ),
    }


_DELIVERED_ADJUSTMENT_SPECS = {
    "transport": {
        "fields": ("transportation_cost", "transport_cost"),
        "basis": ("transportation_cost_basis", "transport_basis"),
    },
    "customs": {
        "fields": ("custom_duty_cost", "customs_duty_cost", "customs_cost", "duty_cost"),
        "basis": ("custom_duty_cost_basis", "customs_cost_basis", "customs_basis"),
    },
    "forwarder_fee": {
        "fields": ("forwarder_fee", "forwarder_fees", "forwarder_cost", "forwarding_cost"),
        "basis": ("forwarder_fee_basis", "forwarder_cost_basis", "forwarder_basis"),
    },
    "capital_cost": {
        "fields": ("capital_cost_12pct", "capital_cost_at_12_percent", "capital_cost"),
        "basis": ("capital_cost_basis",),
    },
    "packaging": {
        "fields": ("packaging_cost", "packing_cost"),
        "basis": ("packaging_cost_basis", "packing_cost_basis"),
    },
    "insurance": {
        "fields": ("insurance_cost",),
        "basis": ("insurance_cost_basis",),
    },
    "bank_fee": {
        "fields": ("bank_fee", "bank_fees", "bank_cost"),
        "basis": ("bank_fee_basis", "bank_cost_basis"),
    },
    "handling": {
        "fields": ("handling_cost", "handling_fee"),
        "basis": ("handling_cost_basis", "handling_fee_basis"),
    },
    "scrap_loss": {
        "fields": ("scrap_loss_cost", "loss_cost"),
        "basis": ("scrap_loss_cost_basis", "loss_cost_basis"),
    },
    "surcharge": {
        "fields": ("surcharge", "surcharge_cost"),
        "basis": ("surcharge_basis", "surcharge_cost_basis"),
    },
    "other_adjustment": {
        "fields": ("other_adjustment", "other_cost_adjustment"),
        "basis": ("other_adjustment_basis", "other_cost_adjustment_basis"),
    },
}


def _first_adjustment_field(
    agent_raw: Dict[str, Any],
    field_names: tuple[str, ...],
) -> tuple[Optional[Decimal], Optional[List[str]], Optional[str]]:
    containers = (
        ["recommended_offer", "supply_chain"],
        ["recommended_offer"],
        ["supply_chain"],
        [],
    )
    for container in containers:
        for field_name in field_names:
            value = _decimal(_get_path(agent_raw, container + [field_name]))
            if value is not None:
                return value, container, field_name
    return None, None, None


def _adjustment_currency_and_basis(
    agent_raw: Dict[str, Any],
    container: List[str],
    field_name: str,
    basis_names: tuple[str, ...],
    offer_currency: Optional[str],
    delivered_currency: Optional[str],
    delivered_basis: Optional[str],
) -> Dict[str, Any]:
    explicit_currency_value = _get_path(
        agent_raw, container + [f"{field_name}_currency"]
    )
    explicit_currency = _currency_from_monetary_unit(explicit_currency_value)
    if not explicit_currency and container == ["recommended_offer", "supply_chain"]:
        explicit_currency_value = _get_path(
            agent_raw, ["recommended_offer", f"{field_name}_currency"]
        )
        explicit_currency = _currency_from_monetary_unit(explicit_currency_value)
    explicit_basis = next((
        _get_path(agent_raw, container + [name])
        for name in basis_names
        if _get_path(agent_raw, container + [name]) not in (None, "")
    ), None)
    if explicit_basis is None and container == ["recommended_offer", "supply_chain"]:
        explicit_basis = next((
            _get_path(agent_raw, ["recommended_offer", name])
            for name in basis_names
            if _get_path(agent_raw, ["recommended_offer", name]) not in (None, "")
        ), None)

    is_offer_scoped = container in (
        ["recommended_offer"],
        ["recommended_offer", "supply_chain"],
    )
    container_currency = (
        _currency_from_monetary_unit(
            _get_path(agent_raw, container + ["currency"])
        )
        if is_offer_scoped else None
    )
    inherited_currency = None
    if not explicit_currency and is_offer_scoped:
        inherited_currency = container_currency or delivered_currency or offer_currency
    inherited_basis = None
    if explicit_basis in (None, "") and is_offer_scoped:
        inherited_basis = delivered_basis
    currency_basis = normalize_unit_basis(explicit_currency_value)
    resolved_basis = currency_basis or explicit_basis or inherited_basis
    if (
        normalize_unit_basis(resolved_basis) == "percentage"
        and any(marker in field_name for marker in ("cost", "fee"))
        and delivered_basis
    ):
        # The numeric field is already a monetary amount. Its explanatory
        # text may describe the percentage used to derive that amount.
        resolved_basis = delivered_basis
        inherited_basis = delivered_basis
    return {
        "currency": explicit_currency or inherited_currency,
        "basis": resolved_basis,
        "currency_inherited_from_offer": bool(
            inherited_currency and not explicit_currency
        ),
        "basis_inherited_from_delivered_cost": bool(
            inherited_basis and explicit_basis in (None, "")
        ),
    }


def _base_unit_cost_audit(
    agent_raw: Dict[str, Any],
    price_info: Dict[str, Any],
) -> Dict[str, Any]:
    offer = price_info.get("normalized_offer") or {}
    base = _decimal(price_info.get("unit_price"))
    converted = _decimal(offer.get("converted_unit_price"))
    original = _decimal(offer.get("original_unit_price"))
    rate = _decimal(offer.get("conversion_rate"))
    currency = normalize_currency_code(price_info.get("unit_price_currency"))
    adjustments: List[Dict[str, Any]] = []
    formula = _decimal_text(base) if base is not None else ""

    premium_percent = _decimal(_first_value(agent_raw, [
        ["recommended_offer", "small_lot_and_distribution_premium_percent"],
        ["recommended_offer", "small_lot_premium_percent"],
        ["small_lot_and_distribution_premium_percent"],
        ["small_lot_premium_percent"],
    ]))
    if (
        base is not None
        and converted is not None
        and premium_percent is not None
        and converted != base
    ):
        premium_amount = converted * premium_percent / Decimal("100")
        reconstructed = converted + premium_amount
        adjustments.append({
            "name": "small_lot_and_distribution_premium",
            "type": "percentage",
            "percent": _decimal_float(premium_percent),
            "amount": _decimal_float(premium_amount),
            "currency": currency,
            "included_in_base_unit_cost": True,
            "source": "recommended_offer.small_lot_and_distribution_premium_percent",
        })
        formula = (
            f"{_decimal_text(converted)} + "
            f"({_decimal_text(converted)} x {_decimal_text(premium_percent)}%)"
        )
        base_reconciliation = base - reconstructed
    elif base is not None and original is not None and rate is not None and converted == base:
        formula = (
            f"{_decimal_text(original)} x {_decimal_text(rate)}"
        )
        base_reconciliation = base - (original * rate)
    elif base is not None and (price_info.get("fx") or {}).get("status") == "already_converted":
        formula = (
            f"{_decimal_text(original or Decimal('0'))} x "
            f"{_decimal_text(rate or Decimal('0'))}"
        )
        base_reconciliation = (
            base - (original * rate)
            if original is not None and rate is not None else Decimal("0")
        )
    else:
        base_reconciliation = Decimal("0")

    return {
        "base_unit_cost": _decimal_float(base),
        "currency": currency,
        "original_unit_price": _decimal_float(original),
        "original_currency": offer.get("original_currency") or offer.get("currency"),
        "converted_unit_price": _decimal_float(converted),
        "converted_currency": offer.get("converted_currency"),
        "conversion_rate": _decimal_float(rate),
        "conversion_rate_date": offer.get("conversion_rate_date"),
        "base_cost_formula": formula,
        "base_cost_adjustments": adjustments,
        "base_cost_reconciliation_difference": _decimal_float(base_reconciliation),
    }


def reconcile_delivered_unit_cost(
    agent_raw: Optional[Dict[str, Any]],
    target_currency: Any,
    fx_rates: Any = None,
) -> Dict[str, Any]:
    """Prove one delivered unit cost from its base and adjustment terms.

    A precomputed agent total is selected only when the backend reconstruction
    reconciles within one micro-unit of currency. Otherwise the result blocks.
    If no precomputed total exists, the backend may rebuild it solely from
    currency- and basis-compatible terms.
    """
    agent_raw = agent_raw or {}
    target = normalize_currency_code(target_currency)
    offer = resolve_component_offer(agent_raw)
    price_info = resolve_unit_price(agent_raw, target_currency=target)
    base_audit = _base_unit_cost_audit(agent_raw, price_info)
    base = _decimal(base_audit.get("base_unit_cost"))
    pricing_unit = offer.get("pricing_unit")
    delivered_value = _decimal(offer.get("delivered_cost_per_unit"))
    delivered_currency = _currency_from_monetary_unit(
        offer.get("delivered_cost_currency")
    )
    delivered_basis_text = offer.get("delivered_cost_basis") or pricing_unit
    delivered_basis = normalize_unit_basis(delivered_basis_text)
    offer_currency = normalize_currency_code(offer.get("currency"))

    reported = delivered_value
    reported_fx = None
    if reported is not None and delivered_currency and target and delivered_currency != target:
        reported_fx = convert_currency(
            float(reported), delivered_currency, target, rates=fx_rates,
        )
        if reported_fx.get("status") != "found":
            return {
                **base_audit,
                "status": "blocked",
                "reason": reported_fx.get("reason") or "exchange_rate_missing",
                "reported_delivered_unit_cost": _decimal_float(reported),
                "reported_delivered_currency": delivered_currency,
                "included_adders": [],
                "excluded_adders": [],
                "adjustments": base_audit["base_cost_adjustments"],
            }
        reported = _decimal(reported_fx["converted_amount"])
        delivered_currency = target

    included: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    adjustment_details: Dict[str, Any] = {}
    adjustment_total = Decimal("0")
    for name, spec in _DELIVERED_ADJUSTMENT_SPECS.items():
        value, container, field_name = _first_adjustment_field(
            agent_raw, spec["fields"],
        )
        if value is None:
            adjustment_details[name] = {"status": "absent"}
            continue
        metadata = _adjustment_currency_and_basis(
            agent_raw,
            container or [],
            field_name or name,
            spec["basis"],
            offer_currency,
            delivered_currency,
            delivered_basis_text,
        )
        currency = normalize_currency_code(metadata.get("currency"))
        basis = normalize_unit_basis(metadata.get("basis"))
        source = ".".join([*(container or []), field_name or name])
        term = {
            "name": name,
            "value": _decimal_float(value),
            "currency": currency,
            "pricing_unit": basis,
            "source": source,
            **metadata,
        }
        if not currency:
            term.update({"status": "excluded", "reason": "currency_missing"})
            excluded.append(term)
            adjustment_details[name] = term
            continue
        if basis != pricing_unit:
            term.update({
                "status": "excluded",
                "reason": "pricing_unit_mismatch",
                "expected_pricing_unit": pricing_unit,
            })
            excluded.append(term)
            adjustment_details[name] = term
            continue
        converted_value = value
        if target and currency != target:
            converted = convert_currency(
                float(value), currency, target, rates=fx_rates,
            )
            if converted.get("status") != "found":
                term.update({
                    "status": "excluded",
                    "reason": converted.get("reason") or "exchange_rate_missing",
                })
                excluded.append(term)
                adjustment_details[name] = term
                continue
            converted_value = _decimal(converted["converted_amount"])
            term["fx"] = converted
        term.update({
            "status": "included",
            "converted_value": _decimal_float(converted_value),
            "converted_currency": target or currency,
        })
        included.append(term)
        adjustment_details[name] = term
        adjustment_total += converted_value

    if base is None:
        return {
            **base_audit,
            "status": "blocked",
            "reason": "unit_price_missing",
            "included_adders": included,
            "excluded_adders": excluded,
            "adjustments": [
                *base_audit["base_cost_adjustments"],
                *included,
            ],
            "adjustment_details": adjustment_details,
        }

    calculated = base + adjustment_total
    difference = reported - calculated if reported is not None else Decimal("0")
    reconciled = (
        reported is None
        or abs(difference) <= DELIVERED_COST_RECONCILIATION_TOLERANCE
    )
    selected = reported if reported is not None and reconciled else calculated
    formula_terms = [_decimal_text(base)] + [
        _decimal_text(_decimal(item["converted_value"]) or Decimal("0"))
        for item in included
    ]
    formula = " + ".join(formula_terms) + f" = {_decimal_text(calculated)}"
    status = "calculated" if reconciled and not excluded else "blocked"
    reason = None
    if not reconciled:
        reason = "delivered_cost_reconciliation_mismatch"
    elif excluded:
        reason = "delivered_cost_adjustments_unresolved"

    return {
        **base_audit,
        "status": status,
        "reason": reason,
        "pricing_unit": pricing_unit,
        "delivered_cost_currency": target or delivered_currency or offer_currency,
        "reported_delivered_unit_cost": _decimal_float(reported),
        "calculated_delivered_unit_cost": _decimal_float(calculated),
        "reported_delivered_unit_cost_exact": (
            _decimal_text(reported) if reported is not None else None
        ),
        "calculated_delivered_unit_cost_exact": _decimal_text(calculated),
        "delivered_cost_per_pricing_unit": (
            _decimal_float(selected) if status == "calculated" else None
        ),
        "reconciliation_difference": _decimal_float(difference),
        "reconciliation_difference_exact": _decimal_text(difference),
        "reconciliation_tolerance": _decimal_float(
            DELIVERED_COST_RECONCILIATION_TOLERANCE
        ),
        "delivered_cost_formula": formula,
        "calculation_source": (
            "agent_precomputed_delivered_cost_reconciled"
            if reported is not None and reconciled
            else "backend_rebuilt_from_base_and_adders"
        ),
        "reported_delivered_cost_used": bool(reported is not None and reconciled),
        "backend_recalculated_for_audit": True,
        "included_adders": included,
        "excluded_adders": excluded,
        "adjustments": [
            *base_audit["base_cost_adjustments"],
            *included,
        ],
        "adjustment_details": adjustment_details,
        "rounding_policy": {
            "decimal_context_precision": getcontext().prec,
            "intermediate_decimal_places": MONEY_INTERMEDIATE_PRECISION,
            "displayed_unit_cost_decimal_places": MONEY_UNIT_DISPLAY_DECIMALS,
            "final_per_product_decimal_places": MONEY_PER_PRODUCT_DISPLAY_DECIMALS,
            "intermediate_rounding": "none",
        },
        "fx": reported_fx,
    }


def resolve_delivered_unit_cost(
    agent_raw: Optional[Dict[str, Any]],
    target_currency: Any,
    fx_rates: Any = None,
) -> Dict[str, Any]:
    return reconcile_delivered_unit_cost(
        agent_raw, target_currency, fx_rates=fx_rates,
    )


def component_offer_requires_regeneration(agent_raw: Optional[Dict[str, Any]]) -> bool:
    offer = resolve_component_offer(agent_raw)
    return bool(
        offer.get("unit_price") is not None
        and (not offer.get("currency") or not offer.get("pricing_unit"))
    )


_BASIS_UNIT_ALIASES = {
    "kg": "kg", "cny/kg": "kg", "rmb/kg": "kg", "eur/kg": "kg", "usd/kg": "kg", "per_kg": "kg",
    "pc": "pc", "pcs": "pc", "piece": "pc", "cny/pc": "pc", "rmb/pc": "pc", "eur/pc": "pc",
    "usd/pc": "pc", "per_piece": "pc", "u": "pc", "cny/pce": "pc", "rmb/pce": "pc",
    "m": "m", "meter": "m", "metre": "m", "cny/m": "m", "rmb/m": "m", "eur/m": "m", "usd/m": "m", "per_m": "m",
    "g": "g", "cny/g": "g", "rmb/g": "g", "eur/g": "g", "usd/g": "g", "per_g": "g",
    "mm": "mm", "cny/mm": "mm", "rmb/mm": "mm", "eur/mm": "mm", "usd/mm": "mm", "per_mm": "mm",
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
    if "/kg" in text or text.endswith("kg"):
        return "kg"
    if "/g" in text or text.endswith(" g"):
        return "g"
    if "/mm" in text or text.endswith(" mm"):
        return "mm"
    if "/m" in text or text.endswith(" m") or text.endswith("/meter") or text.endswith("/metre"):
        return "m"
    if "/pc" in text or "piece" in text or text.endswith("/u"):
        return "pc"
    if "%" in text or "percent" in text:
        return "percentage"
    if "shipment" in text:
        return "shipment"
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
    currency = normalize_currency_code(price_info.get("unit_price_currency"))
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

    quantity_decimal = _decimal(pricing_quantity)
    price_decimal = _decimal(unit_price)
    material_cost_per_product = quantity_decimal * price_decimal
    return {
        "status": "calculated",
        "component_id": component_id,
        "pricing_quantity": pricing_quantity,
        "pricing_unit": pricing_unit,
        "unit_price": unit_price,
        "unit_price_currency": currency,
        "material_cost_per_product": _decimal_float(material_cost_per_product),
        "currency": currency,
        "calculation_precision": "decimal_28",
    }


def convert_component_cost_to_project_currency(
    component_id: str,
    material_result: Dict[str, Any],
    project_currency: Any,
    fx_rates: Any = None,
) -> Dict[str, Any]:
    """Convert one resolved component line before cross-currency aggregation."""
    if material_result.get("status") != "calculated":
        return material_result
    converted = convert_currency(
        material_result.get("material_cost_per_product"),
        material_result.get("currency"),
        project_currency,
        rates=fx_rates,
    )
    if converted.get("status") != "found":
        return {
            **material_result,
            "status": "blocked",
            "reason": converted.get("reason") or "exchange_rate_missing",
            "fx": converted,
            "material_cost_per_product_project_currency": None,
        }
    return {
        **material_result,
        "source_currency": converted["source_currency"],
        "currency": converted["destination_currency"],
        "material_cost_per_product_source_currency": material_result.get("material_cost_per_product"),
        "material_cost_per_product": converted["converted_amount"],
        "converted_to_project_currency": converted["rate"] != 1,
        "fx": converted,
    }


# ---------------------------------------------------------------------------
# Logistics (transport + customs + forwarder) — Olivier's component rule
# ---------------------------------------------------------------------------

_LOGISTICS_FIELD_NAMES = {
    "transport": ["transportation_cost", "transport_cost", "transportation_cost_per_piece"],
    "customs": ["custom_duty_cost", "customs_duty_cost", "customs_cost", "duty_cost", "customs_cost_per_piece"],
    "forwarder": ["forwarder_fee", "forwarder_cost", "forwarding_cost", "forwarder_cost_per_piece"],
}

_LOGISTICS_BASIS_ALIASES = {
    "transportation_cost": ("transportation_cost_basis", "transport_basis"),
    "transport_cost": ("transport_basis", "transport_cost_basis"),
    "transportation_cost_per_piece": ("transportation_cost_per_piece_basis", "transport_basis"),
    "custom_duty_cost": ("custom_duty_cost_basis", "customs_basis"),
    "customs_duty_cost": ("customs_duty_cost_basis", "customs_basis"),
    "customs_cost": ("customs_basis", "customs_cost_basis"),
    "duty_cost": ("duty_cost_basis", "customs_basis"),
    "customs_cost_per_piece": ("customs_cost_per_piece_basis", "customs_basis"),
    "forwarder_fee": ("forwarder_basis", "forwarder_fee_basis"),
    "forwarder_cost": ("forwarder_basis", "forwarder_cost_basis"),
    "forwarding_cost": ("forwarder_basis", "forwarding_cost_basis"),
    "forwarder_cost_per_piece": ("forwarder_cost_per_piece_basis", "forwarder_basis"),
}


def _resolve_logistics_value_legacy(agent_raw: Optional[Dict[str, Any]], field_names: List[str]) -> Dict[str, Any]:
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


def resolve_logistics_value(agent_raw: Optional[Dict[str, Any]], field_names: List[str]) -> Dict[str, Any]:
    """Resolve logistics without applying a plant-currency fallback.

    Only a structured logistics object inside recommended_offer may inherit
    that containing offer's currency, as defined by the new output contract.
    Legacy scalar fields require their own sibling currency.
    """
    agent_raw = agent_raw or {}
    offer = agent_raw.get("recommended_offer") if isinstance(agent_raw.get("recommended_offer"), dict) else {}
    structured_aliases = {
        "transportation_cost": "transport",
        "transport_cost": "transport",
        "transportation_cost_per_piece": "transport",
        "custom_duty_cost": "customs",
        "customs_duty_cost": "customs",
        "duty_cost": "customs",
        "customs_cost_per_piece": "customs",
        "forwarder_cost": "forwarder_fee",
        "forwarding_cost": "forwarder_fee",
        "forwarder_cost_per_piece": "forwarder_fee",
    }
    for field_name in field_names:
        structured_name = structured_aliases.get(field_name)
        candidate = offer.get(structured_name) if structured_name else None
        if isinstance(candidate, dict):
            value = _coerce_number(candidate.get("value"))
            if value is not None:
                explicit_currency = normalize_currency_code(candidate.get("currency"))
                inherited_currency = normalize_currency_code(offer.get("currency")) if not explicit_currency else None
                return {
                    "value": value,
                    "currency": explicit_currency or inherited_currency,
                    "rate_basis": candidate.get("rate_basis") or candidate.get("unit"),
                    "currency_inherited_from_offer": bool(inherited_currency),
                }

    for field_name in field_names:
        for container in (["recommended_offer", "supply_chain"], ["recommended_offer"], ["supply_chain"], ["normalized_cost"], []):
            candidate = _get_path(agent_raw, container + [field_name])
            if candidate in (None, "") or isinstance(candidate, dict):
                continue
            value = _coerce_number(candidate)
            if value is None:
                continue
            basis_names = _LOGISTICS_BASIS_ALIASES.get(
                field_name, (f"{field_name}_basis", f"{field_name}_unit"),
            )
            rate_basis = next((
                _get_path(agent_raw, container + [basis_name])
                for basis_name in basis_names
                if _get_path(agent_raw, container + [basis_name]) not in (None, "")
            ), None)
            explicit_currency = normalize_currency_code(
                _get_path(agent_raw, container + [f"{field_name}_currency"]),
            )
            if not explicit_currency:
                explicit_currency = _currency_from_monetary_unit(
                    _get_path(agent_raw, container + [f"{field_name}_currency"])
                )
            if not explicit_currency and container == ["recommended_offer", "supply_chain"]:
                explicit_currency = _currency_from_monetary_unit(
                    offer.get(f"{field_name}_currency")
                )
            inherited_currency = None
            if container in (["recommended_offer"], ["recommended_offer", "supply_chain"]) and rate_basis:
                inherited_currency = normalize_currency_code(offer.get("currency"))
            return {
                "value": value,
                "currency": explicit_currency or inherited_currency,
                "rate_basis": rate_basis,
                "currency_inherited_from_offer": bool(inherited_currency and not explicit_currency),
            }
    return {"value": None, "currency": None, "rate_basis": None, "currency_inherited_from_offer": False}


def build_canonical_component_costing(
    component_id: str,
    material_family: Optional[str],
    bom_fields: Dict[str, Any],
    agent_raw: Optional[Dict[str, Any]],
    target_currency: Any = None,
    fx_rates: Any = None,
) -> Dict[str, Any]:
    """Build the auditable per-product pricing contract used after write-back."""
    bom_fields = bom_fields or {}
    agent_raw = agent_raw or {}
    offer = resolve_component_offer(agent_raw)
    quantity = resolve_component_pricing_quantity(
        component_id, material_family, bom_fields, agent_raw,
    )
    price_info = resolve_unit_price(agent_raw, target_currency=target_currency)
    material = compute_component_material_cost(
        component_id,
        quantity,
        {
            "unit_price": price_info.get("unit_price"),
            "unit_price_currency": price_info.get("unit_price_currency"),
            "unit_price_basis": offer.get("pricing_basis") or offer.get("pricing_unit"),
        },
    )
    if (
        material.get("status") == "calculated"
        and target_currency
        and (price_info.get("fx") or {}).get("status") != "already_converted"
    ):
        material = convert_component_cost_to_project_currency(
            component_id, material, target_currency, fx_rates=fx_rates,
        )
    elif (price_info.get("fx") or {}).get("status") == "already_converted":
        material["currency"] = normalize_currency_code(target_currency)
        material["fx"] = price_info["fx"]

    source_value = bom_fields.get("quantity_per_product")
    source_unit = bom_fields.get("quantity_unit")
    conversion = None
    basis = quantity.get("pricing_quantity_basis")
    family_text = f"{material_family or ''} {component_id or ''}".lower()
    if any(marker in family_text for marker in _WIRE_FAMILY_MARKERS) and source_unit in {"m", "mm"} and quantity.get("pricing_unit") == "kg":
        conversion = {
            "method": "wire_length_diameter_density_to_mass",
            "diameter_mm": bom_fields.get("diameter_mm"),
            "density_g_cm3": 8.96,
        }
    elif source_unit == "g" and quantity.get("pricing_unit") == "kg":
        conversion = {"method": "grams_to_kilograms", "factor": 0.001}
    elif source_unit == quantity.get("pricing_unit"):
        conversion = {"method": "none_required"}
    elif basis == "estimated_bom_mass":
        conversion = {
            "method": "estimated_bom_mass_grams_to_kilograms",
            "source_mass_g": _coerce_number(
                _get_path(agent_raw, ["technical_specification", "estimated_mass_per_piece_g"])
            ),
            "factor": 0.001,
        }
    elif basis == "indicative_conditional":
        conversion = {
            "method": "grams_to_kilograms",
            "source_mass_g": _coerce_number(
                _get_path(agent_raw, ["technical_specification", "indicative_tin_mass_per_product_g"])
            ),
            "factor": 0.001,
            "conditional": True,
        }

    return {
        "component_id": component_id,
        "technical_quantity": quantity.get("pricing_quantity"),
        "technical_quantity_unit": (
            f"{quantity['pricing_unit']}/product" if quantity.get("pricing_unit") else None
        ),
        "unit_price": price_info.get("unit_price"),
        "pricing_unit": offer.get("pricing_unit"),
        "currency": material.get("currency") or price_info.get("unit_price_currency"),
        "material_cost_per_piece": material.get("material_cost_per_product"),
        "source_quantity": {"value": source_value, "unit": f"{source_unit}/product" if source_unit else None},
        "conversion": conversion,
        "pricing_quantity_basis": basis,
        "fx": price_info.get("fx") or material.get("fx"),
        "original_unit_price": offer.get("unit_price"),
        "original_currency": offer.get("currency"),
        "converted_unit_price": (
            price_info.get("unit_price")
            if (price_info.get("fx") or {}).get("status") == "already_converted"
            else None
        ),
        "warnings": quantity.get("warnings") or [],
        "status": material.get("status"),
        "blocking_reason": material.get("reason"),
    }


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
    target_currency: Any = None,
    fx_rates: Any = None,
    exclude_invalid: bool = False,
) -> Dict[str, Any]:
    """Olivier's rule: transport + customs duty + forwarder fee, each
    converted to the same per-product basis as the BOM pricing quantity,
    then summed. A field that is genuinely absent contributes 0 and does not
    block; a field that is present but whose unit can't be reconciled with
    the BOM quantity blocks with a structured error."""
    agent_raw = agent_raw or {}
    breakdown: Dict[str, Any] = {}
    excluded_adders: List[Dict[str, Any]] = []
    total = 0.0

    for label, names in _LOGISTICS_FIELD_NAMES.items():
        resolved = resolve_logistics_value(agent_raw, names)
        value = resolved["value"]
        if value is None:
            breakdown[label] = {"value": 0.0, "currency": None, "rate_basis": None, "converted_value": 0.0}
            continue
        if not resolved["currency"]:
            if exclude_invalid:
                excluded_adders.append({
                    "field": label,
                    "value": value,
                    "reason": "currency_missing",
                })
                breakdown[label] = {
                    **resolved,
                    "status": "excluded",
                    "reason": "currency_missing",
                    "converted_value": None,
                }
                continue
            return {
                "status": "blocked",
                "component_id": component_id,
                "reason": "currency_missing",
                "field": label,
            }
        basis_kind = normalize_unit_basis(resolved["rate_basis"])
        if basis_kind is None:
            if exclude_invalid:
                excluded_adders.append({
                    "field": label,
                    "value": value,
                    "currency": resolved.get("currency"),
                    "reason": "logistics_rate_basis_unknown",
                })
                breakdown[label] = {
                    **resolved,
                    "status": "excluded",
                    "reason": "logistics_rate_basis_unknown",
                    "converted_value": None,
                }
                continue
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
            if exclude_invalid:
                excluded_adders.append({
                    "field": label,
                    "value": value,
                    "currency": resolved.get("currency"),
                    "rate_basis": resolved.get("rate_basis"),
                    "reason": error,
                })
                breakdown[label] = {
                    **resolved,
                    "status": "excluded",
                    "reason": error,
                    "converted_value": None,
                }
                continue
            return {
                "status": "blocked",
                "component_id": component_id,
                "reason": error,
                "field": label,
                "physical_quantity": pricing_quantity_info.get("pricing_quantity"),
                "physical_unit": pricing_quantity_info.get("pricing_unit"),
                "pricing_unit": resolved["rate_basis"],
            }
        converted_currency = normalize_currency_code(resolved["currency"])
        fx = None
        if target_currency:
            fx = convert_currency(converted, converted_currency, target_currency, rates=fx_rates)
            if fx.get("status") != "found":
                if exclude_invalid:
                    excluded_adders.append({
                        "field": label,
                        "value": value,
                        "currency": resolved.get("currency"),
                        "rate_basis": resolved.get("rate_basis"),
                        "reason": fx.get("reason") or "exchange_rate_missing",
                    })
                    breakdown[label] = {
                        **resolved,
                        "status": "excluded",
                        "reason": fx.get("reason") or "exchange_rate_missing",
                        "converted_value": None,
                        "fx": fx,
                    }
                    continue
                return {
                    "status": "blocked",
                    "component_id": component_id,
                    "reason": fx.get("reason") or "exchange_rate_missing",
                    "field": label,
                    "fx": fx,
                }
            converted = fx["converted_amount"]
            converted_currency = fx["destination_currency"]
        breakdown[label] = {
            "value": value,
            "currency": normalize_currency_code(resolved["currency"]),
            "rate_basis": resolved["rate_basis"],
            "converted_value": converted,
            "converted_currency": converted_currency,
            "currency_inherited_from_offer": resolved.get("currency_inherited_from_offer", False),
            "fx": fx,
        }
        total += converted

    return {
        "status": "calculated_with_exclusions" if excluded_adders else "calculated",
        "component_id": component_id,
        "transport_cost_per_product": total,
        "logistics_breakdown": breakdown,
        "excluded_adders": excluded_adders,
    }
