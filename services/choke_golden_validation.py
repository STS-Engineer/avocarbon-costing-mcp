"""QA-only validation of the generic Choke engine against reviewed fixtures."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping

from services import choke_component_costing as component_costing
from services.choke_financial_calculation import (
    apply_olivier_direct_foh_fee,
    calculate_dl_voh,
)
from services.choke_financial_plan import calculate_financial_plan, solve_selling_price


QA_ENGINE_VERSION = "choke-golden-validation-v1"
REFERENCE_FILES = {
    "choke_24018": (
        "choke_24018_historical_inputs.json",
        "choke_24018_expected_outputs.json",
    ),
}


def _fixture_root() -> Path:
    configured = str(os.getenv("CHOKE_GOLDEN_FIXTURE_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_golden_fixtures(reference_id: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    filenames = REFERENCE_FILES.get(str(reference_id or "").strip())
    if not filenames:
        raise ValueError(f"Unknown golden reference: {reference_id}")
    root = _fixture_root()
    return _load_json(root / filenames[0]), _load_json(root / filenames[1])


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _component_result(item: Mapping[str, Any], target_currency: str) -> Dict[str, Any]:
    schedule = component_costing.resolve_component_unit_price_schedule(
        item.get("unit_price_schedule"), item.get("unit_price_aggregation")
    )
    if schedule.get("status") != "resolved":
        return {
            "component_id": item.get("component_id"),
            "status": "blocked",
            "blocking_reason": schedule.get("reason"),
        }
    unit_price = schedule["unit_price"]
    transport = item.get("transport_rate_per_pricing_unit")
    pricing_unit = str(item.get("pricing_unit") or "").lower()
    currency = str(item.get("currency") or "").upper()
    delivered = _decimal(unit_price) + _decimal(transport)
    raw = {
        "component_id": item.get("component_id"),
        "recommended_offer": {
            "unit_price": unit_price,
            "currency": currency,
            "pricing_unit": pricing_unit,
            "transport_cost": transport,
            "transport_cost_currency": currency,
            "transport_basis": f"{currency}/{pricing_unit}",
            "customs_cost": 0,
            "customs_cost_currency": currency,
            "customs_basis": f"{currency}/{pricing_unit}",
            "forwarder_fee": 0,
            "forwarder_fee_currency": currency,
            "forwarder_basis": f"{currency}/{pricing_unit}",
            "delivered_cost": float(delivered),
            "delivered_cost_currency": currency,
            "delivered_cost_basis": f"{currency}/{pricing_unit}",
            "payment_days": item.get("payment_days"),
            "incoterm": item.get("incoterm"),
        },
    }
    quantity = item.get("quantity_per_product")
    bom_fields = {
        "quantity_per_product": quantity,
        "quantity_unit": pricing_unit,
        "weight_kg_per_product": quantity if pricing_unit == "kg" else None,
        "bom_count_per_product": quantity if pricing_unit == "pc" else None,
    }
    canonical = component_costing.build_canonical_component_costing(
        str(item.get("component_id")),
        item.get("material_family"),
        bom_fields,
        raw,
        target_currency=target_currency,
    )
    reconciliation = component_costing.reconcile_delivered_unit_cost(
        raw, target_currency
    )
    purchasing_quantity = canonical.get("purchasing_quantity_per_product")
    delivered_unit = reconciliation.get("delivered_cost_per_pricing_unit")
    delivered_product = (
        _decimal(purchasing_quantity) * _decimal(delivered_unit)
        if purchasing_quantity is not None and delivered_unit is not None
        else None
    )
    return {
        **canonical,
        "status": (
            "resolved"
            if canonical.get("status") == "calculated"
            and reconciliation.get("status") == "calculated"
            else "blocked"
        ),
        "unit_price_schedule_resolution": schedule,
        "delivered_cost_reconciliation": reconciliation,
        "delivered_material_cost_per_piece": (
            float(delivered_product) if delivered_product is not None else None
        ),
        "normalized_offer": raw["recommended_offer"],
        "source_cells": list(item.get("source_cells") or []),
    }


def calculate_generic_choke_costing(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    """Calculate from canonical inputs using the production formula services."""
    payload = deepcopy(dict(inputs))
    project = payload.get("project") or {}
    currency = str(project.get("currency") or "").upper()
    annual_quantity = project.get("annual_quantity")
    components = [
        _component_result(item, currency)
        for item in payload.get("components") or []
    ]
    blocked_components = [
        item.get("component_id") for item in components
        if item.get("status") != "resolved"
    ]
    base_material = sum(
        (_decimal(item.get("material_cost_per_piece")) for item in components),
        Decimal("0"),
    )
    delivered_material = sum(
        (_decimal(item.get("delivered_material_cost_per_piece")) for item in components),
        Decimal("0"),
    )
    inbound_transport = delivered_material - base_material

    unit_data = dict(payload.get("unit_data") or {})
    dl_voh = calculate_dl_voh(
        payload.get("operations") or [], unit_data, annual_quantity
    )
    other_costs = payload.get("other_cost_inputs") or {}
    total_transport = (
        inbound_transport
        + _decimal(other_costs.get("outbound_transport_per_product"))
    )
    overhead = apply_olivier_direct_foh_fee(
        dl_voh,
        unit_data,
        {
            "transport_cost_per_piece": float(total_transport),
            "transport_breakdown_by_component": [],
            "missing_inputs": [],
        },
    )
    packaging = _decimal(other_costs.get("packaging_per_product"))
    manufacturing = _decimal(overhead.get("manufacturing_cost_per_piece"))
    technical = {
        "project_code": project.get("project_code"),
        "product_id": project.get("product_id"),
        "currency": currency,
        "status": (
            "calculated"
            if not blocked_components and dl_voh.get("status") == "calculated"
            else "blocked"
        ),
        "rule_set": payload.get("rule_set") or "current_approved",
        "material_cost_per_piece": float(base_material),
        "inbound_transport_cost_per_piece": float(inbound_transport),
        "transport_cost_per_piece": float(total_transport),
        "delivered_material_cost_per_piece": float(delivered_material),
        "dl_cost_per_piece": dl_voh.get("dl_cost_per_piece"),
        "voh_cost_per_piece": dl_voh.get("voh_cost_per_piece"),
        "direct_cost_per_piece": overhead.get("direct_cost_per_piece"),
        "foh_percent_dc": overhead.get("foh_percent_dc"),
        "foh_cost_per_piece": overhead.get("foh_cost_per_piece"),
        "fee_percent_dc": overhead.get("fee_percent_dc"),
        "fee_cost_per_piece": overhead.get("fee_cost_per_piece"),
        "manufacturing_cost_per_piece": overhead.get("manufacturing_cost_per_piece"),
        "packaging_cost_per_piece": float(packaging),
        "total_product_cost_per_piece": float(base_material + packaging + manufacturing),
        "component_breakdown": components,
        "most_breakdown_by_scope": dl_voh.get("work_package_calculation") or [],
        "missing_inputs": [
            *(f"component:{item}" for item in blocked_components),
            *(dl_voh.get("missing_inputs") or []),
        ],
    }
    commercial = deepcopy(payload.get("commercial_inputs") or {})
    component_rows = [
        {
            "component_id": item.get("component_id"),
            "supplier": item.get("component_id"),
            "currency": currency,
            "base_cost_per_product": item.get("material_cost_per_piece"),
            "delivered_cost_per_product": item.get("delivered_material_cost_per_piece"),
            "payment_days": source.get("payment_days"),
            "incoterm": source.get("incoterm"),
            "zone_relation": "different",
            "origin_zone": "historical_source_zone",
            "ap_value_basis": "base_purchase_value",
            "source": "golden_input_fixture",
        }
        for item, source in zip(components, payload.get("components") or [])
    ]
    solver = solve_selling_price(
        technical, commercial, unit_data, component_rows, []
    )
    financial = solver.get("financial_result") or calculate_financial_plan(
        technical, commercial, unit_data, component_rows, []
    )
    return {
        "engine": "generic_choke_costing",
        "engine_version": QA_ENGINE_VERSION,
        "shared_calculation_functions": [
            "build_canonical_component_costing",
            "reconcile_delivered_unit_cost",
            "calculate_dl_voh",
            "apply_olivier_direct_foh_fee",
            "calculate_financial_plan",
            "solve_selling_price",
        ],
        "technical_result": technical,
        "financial_result": financial,
        "selling_price_solver": solver,
    }


def _reconciliation(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[Dict[str, Any]]:
    technical = actual.get("technical_result") or {}
    expected_technical = expected.get("technical_outputs") or {}
    fields = {
        "base_material_per_product": "material_cost_per_piece",
        "inbound_transport_per_product": "inbound_transport_cost_per_piece",
        "delivered_material_per_product": "delivered_material_cost_per_piece",
        "dl_per_product": "dl_cost_per_piece",
        "voh_per_product": "voh_cost_per_piece",
        "direct_cost_per_product": "direct_cost_per_piece",
        "foh_per_product": "foh_cost_per_piece",
        "fees_per_product": "fee_cost_per_piece",
        "manufacturing_cost_per_product": "manufacturing_cost_per_piece",
        "total_product_cost_per_product": "total_product_cost_per_piece",
    }
    rows = []
    for expected_name, actual_name in fields.items():
        expected_value = expected_technical.get(expected_name)
        actual_value = technical.get(actual_name)
        difference = (
            _decimal(actual_value) - _decimal(expected_value)
            if actual_value is not None and expected_value is not None else None
        )
        rows.append({
            "metric": expected_name,
            "generic_engine_value": actual_value,
            "expected_excel_value": expected_value,
            "difference": float(difference) if difference is not None else None,
            "status": (
                "match" if difference is not None and abs(difference) <= Decimal("0.000001")
                else "mismatch"
            ),
        })
    solver = actual.get("selling_price_solver") or {}
    expected_solver = expected.get("solver_outputs") or {}
    for metric, actual_name in (
        ("npv", "achieved_npv"),
        ("quoted_y0_selling_price", "solved_y0_selling_price"),
    ):
        expected_value = expected_solver.get(metric)
        actual_value = solver.get(actual_name)
        difference = (
            _decimal(actual_value) - _decimal(expected_value)
            if actual_value is not None and expected_value is not None else None
        )
        rows.append({
            "metric": metric,
            "generic_engine_value": actual_value,
            "expected_excel_value": expected_value,
            "difference": float(difference) if difference is not None else None,
            "status": (
                "match" if difference is not None and abs(difference) <= Decimal("0.000001")
                else "mismatch"
            ),
        })
    return rows


def validate_against_golden_reference(reference_id: str) -> Dict[str, Any]:
    historical_inputs, expected_outputs = load_golden_fixtures(reference_id)
    generic_result = calculate_generic_choke_costing(historical_inputs)
    rows = _reconciliation(generic_result, expected_outputs)
    return {
        "status": "match" if all(row["status"] == "match" for row in rows) else "mismatch",
        "reference_id": reference_id,
        "rule_set": historical_inputs.get("rule_set"),
        "generic_engine_result": generic_result,
        "expected_excel_result": expected_outputs,
        "reconciliation": rows,
        "expected_values_used_as_calculation_inputs": False,
        "production_agents_invoked": False,
    }
