"""Deterministic annual financial model for a completed Choke technical costing.

This module has no filesystem, database, or agent side effects. Callers supply
normalized technical and commercial inputs and receive a reproducible Y-1..Y6
financial plan.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional


CALCULATION_VERSION = "avocarbon-choke-financial-v1"
ZERO = Decimal("0")
ONE = Decimal("1")
DAYS_PER_YEAR = Decimal("365")
MONEY_QUANTUM = Decimal("0.000001")
PER_UNIT_QUANTUM = Decimal("0.000000001")
PERIODS = ["Y-1", "Y0", "Y1", "Y2", "Y3", "Y4", "Y5", "Y6"]


def _d(value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if value in (None, "") or isinstance(value, bool):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _rate(value: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    number = _d(value, default)
    if number is None:
        return None
    return number / Decimal("100") if abs(number) > ONE else number


def _number(value: Optional[Decimal], quantum: Decimal = MONEY_QUANTUM) -> Optional[float]:
    if value is None:
        return None
    return float(value.quantize(quantum))


def _exact(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(str(item) for item in values if item))


def _json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _period_value(values: Any, period: str, default: Any = None) -> Any:
    if isinstance(values, Mapping):
        return values.get(period, default)
    return default


def _annual_rate(values: Any, period: str) -> Decimal:
    value = _period_value(values, period, ZERO)
    return _rate(value, ZERO) or ZERO


def _technical_costs(technical: Mapping[str, Any]) -> Dict[str, Decimal]:
    base_material = _d(
        technical.get("base_material_cost_per_piece"),
        _d(technical.get("material_cost_per_piece"), ZERO),
    ) or ZERO
    logistics = _d(
        technical.get("logistics_cost_per_piece"),
        _d(technical.get("transport_cost_per_piece"), ZERO),
    ) or ZERO
    delivered = _d(
        technical.get("delivered_material_cost_per_piece"),
        base_material + logistics,
    ) or base_material + logistics
    dl = _d(technical.get("dl_cost_per_piece"), ZERO) or ZERO
    voh = _d(technical.get("voh_cost_per_piece"), ZERO) or ZERO
    direct = dl + voh + logistics
    foh_rate = _rate(technical.get("foh_percent_dc"), ZERO) or ZERO
    fee_rate = _rate(technical.get("fee_percent_dc"), ZERO) or ZERO
    foh = _d(technical.get("foh_cost_per_piece"), direct * foh_rate) or ZERO
    fee = _d(technical.get("fee_cost_per_piece"), direct * fee_rate) or ZERO
    return {
        "base_material": base_material,
        "logistics": logistics,
        "delivered_material": delivered,
        "dl": dl,
        "voh": voh,
        "added_value_direct": direct,
        "foh": foh,
        "fee": fee,
        "manufacturing_added_value": direct + foh + fee,
        "total_before_commercial": base_material + direct + foh + fee,
    }


def _quantity_profile(commercial: Mapping[str, Any], sop_year: int) -> tuple[Dict[str, Decimal], List[str], List[str]]:
    missing: List[str] = []
    assumptions: List[str] = []
    explicit = commercial.get("annual_quantities")
    rule = str(commercial.get("quantity_rule") or "").strip().lower()
    quantities: Dict[str, Decimal] = {}
    if isinstance(explicit, Mapping):
        for period in PERIODS:
            value = _d(explicit.get(period))
            if value is not None:
                quantities[period] = value
    if rule == "flat":
        flat = _d(commercial.get("flat_annual_quantity"))
        if flat is None:
            missing.append("flat_annual_quantity")
        else:
            for period in PERIODS[1:]:
                quantities.setdefault(period, flat)
            assumptions.append("Flat annual quantity profile explicitly selected.")
    elif rule == "ramp":
        base = _d(commercial.get("flat_annual_quantity"))
        ramp = commercial.get("ramp_profile")
        if base is None or not isinstance(ramp, Mapping):
            missing.append("flat_annual_quantity/ramp_profile")
        else:
            for period in PERIODS[1:]:
                factor = _d(ramp.get(period))
                if factor is not None:
                    quantities.setdefault(period, base * factor)

    if "Y-1" not in quantities:
        if commercial.get("y_minus_1_quantity_zero") is True:
            quantities["Y-1"] = ZERO
            assumptions.append("Y-1 production quantity explicitly configured as zero.")
        else:
            missing.append("annual_quantities.Y-1 or y_minus_1_quantity_zero")
    for period in PERIODS[1:]:
        if period not in quantities:
            missing.append(f"annual_quantities.{period}")
    return quantities, missing, assumptions


def build_year_structure(sop_year: int) -> List[Dict[str, Any]]:
    return [
        {"period": period, "calendar_year": sop_year + index - 1}
        for index, period in enumerate(PERIODS)
    ]


def _component_rows(
    technical: Mapping[str, Any],
    commercial: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    overrides = commercial.get("supplier_terms") or {}
    rows = []
    for item in technical.get("component_breakdown") or []:
        component_id = str(item.get("component_id") or "").strip()
        if not component_id or item.get("status") != "resolved":
            continue
        override = overrides.get(component_id) if isinstance(overrides, Mapping) else {}
        override = override if isinstance(override, Mapping) else {}
        offer = item.get("normalized_offer") or {}
        base_per_product = _d(item.get("material_cost_per_piece"))
        delivered_per_product = _d(item.get("delivered_material_cost_per_piece"))
        rows.append({
            "component_id": component_id,
            "supplier": override.get("supplier") or offer.get("supplier_name"),
            "currency": item.get("currency") or offer.get("currency"),
            "base_cost_per_product": base_per_product,
            "delivered_cost_per_product": delivered_per_product,
            "payment_days": _d(override.get("payment_days"), _d(offer.get("payment_days"))),
            "incoterm": str(override.get("incoterm") or offer.get("incoterm") or "").upper(),
            "zone_relation": str(override.get("zone_relation") or "").lower(),
            "source": override.get("source") or "component_output",
        })
    return rows


def financial_readiness(
    technical_result: Mapping[str, Any],
    commercial: Mapping[str, Any],
    unit_data: Optional[Mapping[str, Any]] = None,
    component_rows: Optional[List[Mapping[str, Any]]] = None,
    investment_assets: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    mode = str(commercial.get("mode") or "firm").lower()
    missing: List[str] = []
    warnings: List[str] = []
    unit_data = unit_data or {}
    components = list(component_rows or _component_rows(technical_result, commercial))

    sop_year = commercial.get("sop_year")
    if sop_year in (None, "") and commercial.get("sop_date"):
        try:
            sop_year = int(str(commercial["sop_date"])[:4])
        except (ValueError, TypeError):
            sop_year = None
    if _d(sop_year) is None:
        missing.append("sop_year")
    else:
        _, quantity_missing, _ = _quantity_profile(commercial, int(sop_year))
        missing.extend(quantity_missing)

    if _d(commercial.get("initial_selling_price")) is None and commercial.get("solve_selling_price") is not True:
        missing.append("initial_selling_price or solve_selling_price=true")
    productivity = commercial.get("customer_productivity")
    if not isinstance(productivity, Mapping):
        missing.append("customer_productivity")
    else:
        for field in ("percentage", "start_year", "duration", "basis"):
            if productivity.get(field) in (None, ""):
                missing.append(f"customer_productivity.{field}")
    for field in (
        "customer_payment_days",
        "customer_incoterm",
        "customer_delivery_frequency_days",
        "platform",
        "discount_rate",
        "profitability_target",
        "capex_tooling_treatment",
        "ap_value_basis",
        "wip_value_basis",
    ):
        if field not in commercial or commercial.get(field) in (None, ""):
            missing.append(field)
    for explicit_zero_map in (
        "material_indexation_rates",
        "plant_indexation_rates",
        "fx_adjustment_rates",
    ):
        if explicit_zero_map not in commercial:
            missing.append(explicit_zero_map)

    if not commercial.get("production_plant"):
        missing.append("production_plant")
    tax_rate = commercial.get("tax_rate", unit_data.get("company_tax_rate"))
    if tax_rate in (None, ""):
        missing.append("tax_rate")

    for component in components:
        cid = component.get("component_id")
        if component.get("payment_days") is None:
            missing.append(f"supplier_payment_days.{cid}")
        if not component.get("incoterm"):
            missing.append(f"supplier_incoterm.{cid}")
        if component.get("zone_relation") not in {"same", "different"}:
            missing.append(f"supplier_zone_relation.{cid}")

    treatment = commercial.get("capex_tooling_treatment")
    if isinstance(treatment, Mapping):
        currencies = {
            str(asset.get("currency") or "").upper()
            for asset in (investment_assets or [])
            if _d(asset.get("amount")) not in (None, ZERO)
        }
        reporting_currency = str(technical_result.get("currency") or "").upper()
        fx_rates = commercial.get("investment_fx_rates") or {}
        for currency in currencies:
            if currency and currency != reporting_currency and currency not in fx_rates:
                missing.append(f"investment_fx_rates.{currency}")

    unresolved = technical_result.get("unresolved_material_components") or []
    if unresolved:
        labels = [str(item.get("component_id") or item) for item in unresolved]
        if mode == "firm":
            missing.extend(f"unresolved_component.{item}" for item in labels)
        else:
            warnings.append(
                "Preliminary financial model - unresolved component costs excluded: "
                + ", ".join(labels)
            )

    missing = _unique(missing)
    structural = [item for item in missing if not item.startswith("unresolved_component.")]
    if structural or (mode == "firm" and unresolved):
        status = "blocked"
    elif unresolved:
        status = "preliminary_incomplete"
    else:
        status = "ready"
    return {
        "financial_status": status,
        "mode": mode,
        "missing_inputs": missing,
        "warnings": warnings,
        "commercially_usable": status == "ready" and mode == "firm",
    }


def _productivity_base(
    basis: str,
    opening_price: Decimal,
    costs: Mapping[str, Decimal],
    custom: Optional[Decimal],
) -> Optional[Decimal]:
    if basis == "full_price":
        return opening_price
    if basis == "added_value":
        return costs["manufacturing_added_value"]
    if basis == "added_value_plus_non_indexed_material":
        return costs["manufacturing_added_value"] + costs["base_material"]
    if basis == "custom":
        return custom
    return None


def _stock_days(component: Mapping[str, Any], commercial: Mapping[str, Any]) -> Dict[str, Decimal]:
    same_zone = component.get("zone_relation") == "same"
    default_frequency = Decimal("7") if same_zone else Decimal("30")
    lead_time = Decimal("7") if same_zone else Decimal("40")
    overrides = (commercial.get("supplier_stock_overrides") or {}).get(component.get("component_id"), {})
    if isinstance(overrides, Mapping):
        frequency = _d(overrides.get("delivery_frequency_days"), default_frequency) or default_frequency
        lead_time = _d(overrides.get("lead_time_days"), lead_time) or lead_time
    else:
        frequency = default_frequency
    safety = Decimal("0.2") * lead_time + Decimal("0.2") * frequency
    cycle = Decimal("2") / Decimal("3") * frequency
    incoterm = component.get("incoterm") or ""
    transit = lead_time if incoterm in {"FCA", "EXW", "FOB"} else ZERO
    return {
        "rm_transit": transit,
        "rm_in_house": safety + cycle,
        "lead_time": lead_time,
        "frequency": frequency,
    }


def _convert_assets(
    assets: List[Mapping[str, Any]],
    commercial: Mapping[str, Any],
    reporting_currency: str,
) -> List[Dict[str, Any]]:
    fx_rates = commercial.get("investment_fx_rates") or {}
    converted = []
    seen = set()
    for asset in assets:
        source_id = str(asset.get("source_id") or asset.get("work_package_id") or "")
        category = str(asset.get("category") or "")
        dedupe = (source_id, category)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        amount = _d(asset.get("amount"))
        currency = str(asset.get("currency") or reporting_currency).upper()
        if amount in (None, ZERO):
            continue
        rate = ONE if currency == reporting_currency else _d(fx_rates.get(currency))
        converted.append({
            **asset,
            "source_id": source_id,
            "category": category,
            "amount_decimal": amount,
            "currency": currency,
            "fx_rate": rate,
            "converted_amount": amount * rate if rate is not None else None,
        })
    return converted


def _investment_schedule(
    assets: List[Mapping[str, Any]],
    commercial: Mapping[str, Any],
    reporting_currency: str,
) -> Dict[str, Any]:
    converted = _convert_assets(assets, commercial, reporting_currency)
    treatment = commercial.get("capex_tooling_treatment") or {}
    depreciation_years = int(_d(commercial.get("depreciation_years"), Decimal("5")) or 5)
    schedule = {
        period: {
            "generic_capex": ZERO,
            "specific_capex": ZERO,
            "tooling_expenditure": ZERO,
            "customer_collections": ZERO,
            "depreciation": ZERO,
        }
        for period in PERIODS
    }
    details = []
    for asset in converted:
        amount = asset.get("converted_amount")
        if amount is None:
            continue
        category = asset["category"]
        configured = treatment.get(category) if isinstance(treatment, Mapping) else treatment
        if isinstance(configured, str):
            configured = {"type": configured}
        configured = configured if isinstance(configured, Mapping) else {}
        treatment_type = str(configured.get("type") or "").lower()
        if category == "generic_capex":
            schedule["Y-1"]["generic_capex"] += amount
        elif category == "specific_capex":
            schedule["Y-1"]["specific_capex"] += amount
        elif category == "tooling":
            schedule["Y-1"]["tooling_expenditure"] += amount

        collection_percent = _rate(configured.get("customer_collection_percent"), ZERO) or ZERO
        if treatment_type in {"cash", "prepaid", "customer_owned"} and "customer_collection_percent" not in configured:
            collection_percent = ONE
        if collection_percent:
            schedule["Y-1"]["customer_collections"] += amount * collection_percent

        depreciable = treatment_type in {"avocarbon_owned", "depreciation", "amortization", "mixed"}
        if category == "generic_capex" and not treatment_type:
            depreciable = True
        depreciable_percent = _rate(configured.get("depreciable_percent"), ONE if depreciable else ZERO) or ZERO
        depreciable_basis = amount * depreciable_percent
        if depreciable_basis:
            annual = depreciable_basis / Decimal(depreciation_years)
            for period in PERIODS[1:1 + depreciation_years]:
                schedule[period]["depreciation"] += annual
        details.append({
            "source_id": asset["source_id"],
            "category": category,
            "estimated": asset.get("estimated", True),
            "confidence": asset.get("confidence"),
            "source_currency": asset["currency"],
            "source_amount": _number(asset["amount_decimal"]),
            "fx_rate": _number(asset["fx_rate"], PER_UNIT_QUANTUM),
            "converted_amount": _number(amount),
            "reporting_currency": reporting_currency,
            "treatment": treatment_type or "generic_capex_default_depreciation",
            "depreciable_basis": _number(depreciable_basis),
        })
    return {"schedule": schedule, "assets": details, "depreciation_years": depreciation_years}


def calculate_financial_plan(
    technical_result: Mapping[str, Any],
    commercial_inputs: Mapping[str, Any],
    unit_data: Optional[Mapping[str, Any]] = None,
    component_rows: Optional[List[Mapping[str, Any]]] = None,
    investment_assets: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculate an annual P&L/cash/TWC/NPV model from normalized inputs."""
    commercial = dict(commercial_inputs)
    unit_data = dict(unit_data or {})
    components = list(component_rows or _component_rows(technical_result, commercial))
    assets = list(investment_assets or [])
    readiness = financial_readiness(technical_result, commercial, unit_data, components, assets)
    base_result = {
        "calculation_version": CALCULATION_VERSION,
        "input_hash": _json_hash({
            "technical_result": technical_result,
            "commercial_inputs": commercial,
            "unit_data": unit_data,
            "component_rows": components,
            "investment_assets": assets,
        }),
        "calculated_at": datetime.now(timezone.utc).isoformat(),
        **readiness,
    }
    if readiness["financial_status"] == "blocked":
        return {**base_result, "annual_table": [], "npv": None}

    sop_year = int(_d(commercial.get("sop_year"), _d(str(commercial.get("sop_date"))[:4])) or 0)
    quantities, quantity_missing, quantity_assumptions = _quantity_profile(commercial, sop_year)
    if quantity_missing:
        return {
            **base_result,
            "financial_status": "blocked",
            "missing_inputs": _unique([*readiness["missing_inputs"], *quantity_missing]),
            "annual_table": [],
            "npv": None,
        }

    costs = _technical_costs(technical_result)
    reporting_currency = str(technical_result.get("currency") or commercial.get("currency") or "")
    investment = _investment_schedule(assets, commercial, reporting_currency)
    productivity = commercial["customer_productivity"]
    productivity_rate = _rate(productivity.get("percentage"), ZERO) or ZERO
    productivity_start = int(_d(productivity.get("start_year"), ONE) or 1)
    productivity_duration = int(_d(productivity.get("duration"), ZERO) or 0)
    productivity_basis_name = str(productivity.get("basis") or "")
    custom_productivity_basis = _d(productivity.get("custom_basis_value"))
    customer_days = _d(commercial.get("customer_payment_days")) or ZERO
    delivery_frequency = _d(commercial.get("customer_delivery_frequency_days")) or ZERO
    platform = commercial.get("platform") is True
    fg_safety_days = _d(commercial.get("fg_safety_stock_days"), Decimal("10")) or Decimal("10")
    platform_safety = _d(commercial.get("platform_safety_stock_days"), ZERO) or ZERO
    customer_transit = _d(commercial.get("customer_transit_days"), ZERO) or ZERO
    wip_days = _d(commercial.get("wip_days"), Decimal("5")) or Decimal("5")
    ap_basis = str(commercial.get("ap_value_basis"))
    wip_basis = str(commercial.get("wip_value_basis"))
    customer_incoterm = str(commercial.get("customer_incoterm") or "").upper()
    discount_rate = _rate(commercial.get("discount_rate")) or ZERO
    financing_rate = _rate(commercial.get("financing_rate"), Decimal("0.08")) or Decimal("0.08")
    tax_rate = _rate(commercial.get("tax_rate", unit_data.get("company_tax_rate"))) or ZERO
    business_links = commercial.get("business_link_values") or {}

    initial_price = _d(commercial.get("initial_selling_price"), ZERO) or ZERO
    rows: List[Dict[str, Any]] = []
    prior_price = ZERO
    prior_twc = ZERO
    cumulative_material_factor = ONE
    cumulative_plant_factor = ONE
    cumulative_logistics_factor = ONE
    cumulative_discounted = ZERO

    for index, year_info in enumerate(build_year_structure(sop_year)):
        period = year_info["period"]
        quantity = quantities[period]
        if period == "Y-1":
            opening_price = ZERO
            final_price = ZERO
            productivity_amount = ZERO
            material_adjustment = ZERO
            plant_adjustment = ZERO
            fx_adjustment = ZERO
        else:
            material_rate = _annual_rate(commercial.get("material_indexation_rates"), period)
            plant_rate = _annual_rate(commercial.get("plant_indexation_rates"), period)
            logistics_rate = _annual_rate(commercial.get("logistics_indexation_rates"), period)
            fx_rate = _annual_rate(commercial.get("fx_adjustment_rates"), period)
            previous_material_factor = cumulative_material_factor
            previous_plant_factor = cumulative_plant_factor
            cumulative_material_factor *= ONE + material_rate
            cumulative_plant_factor *= ONE + plant_rate
            cumulative_logistics_factor *= ONE + logistics_rate
            opening_price = initial_price if period == "Y0" else prior_price
            year_number = index - 1
            applies = productivity_start <= year_number < productivity_start + productivity_duration
            prod_base = _productivity_base(
                productivity_basis_name,
                opening_price,
                costs,
                custom_productivity_basis,
            ) or ZERO
            productivity_amount = prod_base * productivity_rate if applies else ZERO
            material_adjustment = costs["base_material"] * previous_material_factor * material_rate
            plant_adjustment = costs["manufacturing_added_value"] * previous_plant_factor * plant_rate
            fx_adjustment = opening_price * fx_rate
            final_price = opening_price - productivity_amount + material_adjustment + plant_adjustment + fx_adjustment
            prior_price = final_price

        annual_base_material_pp = costs["base_material"] * cumulative_material_factor
        annual_logistics_pp = costs["logistics"] * cumulative_logistics_factor
        annual_dl_pp = costs["dl"] * cumulative_plant_factor
        annual_voh_pp = costs["voh"] * cumulative_plant_factor
        annual_foh_pp = costs["foh"] * cumulative_plant_factor
        annual_fee_pp = costs["fee"] * cumulative_plant_factor
        sales = quantity * final_price
        material = quantity * annual_base_material_pp
        transport = quantity * annual_logistics_pp
        dl = quantity * annual_dl_pp
        voh = quantity * annual_voh_pp
        gmdc = sales - material - transport - dl - voh
        foh = quantity * annual_foh_pp
        fee = quantity * annual_fee_pp
        ebitda = gmdc - foh - fee
        ar = sales / DAYS_PER_YEAR * customer_days

        component_trace = []
        ap = ZERO
        rm_transit = ZERO
        rm_in_house = ZERO
        for component in components:
            per_product = (
                component.get("base_cost_per_product")
                if ap_basis == "base_purchase_value"
                else component.get("delivered_cost_per_product")
            )
            per_product = _d(per_product, ZERO) or ZERO
            annual_purchase = quantity * per_product * cumulative_material_factor
            payment_days = _d(component.get("payment_days"), ZERO) or ZERO
            component_ap = annual_purchase / DAYS_PER_YEAR * payment_days
            days = _stock_days(component, commercial)
            component_transit = annual_purchase / DAYS_PER_YEAR * days["rm_transit"]
            component_in_house = annual_purchase / DAYS_PER_YEAR * days["rm_in_house"]
            ap += component_ap
            rm_transit += component_transit
            rm_in_house += component_in_house
            component_trace.append({
                "component_id": component.get("component_id"),
                "supplier": component.get("supplier"),
                "payment_days": _number(payment_days),
                "annual_purchase_value": _number(annual_purchase),
                "ap_value": _number(component_ap),
                "currency": component.get("currency") or reporting_currency,
                "incoterm": component.get("incoterm"),
                "zone_relation": component.get("zone_relation"),
                "rm_transit_days": _number(days["rm_transit"]),
                "rm_in_house_days": _number(days["rm_in_house"]),
                "source": component.get("source"),
            })

        if wip_basis == "delivered_material_plus_conversion":
            wip_annual_basis = material + transport + dl + voh
        elif wip_basis == "material_only":
            wip_annual_basis = material
        else:
            wip_annual_basis = material + transport + dl + voh + foh + fee
        wip = wip_annual_basis / DAYS_PER_YEAR * wip_days
        fg_in_house_days = (
            Decimal("2") / Decimal("3") * delivery_frequency
            if platform
            else fg_safety_days + Decimal("2") / Decimal("3") * delivery_frequency
        )
        finished_cost_basis = material + transport + dl + voh + foh + fee
        fg_in_house = finished_cost_basis / DAYS_PER_YEAR * fg_in_house_days
        if platform:
            fg_transit_days = customer_transit
            fg_platform_days = platform_safety + Decimal("2") / Decimal("3") * delivery_frequency
        else:
            fg_transit_days = ZERO if customer_incoterm in {"FCA", "EXW", "FOB"} else customer_transit
            fg_platform_days = ZERO
        fg_transit = finished_cost_basis / DAYS_PER_YEAR * fg_transit_days
        fg_platform = finished_cost_basis / DAYS_PER_YEAR * fg_platform_days
        total_inventory = rm_transit + rm_in_house + wip + fg_in_house + fg_transit + fg_platform
        twc = ar + total_inventory - ap
        delta_twc = twc - prior_twc
        prior_twc = twc

        inv = investment["schedule"][period]
        generic_capex = inv["generic_capex"]
        specific_capex = inv["specific_capex"]
        tooling_expenditure = inv["tooling_expenditure"]
        collections = inv["customer_collections"]
        depreciation = inv["depreciation"]
        investment_cash = generic_capex + specific_capex + tooling_expenditure
        cash_evaluation = ebitda - delta_twc - investment_cash + collections
        financed_basis = abs(cash_evaluation) if cash_evaluation < ZERO else ZERO
        financial_charge = financed_basis * financing_rate
        operating_result = ebitda - depreciation - financial_charge
        taxable_result = max(ZERO, operating_result)
        taxes = taxable_result * tax_rate
        net_result = operating_result - taxes
        business_link = _d(_period_value(business_links, period, ZERO), ZERO) or ZERO
        annual_cash_flow = (
            ebitda
            - financial_charge
            - investment_cash
            + collections
            - taxes
            - delta_twc
            - business_link
        )
        discount_factor = ONE / ((ONE + discount_rate) ** index)
        discounted_cash_flow = annual_cash_flow * discount_factor
        cumulative_discounted += discounted_cash_flow
        rows.append({
            **year_info,
            "quantity": _number(quantity, Decimal("0.001")),
            "selling_price": _number(final_price, PER_UNIT_QUANTUM),
            "price_trace": {
                "opening_price": _number(opening_price, PER_UNIT_QUANTUM),
                "productivity_basis": productivity_basis_name,
                "productivity_adjustment": _number(productivity_amount, PER_UNIT_QUANTUM),
                "material_indexation_adjustment": _number(material_adjustment, PER_UNIT_QUANTUM),
                "plant_indexation_adjustment": _number(plant_adjustment, PER_UNIT_QUANTUM),
                "fx_adjustment": _number(fx_adjustment, PER_UNIT_QUANTUM),
                "formula": "opening - productivity + material_indexation + plant_indexation + fx_adjustment",
            },
            "per_product": {
                "base_material": _number(annual_base_material_pp, PER_UNIT_QUANTUM),
                "transport": _number(annual_logistics_pp, PER_UNIT_QUANTUM),
                "dl": _number(annual_dl_pp, PER_UNIT_QUANTUM),
                "voh": _number(annual_voh_pp, PER_UNIT_QUANTUM),
                "foh": _number(annual_foh_pp, PER_UNIT_QUANTUM),
                "fee": _number(annual_fee_pp, PER_UNIT_QUANTUM),
            },
            "sales": _number(sales),
            "material": _number(material),
            "transport": _number(transport),
            "dl": _number(dl),
            "voh": _number(voh),
            "gmdc": _number(gmdc),
            "foh": _number(foh),
            "fee": _number(fee),
            "ebitda": _number(ebitda),
            "ar": _number(ar),
            "ap": _number(ap),
            "ap_component_breakdown": component_trace,
            "rm_transit": _number(rm_transit),
            "rm_in_house": _number(rm_in_house),
            "wip": _number(wip),
            "fg_in_house": _number(fg_in_house),
            "fg_transit": _number(fg_transit),
            "fg_platform": _number(fg_platform),
            "stock_days": {
                "wip": _number(wip_days),
                "fg_in_house": _number(fg_in_house_days),
                "fg_transit": _number(fg_transit_days),
                "fg_platform": _number(fg_platform_days),
            },
            "total_inventory": _number(total_inventory),
            "twc": _number(twc),
            "delta_twc": _number(delta_twc),
            "generic_capex": _number(generic_capex),
            "specific_capex": _number(specific_capex),
            "tooling_expenditure": _number(tooling_expenditure),
            "customer_collections": _number(collections),
            "depreciation": _number(depreciation),
            "cash_evaluation": _number(cash_evaluation),
            "financed_cash_basis": _number(financed_basis),
            "financial_charge": _number(financial_charge),
            "operating_result": _number(operating_result),
            "taxable_result": _number(taxable_result),
            "taxes": _number(taxes),
            "net_result": _number(net_result),
            "business_link": _number(business_link),
            "annual_cash_flow": _number(annual_cash_flow),
            "discount_factor": _number(discount_factor, PER_UNIT_QUANTUM),
            "discounted_cash_flow": _number(discounted_cash_flow),
            "cumulative_discounted_cash_flow": _number(cumulative_discounted),
            "cash_flow_formula": (
                "EBITDA - financial_charge - CAPEX/tooling expenditure "
                "+ customer collections - taxes - Delta TWC - business_link"
            ),
        })

    return {
        **base_result,
        "project_code": technical_result.get("project_code"),
        "product_id": technical_result.get("product_id"),
        "currency": reporting_currency,
        "annual_table": rows,
        "npv": _number(cumulative_discounted),
        "npv_exact": _exact(cumulative_discounted),
        "npv_time_convention": "Y-1 is period 0; Y0 is period 1; Y6 is period 7.",
        "discount_rate": _number(discount_rate),
        "financing_rate": _number(financing_rate),
        "tax_rate": _number(tax_rate),
        "cost_structure": {
            key: _number(value, PER_UNIT_QUANTUM) for key, value in costs.items()
        },
        "foh_basis": technical_result.get("foh_basis") or "added_value_direct_cost",
        "fee_basis": technical_result.get("fee_basis") or "added_value_direct_cost",
        "ap_value_basis": ap_basis,
        "wip_value_basis": wip_basis,
        "investment_schedule": investment,
        "assumptions": _unique([
            *quantity_assumptions,
            "Default Choke WIP is 5 days." if "wip_days" not in commercial else "",
            "Default finished-goods safety stock is 10 days."
            if "fg_safety_stock_days" not in commercial else "",
            "Generic CAPEX depreciation defaults to five straight-line charges from Y0 through Y4."
            if "depreciation_years" not in commercial else "",
        ]),
        "rounding_policy": {
            "calculation_precision": "Decimal, 28 significant digits",
            "annual_monetary_output": "0.000001 reporting-currency unit",
            "per_product_output": "0.000000001 reporting-currency unit",
            "intermediate_rounding": "none",
        },
    }


def solve_selling_price(
    technical_result: Mapping[str, Any],
    commercial_inputs: Mapping[str, Any],
    unit_data: Optional[Mapping[str, Any]] = None,
    component_rows: Optional[List[Mapping[str, Any]]] = None,
    investment_assets: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Solve Y0 price with deterministic bracketed bisection."""
    commercial = dict(commercial_inputs)
    commercial["solve_selling_price"] = True
    target_config = commercial.get("profitability_target") or {}
    if not isinstance(target_config, Mapping):
        return {"convergence_status": "blocked", "missing_inputs": ["profitability_target"]}
    target_type = str(target_config.get("type") or "")
    if target_type not in {"npv_zero", "npv_amount"}:
        return {
            "convergence_status": "blocked",
            "missing_inputs": ["profitability_target.type"],
            "message": "Supported V1 targets are npv_zero and npv_amount.",
        }
    target = ZERO if target_type == "npv_zero" else _d(target_config.get("value"))
    if target is None:
        return {"convergence_status": "blocked", "missing_inputs": ["profitability_target.value"]}

    lower = _d(commercial.get("solver_lower_bound"), Decimal("0.000001")) or Decimal("0.000001")
    upper = _d(commercial.get("solver_upper_bound"), Decimal("1000000")) or Decimal("1000000")
    tolerance = _d(commercial.get("solver_tolerance"), Decimal("0.000001")) or Decimal("0.000001")
    price_tolerance = (
        _d(commercial.get("solver_price_tolerance"), Decimal("0.000000000001"))
        or Decimal("0.000000000001")
    )
    max_iterations = int(_d(commercial.get("solver_max_iterations"), Decimal("100")) or 100)

    def evaluate(price: Decimal) -> tuple[Optional[Decimal], Dict[str, Any]]:
        candidate = {**commercial, "initial_selling_price": str(price)}
        result = calculate_financial_plan(
            technical_result, candidate, unit_data, component_rows, investment_assets,
        )
        return _d(result.get("npv_exact"), _d(result.get("npv"))), result

    low_value, low_result = evaluate(lower)
    high_value, high_result = evaluate(upper)
    if low_value is None or high_value is None:
        return {
            "convergence_status": "blocked",
            "missing_inputs": _unique([
                *(low_result.get("missing_inputs") or []),
                *(high_result.get("missing_inputs") or []),
            ]),
        }
    low_delta = low_value - target
    high_delta = high_value - target
    if low_delta == ZERO:
        result = low_result
        midpoint = lower
        iterations = 0
    elif high_delta == ZERO:
        result = high_result
        midpoint = upper
        iterations = 0
    elif low_delta * high_delta > ZERO:
        return {
            "convergence_status": "no_solution_in_bounds",
            "target": {"type": target_type, "value": _number(target)},
            "bounds": {
                "lower": _number(lower, PER_UNIT_QUANTUM),
                "upper": _number(upper, PER_UNIT_QUANTUM),
                "lower_npv": _number(low_value),
                "upper_npv": _number(high_value),
            },
            "iterations": 0,
            "annual_financial_table": [],
        }
    else:
        result = low_result
        midpoint = lower
        iterations = 0
        for iterations in range(1, max_iterations + 1):
            midpoint = (lower + upper) / Decimal("2")
            mid_value, result = evaluate(midpoint)
            if mid_value is None:
                break
            mid_delta = mid_value - target
            if abs(mid_delta) <= tolerance or abs(upper - lower) <= price_tolerance:
                break
            if low_delta * mid_delta <= ZERO:
                upper = midpoint
                high_delta = mid_delta
            else:
                lower = midpoint
                low_delta = mid_delta

    achieved = _d(result.get("npv_exact"), _d(result.get("npv")))
    converged = achieved is not None and (
        abs(achieved - target) <= tolerance or abs(upper - lower) <= price_tolerance
    )
    return {
        "convergence_status": "converged" if converged else "max_iterations_reached",
        "commercially_usable": (
            converged
            and result.get("financial_status") == "ready"
            and commercial.get("mode") == "firm"
        ),
        "solved_y0_selling_price": _number(midpoint, PER_UNIT_QUANTUM),
        "target": {"type": target_type, "value": _number(target)},
        "achieved_npv": _number(achieved),
        "iterations": iterations,
        "bounds": {
            "lower": _number(lower, PER_UNIT_QUANTUM),
            "upper": _number(upper, PER_UNIT_QUANTUM),
        },
        "tolerance": _number(tolerance, PER_UNIT_QUANTUM),
        "annual_financial_table": result.get("annual_table") or [],
        "financial_result": result,
        "warning": (
            "Preliminary solved price is assumption-driven and is not commercially usable."
            if commercial.get("mode") != "firm" else None
        ),
    }
