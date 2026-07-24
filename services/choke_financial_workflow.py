"""Filesystem adapter between Choke workflow artifacts and the financial model."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from services.choke_financial_plan import (
    build_historical_comparison,
    calculate_financial_plan,
    financial_readiness,
    solve_selling_price,
)
from services.project_data_paths import atomic_write_json, get_workflow_run_paths
from services.product_profitability_service import get_product_profitability_objective
from services.choke_component_costing import resolve_component_ap_terms


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _first(data: Any, paths: Iterable[Iterable[str]]) -> Any:
    for path in paths:
        current = data
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current
    return None


def _first_with_path(data: Any, paths: Iterable[Iterable[str]]) -> tuple[Any, str | None]:
    for path in paths:
        current = data
        for key in path:
            if not isinstance(current, Mapping):
                current = None
                break
            current = current.get(key)
        if current not in (None, ""):
            return current, ".".join(path)
    return None, None


def _revision(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paths(project_code: str, product_id: str) -> Dict[str, Path]:
    paths = get_workflow_run_paths(project_code, product_id)
    run_dir = paths["run_dir"]
    return {
        **paths,
        "technical_result": run_dir / "final_choke_costing_result.json",
        "financial_input": run_dir / "financial_plan_input.json",
        "financial_result": run_dir / "financial_plan_result.json",
        "solver_result": run_dir / "financial_price_solver_result.json",
        "comparison_result": run_dir / "financial_reference_comparison.json",
    }


def _state(project_code: str, product_id: str) -> Dict[str, Any]:
    return _read_json(_paths(project_code, product_id)["workflow_state_path"], {}) or {}


def _technical(project_code: str, product_id: str) -> Dict[str, Any]:
    path = _paths(project_code, product_id)["technical_result"]
    if not path.exists():
        raise FileNotFoundError(
            "Final technical result not found. Calculate the technical Choke result first."
        )
    return _read_json(path, {}) or {}


def _saved_inputs(project_code: str, product_id: str) -> Dict[str, Any]:
    return _read_json(_paths(project_code, product_id)["financial_input"], {}) or {}


def _commercial_context(
    project_code: str,
    product_id: str,
    overrides: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    state = _state(project_code, product_id)
    customer = dict(state.get("customer_input") or {})
    saved = _saved_inputs(project_code, product_id)
    commercial = {**customer, **saved, **dict(overrides or {})}
    commercial["project_code"] = project_code
    commercial["product_id"] = product_id
    commercial.setdefault(
        "production_plant",
        (state.get("manufacturing_strategy") or {}).get("production_plant"),
    )
    if commercial.get("currency") in (None, ""):
        commercial["currency"] = (
            customer.get("quotation_currency")
            or customer.get("target_price_currency")
        )
    if not isinstance(commercial.get("product_profitability_target"), Mapping):
        commercial["product_profitability_target"] = (
            get_product_profitability_objective(
                commercial.get("product") or commercial.get("product_name"),
                product_id,
            )
        )
    return commercial


def _component_rows(
    technical: Mapping[str, Any],
    commercial: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    terms = commercial.get("supplier_terms") or {}
    result = []
    for component in technical.get("component_breakdown") or []:
        if component.get("status") not in {"resolved", "resolved_assumption"}:
            continue
        cid = str(component.get("component_id") or "")
        override = terms.get(cid, {}) if isinstance(terms, Mapping) else {}
        override = override if isinstance(override, Mapping) else {}
        offer = component.get("normalized_offer") or {}
        ap_terms = component.get("ap_terms") or offer.get("ap_terms") or {}
        payment_days, payment_path = _first_with_path(offer, [
            ["payment_days"],
            ["payment_conditions_days"],
            ["recommended_offer", "payment_days"],
            ["recommended_offer", "payment_conditions_days"],
            ["raw_offer", "payment_days"],
            ["raw_offer", "payment_conditions_days"],
            ["raw_offer", "recommended_offer", "payment_days"],
        ])
        incoterm, incoterm_path = _first_with_path(offer, [
            ["incoterm"],
            ["recommended_offer", "incoterm"],
            ["raw_offer", "incoterm"],
            ["raw_offer", "recommended_offer", "incoterm"],
        ])
        ap_basis, ap_basis_path = _first_with_path(offer, [
            ["ap_value_basis"],
            ["recommended_offer", "ap_value_basis"],
            ["raw_offer", "ap_value_basis"],
            ["raw_offer", "recommended_offer", "ap_value_basis"],
            ["price_basis", "ap_value_basis"],
        ])
        origin_zone, origin_path = _first_with_path(offer, [
            ["origin_zone"],
            ["supplier_country"],
            ["recommended_offer", "origin"],
            ["recommended_offer", "origin_zone"],
            ["raw_offer", "origin_zone"],
            ["raw_offer", "supplier_country"],
            ["raw_offer", "recommended_offer", "origin"],
        ])
        supplier, supplier_path = _first_with_path(offer, [
            ["supplier_name"],
            ["recommended_offer", "supplier_name"],
            ["raw_offer", "supplier_name"],
            ["raw_offer", "recommended_offer", "supplier_name"],
        ])
        override_paths = override.get("source_paths") or {}
        result.append({
            "component_id": cid,
            "supplier": (
                override.get("supplier")
                or ap_terms.get("supplier")
                or supplier
            ),
            "currency": component.get("currency") or offer.get("currency"),
            "base_cost_per_product": component.get("material_cost_per_piece"),
            "delivered_cost_per_product": component.get(
                "delivered_material_cost_per_piece"
            ),
            "payment_days": (
                override.get("payment_days")
                if "payment_days" in override
                else ap_terms.get("payment_days", payment_days)
            ),
            "incoterm": (
                override.get("incoterm")
                or ap_terms.get("incoterm")
                or incoterm
            ),
            "zone_relation": override.get("zone_relation"),
            "origin": ap_terms.get("origin"),
            "origin_zone": (
                override.get("origin_zone")
                or ap_terms.get("origin_zone")
                or origin_zone
            ),
            "payment_term": ap_terms.get("payment_term"),
            "ap_value_basis": (
                override.get("ap_value_basis")
                or ap_terms.get("ap_value_basis")
                or ap_basis
            ),
            "source_paths": {
                "supplier": override_paths.get("supplier") or (
                    ap_terms.get("source_paths") or {}
                ).get("supplier") or supplier_path,
                "payment_days": override_paths.get("payment_days") or (
                    ap_terms.get("source_paths") or {}
                ).get("payment_days") or payment_path,
                "incoterm": override_paths.get("incoterm") or (
                    ap_terms.get("source_paths") or {}
                ).get("incoterm") or incoterm_path,
                "origin": (ap_terms.get("source_paths") or {}).get("origin"),
                "origin_zone": override_paths.get("origin_zone") or (
                    ap_terms.get("source_paths") or {}
                ).get("origin_zone") or origin_path,
                "ap_value_basis": override_paths.get("ap_value_basis") or (
                    ap_terms.get("source_paths") or {}
                ).get("ap_value_basis") or ap_basis_path,
                "base_cost_per_product": "component_breakdown.material_cost_per_piece",
                "delivered_cost_per_product": (
                    "component_breakdown.delivered_material_cost_per_piece"
                ),
            },
            "source": override.get("source") or "saved_component_output",
        })
    return result


def component_ap_readiness(
    project_code: str,
    product_id: str,
    commercial_inputs: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Audit AP readiness from this run's actual saved component JSON files."""
    paths = _paths(project_code, product_id)
    commercial = _commercial_context(project_code, product_id, commercial_inputs)
    overrides = commercial.get("supplier_terms") or {}
    component_dir = paths["components_dir"]
    try:
        technical = _technical(project_code, product_id)
    except FileNotFoundError:
        technical = {}
    expected = ["ferrite_core", "magnet_wire", "lead_tinning", "glue"]
    rows: List[Dict[str, Any]] = []
    rerun: List[str] = []
    for component_id in expected:
        path = component_dir / f"{component_id}.json"
        raw = _read_json(path, None)
        if not isinstance(raw, Mapping):
            rows.append({
                "component_id": component_id,
                "status": "missing_output",
                "source_path": str(path),
                "missing_fields": ["component_output"],
            })
            rerun.append(component_id)
            continue
        terms = resolve_component_ap_terms(dict(raw))
        override = (
            overrides.get(component_id, {})
            if isinstance(overrides, Mapping) else {}
        )
        selected_basis = override.get("ap_value_basis") or terms.get(
            "ap_value_basis"
        )
        component = next((
            item for item in technical.get("component_breakdown") or []
            if item.get("component_id") == component_id
        ), {})
        purchasing_value = (
            component.get("material_cost_per_piece")
            if selected_basis == "base_purchase_value"
            else component.get("delivered_material_cost_per_piece")
            if selected_basis == "delivered_purchase_value"
            else None
        )
        missing = list(terms.get("missing_fields") or [])
        if purchasing_value is None:
            missing.append("purchasing_value")
        missing = list(dict.fromkeys(missing))
        status = "ready" if not missing else "incomplete"
        if status != "ready":
            rerun.append(component_id)
        rows.append({
            "component_id": component_id,
            "supplier": terms.get("supplier"),
            "payment_term": terms.get("payment_term"),
            "normalized_payment_days": terms.get("payment_days"),
            "ap_basis": selected_basis,
            "purchasing_value_selected": purchasing_value,
            "incoterm": terms.get("incoterm"),
            "origin": terms.get("origin"),
            "origin_zone": terms.get("origin_zone"),
            "source_path": str(path),
            "field_source_paths": terms.get("source_paths") or {},
            "status": status,
            "missing_fields": missing,
        })
    return {
        "project_code": project_code,
        "product_id": product_id,
        "component_ap_readiness": rows,
        "components_requiring_rerun": list(dict.fromkeys(rerun)),
        "preserve_outputs": ["bom", "most"],
    }


def _investment_assets(project_code: str, product_id: str) -> List[Dict[str, Any]]:
    most_dir = _paths(project_code, product_id)["most_dir"]
    assets: List[Dict[str, Any]] = []
    if not most_dir.exists():
        return assets
    for path in sorted(most_dir.glob("*.json")):
        raw = _read_json(path, {}) or {}
        operation = (
            raw.get("normalized_operation")
            or raw.get("operation_definition")
            or raw
        )
        source_id = (
            operation.get("work_package_id")
            or operation.get("most_scope_id")
            or path.stem
        )
        currency = (
            operation.get("capex_currency")
            or operation.get("tooling_currency")
            or "EUR"
        )
        for category, field_names in (
            ("generic_capex", ("generic_capex_eur", "generic_capex")),
            ("specific_capex", ("specific_capex_eur", "specific_capex")),
            ("tooling", ("tooling_cost_eur", "tooling_cost")),
        ):
            amount = next(
                (operation.get(name) for name in field_names if operation.get(name) not in (None, "")),
                None,
            )
            if amount in (None, "", 0, 0.0):
                continue
            assets.append({
                "source_id": str(source_id),
                "work_package_id": str(source_id),
                "operation_name": operation.get("operation_name"),
                "category": category,
                "amount": amount,
                "currency": currency,
                "estimated": operation.get("estimated", True),
                "confidence": operation.get("confidence"),
                "validation_questions": operation.get("validation_questions") or [],
                "source_path": str(path),
            })
    return assets


def _unit_data(project_code: str, product_id: str) -> Dict[str, Any]:
    return dict(_state(project_code, product_id).get("unit_data") or {})


def _persist_financial_status(
    project_code: str,
    product_id: str,
    financial_status: str,
    result_path: Path | None = None,
) -> None:
    paths = _paths(project_code, product_id)
    state = _state(project_code, product_id)
    if not state:
        return
    state["workflow_status"] = state.get("status")
    state["bom_status"] = (state.get("bom") or {}).get("status")
    state["component_status"] = (
        "received"
        if state.get("components")
        and all(
            item.get("status") == "received"
            for item in (state.get("components") or {}).values()
            if isinstance(item, Mapping)
        )
        else "pending"
    )
    state["most_status"] = (state.get("most") or {}).get("status")
    technical = _read_json(paths["technical_result"], {}) or {}
    state["final_calculation_status"] = technical.get("status")
    state["financial_status"] = financial_status
    if result_path:
        state["financial_result_path"] = str(result_path)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(paths["workflow_state_path"], state)


def get_financial_readiness(
    project_code: str,
    product_id: str,
    commercial_inputs: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    technical = _technical(project_code, product_id)
    commercial = _commercial_context(project_code, product_id, commercial_inputs)
    unit_data = _unit_data(project_code, product_id)
    result = financial_readiness(
        technical,
        commercial,
        unit_data,
        _component_rows(technical, commercial),
        _investment_assets(project_code, product_id),
    )
    ap_audit = component_ap_readiness(
        project_code, product_id, commercial_inputs
    )
    result.update({
        "project_code": project_code,
        "product_id": product_id,
        "technical_result_status": technical.get("status"),
        "technical_result_revision": _revision(_paths(project_code, product_id)["technical_result"]),
        "component_ap_readiness": ap_audit["component_ap_readiness"],
        "components_requiring_rerun": ap_audit[
            "components_requiring_rerun"
        ],
    })
    return result


def calculate_saved_financial_plan(
    project_code: str,
    product_id: str,
    commercial_inputs: Mapping[str, Any],
) -> Dict[str, Any]:
    paths = _paths(project_code, product_id)
    technical = _technical(project_code, product_id)
    commercial = _commercial_context(project_code, product_id, commercial_inputs)
    atomic_write_json(paths["financial_input"], commercial)
    result = calculate_financial_plan(
        technical,
        commercial,
        _unit_data(project_code, product_id),
        _component_rows(technical, commercial),
        _investment_assets(project_code, product_id),
    )
    result["project_code"] = project_code
    result["product_id"] = product_id
    result["source_technical_result_revision"] = _revision(paths["technical_result"])
    result["source_commercial_input_revision"] = _revision(paths["financial_input"])
    result["calculated_at"] = datetime.now(timezone.utc).isoformat()
    result["save_path"] = str(paths["financial_result"])
    atomic_write_json(paths["financial_result"], result)
    _persist_financial_status(
        project_code, product_id, result["financial_status"], paths["financial_result"]
    )
    return result


def solve_saved_selling_price(
    project_code: str,
    product_id: str,
    commercial_inputs: Mapping[str, Any],
) -> Dict[str, Any]:
    paths = _paths(project_code, product_id)
    technical = _technical(project_code, product_id)
    commercial = _commercial_context(project_code, product_id, commercial_inputs)
    commercial["solve_selling_price"] = True
    atomic_write_json(paths["financial_input"], commercial)
    result = solve_selling_price(
        technical,
        commercial,
        _unit_data(project_code, product_id),
        _component_rows(technical, commercial),
        _investment_assets(project_code, product_id),
    )
    result.update({
        "project_code": project_code,
        "product_id": product_id,
        "source_technical_result_revision": _revision(paths["technical_result"]),
        "source_commercial_input_revision": _revision(paths["financial_input"]),
        "save_path": str(paths["solver_result"]),
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    })
    atomic_write_json(paths["solver_result"], result)
    financial_status = (
        (result.get("financial_result") or {}).get("financial_status")
        or ("blocked" if result.get("convergence_status") != "converged" else "ready")
    )
    _persist_financial_status(
        project_code, product_id, financial_status, paths["solver_result"]
    )
    return result


def get_saved_financial_result(project_code: str, product_id: str) -> Dict[str, Any]:
    paths = _paths(project_code, product_id)
    if paths["financial_result"].exists():
        return _read_json(paths["financial_result"], {})
    if paths["solver_result"].exists():
        return _read_json(paths["solver_result"], {})
    raise FileNotFoundError(
        "Financial result not found. Calculate the financial plan or solve the selling price first."
    )


def save_financial_reference_comparison(
    project_code: str,
    product_id: str,
    historical_values: Mapping[str, Any],
    explanations: Mapping[str, str] | None = None,
    acceptance: Mapping[str, bool] | None = None,
    validation_owner: str | None = None,
) -> Dict[str, Any]:
    paths = _paths(project_code, product_id)
    result = get_saved_financial_result(project_code, product_id)
    system_values = {
        "npv": result.get("npv") or result.get("achieved_npv"),
    }
    annual = result.get("annual_table") or result.get("annual_financial_table") or []
    for row in annual:
        period = row.get("period")
        for metric in (
            "selling_price", "sales", "material", "transport", "dl", "voh",
            "ebitda", "twc", "annual_cash_flow",
        ):
            system_values[f"{period}.{metric}"] = row.get(metric)
    comparison = build_historical_comparison(
        system_values,
        historical_values,
        explanations,
        acceptance,
        validation_owner,
    )
    comparison.update({
        "project_code": project_code,
        "product_id": product_id,
        "source_financial_result_revision": _revision(
            paths["financial_result"]
            if paths["financial_result"].exists()
            else paths["solver_result"]
        ),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "save_path": str(paths["comparison_result"]),
    })
    atomic_write_json(paths["comparison_result"], comparison)
    return comparison


MODEL_AUDIT = [
    {
        "existing_field_or_table": "commercial_costing_parameter.sop_date",
        "intended_use": "SOP year and Y-1..Y6 calendar derivation",
        "missing_field": None,
        "migration_needed": False,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "commercial_costing_parameter.quantity",
        "intended_use": "Single annual quantity",
        "missing_field": "year-specific quantity profile/rule",
        "migration_needed": True,
        "default_allowed": "flat only when explicitly selected",
    },
    {
        "existing_field_or_table": "productivity_scope and Y1/Y2/Y3 percentages",
        "intended_use": "customer productivity",
        "missing_field": "start, duration, Y4-Y6 and custom basis",
        "migration_needed": True,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "incoterm, payment_terms, delivery_frequency, delivery_on_platform",
        "intended_use": "AR and finished-goods inventory",
        "missing_field": "normalized days, platform lead/safety days",
        "migration_needed": True,
        "default_allowed": "stock defaults allowed and traced; payment days not defaulted",
    },
    {
        "existing_field_or_table": "commercial_costing_parameter.factoring_capability",
        "intended_use": "future AR financing/factoring treatment",
        "missing_field": "factoring fee, eligibility, timing and accounting policy",
        "migration_needed": True,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "component output payment_days/incoterm",
        "intended_use": "component AP and raw-material stock",
        "missing_field": "supplier zone relation override",
        "migration_needed": True,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "customer_input target_price and quotation currency",
        "intended_use": "initial price reference and financial reporting currency",
        "missing_field": "validated Y0 selling-price source and annual currency assumptions",
        "migration_needed": True,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "unit.company_tax_rate, FOH/DC, Fee/DC",
        "intended_use": "tax and technical cost handoff",
        "missing_field": "configurable FOH/Fee basis",
        "migration_needed": True,
        "default_allowed": "current basis is explicitly added_value_direct_cost",
    },
    {
        "existing_field_or_table": "MOST generic/specific CAPEX and tooling",
        "intended_use": "Y-1 investment and depreciation",
        "missing_field": "approval and commercial treatment records",
        "migration_needed": True,
        "default_allowed": "generic CAPEX five-year depreciation only",
    },
    {
        "existing_field_or_table": "products.roce_target_percent",
        "intended_use": "product profitability/ROCE/NPV target",
        "missing_field": (
            "approved interpretation mapping ROCE target to the NPV solver residual"
        ),
        "migration_needed": False,
        "default_allowed": False,
    },
    {
        "existing_field_or_table": "none",
        "intended_use": "annual indexation, TWC, cash flow, NPV and solver snapshots",
        "missing_field": "durable normalized financial plan tables",
        "migration_needed": True,
        "default_allowed": False,
    },
]


def get_financial_model_audit() -> List[Dict[str, Any]]:
    return MODEL_AUDIT
