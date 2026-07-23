"""Filesystem adapter between Choke workflow artifacts and the financial model."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from services.choke_financial_plan import (
    calculate_financial_plan,
    financial_readiness,
    solve_selling_price,
)
from services.project_data_paths import atomic_write_json, get_workflow_run_paths


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
    return commercial


def _component_rows(
    technical: Mapping[str, Any],
    commercial: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    terms = commercial.get("supplier_terms") or {}
    result = []
    for component in technical.get("component_breakdown") or []:
        if component.get("status") != "resolved":
            continue
        cid = str(component.get("component_id") or "")
        override = terms.get(cid, {}) if isinstance(terms, Mapping) else {}
        override = override if isinstance(override, Mapping) else {}
        offer = component.get("normalized_offer") or {}
        raw_offer = offer.get("raw_offer") or {}
        result.append({
            "component_id": cid,
            "supplier": (
                override.get("supplier")
                or offer.get("supplier_name")
                or raw_offer.get("supplier_name")
            ),
            "currency": component.get("currency") or offer.get("currency"),
            "base_cost_per_product": component.get("material_cost_per_piece"),
            "delivered_cost_per_product": component.get(
                "delivered_material_cost_per_piece"
            ),
            "payment_days": (
                override.get("payment_days")
                if "payment_days" in override
                else _first(offer, [
                    ["payment_days"],
                    ["payment_conditions_days"],
                    ["raw_offer", "payment_days"],
                    ["raw_offer", "payment_conditions_days"],
                ])
            ),
            "incoterm": (
                override.get("incoterm")
                or _first(offer, [["incoterm"], ["raw_offer", "incoterm"]])
            ),
            "zone_relation": override.get("zone_relation"),
            "source": override.get("source") or "saved_component_output",
        })
    return result


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
    result.update({
        "project_code": project_code,
        "product_id": product_id,
        "technical_result_status": technical.get("status"),
        "technical_result_revision": _revision(_paths(project_code, product_id)["technical_result"]),
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
    commercial["commercial_input_revision"] = _revision(paths["financial_input"])
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
        "existing_field_or_table": "component output payment_days/incoterm",
        "intended_use": "component AP and raw-material stock",
        "missing_field": "supplier zone relation override",
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
        "existing_field_or_table": "none",
        "intended_use": "annual indexation, TWC, cash flow, NPV and solver snapshots",
        "missing_field": "durable normalized financial plan tables",
        "migration_needed": True,
        "default_allowed": False,
    },
]


def get_financial_model_audit() -> List[Dict[str, Any]]:
    return MODEL_AUDIT

