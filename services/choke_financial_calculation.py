import math
import re

from services import choke_component_costing


def _coerce_number(value):
    if value in [None, ""]:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def _normalize_percent(value):
    number = _coerce_number(value)
    if number is None:
        return None
    if number > 1:
        return number / 100
    return number


def _get_path(data, path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_value(data, paths):
    for path in paths:
        value = _get_path(data, path)
        if value not in [None, ""]:
            return value
    return None


def _as_work_packages(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in [
            "work_packages",
            "most_work_packages",
            "operations",
            "most_operations",
            "operation_costs",
            "routing_operations",
        ]:
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    return []


def _as_component_entries(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in [
            "components",
            "component_entries",
            "component_outputs",
            "normalized_components",
        ]:
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    return []


def _entry_bom_fields(entry, raw):
    """Best-effort dimensional fields for callers (e.g. the orchestrator demo
    path) that don't go through the full BOM dimensional map built in
    choke_sequential_agent_workflow.calculate_final_choke_costing_from_saved_outputs."""
    return {
        "weight_kg_per_product": entry.get("weight_kg_per_product") or raw.get("weight_kg_per_product"),
        "physical_mass_g_per_product": entry.get("physical_mass_g_per_product") or raw.get("physical_mass_g_per_product"),
        "physical_length_mm_per_product": entry.get("physical_length_mm_per_product") or raw.get("developed_length_mm"),
        "diameter_mm": entry.get("diameter_mm") or raw.get("diameter_mm"),
        "bom_count_per_product": entry.get("bom_count_per_product"),
        "quantity_per_product": _coerce_number(entry.get("quantity_per_product") or raw.get("quantity_per_product")),
        "quantity_unit": entry.get("quantity_unit") or raw.get("quantity_unit"),
    }


def calculate_transport_cost_from_components(component_entries):
    """Olivier's component-level rule: transport + customs + forwarder, each
    converted to the BOM pricing quantity's own basis before summing. See
    services/choke_component_costing.py for the unit-safe implementation —
    this must never multiply a per-kg/per-m rate by a raw piece count (or
    vice versa) the way the naive `quantity * (t + d + f)` formula used to."""
    transport_breakdown = []
    missing_inputs = []
    total_transport = 0.0

    for entry in _as_component_entries(component_entries):
        raw = entry.get("agent_raw_output") or entry.get("raw_json") or entry
        component_id = entry.get("component_id") or raw.get("component_id") or raw.get("component_reference")
        bom_fields = _entry_bom_fields(entry, raw)
        pricing_quantity_info = choke_component_costing.resolve_component_pricing_quantity(
            component_id, entry.get("component_type") or entry.get("component_family"), bom_fields, raw,
        )
        price_info = choke_component_costing.resolve_unit_price(raw)
        material_result = choke_component_costing.compute_component_material_cost(
            component_id, pricing_quantity_info, price_info,
        )
        material_cost = material_result.get("material_cost_per_product") if material_result["status"] == "calculated" else None

        transport_result = choke_component_costing.compute_component_transport_cost(
            component_id, raw, pricing_quantity_info, material_cost,
        )
        if transport_result["status"] == "blocked":
            missing_inputs.append(f"{component_id}: {transport_result['reason']}")
            component_total = 0.0
            logistics_breakdown = {}
        else:
            component_total = transport_result["transport_cost_per_product"]
            logistics_breakdown = transport_result.get("logistics_breakdown") or {}
        total_transport += component_total

        transport_breakdown.append({
            "component_id": component_id,
            "pricing_quantity": pricing_quantity_info.get("pricing_quantity"),
            "pricing_unit": pricing_quantity_info.get("pricing_unit"),
            "transportation_cost_per_component": (logistics_breakdown.get("transport") or {}).get("converted_value", 0.0),
            "custom_duty_cost_per_component": (logistics_breakdown.get("customs") or {}).get("converted_value", 0.0),
            "forwarder_cost_per_component": (logistics_breakdown.get("forwarder") or {}).get("converted_value", 0.0),
            "transport_cost_per_piece": component_total,
            "currency": (
                _first_value(raw, [
                    ["recommended_offer", "supply_chain", "currency"],
                    ["recommended_offer", "currency"],
                    ["normalized_cost", "currency"],
                    ["currency"],
                ])
                or ""
            ),
        })

    return {
        "transport_cost_per_piece": total_transport,
        "transport_breakdown_by_component": transport_breakdown,
        "missing_inputs": list(dict.fromkeys(missing_inputs)),
    }


def apply_olivier_direct_foh_fee(dl_voh_result, unit_data, transport_result=None):
    transport_result = transport_result or {}
    dl = dl_voh_result.get("dl_cost_per_piece")
    voh = dl_voh_result.get("voh_cost_per_piece")
    transport = transport_result.get("transport_cost_per_piece")
    foh_percent = _coerce_number(unit_data.get("foh_percent_dc")) or 0.0
    fee_percent = _coerce_number(unit_data.get("fee_percent_dc")) or 0.0

    direct_cost = None
    foh_cost = None
    fee_cost = None
    manufacturing_cost = None
    if dl is not None and voh is not None and transport is not None:
        direct_cost = dl + voh + transport
        foh_cost = foh_percent / 100 * direct_cost
        fee_cost = fee_percent / 100 * direct_cost
        manufacturing_cost = direct_cost + foh_cost + fee_cost

    return {
        "transport_cost_per_piece": transport,
        "transport_breakdown_by_component": transport_result.get("transport_breakdown_by_component") or [],
        "direct_cost_per_piece": direct_cost,
        "foh_percent_dc": foh_percent,
        "foh_cost_per_piece": foh_cost,
        "fee_percent_dc": fee_percent,
        "fee_cost_per_piece": fee_cost,
        "manufacturing_cost_per_piece": manufacturing_cost,
        "missing_inputs": transport_result.get("missing_inputs") or [],
        # This is the preliminary plant-percentage costing method (direct_cost
        # x flat unit_data.foh_percent_dc / fee_percent_dc). It intentionally
        # does not replicate the legacy workbook's ROCE-lock / sales-tier /
        # min-max FOH-Fee commercial-pricing mechanism, which belongs to a
        # later commercial-pricing layer.
        "costing_method": "preliminary_plant_percentage_dc",
    }


def _resolve_fx(unit_data, fx_rates, assumptions, missing_inputs):
    operating_currency = unit_data.get("operating_currency")
    selling_currency = unit_data.get("selling_currency")
    if not operating_currency or not selling_currency:
        missing_inputs.append("operating_currency/selling_currency")
        return None
    if operating_currency == selling_currency:
        return 1.0

    fx_rates = fx_rates or {}
    direct_key = f"{operating_currency}_to_{selling_currency}"
    reverse_key = f"{selling_currency}_to_{operating_currency}"
    if fx_rates.get(direct_key):
        return _coerce_number(fx_rates[direct_key])
    if fx_rates.get(reverse_key):
        reverse = _coerce_number(fx_rates[reverse_key])
        return 1 / reverse if reverse else None

    if operating_currency == "TND" and selling_currency == "EUR":
        assumptions.append("FX demo fallback used: 1 EUR = 3.7 TND")
        return 3.7

    missing_inputs.append(f"FX rate {operating_currency}_to_{selling_currency}")
    return None


def _normalized_operation(work_package):
    if isinstance(work_package.get("normalized_operation"), dict):
        return {**work_package, **work_package["normalized_operation"]}
    if isinstance(work_package.get("operation_definition"), dict):
        return {**work_package, **work_package["operation_definition"]}
    return work_package


def calculate_dl_voh(work_packages_or_most_outputs, unit_data, annual_quantity, fx_rates=None):
    unit_data = unit_data or {}
    work_packages = _as_work_packages(work_packages_or_most_outputs)
    assumptions = list(unit_data.get("assumptions") or [])
    missing_inputs = []

    annual_quantity_number = _coerce_number(annual_quantity)
    dl_rate = _coerce_number(unit_data.get("dl_rate_operating_per_hour"))
    voh_rate = _coerce_number(unit_data.get("voh_rate_operating_per_hour"))
    open_hours = _coerce_number(unit_data.get("open_hours_per_year"))
    fx = _resolve_fx(unit_data, fx_rates, assumptions, missing_inputs)

    for field_name, value in [
        ("annual_quantity", annual_quantity_number),
        ("dl_rate_operating_per_hour", dl_rate),
        ("voh_rate_operating_per_hour", voh_rate),
        ("open_hours_per_year", open_hours),
        ("fx_operating_to_selling", fx),
    ]:
        if value in [None, 0]:
            missing_inputs.append(field_name)

    if not work_packages:
        missing_inputs.append("work_packages_or_most_outputs")

    work_package_calculation = []
    total_dl = 0.0
    total_voh = 0.0
    total_tooling_adder = 0.0

    for index, work_package in enumerate(work_packages, start=1):
        operation = _normalized_operation(work_package)
        operation_name = (
            operation.get("operation_name")
            or operation.get("operation")
            or operation.get("operation_description")
            or f"operation_{index}"
        )
        work_package_id = operation.get("work_package_id") or f"wp_{index:02d}"
        p_h = _coerce_number(_first_value(operation, [
            ["p_h"],
            ["station_library_summary", "p_h"],
            ["rate_per_hour_instantaneous"],
            ["produced_per_hour"],
            ["pieces_per_hour"],
        ]))
        oee = _normalize_percent(_first_value(operation, [
            ["oee"],
            ["oee_percent"],
            ["costing_oee_percent"],
            ["station_library_summary", "oee"],
        ]))
        parts_per_cycle = _coerce_number(_first_value(operation, [
            ["parts_per_cycle"],
            ["pieces_per_cycle"],
            ["station_library_summary", "parts_per_cycle"],
        ])) or 1.0
        operator_percent_decimal = _normalize_percent(_first_value(operation, [
            ["operator_percent"],
            ["percent_operator"],
            ["operator_percent_decimal"],
            ["station_library_summary", "operator_percent"],
        ]))
        generic_capex = _coerce_number(_first_value(operation, [
            ["generic_capex_eur"],
            ["generic_capex"],
            ["station_library_summary", "generic_capex_eur"],
        ])) or 0.0
        specific_capex = _coerce_number(_first_value(operation, [
            ["specific_capex_eur"],
            ["specific_capex"],
            ["station_library_summary", "specific_capex_eur"],
        ])) or 0.0
        tooling_cost = _coerce_number(_first_value(operation, [
            ["tooling_cost_eur"],
            ["tooling_cost"],
            ["station_library_summary", "tooling_cost_eur"],
        ]))
        tooling_life = _coerce_number(_first_value(operation, [
            ["tooling_life_pieces"],
            ["tooling_life_parts"],
            ["tooling_lifetime_parts"],
            ["station_library_summary", "tooling_life_pieces"],
        ]))
        tooling_type = str(_first_value(operation, [
            ["tooling_type"], ["station_library_summary", "tooling_type"],
        ]) or "").lower()
        tooling_adder_per_piece = _coerce_number(_first_value(operation, [
            ["tooling_adder_per_piece_eur"],
            ["station_library_summary", "tooling_adder_per_piece_eur"],
        ])) or 0.0

        # A station explicitly reporting p_h=0 and operator_percent=0 together
        # means "no internal AVOCarbon operation here" (e.g. a fully external
        # or customer-performed step), not "data missing". Preserve that as a
        # zero-cost, non-blocking operation instead of collapsing it into the
        # same bucket as a genuinely absent field.
        if p_h == 0 and operator_percent_decimal == 0:
            total_tooling_adder += tooling_adder_per_piece
            work_package_calculation.append({
                "work_package_id": work_package_id,
                "operation_name": operation_name,
                "status": "external_zero_cost",
                "p_h": 0.0,
                "oee": oee,
                "operator_percent_decimal": 0.0,
                "dl_cost_per_piece": 0.0,
                "voh_cost_per_piece": 0.0,
                "tooling_adder_per_piece": tooling_adder_per_piece,
                "raw_operation": work_package,
            })
            continue

        operation_missing = []
        if p_h in [None, 0]:
            operation_missing.append("p_h")
        if oee in [None, 0]:
            operation_missing.append("oee")
        if operator_percent_decimal is None:
            operation_missing.append("operator_percent")

        if operation_missing or missing_inputs:
            for item in operation_missing:
                missing_inputs.append(f"{work_package_id}: {item}")
            work_package_calculation.append({
                "work_package_id": work_package_id,
                "operation_name": operation_name,
                "status": "blocked",
                "missing_inputs": operation_missing,
                "raw_operation": work_package,
            })
            continue

        produced_per_hour_after_oee = p_h * oee * parts_per_cycle
        if produced_per_hour_after_oee <= 0:
            missing_inputs.append(f"{work_package_id}: produced_per_hour_after_oee")
            work_package_calculation.append({
                "work_package_id": work_package_id,
                "operation_name": operation_name,
                "status": "blocked",
                "missing_inputs": ["produced_per_hour_after_oee"],
                "raw_operation": work_package,
            })
            continue

        hm_mach_per_1000 = 1000 / produced_per_hour_after_oee
        hm_dl_per_1000 = hm_mach_per_1000 * operator_percent_decimal
        hourly_dl_selling = dl_rate / fx
        dl_cost_per_piece = hm_dl_per_1000 * hourly_dl_selling / 1000

        yearly_production_hours = annual_quantity_number / produced_per_hour_after_oee
        occupation_rate = yearly_production_hours / open_hours * 1.1
        base_voh_selling_per_hour = voh_rate / fx

        generic_allocated = generic_capex * occupation_rate
        generic_maintenance_energy = 0.15 * generic_allocated
        generic_voh_per_hour = generic_maintenance_energy / yearly_production_hours

        specific_occupation_integer = math.floor(occupation_rate) + 1
        specific_allocated = specific_capex * specific_occupation_integer
        specific_maintenance_energy = 0.15 * specific_allocated
        specific_voh_per_hour = specific_maintenance_energy / yearly_production_hours

        tooling_cost_per_piece = None
        if "lifetime" in tooling_type:
            tooling_voh_per_hour = 0.0
        elif tooling_cost not in [None, 0] and tooling_life not in [None, 0]:
            tooling_cost_per_piece = tooling_cost / tooling_life
            tooling_voh_per_hour = tooling_cost_per_piece * produced_per_hour_after_oee
        else:
            tooling_voh_per_hour = 0.0

        total_voh_per_hour = (
            base_voh_selling_per_hour
            + generic_voh_per_hour
            + specific_voh_per_hour
            + tooling_voh_per_hour
        )
        voh_cost_per_piece = hm_mach_per_1000 * total_voh_per_hour / 1000

        total_dl += dl_cost_per_piece
        total_voh += voh_cost_per_piece
        total_tooling_adder += tooling_adder_per_piece
        work_package_calculation.append({
            "work_package_id": work_package_id,
            "component_id": operation.get("component_id"),
            "operation_id": operation.get("operation_id"),
            "operation_name": operation_name,
            "status": "calculated",
            "p_h": p_h,
            "oee": oee,
            "parts_per_cycle": parts_per_cycle,
            "operator_percent_decimal": operator_percent_decimal,
            "produced_per_hour_after_oee": produced_per_hour_after_oee,
            "H_M_Mach_per_1000": hm_mach_per_1000,
            "H_M_DL_per_1000": hm_dl_per_1000,
            "hourly_dl_selling": hourly_dl_selling,
            "dl_cost_per_piece": dl_cost_per_piece,
            "yearly_production_hours": yearly_production_hours,
            "occupation_rate": occupation_rate,
            "base_voh_selling_per_hour": base_voh_selling_per_hour,
            "generic_voh_per_hour": generic_voh_per_hour,
            "specific_occupation_integer": specific_occupation_integer,
            "specific_voh_per_hour": specific_voh_per_hour,
            "tooling_cost_per_piece": tooling_cost_per_piece,
            "tooling_adder_per_piece": tooling_adder_per_piece,
            "tooling_voh_per_hour": tooling_voh_per_hour,
            "total_voh_per_hour": total_voh_per_hour,
            "voh_cost_per_piece": voh_cost_per_piece,
            "raw_operation": work_package,
        })

    unique_missing = list(dict.fromkeys(missing_inputs))
    return {
        "status": "blocked" if unique_missing else "calculated",
        "currency": unit_data.get("selling_currency"),
        "dl_cost_per_piece": None if unique_missing else total_dl,
        "voh_cost_per_piece": None if unique_missing else total_voh,
        "tooling_adder_per_piece": None if unique_missing else total_tooling_adder,
        "work_package_calculation": work_package_calculation,
        "missing_inputs": unique_missing,
        "assumptions": list(dict.fromkeys(assumptions)),
    }


def calculate_choke_financials(operations_json_list, annual_quantity, plant_data):
    result = calculate_dl_voh(operations_json_list, plant_data, annual_quantity)
    olivier_costs = apply_olivier_direct_foh_fee(
        result,
        plant_data or {},
        {
            "transport_cost_per_piece": 0.0,
            "transport_breakdown_by_component": [],
            "missing_inputs": [],
        },
    )
    return {
        "status": result["status"],
        "currency": result["currency"],
        "dl_cost_per_piece": result["dl_cost_per_piece"],
        "voh_cost_per_piece": result["voh_cost_per_piece"],
        "transport_cost_per_piece": olivier_costs["transport_cost_per_piece"],
        "transport_breakdown_by_component": olivier_costs["transport_breakdown_by_component"],
        "direct_cost_per_piece": olivier_costs["direct_cost_per_piece"],
        "foh_percent_dc": olivier_costs["foh_percent_dc"],
        "foh_cost_per_piece": olivier_costs["foh_cost_per_piece"],
        "fee_percent_dc": olivier_costs["fee_percent_dc"],
        "fee_cost_per_piece": olivier_costs["fee_cost_per_piece"],
        "manufacturing_cost_per_piece": olivier_costs["manufacturing_cost_per_piece"],
        "added_value_cost_per_piece": (
            None
            if result["status"] == "blocked"
            else result["dl_cost_per_piece"] + result["voh_cost_per_piece"]
        ),
        "operations_calculation": result["work_package_calculation"],
        "missing_inputs": result["missing_inputs"],
        "assumptions": result["assumptions"],
    }
