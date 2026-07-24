import json
import hashlib
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from decimal import Decimal
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from services import choke_component_costing as component_costing
from services.choke_classification import classify_choke, classification_trace
from services.currency_service import normalize_currency_code, resolve_project_currency
from services.choke_financial_calculation import calculate_dl_voh
from services.choke_orchestrator import run_choke_orchestration
from services.choke_process_routing import build_choke_process_route
from services.costing_master_data_service import (
    get_master_manufacturing_strategy,
    get_master_unit_data,
)
from services.customer_input_schema import normalize_customer_input
from services.customer_input_extraction import (
    apply_resolution_to_customer_input,
    extract_customer_input_package,
    validate_resolved_customer_input,
)
from services.manufacturing_strategy import resolve_canonical_product
from services.agent_file_proxy_service import build_agent_file_url, verify_agent_pdf_url
from services.azure_blob_storage_service import refresh_blob_sas_url
from services.project_data_paths import (
    BACKEND_ROOT,
    COSTING_RUNS_DIR,
    CUSTOMER_INPUT_DIR,
    DATA_ROOT,
    PROJECT_ROOT,
    atomic_write_json,
    data_reference_candidates,
    ensure_workflow_storage_ready,
    get_legacy_workflow_state_paths,
    get_workflow_run_paths,
    portable_data_reference,
    resolve_customer_input_path,
    workflow_path_diagnostics,
)
from services.public_url_service import get_public_rest_base_url
from services.workspace_agent_client import (
    clean_agent_id,
    trigger_workspace_agent,
    workspace_agent_configuration,
)


BASE_DIR = BACKEND_ROOT
logger = logging.getLogger(__name__)
MOST_WRITEBACK_INSTRUCTION = (
    "Analyze only this work package using your native MOST JSON structure "
    "from most_cycle_output_template.json. "
    "After producing the complete native MOST JSON, invoke the configured MCP "
    "write-back action whose description is exactly "
    "'Save one final MOST operation JSON to the backend workflow'. "
    "Do not depend on or search for a generated runtime callable name because "
    "generated names may change between runs. "
    "Pass the exact project_code, product_id, work_package_id, most_scope_id, "
    "trigger_run_id, and raw_json containing the complete native MOST JSON object. "
    "Copy trigger_run_id exactly from this input and pass it unchanged to "
    "save_most_output; never invent or omit it. If trigger_run_id is absent, stop "
    "and return MOST_WRITEBACK_BLOCKED. Confirm the save_most_output success "
    "response before reporting completion. "
    "Do not perform tool discovery through database tools or unrelated write actions. "
    "Never call create_or_update_component, create_or_update_bom_line, "
    "save_component_output, save_component_costing_result, store_agent_json, "
    "or import_agent_costing_package as a fallback. "
    "If the correct MOST write-back action is not callable, stop immediately "
    "and return MOST_WRITEBACK_ACTION_NOT_BOUND without executing any other "
    "write action. "
    "Treat the save as successful only when success=true, raw_most_saved=true, "
    "normalized_most_saved=true, and the returned work_package_id matches the "
    "requested work_package_id. "
    "If the correct action returns an error, return the exact MCP error as "
    "MOST_WRITEBACK_FAILED."
)

COMPONENT_COSTING_INSTRUCTION = (
    "Cost only this component. Return one complete JSON and call save_component_output. "
    "A usable recommended_offer must contain unit_price as a JSON number, currency, "
    "pricing_unit (pc, kg, g, or m), supplier_name, payment_days as a JSON number, "
    "incoterm, origin, origin_zone, and ap_value_basis explicitly set to "
    "base_purchase_value or delivered_purchase_value, "
    "transport_cost with transport_basis, customs_cost with customs_basis, and "
    "forwarder_fee with forwarder_basis. Currency and every basis must be explicit. "
    "For legacy compatibility, unit_price_currency must equal currency and "
    "unit_price_basis must identify the same pricing_unit; transportation_cost_basis, "
    "customs_cost_basis, and forwarder_cost_basis must match transport_basis, "
    "customs_basis, and forwarder_basis respectively. "
    "If currency or pricing_unit cannot be determined, return status=blocked with exactly "
    "one explicit missing field and do not return a usable recommended_offer. "
    "If a supplier "
    "price was converted, also provide original_unit_price, original_currency, "
    "conversion_rate, conversion_rate_date, converted_unit_price, and "
    "converted_currency. Never omit currency or pricing_unit for a numerical price. "
    "Never infer offer currency from the production plant. Do not report a technical "
    "length or mass as a piece quantity. Use annual_product_quantity and the separate "
    "annual_purchasing_quantity with annual_purchasing_unit for supplier-volume pricing; "
    "never send metres to a supplier price basis expressed per kg."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_bom_lifecycle(event: str, **fields: Any) -> None:
    safe = {
        key: value
        for key, value in fields.items()
        if key not in {
            "access_token", "authorization", "input_text", "raw_json",
        }
    }
    logger.info(
        "bom_agent_lifecycle %s",
        json.dumps({"event": event, **safe}, default=str),
    )


def _id_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass

    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _safe_part(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{field_name} must not contain path separators.")
    return text


def _slug(value: Any, fallback: str = "item") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _relative(path: Path) -> str:
    return portable_data_reference(path)


def _public_base_url(request_base_url: Optional[str] = None) -> str:
    _load_env()
    return get_public_rest_base_url(request_base_url)


def _is_local_url(url: Optional[str]) -> bool:
    text = str(url or "").lower()
    return "://localhost" in text or "://127.0.0.1" in text or "://0.0.0.0" in text


def _drawing_file_url_from_path(
    drawing_file_path: Optional[str],
    request_base_url: Optional[str] = None,
) -> Optional[str]:
    if not drawing_file_path:
        return None
    parts = str(drawing_file_path).replace("\\", "/").split("/")
    try:
        upload_index = parts.index("uploads")
        project_code = parts[upload_index + 1]
        filename = parts[upload_index + 2]
    except (ValueError, IndexError):
        return None
    if not project_code or not filename or filename != Path(filename).name:
        return None
    base_url = _public_base_url(request_base_url)
    try:
        expiry_seconds = max(7200, int(os.getenv("AGENT_FILE_URL_EXPIRY_SECONDS", "14400")))
        return build_agent_file_url(
            base_url,
            project_code,
            filename,
            expiry_seconds=expiry_seconds,
        )
    except (RuntimeError, ValueError):
        return None


def _file_evidence(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return None
    stat = path.stat()
    return {
        "path": _relative(path),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _existing_bom_output_evidence(project_code: str, product_id: str) -> Dict[str, Any]:
    evidence = {
        "raw_bom": _file_evidence(_bom_raw_path(project_code, product_id)),
        "normalized_bom": _file_evidence(_bom_normalized_path(project_code, product_id)),
    }
    return {key: value for key, value in evidence.items() if value is not None}


def _write_json(path: Path, payload: Any) -> str:
    atomic_write_json(path, payload)
    return _relative(path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(project_code: str, product_id: str) -> Path:
    return get_workflow_run_paths(project_code, product_id)["run_dir"]


def _state_path(project_code: str, product_id: str) -> Path:
    return get_workflow_run_paths(project_code, product_id)["workflow_state_path"]


def _events_path(project_code: str, product_id: str) -> Path:
    return get_workflow_run_paths(project_code, product_id)["workflow_events_path"]


def _state_path_candidates(project_code: str, product_id: str) -> List[Path]:
    return [_state_path(project_code, product_id)]


def _bom_raw_path(project_code: str, product_id: str) -> Path:
    return get_workflow_run_paths(project_code, product_id)["raw_bom_path"]


def _bom_normalized_path(project_code: str, product_id: str) -> Path:
    return get_workflow_run_paths(project_code, product_id)["normalized_bom_path"]


def _component_output_path(project_code: str, product_id: str, component_id: str) -> Path:
    return (
        get_workflow_run_paths(project_code, product_id)["components_dir"]
        / f"{_safe_part(component_id, 'component_id')}.json"
    )


def _normalized_component_output_path(project_code: str, product_id: str, component_id: str) -> Path:
    return (
        _run_dir(project_code, product_id)
        / "components_normalized"
        / f"{_safe_part(component_id, 'component_id')}.json"
    ).resolve()


def _most_output_path(project_code: str, product_id: str, work_package_id: str) -> Path:
    return (
        get_workflow_run_paths(project_code, product_id)["most_dir"]
        / f"{_safe_part(work_package_id, 'work_package_id')}.json"
    )


def _normalized_most_output_path(project_code: str, product_id: str, work_package_id: str) -> Path:
    return (
        _run_dir(project_code, product_id)
        / "most_normalized"
        / f"{_safe_part(work_package_id, 'work_package_id')}.json"
    ).resolve()


def _load_customer_input(input_file: str) -> Dict[str, Any]:
    candidate = resolve_customer_input_path(input_file)
    payload = _read_json(candidate, {}) or {}
    payload["_input_file"] = _relative(candidate)
    return payload


def _resolve_customer_input_context(customer_input: Dict[str, Any]) -> Dict[str, Any]:
    resolution = extract_customer_input_package(
        customer_input,
        customer_input.get("attachment_manifest") or [],
    )
    resolved = apply_resolution_to_customer_input(customer_input, resolution)
    resolved["attachment_manifest"] = customer_input.get("attachment_manifest") or []
    resolved["customer_input_resolution"] = resolution
    resolved["resolved_customer_context"] = resolution
    return resolved


def _project_from_input(customer_input: Dict[str, Any]) -> Dict[str, Any]:
    validation = normalize_customer_input(customer_input)
    normalized = validation.get("customer_input") or {}
    project_code = normalized.get("project_code")
    generated_fields: Dict[str, Any] = {}
    if not project_code:
        project_code = f"RFQ-{_id_timestamp()}"
        normalized["project_code"] = project_code
        generated_fields["project_code"] = project_code
    product_id = (
        customer_input.get("workflow_product_id")
        or normalized.get("product_id")
        or normalized.get("part_number")
    )
    if not product_id:
        product_id = f"UNKNOWN-PART-{_id_timestamp()}"
        normalized["product_id"] = product_id
        generated_fields["product_id"] = product_id
        generated_fields["workflow_product_id"] = product_id
    normalized["workflow_product_id"] = product_id
    for passthrough_key in [
        "drawing_file_path",
        "drawing_file_url",
        "drawing_file_url_local",
        "drawing_agent_proxy_url",
        "drawing_access_mode",
        "drawing_blob_url",
        "drawing_sas_url",
        "drawing_azure_upload",
        "drawing_original_filename",
        "technical_fields_pending_bom",
        "technical_fields_extracted_from_bom",
        "technical_fields_from_bom",
        "drawing_number",
        "drawing_revision",
        "drawing_status",
        "attachment_manifest",
        "customer_input_resolution",
        "resolved_customer_context",
        "quotation_currency",
        "target_price_currency",
        "purchasing_currency",
        "delivery_country",
        "delivery_city",
        "quantity_by_year",
        "qmax",
        "annual_quantity_derivation",
    ]:
        if customer_input.get(passthrough_key) not in [None, "", [], {}]:
            normalized[passthrough_key] = customer_input.get(passthrough_key)
    missing_inputs = [
        item for item in (validation.get("missing_inputs") or [])
        if item not in {"project_code", "product_id or part_number"}
    ]
    return {
        "normalized_input": normalized,
        "project_code": project_code,
        "product_id": product_id,
        "missing_inputs": missing_inputs,
        "generated_fields": generated_fields,
    }


def _load_state(project_code: str, product_id: str) -> Dict[str, Any]:
    state = None
    for candidate in _state_path_candidates(project_code, product_id):
        state = _read_json(candidate, None)
        if isinstance(state, dict):
            break
    if isinstance(state, dict):
        state.setdefault("bom", {"status": "pending"})
        state.setdefault("components", {})
        state.setdefault("most", {})
        state.setdefault("missing_outputs", [])
        state.setdefault("errors", [])
        return state
    return {
        "project_code": project_code,
        "product_id": product_id,
        "input_file": None,
        "drawing_file_path": None,
        "status": "created",
        "current_step": "created",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "bom": {
            "status": "pending",
            "save_path": _relative(_bom_raw_path(project_code, product_id)),
            "trigger_result": None,
        },
        "components": {},
        "most": {},
        "missing_outputs": [],
        "errors": [],
    }


def _existing_state(project_code: str, product_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    candidate = _state_path(project_code, product_id)
    state = _read_json(candidate, None)
    if isinstance(state, dict):
        return state, candidate
    return None, None


def recover_legacy_workflow_state(
    project_code: str,
    product_id: str,
    apply: bool = True,
) -> Dict[str, Any]:
    canonical_paths = get_workflow_run_paths(project_code, product_id)
    canonical_state_path = canonical_paths["workflow_state_path"]
    if canonical_state_path.exists():
        return {
            "status": "canonical_found",
            "canonical_state_path": str(canonical_state_path),
            "legacy_state_paths": [],
            "migrated": False,
        }
    legacy_states = get_legacy_workflow_state_paths(project_code, product_id)
    if not legacy_states:
        return {
            "status": "not_found",
            "canonical_state_path": str(canonical_state_path),
            "legacy_state_paths": [],
            "migrated": False,
        }
    if len(legacy_states) > 1:
        return {
            "status": "split_state",
            "canonical_state_path": str(canonical_state_path),
            "legacy_state_paths": [str(path) for path in legacy_states],
            "migrated": False,
        }
    source_state = legacy_states[0]
    if apply:
        canonical_paths["run_dir"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source_state.parent,
            canonical_paths["run_dir"],
            dirs_exist_ok=True,
        )
    return {
        "status": "migrated" if apply else "recoverable",
        "canonical_state_path": str(canonical_state_path),
        "legacy_state_paths": [str(source_state)],
        "migrated": apply,
    }


def append_workflow_event(
    project_code: str,
    product_id: str,
    event: str,
    **details: Any,
) -> Dict[str, Any]:
    payload = {
        "timestamp": _now_iso(),
        "event": event,
        "project_code": project_code,
        "product_id": product_id,
        **{key: value for key, value in details.items() if value is not None},
    }
    path = _events_path(project_code, product_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    return payload


def _save_state(state: Dict[str, Any]) -> Dict[str, Any]:
    try:
        revision = int(state.get("workflow_revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    state["workflow_revision"] = revision + 1
    state["updated_at"] = _now_iso()
    _write_json(_state_path(state["project_code"], state["product_id"]), state)
    return state


def _is_resolved_bom_trigger_error(error: Any) -> bool:
    if not isinstance(error, dict) or error.get("stage") != "bom":
        return False
    trigger_result = error.get("trigger_result") or {}
    return (
        trigger_result.get("status") == "failed"
        or trigger_result.get("http_status") is not None
        or "trigger" in str(error.get("message") or "").lower()
    )


def _apply_bom_received_precedence(state: Dict[str, Any]) -> Dict[str, Any]:
    existing_bom = dict(state.get("bom") or {})
    trigger_result = dict(existing_bom.get("trigger_result") or {})
    if trigger_result:
        trigger_result["resolved_by_writeback"] = True
        trigger_result["effective_status"] = "received"

    active_errors = list(state.get("errors") or [])
    resolved_errors = [error for error in active_errors if _is_resolved_bom_trigger_error(error)]
    remaining_errors = [error for error in active_errors if not _is_resolved_bom_trigger_error(error)]
    historical_errors = list(state.get("historical_errors") or [])
    for error in resolved_errors:
        if error not in historical_errors:
            historical_errors.append(error)

    advanced_statuses = {
        "components_received",
        "most_triggering",
        "most_received",
        "calculated",
    }
    if state.get("status") not in advanced_statuses:
        state["status"] = "bom_received"
        state["current_step"] = "Step 2 External Component Costing Agent"
    state["bom_status"] = "received"
    state["retry_available"] = False
    state["retryable"] = False
    state["errors"] = remaining_errors
    state["historical_errors"] = historical_errors
    state["bom"] = {
        **existing_bom,
        "status": "received",
        "display_status": "received",
        "retryable": False,
        "retry_available": False,
        **({"trigger_result": trigger_result} if trigger_result else {}),
    }
    return state


def _apply_most_received_precedence(state: Dict[str, Any]) -> Dict[str, Any]:
    most = state.setdefault("most", {})
    required = list(
        state.get("required_most_work_package_ids")
        or (state.get("process_decomposition") or {}).get("required_work_package_ids")
        or []
    )
    for work_package_id in required:
        entry = most.get(work_package_id)
        if not isinstance(entry, dict) or entry.get("status") != "received":
            continue
        entry.update({
            "lifecycle_status": "most_received",
            "retryable": False,
            "failure_reason": None,
        })

    remaining = [
        work_package_id
        for work_package_id in required
        if (most.get(work_package_id) or {}).get("status") != "received"
    ]
    if required and not remaining:
        most.update({
            "status": "most_received",
            "lifecycle_status": "most_received",
            "retryable": False,
            "failure_reason": None,
        })
        state["status"] = "most_received"
        state["current_step"] = "Step 4 Final Calculation"
        state["missing_outputs"] = [
            item
            for item in state.get("missing_outputs") or []
            if not str(item).startswith("most:")
        ]
    return state


def get_workflow_state(project_code: str, product_id: str) -> Dict[str, Any]:
    path_diagnostics = workflow_path_diagnostics(project_code, product_id)
    logger.info("workflow status path: %s", json.dumps(path_diagnostics, default=str))
    state, state_path = _existing_state(project_code, product_id)
    raw_path = _bom_raw_path(project_code, product_id)
    raw_reference = (
        ((state or {}).get("bom") or {}).get("save_path")
        or _relative(raw_path)
    )
    raw_exists = any(path.exists() for path in data_reference_candidates(raw_reference))
    append_workflow_event(
        project_code,
        product_id,
        "get_status_called",
        workflow_state_path=str(state_path or _state_path(project_code, product_id).resolve()),
        workflow_state_exists=state is not None,
        raw_bom_exists=raw_exists,
        status_before=(state or {}).get("status"),
        **{
            key: value
            for key, value in path_diagnostics.items()
            if key not in {"project_code", "product_id"}
        },
    )
    if state is None:
        response = {
            "status": "not_found",
            "message": "Workflow state not found",
            "project_code": project_code,
            "product_id": product_id,
            "debug_url": f"/api/choke-workflow/debug/{project_code}/{product_id}",
            "debug_hint": f"Use /api/choke-workflow/debug/{project_code}/{product_id}",
            "canonical_data_root": path_diagnostics["resolved_data_root"],
            "canonical_workflow_state_path": path_diagnostics["resolved_workflow_state_path"],
            "process_id": path_diagnostics["process_id"],
            "cwd": path_diagnostics["cwd"],
            "git_commit": path_diagnostics.get("git_commit"),
            "workflow_path_version": path_diagnostics.get("workflow_path_version"),
            "persistent_storage_enabled": path_diagnostics.get("persistent_storage_enabled"),
            "startup_module": path_diagnostics.get("startup_module"),
        }
        if raw_exists:
            response["diagnostic_warning"] = "Raw BOM exists but state was not updated correctly."
        return response
    state.setdefault("bom", {"status": "pending"})
    state.setdefault("components", {})
    state.setdefault("most", {})
    state.setdefault("missing_outputs", [])
    state.setdefault("errors", [])
    status_before_wait_update = state.get("status")
    _apply_bom_callback_waiting_state(state)
    if state.get("status") != status_before_wait_update:
        _save_state(state)
        if state.get("status") == "bom_callback_timeout":
            append_workflow_event(
                project_code,
                product_id,
                "bom_callback_timeout",
                accepted_at=(state.get("bom") or {}).get("accepted_at"),
                elapsed_waiting_seconds=(
                    (state.get("bom") or {}).get("elapsed_waiting_seconds")
                ),
                callback_timeout_seconds=(
                    (state.get("bom") or {}).get("callback_timeout_seconds")
                ),
                status_before=status_before_wait_update,
                status_after="bom_callback_timeout",
            )
    normalized_reference = (
        (state.get("bom") or {}).get("normalized_path")
        or _relative(_bom_normalized_path(project_code, product_id))
    )
    normalized_exists = any(
        path.exists() for path in data_reference_candidates(normalized_reference)
    )
    if (state.get("bom") or {}).get("status") == "received":
        was_inconsistent = (
            state.get("status") != "bom_received"
            or (state.get("bom") or {}).get("retryable") is not False
            or state.get("retry_available") is not False
            or any(_is_resolved_bom_trigger_error(error) for error in state.get("errors") or [])
        )
        _apply_bom_received_precedence(state)
        if was_inconsistent:
            _save_state(state)
    elif raw_exists or normalized_exists:
        state["stale_previous_output"] = {
            "raw_bom_exists": raw_exists,
            "normalized_bom_exists": normalized_exists,
            "reason": "BOM files exist but no current write-back marked them received.",
            "trigger_run_id": (state.get("bom") or {}).get("trigger_run_id"),
        }
        state["diagnostic_warning"] = (
            "Previous BOM files exist but are not attributed to the current trigger run."
        )
    if (state.get("bom") or {}).get("status") != "received":
        state["missing_outputs"] = ["bom"]
    most_semantics_before = json.dumps(
        {
            "status": state.get("status"),
            "current_step": state.get("current_step"),
            "missing_outputs": state.get("missing_outputs"),
            "most": state.get("most"),
        },
        sort_keys=True,
        default=str,
    )
    _apply_most_received_precedence(state)
    most_semantics_after = json.dumps(
        {
            "status": state.get("status"),
            "current_step": state.get("current_step"),
            "missing_outputs": state.get("missing_outputs"),
            "most": state.get("most"),
        },
        sort_keys=True,
        default=str,
    )
    if most_semantics_after != most_semantics_before:
        _save_state(state)
    state["canonical_data_root"] = path_diagnostics["resolved_data_root"]
    state["canonical_workflow_state_path"] = path_diagnostics["resolved_workflow_state_path"]
    state["process_id"] = path_diagnostics["process_id"]
    state["cwd"] = path_diagnostics["cwd"]
    state["git_commit"] = path_diagnostics.get("git_commit")
    state["workflow_path_version"] = path_diagnostics.get("workflow_path_version")
    state["persistent_storage_enabled"] = path_diagnostics.get("persistent_storage_enabled")
    state["startup_module"] = path_diagnostics.get("startup_module")
    return state


def run_storage_self_test() -> Dict[str, Any]:
    ensure_workflow_storage_ready()
    suffix = uuid.uuid4().hex[:12]
    project_code = f"STORAGE-SELF-TEST-{suffix}"
    product_id = "SELF-TEST-PRODUCT"
    paths = get_workflow_run_paths(project_code, product_id)
    run_dir = paths["run_dir"]
    try:
        state = _load_state(project_code, product_id)
        state.update({
            "status": "starting",
            "input_file": "storage-self-test",
            "customer_input": {"project_code": project_code, "product_id": product_id},
        })
        _save_state(state)
        rest_write_path = str(paths["workflow_state_path"])

        mcp_read_state, mcp_read_state_path = _existing_state(project_code, product_id)
        writeback = save_bom_output(
            project_code=project_code,
            product_id=product_id,
            raw_json={"bom": []},
        )
        rest_read_state, rest_read_state_path = _existing_state(project_code, product_id)
        mcp_write_path = str(writeback.get("workflow_state_path") or "")
        mcp_read_path = str(mcp_read_state_path or "")
        rest_read_path = str(rest_read_state_path or "")
        identical = len({rest_write_path, mcp_read_path, mcp_write_path, rest_read_path}) == 1
        success = bool(
            identical
            and mcp_read_state
            and rest_read_state
            and writeback.get("success")
        )
        return {
            "success": success,
            "rest_write_path": rest_write_path,
            "mcp_read_path": mcp_read_path,
            "mcp_write_path": mcp_write_path,
            "rest_read_path": rest_read_path,
            "all_paths_identical": identical,
        }
    finally:
        canonical_root = get_workflow_run_paths(project_code, product_id)["run_dir"]
        if canonical_root == run_dir and DATA_ROOT in run_dir.parents and run_dir.exists():
            shutil.rmtree(run_dir)


def _trigger_status(trigger_result: Dict[str, Any]) -> str:
    if (trigger_result or {}).get("status") in {"accepted", "dry_run"}:
        return "triggered"
    return "failed"


def _json_input_text(instructions: List[str], payload: Dict[str, Any], save_address: str) -> str:
    return json.dumps(
        {
            "instructions": instructions,
            "save_address": save_address,
            "payload": payload,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def _trigger(agent_id_env: str, fallback_agent_name: str, input_text: str, conversation_key: str, idempotency_key: str, dry_run: bool) -> Dict[str, Any]:
    _load_env()
    return trigger_workspace_agent(
        agent_id=os.getenv(agent_id_env) or fallback_agent_name,
        access_token=os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN"),
        input_text=input_text,
        conversation_key=conversation_key,
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )


RETRYABLE_TRIGGER_HTTP_STATUSES = {409, 429, 500, 502, 503, 504}
RETRYABLE_TRIGGER_ERROR_TYPES = {"timeout", "connection_error"}


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _bom_callback_timeout_seconds() -> int:
    return _positive_int_env("BOM_CALLBACK_TIMEOUT_SECONDS", 900)


def get_bom_agent_configuration_health() -> Dict[str, Any]:
    _load_env()
    diagnostic = workspace_agent_configuration(
        agent_id=os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID"),
        access_token=os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN"),
    )
    return {
        "service": "choke-bom-workspace-agent",
        **diagnostic,
        "environment_variables": {
            "agent_id": "CHATGPT_CHOKE_BOM_AGENT_ID",
            "access_token": "CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN",
            "invocation_timeout": "WORKSPACE_AGENT_TRIGGER_TIMEOUT_SECONDS",
            "callback_timeout": "BOM_CALLBACK_TIMEOUT_SECONDS",
        },
        "callback_timeout_seconds": _bom_callback_timeout_seconds(),
        "execution_mode": "synchronous_request_path",
    }


def _safe_trigger_failure(trigger_result: Dict[str, Any]) -> Dict[str, Any]:
    error_type = str(trigger_result.get("error_type") or "")
    http_status = trigger_result.get("http_status")
    missing = trigger_result.get("missing_inputs") or []
    if missing:
        code = "bom_agent_configuration_missing"
        message = (
            "BOM Workspace Agent configuration is incomplete: "
            + ", ".join(str(item) for item in missing)
        )
    elif http_status in {401, 403}:
        code = f"workspace_agent_http_{http_status}"
        message = (
            "BOM Workspace Agent authorization was rejected."
            if http_status == 401
            else "BOM Workspace Agent access is forbidden."
        )
    elif error_type == "timeout":
        code = "workspace_agent_timeout"
        message = "BOM Workspace Agent invocation timed out."
    elif error_type == "invalid_trigger_url":
        code = "workspace_agent_invalid_endpoint"
        message = str(trigger_result.get("message") or "Invalid trigger endpoint.")
    elif error_type:
        code = f"workspace_agent_{error_type}"
        message = str(
            trigger_result.get("note")
            or "BOM Workspace Agent invocation failed."
        )
    else:
        code = "workspace_agent_trigger_failed"
        message = "BOM Workspace Agent invocation failed."
    return {
        "code": code,
        "message": message,
        "http_status": http_status,
        "retryable": _is_retryable_trigger_result(trigger_result),
    }


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_bom_callback_waiting_state(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    bom = dict(state.get("bom") or {})
    if bom.get("status") in {"received", "bom_normalized"}:
        return state
    lifecycle_status = bom.get("lifecycle_status")
    if lifecycle_status not in {
        "trigger_request_accepted",
        "awaiting_bom_callback",
        "bom_callback_timeout",
    }:
        return state

    accepted_at = _parse_iso_datetime(bom.get("accepted_at"))
    if accepted_at is None:
        return state
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timeout_seconds = int(
        bom.get("callback_timeout_seconds") or _bom_callback_timeout_seconds()
    )
    elapsed_seconds = max(0, int((current - accepted_at).total_seconds()))
    remaining_seconds = max(0, timeout_seconds - elapsed_seconds)
    timed_out = elapsed_seconds >= timeout_seconds
    bom.update({
        "accepted_at": accepted_at.isoformat(),
        "callback_timeout_seconds": timeout_seconds,
        "elapsed_waiting_seconds": elapsed_seconds,
        "callback_timeout_remaining_seconds": remaining_seconds,
        "retryable": timed_out,
        "retry_available": timed_out,
        "lifecycle_status": (
            "bom_callback_timeout" if timed_out else "awaiting_bom_callback"
        ),
    })
    state["bom"] = bom
    state["status"] = (
        "bom_callback_timeout" if timed_out else "awaiting_bom_callback"
    )
    state["bom_status"] = "failed" if timed_out else "triggered"
    bom["display_status"] = "failed" if timed_out else "triggered"
    state["current_step"] = "Step 1 BOM Agent"
    state["retryable"] = timed_out
    state["retry_available"] = timed_out
    state["missing_outputs"] = ["bom"]
    if timed_out:
        state["message"] = (
            "BOM callback timeout reached. A controlled retry is now available."
        )
    else:
        state["message"] = (
            "Agent request accepted and queued. Waiting for BOM output."
        )
    return state


def _trigger_backoff_seconds() -> List[float]:
    configured = os.getenv("WORKSPACE_AGENT_TRIGGER_BACKOFF_SECONDS", "5,15,30")
    values = []
    for item in configured.split(","):
        try:
            values.append(max(0.0, float(item.strip())))
        except ValueError:
            continue
    return values or [5.0, 15.0, 30.0]


def _is_retryable_trigger_result(result: Dict[str, Any]) -> bool:
    return (
        result.get("http_status") in RETRYABLE_TRIGGER_HTTP_STATUSES
        or result.get("error_type") in RETRYABLE_TRIGGER_ERROR_TYPES
    )


def _trigger_response_body(result: Dict[str, Any]) -> Any:
    body = (
        result.get("response")
        or result.get("error")
        or result.get("response_body")
        or result.get("note")
    )
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body
    return body


def _trigger_bom_agent_with_retries(
    project_code: str,
    product_id: str,
    input_text: str,
    dry_run: bool,
    status_before: Optional[str],
) -> Dict[str, Any]:
    _load_env()
    config = get_bom_agent_configuration_health()
    if not dry_run and config["status"] != "configured":
        result = {
            "status": "failed",
            "error_type": "configuration_error",
            "missing_inputs": config["missing_configuration"],
            "message": "BOM Workspace Agent configuration is incomplete.",
            "http_status": None,
            "retryable": False,
            "attempts": [],
            "configuration": config,
        }
        _log_bom_lifecycle(
            "agent_invocation_failed",
            project_code=project_code,
            product_id=product_id,
            error_code="bom_agent_configuration_missing",
            missing_configuration=config["missing_configuration"],
        )
        return result
    # One user action creates one Workspace Agent request. A later retry is an
    # explicit workflow action with a fresh drawing URL.
    max_attempts = 1
    backoffs = _trigger_backoff_seconds()
    attempts = []
    last_result: Dict[str, Any] = {}
    idempotency_key = f"{project_code}:{product_id}:sequential:bom:{uuid.uuid4()}"

    for attempt_number in range(1, max_attempts + 1):
        _log_bom_lifecycle(
            "before_agent_invocation",
            project_code=project_code,
            product_id=product_id,
            attempt_number=attempt_number,
            agent_id_masked=config.get("agent_id_masked"),
            token_present=config.get("token_present"),
            endpoint=config.get("endpoint"),
            timeout_seconds=config.get("invocation_timeout_seconds"),
            payload_bytes=len(input_text.encode("utf-8")),
        )
        try:
            result = _trigger(
                "CHATGPT_CHOKE_BOM_AGENT_ID",
                "",
                input_text,
                f"{project_code}:{product_id}:sequential:bom",
                idempotency_key,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.exception(
                "Unexpected BOM Workspace Agent invocation exception "
                "for %s/%s",
                project_code,
                product_id,
            )
            result = {
                "status": "failed",
                "http_status": None,
                "error_type": "execution_exception",
                "note": "BOM Workspace Agent invocation raised an exception.",
                "error": type(exc).__name__,
            }
        last_result = result or {}
        _log_bom_lifecycle(
            "agent_response_received",
            project_code=project_code,
            product_id=product_id,
            attempt_number=attempt_number,
            result_status=last_result.get("status"),
            http_status=last_result.get("http_status"),
            error_type=last_result.get("error_type"),
            elapsed_seconds=last_result.get("elapsed_seconds"),
            request_correlation_id=last_result.get("request_correlation_id"),
        )
        accepted = last_result.get("status") in {"accepted", "dry_run"}
        retryable = not accepted and _is_retryable_trigger_result(last_result)
        has_next_attempt = retryable and attempt_number < max_attempts
        next_retry_seconds = (
            backoffs[min(attempt_number - 1, len(backoffs) - 1)]
            if has_next_attempt
            else None
        )
        attempt = {
            "attempt_number": attempt_number,
            "timestamp": _now_iso(),
            "http_status": last_result.get("http_status"),
            "response_body": _trigger_response_body(last_result),
            "result_status": last_result.get("status"),
            "error_type": last_result.get("error_type"),
            "retryable": retryable,
            "next_retry_seconds": next_retry_seconds,
        }
        attempts.append(attempt)
        append_workflow_event(
            project_code,
            product_id,
            "bom_trigger_attempt",
            attempt_number=attempt_number,
            http_status=attempt.get("http_status"),
            retryable=retryable,
            next_retry_seconds=next_retry_seconds,
            status_before=status_before,
            result_status=attempt.get("result_status"),
        )
        if accepted:
            append_workflow_event(
                project_code,
                product_id,
                "bom_trigger_accepted",
                attempt_number=attempt_number,
                http_status=last_result.get("http_status"),
                status_before=status_before,
                status_after="trigger_request_accepted",
            )
            break
        if not has_next_attempt:
            append_workflow_event(
                project_code,
                product_id,
                "trigger_request_failed",
                attempt_number=attempt_number,
                http_status=last_result.get("http_status"),
                status_before=status_before,
                status_after="trigger_request_failed",
            )
            break
        time.sleep(next_retry_seconds or 0)

    final_retryable = (
        last_result.get("status") not in {"accepted", "dry_run"}
        and _is_retryable_trigger_result(last_result)
    )
    return {
        **last_result,
        "attempts": attempts,
        "retryable": final_retryable,
        "max_attempts": max_attempts,
    }


def _build_bom_trigger_payload(
    project_code: str,
    product_id: str,
    normalized_input: Dict[str, Any],
    request_base_url: Optional[str] = None,
    trigger_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    _load_env()
    project_code = _safe_part(project_code, "project_code")
    product_id = _safe_part(product_id, "product_id")
    trigger_run_id = str(trigger_run_id or uuid.uuid4()).strip()
    save_address = _relative(_bom_raw_path(project_code, product_id))
    drawing_file_path = normalized_input.get("drawing_file_path")
    generated_proxy_url = _drawing_file_url_from_path(drawing_file_path, request_base_url)
    sas_refresh = {"status": "not_available", "reason": "blob_metadata_missing"}
    if normalized_input.get("drawing_azure_upload") or normalized_input.get("drawing_blob_url"):
        sas_refresh = refresh_blob_sas_url(
            normalized_input.get("drawing_azure_upload"),
            normalized_input.get("drawing_blob_url"),
        )
    fresh_sas_url = sas_refresh.get("sas_url") if sas_refresh.get("status") == "generated" else None
    diagnostic_url = (
        normalized_input.get("drawing_file_url")
        if normalized_input.get("drawing_access_mode") == "diagnostic_url"
        else None
    )
    drawing_file_url = (
        generated_proxy_url
        or fresh_sas_url
        or diagnostic_url
    )
    drawing_access_mode = None
    if generated_proxy_url:
        drawing_access_mode = "backend_signed_proxy"
    elif fresh_sas_url:
        drawing_access_mode = "azure_blob_sas"
    elif diagnostic_url:
        drawing_access_mode = "diagnostic_url"
    else:
        drawing_access_mode = "missing"
    drawing_url_candidates = []
    if generated_proxy_url:
        drawing_url_candidates.append({
            "access_mode": "backend_signed_proxy",
            "url": generated_proxy_url,
            "fresh": True,
        })
    if fresh_sas_url:
        drawing_url_candidates.append({
            "access_mode": "azure_blob_sas",
            "url": fresh_sas_url,
            "fresh": True,
        })
    if diagnostic_url:
        drawing_url_candidates.append({
            "access_mode": "diagnostic_url",
            "url": diagnostic_url,
            "fresh": False,
        })
    warnings = []
    if drawing_file_path and not drawing_file_url:
        warnings.append("Uploaded drawing PDF exists but no drawing_file_url could be generated.")
    if drawing_access_mode != "azure_blob_sas" and drawing_file_url and _is_local_url(drawing_file_url):
        warnings.append(
            "BOM Agent triggered, but drawing_file_url is local. "
            "Use ngrok/Azure PUBLIC_BASE_URL for the agent to access the PDF."
        )
    if drawing_access_mode != "azure_blob_sas" and not os.getenv("PUBLIC_BASE_URL"):
        warnings.append(
            "PUBLIC_BASE_URL is not configured. Workspace Agents cannot access local uploaded PDFs "
            "unless file attachment support is used."
        )

    writeback_instruction = (
        "Analyze the drawing according to your permanent agent instructions. "
        "After producing the complete BOM JSON, call save_bom_output exactly once "
        "with the exact project_code, product_id, trigger_run_id, and raw_json. "
        "The backend accepts completion only from this correlated write-back."
    )
    choke_classification = classify_choke(normalized_input, {})
    payload = {
        "project_code": project_code,
        "product_id": product_id,
        "trigger_run_id": trigger_run_id,
        **classification_trace(choke_classification),
        "drawing_file_url": drawing_file_url,
        "drawing_agent_proxy_url": generated_proxy_url,
        "drawing_reference": normalized_input.get("drawing_reference"),
        "save_address": save_address,
        "instruction": writeback_instruction,
    }
    input_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return {
        "payload": payload,
        "input_text": input_text,
        "save_address": save_address,
        "drawing_file_path": drawing_file_path,
        "drawing_file_url": drawing_file_url,
        "drawing_agent_proxy_url": generated_proxy_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_blob_url": normalized_input.get("drawing_blob_url"),
        "drawing_sas_url": fresh_sas_url,
        "drawing_sas_refresh": sas_refresh,
        "drawing_url_candidates": drawing_url_candidates,
        "drawing_url_is_local": _is_local_url(drawing_file_url),
        "warnings": warnings,
        "trigger_run_id": trigger_run_id,
    }


def _validate_and_select_drawing_url(bom_trigger: Dict[str, Any]) -> Dict[str, Any]:
    validations = []
    selected = None
    for candidate in bom_trigger.get("drawing_url_candidates") or []:
        check = verify_agent_pdf_url(candidate.get("url"))
        result = {
            "access_mode": candidate.get("access_mode"),
            "fresh": candidate.get("fresh"),
            "validation": check,
        }
        validations.append(result)
        if check.get("success"):
            selected = {
                "access_mode": candidate.get("access_mode"),
                "url": candidate.get("url"),
                "validation": check,
            }
            break
    if selected:
        bom_trigger["drawing_file_url"] = selected["url"]
        bom_trigger["drawing_access_mode"] = selected["access_mode"]
        bom_trigger["payload"]["drawing_file_url"] = selected["url"]
        bom_trigger["input_text"] = json.dumps(
            bom_trigger["payload"],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return {
        "success": selected is not None,
        "method": "GET",
        "selected": selected,
        "candidate_validations": validations,
        "rejection_reason": None if selected else "no_accessible_drawing_url",
    }


def build_bom_trigger_preview(
    input_file: str,
    request_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    customer_input = _load_customer_input(input_file)
    project = _project_from_input(customer_input)
    normalized_input = project["normalized_input"]
    trigger_payload = _build_bom_trigger_payload(
        project["project_code"],
        project["product_id"],
        normalized_input,
        request_base_url=request_base_url,
    )
    return {
        "project_code": project["project_code"],
        "product_id": project["product_id"],
        **trigger_payload,
    }


def test_bom_agent_trigger(
    project_code: str,
    product_id: str,
    drawing_file_url: str,
    drawing_reference: Optional[str] = None,
) -> Dict[str, Any]:
    _load_env()
    normalized_input = {
        "drawing_file_url": str(drawing_file_url or "").strip(),
        "drawing_reference": str(drawing_reference or "").strip() or None,
        "drawing_access_mode": "diagnostic_url",
    }
    trigger = _build_bom_trigger_payload(project_code, product_id, normalized_input)
    result = trigger_workspace_agent(
        agent_id=str(os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID") or "").strip(),
        access_token=str(os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN") or "").strip(),
        input_text=trigger["input_text"],
        conversation_key=f"diagnostic:{project_code}:{product_id}",
        idempotency_key=f"diagnostic:{project_code}:{product_id}:{uuid.uuid4()}",
        dry_run=False,
    )
    return {
        "endpoint": result.get("endpoint"),
        "method": result.get("method"),
        "agent_id_prefix": result.get("agent_id_prefix"),
        "token_present": result.get("token_present"),
        "payload_size": result.get("payload_size"),
        "http_status": result.get("http_status"),
        "response": result.get("response") or result.get("error") or result.get("message"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "request_correlation_id": result.get("request_correlation_id"),
        "status": result.get("status"),
        "error_type": result.get("error_type"),
    }


def start_real_choke_workflow(
    input_file: str,
    dry_run: bool = False,
    request_base_url: Optional[str] = None,
    workflow_request_id: Optional[str] = None,
) -> Dict[str, Any]:
    _log_bom_lifecycle(
        "trigger_execution_entered",
        input_file=input_file,
        workflow_request_id=workflow_request_id,
        execution_mode="synchronous_request_path",
        dry_run=dry_run,
    )
    ensure_workflow_storage_ready()
    customer_input = _load_customer_input(input_file)
    input_reference = customer_input.get("_input_file")
    customer_input = _resolve_customer_input_context(customer_input)
    customer_input["_input_file"] = input_reference
    _write_json(resolve_customer_input_path(input_reference), {
        key: value for key, value in customer_input.items() if key != "_input_file"
    })
    project = _project_from_input(customer_input)
    normalized_input = project["normalized_input"]
    project_code = project["project_code"]
    product_id = project["product_id"]
    if project.get("generated_fields"):
        input_path = resolve_customer_input_path(customer_input["_input_file"])
        stored_input = _read_json(input_path, {}) or {}
        stored_input.update(project["generated_fields"])
        stored_input.setdefault("technical_fields_pending_bom", True)
        _write_json(input_path, stored_input)
        customer_input.update(stored_input)

    choke_classification = classify_choke(normalized_input, {})
    manufacturing_strategy = get_master_manufacturing_strategy(
        normalized_input.get("product_line"),
        normalized_input.get("product"),
        normalized_input.get("customer_delivery_zone"),
    )
    manufacturing_strategy = {
        **manufacturing_strategy,
        **classification_trace(choke_classification),
    }
    unit_data = get_master_unit_data(manufacturing_strategy.get("production_plant"))
    path_diagnostics = workflow_path_diagnostics(project_code, product_id)
    logger.info("workflow start path: %s", json.dumps(path_diagnostics, default=str))
    run_dir = _run_dir(project_code, product_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    trigger_run_id = str(uuid.uuid4())
    trigger_requested_at = _now_iso()
    bom_trigger = _build_bom_trigger_payload(
        project_code,
        product_id,
        normalized_input,
        request_base_url=request_base_url,
        trigger_run_id=trigger_run_id,
    )
    input_text = bom_trigger["input_text"]
    save_address = bom_trigger["save_address"]
    existing_state = _load_state(project_code, product_id)
    status_before = existing_state.get("status")
    stale_previous_output = _existing_bom_output_evidence(project_code, product_id)
    validation_attempt = {
        "attempt_id": str(uuid.uuid4()),
        "stage": "drawing_access_validation",
        "status": "started",
        "timestamp": trigger_requested_at,
        "method": "GET",
    }
    state = existing_state
    state.update({
        "workflow_request_id": workflow_request_id,
        "input_file": customer_input["_input_file"],
        "drawing_file_path": normalized_input.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_agent_proxy_url": bom_trigger.get("drawing_agent_proxy_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "status": "validating_drawing_access",
        "bom_status": "triggering",
        "current_step": "Step 1 BOM Agent",
        "manufacturing_strategy": manufacturing_strategy,
        "choke_classification": choke_classification,
        **classification_trace(choke_classification),
        "unit_data": unit_data,
        "customer_input": normalized_input,
        "customer_input_resolution": customer_input.get("customer_input_resolution") or {},
        "resolved_customer_context": customer_input.get("resolved_customer_context") or {},
        "components": state.get("components") or {},
        "most": state.get("most") or {},
        "process_decomposition": state.get("process_decomposition"),
        "missing_outputs": ["bom"],
        "warnings": bom_trigger.get("warnings") or [],
    })
    state["bom"] = {
        **dict(state.get("bom") or {}),
        "status": "validating_drawing_access",
        "trigger_run_id": trigger_run_id,
        "trigger_requested_at": trigger_requested_at,
        "save_path": save_address,
        "drawing_file_path": bom_trigger.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_agent_proxy_url": bom_trigger.get("drawing_agent_proxy_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "warnings": bom_trigger.get("warnings") or [],
        "trigger_result": None,
        "trigger_attempts": [validation_attempt],
        "retryable": False,
        "input_text": input_text,
        "stale_previous_output": stale_previous_output or None,
    }
    _save_state(state)
    _log_bom_lifecycle(
        "workflow_created",
        project_code=project_code,
        product_id=product_id,
        workflow_request_id=workflow_request_id,
        workflow_state_path=str(_state_path(project_code, product_id).resolve()),
        status="validating_drawing_access",
    )
    persisted_state, persisted_path = _existing_state(project_code, product_id)
    if persisted_state is None or persisted_path is None or not persisted_path.exists():
        raise RuntimeError(
            "Canonical workflow state persistence verification failed; Agent was not triggered."
        )
    append_workflow_event(
        project_code,
        product_id,
        "workflow_start_requested",
        input_file=customer_input["_input_file"],
        drawing_file_path=normalized_input.get("drawing_file_path"),
        drawing_file_url=bom_trigger.get("drawing_file_url"),
        status_before=status_before,
        **{
            key: value
            for key, value in path_diagnostics.items()
            if key not in {"project_code", "product_id"}
        },
    )

    pdf_url_check = {"success": True, "skipped": bool(dry_run), "method": "GET"}
    if not dry_run:
        pdf_url_check = _validate_and_select_drawing_url(bom_trigger)
        validation_attempt.update({
            "status": "accepted" if pdf_url_check.get("success") else "failed",
            "completed_at": _now_iso(),
            "selected_access_mode": (
                (pdf_url_check.get("selected") or {}).get("access_mode")
            ),
            "candidate_validations": pdf_url_check.get("candidate_validations") or [],
        })
        persisted_state["bom"]["trigger_attempts"] = [validation_attempt]
        persisted_state["bom"]["pdf_url_check"] = pdf_url_check
        if not pdf_url_check.get("success"):
            persisted_state["status"] = "bom_trigger_failed"
            persisted_state["bom_status"] = "failed"
            persisted_state["current_step"] = "Step 1 BOM Agent"
            persisted_state["missing_outputs"] = ["bom"]
            persisted_state["bom"] = {
                **dict(persisted_state.get("bom") or {}),
                "status": "bom_trigger_failed",
                "display_status": "failed",
                "retryable": True,
                "pdf_url_check": pdf_url_check,
            }
            persisted_state.setdefault("errors", []).append({
                "stage": "bom_pdf_access",
                "message": "No current drawing URL passed PDF validation; Workspace Agent was not triggered.",
                "details": pdf_url_check,
            })
            _save_state(persisted_state)
            _log_bom_lifecycle(
                "agent_invocation_failed",
                project_code=project_code,
                product_id=product_id,
                error_code="drawing_access_validation_failed",
                status_after="bom_trigger_failed",
            )
            return {
                "message": "Drawing access validation failed; retry will generate fresh URLs.",
                "status": "bom_trigger_failed",
                "retryable": True,
                "state": persisted_state,
                "canonical_workflow_state_path": str(persisted_path),
                "workflow_state_exists_before_trigger": True,
                "data_root": str(DATA_ROOT),
                "pdf_url_check": pdf_url_check,
            }
        persisted_state.update({
            "drawing_file_url": bom_trigger.get("drawing_file_url"),
            "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
            "status": "trigger_request_sending",
            "bom_status": "triggering",
        })
        persisted_state["bom"] = {
            **dict(persisted_state.get("bom") or {}),
            "status": "trigger_request_sending",
            "lifecycle_status": "trigger_request_sending",
            "drawing_file_url": bom_trigger.get("drawing_file_url"),
            "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
            "input_text": bom_trigger.get("input_text"),
        }
        _save_state(persisted_state)
    input_text = bom_trigger["input_text"]
    trigger_result = _trigger_bom_agent_with_retries(
        project_code=project_code,
        product_id=product_id,
        input_text=input_text,
        dry_run=dry_run,
        status_before=status_before,
    )
    accepted = trigger_result.get("status") in {"accepted", "dry_run"}
    retryable_failure = not accepted and trigger_result.get("retryable") is True
    workflow_status = (
        "awaiting_bom_callback"
        if accepted
        else "trigger_request_failed"
    )
    bom_status = (
        "awaiting_bom_callback"
        if accepted
        else "trigger_request_failed"
    )
    latest_state, _ = _existing_state(project_code, product_id)
    state = latest_state or persisted_state
    if (state.get("bom") or {}).get("status") != "received":
        state["status"] = workflow_status
        state["bom_status"] = "triggered" if accepted else "failed"
        state["current_step"] = "Step 1 BOM Agent"
        combined_attempts = [validation_attempt, *(trigger_result.get("attempts") or [])]
        state["bom"] = {
            **dict(state.get("bom") or {}),
            "status": bom_status,
            "display_status": "triggered" if accepted else "failed",
            "lifecycle_status": "awaiting_bom_callback" if accepted else bom_status,
            "trigger_request_status": (
                "trigger_request_accepted" if accepted else "trigger_request_failed"
            ),
            "accepted_at": _now_iso() if accepted else None,
            "callback_timeout_seconds": _bom_callback_timeout_seconds(),
            "trigger_result": trigger_result,
            "trigger_attempts": combined_attempts,
            "retryable": False if accepted else retryable_failure,
            "retry_available": False if accepted else retryable_failure,
            "pdf_url_check": pdf_url_check,
        }
        if not accepted:
            safe_failure = _safe_trigger_failure(trigger_result)
            state["bom"]["safe_error"] = safe_failure
            state.setdefault("errors", []).append({
                "stage": "bom",
                "error_code": safe_failure["code"],
                "message": safe_failure["message"],
                "trigger_result": trigger_result,
            })
            _log_bom_lifecycle(
                "agent_invocation_failed",
                project_code=project_code,
                product_id=product_id,
                error_code=safe_failure["code"],
                http_status=safe_failure["http_status"],
                retryable=safe_failure["retryable"],
                status_after="trigger_request_failed",
            )
    else:
        _apply_bom_received_precedence(state)
    _save_state(state)
    event_paths = {
        "input_file": customer_input["_input_file"],
        "workflow_state_path": str(_state_path(project_code, product_id).resolve()),
        "run_dir": str(run_dir.resolve()),
        "status_after": state.get("status"),
    }
    append_workflow_event(project_code, product_id, "workflow_started", **event_paths)
    append_workflow_event(
        project_code,
        product_id,
        "bom_agent_triggered",
        **event_paths,
        bom_status=bom_status,
        trigger_attempts=trigger_result.get("attempts") or [],
        raw_bom_save_path=save_address,
    )
    return {
        "message": (
            "Agent request accepted and queued. Waiting for BOM output."
            if accepted
            else "BOM Agent trigger request failed."
        ),
        "state": state,
        "trigger_report": {
            "bom": state["bom"],
            "components_triggered": [],
            "most_triggered": [],
        },
        "path_diagnostics": path_diagnostics,
        "status": state.get("status"),
        "canonical_workflow_state_path": str(persisted_path),
        "workflow_state_exists_before_trigger": True,
        "data_root": str(DATA_ROOT),
    }


def retry_bom_agent(project_code: str, product_id: str) -> Dict[str, Any]:
    _log_bom_lifecycle(
        "retry_request_received",
        project_code=project_code,
        product_id=product_id,
    )
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise FileNotFoundError("Workflow state not found. Start the workflow before retrying the BOM Agent.")
    status_before = state.get("status")
    existing_bom = dict(state.get("bom") or {})
    if existing_bom.get("status") == "received":
        return {
            "status": "bom_received",
            "project_code": project_code,
            "product_id": product_id,
            "skipped": True,
            "reason": "bom_already_received",
            "state": state,
        }
    _apply_bom_callback_waiting_state(state)
    existing_bom = dict(state.get("bom") or {})
    if (
        existing_bom.get("lifecycle_status") == "awaiting_bom_callback"
        and ((existing_bom.get("trigger_result") or {}).get("status") == "accepted")
    ):
        return {
            "status": "awaiting_bom_callback",
            "project_code": project_code,
            "product_id": product_id,
            "skipped": True,
            "reason": "bom_callback_wait_still_active",
            "message": "Agent request is already accepted and still within the callback timeout.",
            "retry_available": False,
            "state": state,
        }
    append_workflow_event(
        project_code,
        product_id,
        "retry_bom_requested",
        status_before=status_before,
        workflow_state_path=str(_state_path(project_code, product_id).resolve()),
    )

    customer_input = dict(state.get("customer_input") or {})
    for key in [
        "drawing_file_path",
        "drawing_access_mode",
        "drawing_blob_url",
        "drawing_sas_url",
        "drawing_azure_upload",
    ]:
        value = state.get(key) or existing_bom.get(key)
        if value not in [None, ""]:
            customer_input[key] = value
    customer_input.setdefault("project_code", project_code)
    customer_input.setdefault("workflow_product_id", product_id)
    customer_input.setdefault("product_id", product_id)
    trigger_run_id = str(uuid.uuid4())
    trigger_requested_at = _now_iso()
    bom_trigger = _build_bom_trigger_payload(
        project_code,
        product_id,
        customer_input,
        trigger_run_id=trigger_run_id,
    )
    if not bom_trigger.get("drawing_file_url"):
        raise ValueError("BOM Agent retry requires drawing_file_url in workflow state or customer_input.")

    validation_attempt = {
        "attempt_id": str(uuid.uuid4()),
        "stage": "drawing_access_validation",
        "status": "started",
        "timestamp": _now_iso(),
        "method": "GET",
    }
    state["status"] = "validating_drawing_access"
    state["bom_status"] = "triggering"
    state["current_step"] = "Step 1 BOM Agent"
    state["missing_outputs"] = ["bom"]
    state["bom"] = {
        **existing_bom,
        "status": "validating_drawing_access",
        "trigger_run_id": trigger_run_id,
        "trigger_requested_at": trigger_requested_at,
        "trigger_result": None,
        "trigger_attempts": [validation_attempt],
        "retryable": False,
        "drawing_agent_proxy_url": bom_trigger.get("drawing_agent_proxy_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
    }
    _save_state(state)
    pdf_url_check = _validate_and_select_drawing_url(bom_trigger)
    validation_attempt.update({
        "status": "accepted" if pdf_url_check.get("success") else "failed",
        "completed_at": _now_iso(),
        "selected_access_mode": (
            (pdf_url_check.get("selected") or {}).get("access_mode")
        ),
        "candidate_validations": pdf_url_check.get("candidate_validations") or [],
    })
    if not pdf_url_check.get("success"):
        state["status"] = "bom_trigger_failed"
        state["bom_status"] = "failed"
        state["bom"] = {
            **dict(state.get("bom") or {}),
            "status": "bom_trigger_failed",
            "display_status": "failed",
            "retryable": True,
            "pdf_url_check": pdf_url_check,
            "trigger_attempts": [validation_attempt],
        }
        state.setdefault("errors", []).append({
            "stage": "bom_pdf_access",
            "message": "Drawing access validation failed during retry.",
            "details": pdf_url_check,
        })
        _save_state(state)
        return {
            "status": "bom_trigger_failed",
            "retryable": True,
            "project_code": project_code,
            "product_id": product_id,
            "bom": state["bom"],
            "trigger_attempts": state["bom"]["trigger_attempts"],
            "state": state,
        }

    state["status"] = "trigger_request_sending"
    state["bom_status"] = "triggering"
    state["bom"] = {
        **dict(state.get("bom") or {}),
        "status": "trigger_request_sending",
        "lifecycle_status": "trigger_request_sending",
        "pdf_url_check": pdf_url_check,
        "trigger_attempts": [validation_attempt],
    }
    _save_state(state)
    input_text = bom_trigger["input_text"]
    trigger_result = _trigger_bom_agent_with_retries(
        project_code=project_code,
        product_id=product_id,
        input_text=input_text,
        dry_run=False,
        status_before=status_before,
    )
    accepted = trigger_result.get("status") == "accepted"
    retryable_failure = not accepted and trigger_result.get("retryable") is True
    state["status"] = "awaiting_bom_callback" if accepted else "trigger_request_failed"
    state["bom_status"] = "triggered" if accepted else "failed"
    state["current_step"] = "Step 1 BOM Agent"
    state["missing_outputs"] = ["bom"]
    state["bom"] = {
        **dict(state.get("bom") or {}),
        "status": (
            "awaiting_bom_callback"
            if accepted
            else "trigger_request_failed"
        ),
        "display_status": "triggered" if accepted else "failed",
        "trigger_request_status": (
            "trigger_request_accepted" if accepted else "trigger_request_failed"
        ),
        "accepted_at": _now_iso() if accepted else None,
        "callback_timeout_seconds": _bom_callback_timeout_seconds(),
        "retryable": False if accepted else retryable_failure,
        "retry_available": False if accepted else retryable_failure,
        "trigger_result": trigger_result,
        "trigger_attempts": [validation_attempt, *(trigger_result.get("attempts") or [])],
        "input_text": input_text,
        "save_path": existing_bom.get("save_path") or bom_trigger.get("save_address"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "pdf_url_check": pdf_url_check,
        "lifecycle_status": (
            "awaiting_bom_callback" if accepted else "trigger_request_failed"
        ),
    }
    if not accepted:
        safe_failure = _safe_trigger_failure(trigger_result)
        state["bom"]["safe_error"] = safe_failure
        state.setdefault("errors", []).append({
            "stage": "bom",
            "error_code": safe_failure["code"],
            "message": safe_failure["message"],
            "trigger_result": trigger_result,
        })
        _log_bom_lifecycle(
            "agent_invocation_failed",
            project_code=project_code,
            product_id=product_id,
            error_code=safe_failure["code"],
            http_status=safe_failure["http_status"],
            retryable=safe_failure["retryable"],
            status_after="trigger_request_failed",
        )
    else:
        _log_bom_lifecycle(
            "agent_response_received",
            project_code=project_code,
            product_id=product_id,
            http_status=trigger_result.get("http_status"),
            status_after="awaiting_bom_callback",
        )
    _save_state(state)
    return {
        "status": state["status"],
        "project_code": project_code,
        "product_id": product_id,
        "bom": state["bom"],
        "trigger_attempts": state["bom"]["trigger_attempts"],
        "state": state,
        "message": (
            "Agent request accepted and queued. Waiting for BOM output."
            if accepted
            else "BOM Agent trigger request failed."
        ),
    }


def _extract_component_list(raw_bom: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_bom, list):
        return [item for item in raw_bom if isinstance(item, dict)]
    if not isinstance(raw_bom, dict):
        return []
    candidates = [
        raw_bom.get("components"),
        raw_bom.get("normalized_components"),
        raw_bom.get("bom"),
        raw_bom.get("line_items"),
        raw_bom.get("bill_of_material"),
        raw_bom.get("bill_of_materials"),
        raw_bom.get("materials"),
        raw_bom.get("material_lines"),
        raw_bom.get("nomenclature"),
        raw_bom.get("tableau_nomenclature"),
        raw_bom.get("liste_matieres"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in [
                "components",
                "lines",
                "line_items",
                "items",
                "materials",
                "materiaux",
                "matieres",
                "bom",
                "nomenclature",
            ]:
                nested = candidate.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    for value in raw_bom.values():
        if isinstance(value, dict):
            nested = _extract_component_list(value)
            if nested:
                return nested
    return []


def _component_text(component: Dict[str, Any]) -> str:
    return json.dumps(component, ensure_ascii=False, default=str).lower()


def _component_type(component: Dict[str, Any]) -> str:
    return str(
        component.get("component_type")
        or component.get("material_type")
        or component.get("component_family")
        or component.get("family")
        or component.get("type")
        or component.get("product")
        or component.get("poste")
        or component.get("component_name")
        or component.get("product_designation")
        or component.get("produit_designation")
        or component.get("produit")
        or component.get("designation")
        or component.get("description")
        or component.get("specification")
        or ""
    )


def _normalized_component_identity_text(component: Dict[str, Any]) -> str:
    values = [
        component.get("component_id"),
        component.get("component_type"),
        component.get("component_family"),
        component.get("poste"),
        component.get("component_name"),
        component.get("product_designation"),
        component.get("produit_designation"),
        component.get("product"),
        component.get("designation"),
        component.get("description"),
        component.get("specification"),
    ]
    text = " ".join(str(value) for value in values if value not in [None, ""])
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


_COMPONENT_ID_ALIASES = {
    "ferrite": "ferrite_core",
    "core": "ferrite_core",
    "copper_wire": "magnet_wire",
    "enameled_wire": "magnet_wire",
    "enamelled_wire": "magnet_wire",
    "tin_plating": "lead_tinning",
    "lead_tin_plating": "lead_tinning",
    "tinning": "lead_tinning",
    "etamage": "lead_tinning",
    "adhesive": "glue",
    "epoxy": "glue",
    "colle": "glue",
}

_COMPONENT_FAMILY_LABELS = {
    "ferrite_core": "ferrite",
    "magnet_wire": "wire",
    "lead_tinning": "tinning",
    "glue": "glue",
}

_NUMERIC_TOKEN_RE = re.compile(r"^-?\d+(\.\d+)?$")

_NOT_RETAINED_MARKERS = [
    "non retenue",
    "non retenu",
    "not retained",
    "not used",
    "non utilise",
    "not selected",
    "non selectionne",
    "not required",
    "non requis",
    "excluded",
    "sans objet",
    "not applicable",
]


def _classify_component_material(text: str) -> Optional[str]:
    """Infer the canonical material family from free-text identity fields."""
    if any(term in text for term in ["glue", "adhesive", "epoxy", "colle"]):
        return "glue"
    if any(term in text for term in ["tinning", "tin plating", "etamage", "etain"]):
        return "lead_tinning"
    if any(term in text for term in [
        "magnet wire",
        "copper wire",
        "enameled",
        "enamelled",
        "wire",
        "fil cuivre",
        "fil bobine",
        " fil ",
    ]):
        return "magnet_wire"
    if any(term in text for term in ["ferrite", "core", "magnetic"]):
        return "ferrite_core"
    return None


def _is_blank_or_numeric(value: Any) -> bool:
    if value in (None, ""):
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(_NUMERIC_TOKEN_RE.match(text))


def _component_id(component: Dict[str, Any], index: int) -> str:
    text_classification = _classify_component_material(_normalized_component_identity_text(component))
    explicit = (
        component.get("component_id")
        or component.get("component_code")
        or component.get("component_reference")
        or component.get("part_number")
        or component.get("id")
    )
    if explicit not in (None, ""):
        explicit_id = _slug(explicit, f"component_{index}")
        aliased = _COMPONENT_ID_ALIASES.get(explicit_id)
        if aliased:
            return aliased
        # An explicit id/component_id that is just a bare row number (e.g. "id": 1)
        # or that isn't a recognized alias carries no reliable material information
        # by itself. Prefer text-derived classification from descriptive fields,
        # and only fall back to the raw explicit id when the text is inconclusive.
        if text_classification:
            return text_classification
        return explicit_id
    if text_classification:
        return text_classification
    return _slug(_component_type(component), f"component_{index}")


def _component_quantity(component: Dict[str, Any]) -> Optional[float]:
    for key in ["quantity_per_product", "quantity_per_assembly", "quantity", "qty", "quantite"]:
        value = component.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            value = value.get("value", value.get("quantity"))
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            continue
    return None


def _component_is_excluded(component: Dict[str, Any]) -> bool:
    """A BOM line must not be treated as a required external component when the
    BOM itself marks it as not used (zero quantity, "not retained", excluded, ...)."""
    quantity = _component_quantity(component)
    if quantity is not None and quantity == 0:
        return True
    classification = str(
        component.get("classification")
        or component.get("component_classification")
        or ""
    ).strip().lower()
    if classification in {"internal", "excluded", "not_required", "not_selected"}:
        return True
    text_fields = [
        component.get("specification"),
        component.get("status"),
        component.get("validation_status"),
        component.get("presence"),
        component.get("note"),
        component.get("notes"),
        component.get("remark"),
        component.get("comment"),
    ]
    text = " ".join(str(value) for value in text_fields if value not in (None, ""))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    if any(marker in text for marker in _NOT_RETAINED_MARKERS):
        return True
    return False


def _component_display_name(
    component: Dict[str, Any],
    component_id: str,
    component_type: str,
    route_type: Optional[str],
) -> str:
    """Priority order: component_name, product_designation, description,
    designation, name, then component_id as final fallback. Never returns a
    bare numeric row index when a descriptive field is available."""
    candidates = [
        component.get("component_name"),
        component.get("product_designation"),
        component.get("produit_designation"),
        component.get("description"),
        component.get("designation"),
        component.get("name"),
        component.get("component"),
        component.get("product"),
        component.get("poste"),
    ]
    for candidate in candidates:
        if not _is_blank_or_numeric(candidate):
            return str(candidate).strip()
    return component_type or route_type or component_id or "component"


def _component_family(component_id: str, component: Dict[str, Any]) -> Optional[str]:
    """Derive the material family from the canonical component id first
    (which is itself description-derived), only falling back to raw BOM
    family/category fields for components outside the known material set.
    This prevents a mislabeled raw "component_family" (e.g. a glue line
    tagged "ferrite" because it fixes a ferrite core) from leaking through."""
    label = _COMPONENT_FAMILY_LABELS.get(component_id)
    if label:
        return label
    return (
        component.get("category")
        or component.get("component_category")
        or component.get("component_family")
        or component.get("family")
    )


def _external_costing_route(component: Dict[str, Any]) -> Optional[str]:
    explicit_route = str(component.get("costing_route") or "").strip().lower()
    if explicit_route in {"not_external_agent", "internal_costing"}:
        return None
    explicit_family = str(
        component.get("external_component_type")
        or component.get("component_family")
        or component.get("category")
        or ""
    ).strip().lower()
    if explicit_route == "external_component_costing_agent":
        return explicit_family or _component_type(component).strip().lower() or "external_component"
    canonical_id = _component_id(component, 1)
    if canonical_id == "glue":
        text = _component_text(component)
        if any(term in text for term in [
            "present",
            "required",
            "to_confirm",
            "to confirm",
            "a confirmer",
            "ambigu",
            "impossible de conclure",
        ]):
            return "glue"
        return None
    if canonical_id == "lead_tinning":
        return "tin"
    if canonical_id == "magnet_wire":
        return "enameled_wire"
    if canonical_id == "ferrite_core":
        return "ferrite"
    text = _component_text(component)
    if any(term in text for term in ["complete choke", "full choke", "assembly"]):
        return None
    if any(term in text for term in ["ferrite core", "ferrite", "magnetic component", "magnetic"]):
        return "ferrite"
    if any(term in text for term in ["magnet wire", "copper wire", "enameled_wire", "enameled wire", "enamelled wire", "wire"]):
        return "enameled_wire"
    if any(term in text for term in ["lead tin", "tin plating", "tinning"]):
        return "tin"
    if any(term in text for term in ["glue", "adhesive", "epoxy"]):
        relevance = component.get("costing_relevance")
        status = str(component.get("status") or component.get("presence") or "").lower()
        if relevance is True or status in {"present", "required", "to_confirm", "to confirm"}:
            return "glue"
    return None


def normalize_bom(
    raw_bom: Dict[str, Any],
    customer_input: Optional[Dict[str, Any]] = None,
    choke_classification: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    components = []
    external_components = []
    seen_component_ids = set()
    for index, component in enumerate(_extract_component_list(raw_bom), start=1):
        component_id = _component_id(component, index)
        if component_id in seen_component_ids:
            continue
        seen_component_ids.add(component_id)
        component_type = _component_type(component)
        route_type = _external_costing_route(component)
        excluded_not_required = _component_is_excluded(component)
        if excluded_not_required and route_type:
            logger.info(
                "normalize_bom: excluding component_id=%s from external costing "
                "(quantity is zero or BOM marks it as not retained/used)",
                component_id,
            )
            route_type = None
        is_external_costing = bool(route_type)
        normalized = {
            "component_id": component_id,
            "component_type": component_type or route_type or "component",
            "component": _component_display_name(component, component_id, component_type, route_type),
            "category": _component_family(component_id, component) or route_type,
            "quantity_per_product": (
                component.get("quantity_per_product")
                or component.get("quantity_per_assembly")
                or component.get("quantity")
                or component.get("qty")
                or component.get("quantite")
            ),
            "component_definition": component,
            "costing_route": "external_component_costing_agent" if is_external_costing else "not_external_agent",
            "external_component_type": route_type,
            "status": component.get("status") or component.get("validation_status"),
            "certainty": component.get("certainty") or component.get("confidence"),
            "excluded_not_required": excluded_not_required,
        }
        components.append(normalized)
        if is_external_costing:
            external_components.append(normalized)
    classification = choke_classification or classify_choke(customer_input or {}, raw_bom)
    return {
        "status": "normalized",
        "components": components,
        "external_components": external_components,
        "choke_classification": classification,
        **classification_trace(classification),
        "raw_bom": raw_bom,
    }


def _scalar_from_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ["value", "text", "name", "number", "reference", "status"]:
            nested = value.get(key)
            if nested not in [None, "", [], {}]:
                return _scalar_from_value(nested)
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            nested = _scalar_from_value(item)
            if nested not in [None, "", [], {}]:
                return nested
        return None
    return value if value not in [None, ""] else None


def _find_recursive_value(data: Any, keys: List[str], skip_keys: Optional[set] = None) -> Any:
    key_set = {key.lower() for key in keys}
    skip = skip_keys or set()
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in key_set:
                scalar = _scalar_from_value(value)
                if scalar not in [None, "", [], {}]:
                    return scalar
        for key, value in data.items():
            if str(key).lower() in skip:
                continue
            nested = _find_recursive_value(value, keys, skip_keys)
            if nested not in [None, "", [], {}]:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _find_recursive_value(item, keys, skip_keys)
            if nested not in [None, "", [], {}]:
                return nested
    return None


# Container keys that hold individual BOM line items (mirrors _extract_component_list's
# candidates). The assembly-level product identity must never be resolved from a
# descriptive field that actually belongs to one of these per-line entries.
_BOM_LINE_CONTAINER_KEYS = {
    "components",
    "normalized_components",
    "bom",
    "line_items",
    "bill_of_material",
    "bill_of_materials",
    "materials",
    "material_lines",
    "nomenclature",
    "tableau_nomenclature",
    "liste_matieres",
    "lines",
    "items",
    "materiaux",
    "matieres",
}


def extract_bom_technical_fields(raw_bom: Any) -> Dict[str, Any]:
    quote_information = raw_bom.get("quote_information") if isinstance(raw_bom, dict) else {}
    quote_information = quote_information if isinstance(quote_information, dict) else {}

    def quote_value(keys: List[str]) -> Any:
        for key in keys:
            value = _scalar_from_value(quote_information.get(key))
            if value not in [None, "", [], {}]:
                return value
        return None

    root = raw_bom if isinstance(raw_bom, dict) else {}
    source = root.get("source") if isinstance(root.get("source"), dict) else {}
    product_metadata = []
    for container_name in ["summary", "product_metadata", "metadata", "product_info"]:
        container = root.get(container_name)
        if isinstance(container, dict):
            product_metadata.append(container)

    product_evidence = []
    for container in [quote_information, root, source, *product_metadata]:
        for key in [
            "product",
            "product_name",
            "product_type",
            "product_description",
            "product_designation",
            "drawing_title",
            "designation",
            "title",
        ]:
            value = _scalar_from_value(container.get(key))
            if value not in [None, "", [], {}] and str(value) not in product_evidence:
                product_evidence.append(str(value))

    product_name = (
        quote_value(["product_name", "product", "product_description"])
        or _scalar_from_value(source.get("product"))
        or _scalar_from_value(source.get("product_name"))
        or _scalar_from_value(source.get("drawing_title"))
        or next(iter(product_evidence), None)
        or _find_recursive_value(
            raw_bom,
            [
                "product_name",
                "product",
                "product_designation",
                "product_description",
                "choke_type",
                "product_type",
                "drawing_title",
            ],
            skip_keys=_BOM_LINE_CONTAINER_KEYS,
        )
    )
    part_number = (
        quote_value(["part_number", "part_no", "customer_part_number", "product_reference"])
        or _scalar_from_value(source.get("part_no"))
        or _scalar_from_value(source.get("part_number"))
        or _find_recursive_value(raw_bom, [
            "part_number",
            "part_no",
            "customer_part_number",
            "product_reference",
            "product_reference_number",
            "drawing_part_number",
            "item_number",
        ])
    )
    product_resolution = resolve_canonical_product(
        "Chokes",
        evidence_values=product_evidence or [product_name],
        part_number=part_number,
    )

    return {
        "product_name": product_name,
        "canonical_product": product_resolution.get("canonical_product"),
        "product_candidates": product_resolution.get("candidates") or [],
        "product_resolution": product_resolution,
        "part_number": part_number,
        "drawing_number": quote_value(["drawing_number", "drawing_no", "drawing_reference"]) or _find_recursive_value(raw_bom, [
            "drawing_number",
            "drawing_no",
            "drawing_reference",
            "plan_number",
            "drawing",
        ]),
        "drawing_revision": quote_value(["drawing_revision", "drawing_rev", "revision", "rev"]) or _find_recursive_value(raw_bom, [
            "drawing_revision",
            "drawing_rev",
            "revision",
            "rev",
        ]),
        "drawing_status": quote_value(["drawing_status", "drawing_availability", "drawing_validation_status"]) or _find_recursive_value(raw_bom, [
            "drawing_status",
            "drawing_availability",
            "drawing_validation_status",
            "plan_status",
        ]),
    }


def _customer_input_path_from_state(state: Dict[str, Any]) -> Optional[Path]:
    input_file = state.get("input_file")
    if not input_file:
        return None
    try:
        return resolve_customer_input_path(input_file)
    except (FileNotFoundError, ValueError):
        return None


def _refresh_resolved_customer_context(state: Dict[str, Any]) -> Dict[str, Any]:
    path = _customer_input_path_from_state(state)
    stored = _read_json(path, {}) if path else {}
    source = {**(stored or {}), **(state.get("customer_input") or {})}
    resolution = extract_customer_input_package(
        source,
        source.get("attachment_manifest") or [],
    )
    resolved = apply_resolution_to_customer_input(source, resolution)
    resolved.setdefault("workflow_product_id", state.get("product_id"))
    resolved["attachment_manifest"] = source.get("attachment_manifest") or []
    resolved["customer_input_resolution"] = resolution
    resolved["resolved_customer_context"] = resolution
    state["customer_input"] = resolved
    state["customer_input_resolution"] = resolution
    state["resolved_customer_context"] = resolution
    if path:
        _write_json(path, resolved)
    return validate_resolved_customer_input(resolution)


def _update_customer_input_from_bom(
    state: Dict[str, Any],
    extracted: Dict[str, Any],
) -> Dict[str, Any]:
    path = _customer_input_path_from_state(state)
    current = dict(state.get("customer_input") or {})
    if path:
        current = {**current, **(_read_json(path, {}) or {})}
    updates: Dict[str, Any] = {}

    product_name = extracted.get("product_name")
    canonical_product = extracted.get("canonical_product")
    part_number = extracted.get("part_number")
    if canonical_product:
        updates["product"] = canonical_product
        updates["product_name"] = canonical_product
        if product_name and product_name != canonical_product:
            updates["bom_product_name_evidence"] = product_name
    elif product_name:
        updates["product_name"] = product_name
    if extracted.get("product_candidates"):
        updates["product_candidates"] = extracted["product_candidates"]
    if part_number:
        if not current.get("part_number"):
            updates["part_number"] = part_number
    for field_name in ["drawing_number", "drawing_revision", "drawing_status"]:
        if extracted.get(field_name):
            updates[field_name] = extracted[field_name]

    extracted_values = {
        key: value
        for key, value in extracted.items()
        if value not in [None, "", [], {}]
    }
    if extracted_values:
        current["technical_fields_extracted_from_bom"] = True
        current["technical_fields_pending_bom"] = False
        current["technical_fields_from_bom"] = extracted_values
        current.setdefault("workflow_product_id", state.get("product_id"))
    if updates:
        current.update(updates)
    current.setdefault("workflow_product_id", state.get("product_id"))
    state["customer_input"] = current
    if extracted_values or updates:
        if path:
            _write_json(path, current)

    return {
        "status": "updated" if updates else ("extracted" if extracted_values else "no_fields_found"),
        "path": _relative(path) if path else None,
        "updates": updates,
        "extracted": extracted_values,
    }


def _refresh_master_data_for_state(state: Dict[str, Any]) -> Dict[str, Any]:
    customer_input = state.get("customer_input") or {}
    choke_classification = state.get("choke_classification") or classify_choke(
        customer_input,
        (_load_normalized_bom(state.get("project_code"), state.get("product_id")) or {}).get("raw_bom")
        if state.get("project_code") and state.get("product_id") else {},
    )
    state["choke_classification"] = choke_classification
    state.update(classification_trace(choke_classification))
    product_line = customer_input.get("product_line") or "Chokes"
    product = customer_input.get("product")
    delivery_zone = customer_input.get("customer_delivery_zone")
    product_resolution = resolve_canonical_product(
        product_line,
        evidence_values=[product] if product else [],
        part_number=None,
    )
    canonical_product = product_resolution.get("canonical_product")
    state["product_resolution"] = product_resolution
    state["canonical_product"] = canonical_product
    if canonical_product:
        customer_input["canonical_product"] = canonical_product
    else:
        customer_input.pop("canonical_product", None)

    if product and product_resolution.get("status") != "resolved":
        manufacturing_strategy = {
            "status": "product_not_mapped",
            "product_received": product,
            "available_product_candidates": product_resolution.get("candidates") or [],
            "message": "Product is saved but no manufacturing strategy mapping was found.",
            **classification_trace(choke_classification),
        }
        state["manufacturing_strategy"] = manufacturing_strategy
        state["unit_data"] = get_master_unit_data(None)
        state["production_plant"] = None
        customer_input.pop("production_plant", None)
        state["customer_input"] = customer_input
        return manufacturing_strategy

    manufacturing_strategy = get_master_manufacturing_strategy(
        product_line,
        canonical_product or product,
        delivery_zone,
    )
    manufacturing_strategy = {
        **manufacturing_strategy,
        **classification_trace(choke_classification),
    }
    production_plant = manufacturing_strategy.get("production_plant")
    unit_data = get_master_unit_data(production_plant)
    state["manufacturing_strategy"] = manufacturing_strategy
    state["unit_data"] = unit_data
    state["production_plant"] = production_plant
    if production_plant:
        customer_input["production_plant"] = production_plant
    else:
        customer_input.pop("production_plant", None)
    state["customer_input"] = customer_input
    if manufacturing_strategy.get("status") == "found":
        return {
            "status": "refreshed",
            "manufacturing_strategy_source": manufacturing_strategy.get("source"),
            "production_plant": production_plant,
            "unit_data_source": unit_data.get("source"),
        }
    missing = list(manufacturing_strategy.get("missing_inputs") or [])
    return {
        "status": "missing_strategy",
        "missing_inputs": missing,
        "message": manufacturing_strategy.get("message") or (
            "Manufacturing strategy needs product and delivery_zone."
        ),
    }


def save_bom_output(
    project_code: str,
    product_id: str,
    raw_json: Dict[str, Any],
    trigger_run_id: Optional[str] = None,
    allow_create_without_start: bool = False,
) -> Dict[str, Any]:
    _log_bom_lifecycle(
        "callback_received",
        project_code=project_code,
        product_id=product_id,
        trigger_run_id_present=bool(trigger_run_id),
        raw_json_type=type(raw_json).__name__,
    )
    raw_keys = list(raw_json.keys()) if isinstance(raw_json, dict) else []
    correlation_id = None
    if isinstance(raw_json, dict):
        correlation_id = raw_json.get("request_correlation_id") or raw_json.get("correlation_id")
        if correlation_id is not None:
            correlation_id = str(correlation_id)[:128]
    initial_debug = {
        "timestamp": _now_iso(),
        "tool": "save_bom_output",
        "project_code_received": project_code,
        "product_id_received": product_id,
        "raw_json_type": type(raw_json).__name__,
        "raw_json_top_level_keys": raw_keys,
        "data_root": str(DATA_ROOT),
        "request_correlation_id": correlation_id,
        "trigger_run_id_received": trigger_run_id,
    }
    logger.info("save_bom_output called: %s", json.dumps(initial_debug, default=str))

    state_path: Optional[Path] = None
    try:
        _safe_part(project_code, "project_code")
        _safe_part(product_id, "product_id")
        if not isinstance(raw_json, dict):
            raise ValueError("raw_json must be a JSON object.")
    except (TypeError, ValueError) as exc:
        error_response = {
            "success": False,
            "status": "failed",
            "error_code": "invalid_writeback_payload",
            "message": str(exc),
            "project_code": project_code,
            "product_id": product_id,
            "workflow_state_path": None,
        }
        logger.warning("save_bom_output validation failed: %s", json.dumps(error_response, default=str))
        try:
            append_workflow_event(
                project_code,
                product_id,
                "save_bom_output_validation_failed",
                **initial_debug,
                error=str(exc),
            )
        except (OSError, ValueError):
            pass
        return error_response

    try:
        existing_state, existing_state_path = _existing_state(project_code, product_id)
        recovery = None
        if existing_state is None:
            recovery = recover_legacy_workflow_state(project_code, product_id, apply=True)
            if recovery.get("status") == "split_state":
                return {
                    "success": False,
                    "status": "failed",
                    "error_code": "split_state",
                    "message": "Multiple legacy workflow states exist; repair is required before write-back.",
                    "project_code": project_code,
                    "product_id": product_id,
                    "workflow_state_path": recovery.get("canonical_state_path"),
                    "legacy_state_paths": recovery.get("legacy_state_paths"),
                }
            if recovery.get("migrated"):
                existing_state, existing_state_path = _existing_state(project_code, product_id)
        if existing_state is None and not allow_create_without_start:
            missing_response = {
                "success": False,
                "status": "failed",
                "error_code": "workflow_state_not_found",
                "message": "Workflow state not found. Start the workflow before BOM write-back.",
                "project_code": project_code,
                "product_id": product_id,
                "workflow_state_path": str(_state_path(project_code, product_id)),
            }
            append_workflow_event(
                project_code,
                product_id,
                "save_bom_output_failed",
                error_code=missing_response["error_code"],
                message=missing_response["message"],
                workflow_state_path=missing_response["workflow_state_path"],
            )
            return missing_response
        expected_trigger_run_id = str(
            ((existing_state or {}).get("bom") or {}).get("trigger_run_id") or ""
        ).strip()
        received_trigger_run_id = str(trigger_run_id or "").strip()
        if expected_trigger_run_id and not received_trigger_run_id:
            response = {
                "success": False,
                "status": "rejected",
                "error_code": "missing_trigger_run_id",
                "message": "BOM callback is missing trigger_run_id for the current run.",
                "project_code": project_code,
                "product_id": product_id,
                "workflow_state_path": str(_state_path(project_code, product_id)),
            }
            append_workflow_event(
                project_code,
                product_id,
                "save_bom_output_rejected",
                error_code=response["error_code"],
                expected_trigger_run_id=expected_trigger_run_id,
            )
            return response
        if expected_trigger_run_id and received_trigger_run_id != expected_trigger_run_id:
            stale_callback = {
                "received_at": _now_iso(),
                "received_trigger_run_id": received_trigger_run_id,
                "expected_trigger_run_id": expected_trigger_run_id,
                "raw_json_top_level_keys": raw_keys,
            }
            existing_state.setdefault("stale_bom_callbacks", []).append(stale_callback)
            _save_state(existing_state)
            response = {
                "success": False,
                "status": "stale_callback",
                "error_code": "trigger_run_id_mismatch",
                "message": "BOM callback belongs to a different trigger run.",
                "project_code": project_code,
                "product_id": product_id,
                "expected_trigger_run_id": expected_trigger_run_id,
                "received_trigger_run_id": received_trigger_run_id,
                "workflow_state_path": str(_state_path(project_code, product_id)),
            }
            append_workflow_event(
                project_code,
                product_id,
                "stale_bom_callback_recorded",
                expected_trigger_run_id=expected_trigger_run_id,
                received_trigger_run_id=received_trigger_run_id,
            )
            return response
        status_before = (existing_state or {}).get("status")
        append_workflow_event(
            project_code,
            product_id,
            "bom_received",
            trigger_run_id=received_trigger_run_id or expected_trigger_run_id,
            status_before=status_before,
            status_after="bom_received",
        )
        run_dir = _run_dir(project_code, product_id).resolve()
        state_path = _state_path(project_code, product_id).resolve()
        raw_path = _bom_raw_path(project_code, product_id).resolve()
        normalized_path = _bom_normalized_path(project_code, product_id).resolve()
        canonical_diagnostics = workflow_path_diagnostics(project_code, product_id)
        path_debug = {
            **initial_debug,
            **{
                key: value
                for key, value in canonical_diagnostics.items()
                if key not in {"project_code", "product_id"}
            },
            "resolved_run_directory": str(run_dir),
            "workflow_state_path": str(state_path),
            "raw_bom_path": str(raw_path),
            "normalized_bom_path": str(normalized_path),
            "state_exists_before": existing_state is not None,
            "state_status_before": status_before,
        }
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_called",
            **path_debug,
        )
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_paths_resolved",
            **path_debug,
        )
        logger.info("save_bom_output paths resolved: %s", json.dumps(path_debug, default=str))

        _write_json(raw_path, raw_json)
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_raw_saved",
            raw_bom_path=str(raw_path),
            raw_bom_exists=raw_path.exists(),
            request_correlation_id=correlation_id,
        )

        choke_classification = classify_choke(
            (existing_state or {}).get("customer_input") or {},
            raw_json,
        )
        normalized = normalize_bom(
            raw_json,
            (existing_state or {}).get("customer_input") or {},
            choke_classification,
        )
        _write_json(normalized_path, normalized)
        component_ids = [
            item.get("component_id")
            for item in normalized.get("components") or []
            if isinstance(item, dict) and item.get("component_id")
        ]
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_normalized_saved",
            normalized_bom_path=str(normalized_path),
            normalized_bom_exists=normalized_path.exists(),
            component_ids=component_ids,
            request_correlation_id=correlation_id,
        )
        append_workflow_event(
            project_code,
            product_id,
            "bom_normalized",
            trigger_run_id=received_trigger_run_id or expected_trigger_run_id,
            normalized_bom_path=str(normalized_path),
            component_ids=component_ids,
            status_after="bom_normalized",
        )

        state = existing_state if isinstance(existing_state, dict) else _load_state(project_code, product_id)
        if existing_state is None and allow_create_without_start:
            state["writeback_created_state_without_start"] = True
            state.setdefault("warnings", []).append(
                "BOM write-back created workflow state because no started workflow state was found."
            )
        extracted = extract_bom_technical_fields(raw_json)
        customer_input_update = _update_customer_input_from_bom(state, extracted)
        if customer_input_update.get("status") in {"updated", "extracted"}:
            input_path = _customer_input_path_from_state(state)
            if input_path:
                state["customer_input"] = _read_json(input_path, state.get("customer_input") or {}) or {}
        choke_classification = classify_choke(state.get("customer_input") or {}, raw_json)
        state["choke_classification"] = choke_classification
        state.update(classification_trace(choke_classification))
        normalized["choke_classification"] = choke_classification
        normalized.update(classification_trace(choke_classification))
        _write_json(normalized_path, normalized)
        master_data_refresh = _refresh_master_data_for_state(state)
        state["process_decomposition"] = build_choke_process_route(
            state.get("customer_input") or {},
            normalized,
            choke_classification,
        )
        existing_bom = dict(state.get("bom") or {})
        state["bom"] = {
            **existing_bom,
            "status": "received",
            "callback_status": "bom_received",
            "normalization_status": "bom_normalized",
            "lifecycle_status": "bom_normalized",
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
            "received_for_trigger_run_id": (
                received_trigger_run_id or existing_bom.get("trigger_run_id")
            ),
            "raw_bom_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "normalized_bom_sha256": hashlib.sha256(normalized_path.read_bytes()).hexdigest(),
            "retryable": False,
        }
        _apply_bom_received_precedence(state)
        state["technical_fields_extracted_from_bom"] = bool(customer_input_update.get("extracted"))
        state["technical_fields_from_bom"] = customer_input_update.get("extracted") or {}
        state["customer_input_update"] = customer_input_update
        state["master_data_refresh"] = master_data_refresh
        required_external_components = _required_external_components(normalized)
        state["required_external_component_ids"] = [
            item["component_id"] for item in required_external_components
        ]
        state["missing_outputs"] = list(dict.fromkeys(
            f"component:{item['component_id']}"
            for item in required_external_components
            if isinstance(item, dict) and item.get("component_id")
        ))
        _save_state(state)

        completed_debug = {
            "workflow_state_path": str(state_path),
            "raw_bom_path": str(raw_path),
            "raw_bom_exists": raw_path.exists(),
            "normalized_bom_path": str(normalized_path),
            "normalized_bom_exists": normalized_path.exists(),
            "component_ids": component_ids,
            "state_status_after": state.get("status"),
            "bom_status_after": (state.get("bom") or {}).get("status"),
            "missing_outputs_after": state.get("missing_outputs") or [],
            "request_correlation_id": correlation_id,
        }
        logger.info("save_bom_output completed: %s", json.dumps(completed_debug, default=str))
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_state_updated",
            state_exists_before=existing_state is not None,
            state_status_before=status_before,
            **completed_debug,
        )
        # Kept for compatibility with existing diagnostics.
        append_workflow_event(
            project_code,
            product_id,
            "save_bom_output_completed",
            state_exists_before=existing_state is not None,
            state_status_before=status_before,
            **completed_debug,
        )
        debug = {
            **path_debug,
            **completed_debug,
            "state_exists_before_save": existing_state is not None,
            "state_status_before_save": status_before,
            "state_status_after_save": state.get("status"),
            "raw_bom_saved": raw_path.exists(),
            "normalized_bom_saved": normalized_path.exists(),
            "missing_outputs_after_save": state.get("missing_outputs") or [],
            "errors": [],
        }
        return {
            "success": True,
            "status": "saved",
            "tool": "save_bom_output",
            "project_code": project_code,
            "product_id": product_id,
            "workflow_state_path": str(state_path),
            "state_exists_before": existing_state is not None,
            "state_status_before": status_before,
            "state_status_after": state.get("status"),
            "raw_bom_saved": raw_path.exists(),
            "normalized_bom_saved": normalized_path.exists(),
            "component_ids": component_ids,
            "state": state,
            "normalized_bom": normalized,
            "debug": debug,
            "state_merge": {
                "existing_state_found": existing_state_path is not None,
                "existing_state_path": str(existing_state_path) if existing_state_path else None,
                "saved_state_path": str(state_path),
            },
        }
    except Exception as exc:
        error_response = {
            "success": False,
            "status": "failed",
            "error_code": "bom_writeback_failed",
            "message": str(exc),
            "project_code": project_code,
            "product_id": product_id,
            "workflow_state_path": str(state_path) if state_path else None,
        }
        logger.exception("save_bom_output failed: %s", json.dumps(error_response, default=str))
        try:
            append_workflow_event(
                project_code,
                product_id,
                "save_bom_output_failed",
                error_code=error_response["error_code"],
                message=error_response["message"],
                workflow_state_path=error_response["workflow_state_path"],
                request_correlation_id=correlation_id,
            )
        except (OSError, ValueError):
            pass
        return error_response


def _load_normalized_bom(project_code: str, product_id: str) -> Dict[str, Any]:
    normalized = _read_json(_bom_normalized_path(project_code, product_id), None)
    if isinstance(normalized, dict):
        return normalized
    raw = _read_json(_bom_raw_path(project_code, product_id), None)
    if raw is None:
        raise FileNotFoundError("BOM output is not available yet.")
    normalized = normalize_bom(raw)
    _write_json(_bom_normalized_path(project_code, product_id), normalized)
    return normalized


def _state_bom_path_candidates(state: Dict[str, Any], key: str, fallback: Path) -> List[Path]:
    candidates: List[Path] = []
    reference = (state.get("bom") or {}).get(key)
    if reference:
        candidates.extend(data_reference_candidates(reference))
    candidates.append(fallback.resolve())
    return list(dict.fromkeys(candidates))


def _read_first_json(paths: List[Path]) -> tuple[Any, Optional[Path]]:
    for path in paths:
        payload = _read_json(path, None)
        if payload is not None:
            return payload, path
    return None, None


def _first_list(payload: Any, keys: List[str]) -> List[Any]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _first_list(value, keys)
            if nested:
                return nested
    return []


def get_bom_output(project_code: str, product_id: str) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    raw_candidates = _state_bom_path_candidates(
        state,
        "save_path",
        _bom_raw_path(project_code, product_id),
    )
    normalized_candidates = _state_bom_path_candidates(
        state,
        "normalized_path",
        _bom_normalized_path(project_code, product_id),
    )
    raw_bom, resolved_raw_path = _read_first_json(raw_candidates)
    normalized_bom, resolved_normalized_path = _read_first_json(normalized_candidates)
    state_bom = state.get("bom") or {}
    if raw_bom is None and normalized_bom is None:
        return {
            "project_code": project_code,
            "product_id": product_id,
            "status": "missing",
            "raw_bom_available": False,
            "normalized_bom_available": False,
            "raw_bom": None,
            "normalized_bom": None,
            "components": [],
            "process_scopes_for_most": [],
            "points_to_confirm": [],
            "technical_fields": {},
            "state_bom": state_bom,
            "attempted_paths": [str(path) for path in [*raw_candidates, *normalized_candidates]],
            "data_root": str(DATA_ROOT),
            "cwd": str(Path.cwd().resolve()),
        }
    if not isinstance(normalized_bom, dict):
        normalized_bom = normalize_bom(raw_bom or {})
        generated_path = _bom_normalized_path(project_code, product_id)
        _write_json(generated_path, normalized_bom)
        resolved_normalized_path = generated_path

    components = []
    for item in normalized_bom.get("components") or []:
        if not isinstance(item, dict):
            continue
        definition = item.get("component_definition") or {}
        components.append({
            "component_id": item.get("component_id"),
            "component": item.get("component") or item.get("component_type"),
            "quantity_per_product": item.get("quantity_per_product"),
            "category": item.get("category") or item.get("external_component_type"),
            "costing_route": item.get("costing_route"),
            "costing_relevance": definition.get("costing_relevance") if isinstance(definition, dict) else None,
        })

    process_scopes = _first_list(
        raw_bom,
        ["process_scopes_for_most", "process_scopes", "most_scopes", "work_packages"],
    )
    if not process_scopes:
        try:
            process = build_choke_process_route(
                state.get("customer_input") or {},
                normalized_bom,
                state.get("choke_classification") or normalized_bom.get("choke_classification"),
            )
            process_scopes = process.get("work_packages") or []
        except (TypeError, ValueError, KeyError):
            process_scopes = []

    points_to_confirm = _first_list(
        raw_bom,
        ["points_to_confirm", "points_to_confirm_or_validate", "items_to_confirm", "open_points"],
    )
    return {
        "project_code": project_code,
        "product_id": product_id,
        "status": "found",
        "raw_bom_available": raw_bom is not None,
        "normalized_bom_available": normalized_bom is not None,
        "raw_bom": raw_bom,
        "normalized_bom": normalized_bom,
        "components": components,
        "process_scopes_for_most": process_scopes,
        "points_to_confirm": points_to_confirm,
        "technical_fields": extract_bom_technical_fields(raw_bom or {}),
        "paths": {
            "raw_bom_path": state_bom.get("save_path") or _relative(_bom_raw_path(project_code, product_id)),
            "normalized_bom_path": state_bom.get("normalized_path") or _relative(_bom_normalized_path(project_code, product_id)),
            "resolved_raw_bom_path": str(resolved_raw_path) if resolved_raw_path else None,
            "resolved_normalized_bom_path": str(resolved_normalized_path) if resolved_normalized_path else None,
        },
    }


def get_workflow_debug(project_code: str, product_id: str) -> Dict[str, Any]:
    _load_env()
    state, existing_state_path = _existing_state(project_code, product_id)
    canonical_state_path = _state_path(project_code, product_id).resolve()
    canonical_raw_path = _bom_raw_path(project_code, product_id).resolve()
    canonical_normalized_path = _bom_normalized_path(project_code, product_id).resolve()
    raw_candidates = _state_bom_path_candidates(state or {}, "save_path", canonical_raw_path)
    normalized_candidates = _state_bom_path_candidates(
        state or {},
        "normalized_path",
        canonical_normalized_path,
    )
    raw_bom, resolved_raw_path = _read_first_json(raw_candidates)
    normalized_bom, resolved_normalized_path = _read_first_json(normalized_candidates)
    raw_path = resolved_raw_path or canonical_raw_path
    normalized_path = resolved_normalized_path or canonical_normalized_path

    run_roots = list(dict.fromkeys([
        COSTING_RUNS_DIR.resolve(),
        (BACKEND_ROOT / "data" / "costing_runs").resolve(),
        (PROJECT_ROOT / "data" / "costing_runs").resolve(),
        (Path.cwd() / "data" / "costing_runs").resolve(),
    ]))
    matching_run_dirs = []
    for root in run_roots:
        project_dir = root / project_code
        if project_dir.exists() and project_dir.is_dir():
            matching_run_dirs.extend(
                str(path.resolve()) for path in project_dir.iterdir() if path.is_dir()
            )

    customer_roots = list(dict.fromkeys([
        CUSTOMER_INPUT_DIR.resolve(),
        (BACKEND_ROOT / "data" / "customer_inputs").resolve(),
        (PROJECT_ROOT / "data" / "customer_inputs").resolve(),
    ]))
    matching_customer_inputs = []
    for root in customer_roots:
        if root.exists():
            matching_customer_inputs.extend(
                str(path.resolve()) for path in root.glob(f"{project_code}*.json")
            )

    normalized_component_ids = []
    if isinstance(normalized_bom, dict):
        normalized_component_ids = [
            item.get("component_id")
            for item in normalized_bom.get("components") or []
            if isinstance(item, dict) and item.get("component_id")
        ]
    response = {
        "project_code": project_code,
        "product_id": product_id,
        "data_root": str(DATA_ROOT),
        "cwd": str(Path.cwd().resolve()),
        "run_dir": str(_run_dir(project_code, product_id).resolve()),
        "workflow_state_path": str(existing_state_path or canonical_state_path),
        "workflow_state_exists": state is not None,
        "workflow_state": state,
        "raw_bom_path": str(raw_path),
        "raw_bom_exists": raw_path.exists(),
        "raw_bom_preview_keys": list(raw_bom.keys()) if isinstance(raw_bom, dict) else [],
        "normalized_bom_path": str(normalized_path),
        "normalized_bom_exists": normalized_path.exists(),
        "normalized_component_ids": normalized_component_ids,
        "bom_trigger_attempts": ((state or {}).get("bom") or {}).get("trigger_attempts") or [],
        "last_bom_trigger_status": (
            (((state or {}).get("bom") or {}).get("trigger_result") or {}).get("status")
            or ((state or {}).get("bom") or {}).get("status")
        ),
        "drawing_file_url_present": bool(
            (state or {}).get("drawing_file_url")
            or ((state or {}).get("bom") or {}).get("drawing_file_url")
            or ((state or {}).get("customer_input") or {}).get("drawing_file_url")
        ),
        "drawing_access_mode": (
            (state or {}).get("drawing_access_mode")
            or ((state or {}).get("bom") or {}).get("drawing_access_mode")
            or ((state or {}).get("customer_input") or {}).get("drawing_access_mode")
        ),
        "agent_ids": {
            "bom": clean_agent_id(os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID")),
            "external_component": clean_agent_id(os.getenv("CHATGPT_EXTERNAL_COMPONENT_AGENT_ID")),
            "most": clean_agent_id(os.getenv("CHATGPT_MOST_AGENT_ID")),
        },
        "env_checks": {
            "CHATGPT_CHOKE_BOM_AGENT_ID_present": bool(os.getenv("CHATGPT_CHOKE_BOM_AGENT_ID")),
            "CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN_present": bool(os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")),
            "PUBLIC_BASE_URL": os.getenv("PUBLIC_BASE_URL"),
            "DATA_ROOT": str(DATA_ROOT),
        },
        "matching_run_dirs_for_project": sorted(set(matching_run_dirs)),
        "matching_customer_input_files": sorted(set(matching_customer_inputs)),
        "workflow_state_attempted_paths": [
            str(path) for path in _state_path_candidates(project_code, product_id)
        ],
        "raw_bom_attempted_paths": [str(path) for path in raw_candidates],
        "normalized_bom_attempted_paths": [str(path) for path in normalized_candidates],
    }
    append_workflow_event(
        project_code,
        product_id,
        "get_debug_called",
        workflow_state_path=response["workflow_state_path"],
        workflow_state_exists=response["workflow_state_exists"],
        raw_bom_path=response["raw_bom_path"],
        raw_bom_exists=response["raw_bom_exists"],
        normalized_bom_path=response["normalized_bom_path"],
        normalized_bom_exists=response["normalized_bom_exists"],
    )
    return response


def get_writeback_debug(project_code: str, product_id: str) -> Dict[str, Any]:
    state, existing_state_path = _existing_state(project_code, product_id)
    state = state or {}
    canonical_state_path = _state_path(project_code, product_id).resolve()
    raw_candidates = _state_bom_path_candidates(
        state,
        "save_path",
        _bom_raw_path(project_code, product_id),
    )
    normalized_candidates = _state_bom_path_candidates(
        state,
        "normalized_path",
        _bom_normalized_path(project_code, product_id),
    )
    raw_bom, resolved_raw_path = _read_first_json(raw_candidates)
    normalized_bom, resolved_normalized_path = _read_first_json(normalized_candidates)
    component_ids = []
    if isinstance(normalized_bom, dict):
        component_ids = [
            item.get("component_id")
            for item in normalized_bom.get("components") or []
            if isinstance(item, dict) and item.get("component_id")
        ]

    events = []
    events_path = _events_path(project_code, product_id).resolve()
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_name = str(event.get("event") or "")
            if "save_bom_output" in event_name or event_name == "legacy_save_choke_bom_result_called":
                events.append(event)

    latest_error = next(
        (
            event
            for event in reversed(events)
            if event.get("event") in {
                "save_bom_output_validation_failed",
                "save_bom_output_failed",
            }
        ),
        None,
    )
    raw_path = resolved_raw_path or _bom_raw_path(project_code, product_id).resolve()
    normalized_path = (
        resolved_normalized_path or _bom_normalized_path(project_code, product_id).resolve()
    )
    return {
        "project_code": project_code,
        "product_id": product_id,
        "data_root": str(DATA_ROOT),
        "workflow_state_path": str(existing_state_path or canonical_state_path),
        "workflow_state_exists": bool(existing_state_path),
        "raw_bom_path": str(raw_path),
        "raw_bom_exists": raw_path.exists(),
        "normalized_bom_path": str(normalized_path),
        "normalized_bom_exists": normalized_path.exists(),
        "workflow_events_path": str(events_path),
        "workflow_events": events,
        "latest_writeback_error": latest_error,
        "component_ids": component_ids,
        "raw_bom_top_level_keys": list(raw_bom.keys()) if isinstance(raw_bom, dict) else [],
    }


def update_commercial_fields(
    project_code: str,
    product_id: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    allowed_fields = {
        "customer",
        "final_customer",
        "customer_delivery_zone",
        "annual_quantity",
        "currency",
        "quotation_currency",
        "target_price_currency",
        "purchasing_currency",
        "delivery_country",
        "delivery_city",
        "target_price",
        "sop_date",
        "product",
        "product_name",
        "product_family",
        "part_number",
        "drawing_reference",
    }
    updates = {
        key: value
        for key, value in fields.items()
        if key in allowed_fields
    }
    annual_quantity = updates.get("annual_quantity")
    if annual_quantity not in [None, ""]:
        try:
            number = float(annual_quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError("annual_quantity must be numeric.") from exc
        if number <= 0:
            raise ValueError("annual_quantity must be greater than zero.")
        updates["annual_quantity"] = int(number) if number.is_integer() else number

    selected_product = updates.get("product") or updates.get("product_name")
    if selected_product:
        selected_product = str(selected_product).strip()
        updates["product"] = selected_product
        updates["product_name"] = selected_product
    if updates.get("currency") and not updates.get("quotation_currency"):
        updates["quotation_currency"] = updates["currency"]

    customer_input = {**(state.get("customer_input") or {}), **updates}
    explicit_fields = set(customer_input.get("_explicit_user_fields") or [])
    explicit_fields.update(key for key, value in updates.items() if value not in [None, ""])
    customer_input["_explicit_user_fields"] = sorted(explicit_fields)
    customer_input["project_code"] = project_code
    customer_input.setdefault("workflow_product_id", product_id)
    state["customer_input"] = customer_input
    for field_name in [
        "product",
        "product_name",
        "customer",
        "final_customer",
        "customer_delivery_zone",
        "annual_quantity",
        "currency",
    ]:
        if field_name in updates:
            state[field_name] = updates[field_name]

    input_path = _customer_input_path_from_state(state)
    if input_path:
        stored_input = _read_json(input_path, {}) or {}
        stored_input.update(updates)
        stored_input["project_code"] = project_code
        stored_input.setdefault("workflow_product_id", product_id)
        _write_json(input_path, stored_input)

    customer_input_validation = _refresh_resolved_customer_context(state)
    master_data_refresh = _refresh_master_data_for_state(state)
    state["master_data_refresh"] = master_data_refresh
    _save_state(state)
    response_status = (
        "product_not_mapped"
        if master_data_refresh.get("status") == "product_not_mapped"
        else "updated"
    )
    return {
        "success": True,
        "status": response_status,
        "project_code": project_code,
        "product_id": product_id,
        "updated_fields": updates,
        "saved_fields": updates,
        "customer_input_path": str(input_path.resolve()) if input_path else None,
        "workflow_state_path": str(_state_path(project_code, product_id).resolve()),
        "customer_input": state["customer_input"],
        "manufacturing_strategy": state.get("manufacturing_strategy"),
        "production_plant": state.get("production_plant"),
        "unit_data": state.get("unit_data"),
        "customer_input_validation": customer_input_validation,
        "resolved_fields": customer_input_validation.get("resolved_fields") or [],
        "missing_fields": customer_input_validation.get("missing_fields") or [],
        "conflicts": customer_input_validation.get("conflicts") or [],
        "warnings": customer_input_validation.get("warnings") or [],
        "component_costing_ready": customer_input_validation.get("component_costing_ready") is True,
        "state": state,
    }


def get_customer_input_resolution(project_code: str, product_id: str) -> Dict[str, Any]:
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        customer_input = None
        for path in CUSTOMER_INPUT_DIR.glob("*.json"):
            candidate = _read_json(path, {}) or {}
            candidate_product_id = candidate.get("workflow_product_id") or candidate.get("product_id")
            if candidate.get("project_code") == project_code and candidate_product_id == product_id:
                customer_input = candidate
                break
        if customer_input is None:
            raise ValueError("Customer input and workflow state not found.")
        resolved = _resolve_customer_input_context(customer_input)
        resolution = resolved.get("resolved_customer_context") or {}
        return {
            "project_code": project_code,
            "product_id": product_id,
            **validate_resolved_customer_input(resolution),
            "customer_input": resolved,
        }
    validation = _refresh_resolved_customer_context(state)
    _refresh_master_data_for_state(state)
    _save_state(state)
    return {
        "project_code": project_code,
        "product_id": product_id,
        **validation,
        "customer_input": state.get("customer_input") or {},
    }


def _component_status_is_unconfirmed(component: Dict[str, Any]) -> bool:
    status = str(component.get("status") or "").strip().lower().replace("-", "_")
    return status in {"to_confirm", "to confirm", "unconfirmed", "pending_confirmation"}


def _required_external_components(
    normalized_bom: Dict[str, Any],
    include_unconfirmed: bool = False,
) -> List[Dict[str, Any]]:
    required = []
    seen = set()
    for component in normalized_bom.get("components") or []:
        component_id = component.get("component_id")
        if not component_id or component_id in seen:
            continue
        if component.get("costing_route") != "external_component_costing_agent":
            continue
        if _component_status_is_unconfirmed(component) and not include_unconfirmed:
            continue
        seen.add(component_id)
        required.append(component)
    return required


def _component_trigger_payload(
    state: Dict[str, Any],
    component: Dict[str, Any],
) -> Dict[str, Any]:
    customer_input = state.get("customer_input") or {}
    component_id = component["component_id"]
    component_definition = component.get("component_definition") or {}
    component_family = component.get("external_component_type") or component.get("category")
    dimensional_source = {**component_definition}
    dimensional_source.setdefault("quantity_per_product", component.get("quantity_per_product"))
    purchasing_quantity = component_costing.resolve_annual_purchasing_quantity(
        component_id,
        component_family,
        component_costing.extract_bom_dimensional_fields(component_id, dimensional_source),
        customer_input.get("annual_quantity"),
    )
    costing_scope = "external_bought_component"
    excluded_costs: List[str] = []
    component_instruction = COMPONENT_COSTING_INSTRUCTION
    if component_id == "magnet_wire":
        costing_scope = "raw_enameled_wire_material_only"
        excluded_costs = ["winding", "forming", "tooling", "fixture", "internal_added_value"]
        component_instruction += (
            " For magnet_wire, quote raw enameled wire material only in a mass-compatible "
            "basis; exclude winding, forming, tooling, fixtures, and internal added value."
        )
    elif component_id == "lead_tinning":
        costing_scope = "tin_consumable_material_only"
        excluded_costs = ["tinning_operation", "soldering_operation", "labor", "handling", "internal_added_value"]
        component_instruction += (
            " For lead_tinning, cost only the identified tin/Sn consumable material in a "
            "mass-compatible supplier basis. Never quote subcontract tinning, soldering, "
            "labor, handling, or process conversion; those belong to the internal MOST operation. "
            "Do not invent solder paste, flux, tin wire, or an alloy when the BOM does not confirm it."
        )
    return {
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        **classification_trace(state.get("choke_classification")),
        "component_id": component_id,
        "component_name": component.get("component"),
        "component_family": component_family,
        "classification": "External",
        "product": customer_input.get("product"),
        "product_line": customer_input.get("product_line") or "Chokes",
        "annual_quantity": customer_input.get("annual_quantity"),
        "annual_product_quantity": purchasing_quantity.get("annual_product_quantity"),
        "purchasing_quantity_per_product": purchasing_quantity.get("purchasing_quantity_per_product"),
        "annual_purchasing_quantity": purchasing_quantity.get("annual_purchasing_quantity"),
        "annual_purchasing_unit": purchasing_quantity.get("annual_purchasing_unit"),
        "purchasing_quantity_basis": purchasing_quantity.get("purchasing_quantity_basis"),
        "purchasing_quantity_status": purchasing_quantity.get("status"),
        "costing_scope": costing_scope,
        "excluded_costs": excluded_costs,
        "destination_zone": customer_input.get("customer_delivery_zone"),
        "production_plant": state.get("production_plant") or (state.get("unit_data") or {}).get("plant"),
        "reporting_currency": normalize_currency_code(customer_input.get("quotation_currency") or customer_input.get("currency")),
        "bom_quantity_per_product": component.get("quantity_per_product"),
        "technical_specification": component_definition,
        "drawing_reference": customer_input.get("drawing_reference") or customer_input.get("drawing_number"),
        "bom_source_path": (state.get("bom") or {}).get("normalized_path"),
        "manufacturing_strategy": state.get("manufacturing_strategy") or {},
        "save_address": _relative(_component_output_path(state["project_code"], state["product_id"], component_id)),
        "instruction": component_instruction,
    }


def _component_validation_response(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    customer_input = state.get("customer_input") or {}
    resolution = state.get("resolved_customer_context") or state.get("customer_input_resolution") or {}
    validation = validate_resolved_customer_input(resolution) if resolution else {}
    direct = list(validation.get("missing_fields") or [])
    conflicts = list(validation.get("conflicts") or [])
    legacy_names = {
        "product_name": "product",
        "delivery_zone": "customer_delivery_zone",
        "quotation_currency": "quotation_currency",
    }
    direct = [legacy_names.get(item, item) for item in direct]
    if direct:
        dependent = []
        if "product" in direct:
            dependent.append("production_plant")
        return {
            "status": "blocked",
            "missing_inputs": direct,
            "dependent_missing_inputs": list(dict.fromkeys(dependent)),
            "message": (
                "Select a product first. The production plant will then be resolved from the manufacturing strategy."
                if direct == ["product"]
                else "Complete required commercial fields before external component costing."
            ),
            "resolved_fields": validation.get("resolved_fields") or [],
            "conflicts": conflicts,
            "warnings": validation.get("warnings") or [],
            "component_costing_ready": False,
            "available_product_candidates": (
                (state.get("product_resolution") or {}).get("candidates")
                or (state.get("manufacturing_strategy") or {}).get("available_product_candidates")
                or []
            ),
        }
    if conflicts:
        return {
            "status": "blocked",
            "missing_inputs": [],
            "dependent_missing_inputs": [],
            "resolved_fields": validation.get("resolved_fields") or [],
            "conflicts": conflicts,
            "warnings": validation.get("warnings") or [],
            "component_costing_ready": False,
            "message": "Confirm conflicting customer-input fields before external component costing.",
        }
    strategy = state.get("manufacturing_strategy") or {}
    unit_data = state.get("unit_data") or {}
    dependent = []
    if strategy.get("status") not in {"found", "selected"}:
        dependent.append("manufacturing_strategy")
    if not (state.get("production_plant") or unit_data.get("plant")):
        dependent.append("production_plant")
    if unit_data.get("status") not in {"found", "selected"}:
        dependent.append("unit_data")
    if dependent:
        return {
            "status": "strategy_not_found" if "manufacturing_strategy" in dependent else "blocked",
            "product": customer_input.get("product"),
            "delivery_zone": customer_input.get("customer_delivery_zone"),
            "missing_inputs": [],
            "dependent_missing_inputs": dependent,
            "available_product_candidates": (
                strategy.get("available_product_candidates")
                or (state.get("product_resolution") or {}).get("candidates")
                or []
            ),
            "message": "No manufacturing strategy matched the selected product and delivery zone.",
        }
    return None


def trigger_next_component_costing(
    project_code: str,
    product_id: str,
    dry_run: bool = False,
    force: bool = False,
    include_unconfirmed: bool = False,
) -> Dict[str, Any]:
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found. Start the workflow before triggering components.")
    if (state.get("bom") or {}).get("status") != "received":
        raise ValueError("BOM output must be received before triggering component costing.")
    normalized_bom = _load_normalized_bom(project_code, product_id)
    state["customer_input_validation"] = _refresh_resolved_customer_context(state)
    state["master_data_refresh"] = _refresh_master_data_for_state(state)
    validation = _component_validation_response(state)
    if validation:
        _save_state(state)
        return {**validation, "project_code": project_code, "product_id": product_id, "state": state}

    required = _required_external_components(normalized_bom, include_unconfirmed=include_unconfirmed)
    state.setdefault("components", {})
    status_before = state.get("status")
    append_workflow_event(
        project_code, product_id, "component_batch_trigger_requested",
        status_before=status_before,
        component_ids=[item["component_id"] for item in required],
    )
    triggered, skipped, failed = [], [], []
    for component in required:
        component_id = component["component_id"]
        previous = state["components"].get(component_id) or {}
        saved_raw = _read_json(_component_output_path(project_code, product_id, component_id), {}) or {}
        saved_price_incomplete = component_costing.component_offer_requires_regeneration(saved_raw) if saved_raw else False
        requires_regeneration = previous.get("requires_regeneration") is True or saved_price_incomplete
        if saved_price_incomplete:
            previous = {
                **previous,
                "costing_readiness": "incomplete",
                "requires_regeneration": True,
            }
        if (
            not force
            and previous.get("status") in {"triggered", "received", "failed"}
            and not requires_regeneration
        ):
            skipped.append({"component_id": component_id, "status": previous.get("status"), "reason": "already_processed"})
            continue
        correlation_id = str(uuid.uuid4())
        payload = _component_trigger_payload(state, component)
        conversation_key = f"{project_code}:{product_id}:component:{component_id}:v1"
        append_workflow_event(
            project_code, product_id, "component_trigger_requested",
            component_id=component_id,
            correlation_id=correlation_id,
            status_before=previous.get("status"),
            save_path=payload["save_address"],
        )
        trigger_result = _trigger(
            "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
            "External Component Costing Agent",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            conversation_key,
            correlation_id,
            dry_run=dry_run,
        )
        accepted = trigger_result.get("status") in {"accepted", "dry_run"}
        entry = {
            **previous,
            "status": "triggered" if accepted else "failed",
            "component_id": component_id,
            "component_name": component.get("component"),
            "component_family": component.get("external_component_type") or component.get("category"),
            "trigger_payload": payload,
            "conversation_key": conversation_key,
            "correlation_id": correlation_id,
            "trigger_result": trigger_result,
            "save_path": payload["save_address"],
            "normalized_path": _relative(_normalized_component_output_path(project_code, product_id, component_id)),
            "received_at": previous.get("received_at") if not force else None,
            "costing_readiness": "pending" if accepted else previous.get("costing_readiness"),
            "requires_regeneration": False if accepted else requires_regeneration,
        }
        state["components"][component_id] = entry
        summary = {
            "component_id": component_id,
            "status": "accepted" if accepted else "failed",
            "http_status": trigger_result.get("http_status"),
            "conversation_url": (trigger_result.get("response") or {}).get("conversation_url")
            if isinstance(trigger_result.get("response"), dict) else None,
            "correlation_id": correlation_id,
        }
        if accepted:
            triggered.append(summary)
            append_workflow_event(project_code, product_id, "component_trigger_accepted", component_id=component_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="triggered")
        else:
            failed.append(summary)
            append_workflow_event(project_code, product_id, "component_trigger_failed", component_id=component_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="failed", http_status=trigger_result.get("http_status"))

    required_ids = [item["component_id"] for item in required]
    state["required_external_component_ids"] = required_ids
    state["missing_outputs"] = [
        f"component:{component_id}" for component_id in required_ids
        if (state["components"].get(component_id) or {}).get("status") != "received"
        or (state["components"].get(component_id) or {}).get("requires_regeneration") is True
    ]
    if triggered:
        state["status"] = "components_triggered"
        state["current_step"] = "Step 2 External Component Costing Agent"
    _save_state(state)
    return {
        "status": "component_agents_triggered" if triggered else ("component_agents_failed" if failed else "no_components_triggered"),
        "project_code": project_code,
        "product_id": product_id,
        "triggered_components": triggered,
        "skipped_components": skipped,
        "failed_components": failed,
        "component_triggers": [state["components"][item["component_id"]] for item in triggered],
        "state": state,
    }


def _output_value(raw_json: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw_json and raw_json[key] not in [None, ""]:
            return raw_json[key]
    return default


def normalize_component_output(
    state: Dict[str, Any],
    bom_component: Dict[str, Any],
    raw_json: Dict[str, Any],
) -> Dict[str, Any]:
    customer_input = state.get("customer_input") or {}
    offer = raw_json.get("recommended_offer") if isinstance(raw_json.get("recommended_offer"), dict) else {}
    normalized_offer = {
        "supplier": _output_value(offer, "supplier", "supplier_name"),
        "origin": _output_value(offer, "origin"),
        "incoterm": _output_value(offer, "incoterm"),
        "supplier_currency": _output_value(offer, "supplier_currency", "currency"),
        "fca_price_per_piece": _output_value(offer, "fca_price_per_piece", "fca_price"),
        "price_in_reporting_currency": _output_value(offer, "price_in_reporting_currency", "material_cost", "price_per_piece"),
        "transportation_cost_per_piece": _output_value(offer, "transportation_cost_per_piece", "transportation_cost"),
        "customs_cost_per_piece": _output_value(offer, "customs_cost_per_piece", "custom_duty_cost", "customs_cost"),
        "forwarder_cost_per_piece": _output_value(offer, "forwarder_cost_per_piece", "forwarder_cost"),
        "capital_cost_per_piece": _output_value(offer, "capital_cost_per_piece", "capital_cost"),
        "cash_locked_per_piece": _output_value(offer, "cash_locked_per_piece", "cash_locked"),
        "delivered_cost_per_piece": _output_value(offer, "delivered_cost_per_piece", "delivered_cost"),
        # Explicit pricing-unit fields (services/choke_component_costing.py). A
        # legacy offer that only sets price_in_reporting_currency with no
        # stated basis intentionally leaves unit_price_basis unset, so final
        # costing blocks instead of assuming a unit.
        "unit_price": _output_value(offer, "unit_price", "price_in_reporting_currency"),
        "unit_price_currency": _output_value(offer, "unit_price_currency", "supplier_currency", "currency"),
        "unit_price_basis": _output_value(offer, "unit_price_basis", "pricing_unit"),
        "transportation_cost_per_piece_basis": _output_value(offer, "transportation_cost_basis", "transportation_cost_per_piece_basis"),
        "customs_cost_per_piece_basis": _output_value(offer, "customs_cost_basis", "customs_cost_per_piece_basis"),
        "forwarder_cost_per_piece_basis": _output_value(offer, "forwarder_cost_basis", "forwarder_cost_per_piece_basis"),
    }
    resolved_offer = component_costing.resolve_component_offer(raw_json)
    dimensional_source = dict(bom_component.get("component_definition") or {})
    dimensional_source.setdefault("quantity_per_product", bom_component.get("quantity_per_product"))
    bom_fields = component_costing.extract_bom_dimensional_fields(
        bom_component["component_id"], dimensional_source,
    )
    reporting_currency = resolve_project_currency(
        customer_input.get("currency"),
        (state.get("unit_data") or {}).get("selling_currency"),
    )
    canonical = component_costing.build_canonical_component_costing(
        bom_component["component_id"],
        raw_json.get("component_family") or bom_component.get("external_component_type") or bom_component.get("category"),
        bom_fields,
        raw_json,
        target_currency=reporting_currency,
    )
    normalized_offer.update(resolved_offer)
    normalized_offer.update({
        "unit_price": resolved_offer.get("unit_price"),
        "unit_price_currency": resolved_offer.get("currency"),
        "currency": resolved_offer.get("currency"),
        "pricing_unit": resolved_offer.get("pricing_unit"),
        "pricing_basis": resolved_offer.get("pricing_basis"),
        "unit_price_basis": resolved_offer.get("pricing_basis"),
        "source_path": resolved_offer.get("source_path"),
        "converted_to_project_currency": resolved_offer.get("converted_to_project_currency", False),
    })
    for normalized_key, aliases in {
        "price_in_reporting_currency": ("material_cost", "price_in_reporting_currency"),
        "transportation_cost_per_piece": ("transportation_cost", "transportation_cost_per_piece"),
        "customs_cost_per_piece": ("custom_duty_cost", "customs_cost_per_piece"),
        "forwarder_cost_per_piece": ("forwarder_cost", "forwarder_cost_per_piece"),
        "delivered_cost_per_piece": ("delivered_cost", "delivered_cost_per_piece"),
    }.items():
        if normalized_offer[normalized_key] is None:
            normalized_offer[normalized_key] = _output_value(raw_json, *aliases)
    raw_cost_basis = raw_json.get("cost_basis") if isinstance(raw_json.get("cost_basis"), dict) else {}
    cost_basis = {
        "basis_status": raw_cost_basis.get("basis_status") or "not_available",
        "source": raw_cost_basis.get("source"),
        "source_date": raw_cost_basis.get("source_date"),
        "confidence": raw_cost_basis.get("confidence") or "low",
    }
    analysis_status = str(raw_json.get("analysis_status") or raw_json.get("status") or "assumption_based").lower()
    if analysis_status not in {"complete", "assumption_based", "blocked"}:
        analysis_status = "assumption_based"
    pricing_missing = []
    if resolved_offer.get("unit_price") is not None:
        if not resolved_offer.get("currency"):
            pricing_missing.append("recommended_offer.currency")
        if not resolved_offer.get("pricing_unit"):
            pricing_missing.append("recommended_offer.pricing_unit")
    if pricing_missing:
        analysis_status = "blocked"
    return {
        "schema_version": "1.0",
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        **classification_trace(state.get("choke_classification")),
        "component_id": bom_component["component_id"],
        "component_name": raw_json.get("component_name") or bom_component.get("component"),
        "component_family": raw_json.get("component_family") or bom_component.get("external_component_type") or bom_component.get("category"),
        "classification": "External",
        "analysis_status": analysis_status,
        "quantity_per_product": bom_component.get("quantity_per_product"),
        "technical_quantity": canonical.get("technical_quantity"),
        "technical_quantity_unit": canonical.get("technical_quantity_unit"),
        "unit_price": canonical.get("unit_price"),
        "pricing_unit": canonical.get("pricing_unit"),
        "currency": canonical.get("currency"),
        "material_cost_per_piece": canonical.get("material_cost_per_piece"),
        "source_quantity": canonical.get("source_quantity"),
        "conversion": canonical.get("conversion"),
        "annual_quantity": customer_input.get("annual_quantity"),
        "destination_zone": customer_input.get("customer_delivery_zone"),
        "reporting_currency": reporting_currency,
        "technical_specification": raw_json.get("technical_specification") or bom_component.get("component_definition") or {},
        "cost_basis": cost_basis,
        "recommended_offer": normalized_offer,
        "pricing_completeness": {
            "status": "incomplete" if pricing_missing else "complete",
            "missing_inputs": pricing_missing,
            "requires_regeneration": bool(pricing_missing),
        },
        "fx": raw_json.get("fx") if isinstance(raw_json.get("fx"), list) else [],
        "material_indexation": raw_json.get("material_indexation") if isinstance(raw_json.get("material_indexation"), list) else [],
        "productivity": raw_json.get("productivity") if isinstance(raw_json.get("productivity"), list) else [],
        "assumptions": raw_json.get("assumptions") if isinstance(raw_json.get("assumptions"), list) else [],
        "unconfirmed_values": raw_json.get("unconfirmed_values") if isinstance(raw_json.get("unconfirmed_values"), list) else [],
        "required_confirmations": raw_json.get("required_confirmations") if isinstance(raw_json.get("required_confirmations"), list) else [],
        "commercially_usable": raw_json.get("commercially_usable") is True,
        "pricing_quantity_basis": canonical.get("pricing_quantity_basis"),
        "fx_resolution": canonical.get("fx"),
        "pricing_warnings": canonical.get("warnings") or [],
    }


def save_component_output(project_code: str, product_id: str, component_id: str, raw_json: Dict[str, Any]) -> Dict[str, Any]:
    component_id = _safe_part(component_id, "component_id")
    correlation_id = str(uuid.uuid4())
    state, state_path = _existing_state(project_code, product_id)
    status_before = (state or {}).get("status")
    append_workflow_event(project_code, product_id, "save_component_output_called", component_id=component_id, correlation_id=correlation_id, status_before=status_before, workflow_state_path=str(state_path) if state_path else None)
    try:
        if state is None:
            raise ValueError("Workflow state not found. Component write-back cannot create a workflow.")
        if (state.get("bom") or {}).get("status") != "received":
            raise ValueError("BOM output must be received before component write-back.")
        if not isinstance(raw_json, dict):
            raise ValueError("raw_json must be a JSON object.")
        returned_id = raw_json.get("component_id")
        if returned_id not in [None, ""] and str(returned_id).strip() != component_id:
            raise ValueError("raw_json component_id does not match the tool component_id.")
        classification = raw_json.get("classification") or raw_json.get("output_classification")
        if classification not in [None, ""] and str(classification).strip().lower() != "external":
            raise ValueError("Component output classification must be External.")
        normalized_bom = _load_normalized_bom(project_code, product_id)
        bom_component = next((item for item in normalized_bom.get("components") or [] if item.get("component_id") == component_id), None)
        if not bom_component:
            raise ValueError(f"component_id {component_id} does not exist in the normalized BOM.")
        if bom_component.get("costing_route") != "external_component_costing_agent":
            raise ValueError(f"component_id {component_id} is not routed to external component costing.")

        raw_path = _component_output_path(project_code, product_id, component_id)
        normalized_path = _normalized_component_output_path(project_code, product_id, component_id)
        normalized = normalize_component_output(state, bom_component, raw_json)
        _write_json(raw_path, raw_json)
        _write_json(normalized_path, normalized)
        state.setdefault("components", {})
        existing = state["components"].get(component_id, {})
        pricing_completeness = normalized.get("pricing_completeness") or {}
        requires_regeneration = pricing_completeness.get("requires_regeneration") is True
        state["components"][component_id] = {
            **existing,
            "status": "received",
            "component_id": component_id,
            "component_name": bom_component.get("component"),
            "component_family": bom_component.get("external_component_type") or bom_component.get("category"),
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
            "costing_readiness": pricing_completeness.get("status") or "complete",
            "requires_regeneration": requires_regeneration,
        }
        required_ids = list(state.get("required_external_component_ids") or [])
        if not required_ids:
            required_ids = [item["component_id"] for item in _required_external_components(normalized_bom)]
        remaining = [
            item for item in required_ids
            if (state["components"].get(item) or {}).get("status") != "received"
            or (state["components"].get(item) or {}).get("requires_regeneration") is True
        ]
        state["missing_outputs"] = [f"component:{item}" for item in remaining]
        if not remaining:
            state["status"] = "components_received"
            state["current_step"] = "Step 3 MOST Agent"
        elif state.get("status") not in {"components_triggered", "components_received"}:
            state["status"] = "components_triggered"
            state["current_step"] = "Step 2 External Component Costing Agent"
        _save_state(state)
        append_workflow_event(project_code, product_id, "save_component_output_completed", component_id=component_id, correlation_id=correlation_id, status_before=status_before, status_after=state.get("status"), raw_path=_relative(raw_path), normalized_path=_relative(normalized_path))
        if not remaining:
            append_workflow_event(project_code, product_id, "all_component_outputs_received", component_id=component_id, correlation_id=correlation_id, status_before=status_before, status_after="components_received")
        return {
            "success": True,
            "status": "saved",
            "tool": "save_component_output",
            "project_code": project_code,
            "product_id": product_id,
            "component_id": component_id,
            "raw_component_saved": raw_path.exists(),
            "normalized_component_saved": normalized_path.exists(),
            "state_status_after": state.get("status"),
            "remaining_component_ids": remaining,
            "state": state,
        }
    except Exception as exc:
        append_workflow_event(project_code, product_id, "save_component_output_failed", component_id=component_id, correlation_id=correlation_id, status_before=status_before, error=str(exc))
        raise


def get_component_output(project_code: str, product_id: str, component_id: str) -> Dict[str, Any]:
    component_id = _safe_part(component_id, "component_id")
    raw_path = _component_output_path(project_code, product_id, component_id)
    normalized_path = _normalized_component_output_path(project_code, product_id, component_id)
    if not raw_path.exists() and not normalized_path.exists():
        return {"status": "missing", "project_code": project_code, "product_id": product_id, "component_id": component_id}
    return {
        "status": "found",
        "project_code": project_code,
        "product_id": product_id,
        "component_id": component_id,
        "raw_component": _read_json(raw_path, None),
        "normalized_component": _read_json(normalized_path, None),
        "paths": {"raw": _relative(raw_path), "normalized": _relative(normalized_path)},
    }


def get_component_outputs(project_code: str, product_id: str) -> Dict[str, Any]:
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found.")
    normalized_bom = _load_normalized_bom(project_code, product_id)
    outputs = [get_component_output(project_code, product_id, item["component_id"]) for item in _required_external_components(normalized_bom, include_unconfirmed=True)]
    return {"status": "found", "project_code": project_code, "product_id": product_id, "component_outputs": outputs}


def _most_component_technical_data(project_code: str, product_id: str, component: Dict[str, Any]) -> Dict[str, Any]:
    technical = dict(component.get("component_definition") or {})
    normalized_path = _normalized_component_output_path(project_code, product_id, component["component_id"])
    normalized_output = _read_json(normalized_path, {}) or {}
    if isinstance(normalized_output.get("technical_specification"), dict):
        technical.update(normalized_output["technical_specification"])
    return technical


def build_most_process_decomposition(state: Dict[str, Any], normalized_bom: Dict[str, Any]) -> Dict[str, Any]:
    """Build MOST work packages exclusively through the central evidence router."""
    classification = state.get("choke_classification") or normalized_bom.get("choke_classification")
    if not classification or classification.get("choke_subtype") == "unknown_choke":
        classification = classify_choke(
            state.get("customer_input") or {},
            normalized_bom.get("raw_bom") or {},
        )
    result = build_choke_process_route(
        state.get("customer_input") or {},
        normalized_bom,
        classification,
        state.get("preliminary_routing_policy") or {},
    )
    component_map = {
        item.get("component_id"): item
        for item in normalized_bom.get("components") or []
        if item.get("component_id")
    }
    for package in result.get("work_packages") or []:
        package_components = [
            component_map[item]
            for item in package.get("component_ids") or []
            if item in component_map
        ]
        technical_inputs: Dict[str, Any] = {}
        for component in package_components:
            technical_inputs.update(
                _most_component_technical_data(state["project_code"], state["product_id"], component)
            )
        package["technical_inputs"] = technical_inputs
        package["production_plant"] = state.get("production_plant") or (state.get("unit_data") or {}).get("plant")
        package["annual_quantity"] = (state.get("customer_input") or {}).get("annual_quantity")
        package.update(classification_trace(classification))
    return result


def _most_trigger_payload(
    state: Dict[str, Any],
    work_package: Dict[str, Any],
    trigger_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    work_package_id = work_package["work_package_id"]
    trigger_run_id = str(trigger_run_id or uuid.uuid4())
    return {
        "project_code": state["project_code"],
        "product_id": str(state["product_id"]),
        **classification_trace(state.get("choke_classification")),
        "work_package_id": work_package_id,
        "most_scope_id": work_package_id,
        "trigger_run_id": trigger_run_id,
        "operation_id": work_package.get("operation_id"),
        "operation_name": work_package.get("operation_name"),
        "component_ids": work_package.get("component_ids") or [],
        "technical_inputs": work_package.get("technical_inputs") or {},
        "annual_quantity": work_package.get("annual_quantity"),
        "production_plant": work_package.get("production_plant"),
        "unit_data": state.get("unit_data") or {},
        "save_address": _relative(_most_output_path(state["project_code"], state["product_id"], work_package_id)),
        "instruction": MOST_WRITEBACK_INSTRUCTION,
    }


def _most_eligibility_report(
    normalized_bom: Dict[str, Any],
    process: Dict[str, Any],
) -> Dict[str, Any]:
    operations = [
        *(process.get("operations") or []),
        *(process.get("excluded_operations") or []),
    ]
    component_rows = []
    for component in normalized_bom.get("components") or []:
        component_id = component.get("component_id")
        linked_operations = [
            operation
            for operation in operations
            if component_id and component_id in (operation.get("component_ids") or [])
        ]
        confirmed_operations = [
            operation.get("operation_name")
            for operation in linked_operations
            if operation.get("status") == "confirmed"
        ]
        component_rows.append({
            "component_id": component_id,
            "component_name": component.get("component"),
            "component_classification": (
                component.get("category")
                or component.get("external_component_type")
                or component.get("component_type")
            ),
            "costing_route": component.get("costing_route"),
            "external_or_internal": (
                "external"
                if component.get("costing_route")
                == "external_component_costing_agent"
                else "internal"
            ),
            "proposed_operations": [
                operation.get("operation_name") for operation in linked_operations
            ],
            "confirmed_for_most": bool(confirmed_operations),
            "confirmed_operations": confirmed_operations,
            "exclusion_reason": (
                None
                if confirmed_operations
                else "No confirmed finished-product operation uses this BOM component."
            ),
        })
    operation_rows = []
    for operation in operations:
        evidence = operation.get("evidence") or []
        operation_rows.append({
            "operation_key": operation.get("operation_key"),
            "operation_name": operation.get("operation_name"),
            "operation_confidence": (
                max(
                    (item.get("confidence") or "" for item in evidence),
                    key=lambda value: {"confirmed": 4, "high": 3, "medium": 2, "low": 1}.get(
                        str(value).lower(), 0
                    ),
                    default=None,
                )
            ),
            "evidence": evidence,
            "confirmed_for_most": operation.get("status") == "confirmed",
            "status": operation.get("status"),
            "exclusion_reason": (
                None
                if operation.get("status") == "confirmed"
                else operation.get("reason_selected")
            ),
            "component_ids": operation.get("component_ids") or [],
        })
    return {"components": component_rows, "operations": operation_rows}


def trigger_most_operations(
    project_code: str,
    product_id: str,
    dry_run: bool = False,
    force: bool = False,
    only_work_package_id: Optional[str] = None,
    active_trigger_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    product_id = str(product_id)
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found. Start the workflow before triggering MOST.")
    state["product_id"] = product_id
    if (state.get("bom") or {}).get("status") != "received":
        raise ValueError("BOM output must be received before triggering MOST.")
    normalized_bom = _load_normalized_bom(project_code, product_id)
    required_component_ids = list(state.get("required_external_component_ids") or [
        item["component_id"] for item in _required_external_components(normalized_bom)
    ])
    missing_components = [
        item for item in required_component_ids
        if (state.get("components", {}).get(item) or {}).get("status") != "received"
    ]
    if missing_components:
        raise ValueError(f"Component outputs must be received before MOST: {', '.join(missing_components)}")
    customer_input = state.get("customer_input") or {}
    if customer_input.get("annual_quantity") in [None, "", 0]:
        raise ValueError("MOST/component-operation planning needs annual_quantity before operations can be triggered.")
    process = build_most_process_decomposition(state, normalized_bom)
    eligible_operations = [
        item for item in process.get("operations") or []
        if item.get("status") == "confirmed"
    ]
    skipped_operations = [
        item for item in [
            *(process.get("operations") or []),
            *(process.get("excluded_operations") or []),
        ]
        if item.get("status") != "confirmed"
    ]
    eligibility_report = _most_eligibility_report(normalized_bom, process)
    process["eligibility_report"] = eligibility_report
    state["process_decomposition"] = process
    state["required_most_work_package_ids"] = process.get("required_work_package_ids") or []
    state.setdefault("most", {})

    if process.get("status") == "blocked":
        blocking_reason = process.get("blocked_reason") or "no_confirmed_operations"
        state["status"] = "most_blocked"
        state["current_step"] = "Step 3 MOST Assemblage"
        state["most"].update({
            "status": "most_blocked",
            "lifecycle_status": "most_blocked",
            "trigger_result": None,
            "trigger_attempts": [],
            "blocking_reason": blocking_reason,
            "eligible_operations": eligible_operations,
            "skipped_operations": skipped_operations,
            "eligibility_report": eligibility_report,
        })
        state["missing_outputs"] = [
            f"most:{item}" for item in state["required_most_work_package_ids"]
            if (state["most"].get(item) or {}).get("status") != "received"
        ]
        _save_state(state)
        logger.warning(
            "trigger_most_operations blocked for %s/%s: blocked_reason=%s missing_inputs=%s",
            project_code, product_id, process.get("blocked_reason"), process.get("missing_inputs"),
        )
        return {
            "success": False,
            "status": "most_blocked",
            "lifecycle_status": "most_blocked",
            "triggered": False,
            "reason": blocking_reason,
            "blocked_reason": blocking_reason,
            "message": "No confirmed assembly/process operations are eligible for MOST.",
            "missing_inputs": process.get("missing_inputs") or [],
            "project_code": project_code,
            "product_id": product_id,
            "triggered_work_packages": [],
            "skipped_work_packages": [],
            "failed_work_packages": [],
            "most_triggers": [],
            "most": {
                "status": "most_blocked",
                "lifecycle_status": "most_blocked",
                "trigger_result": None,
                "trigger_attempts": [],
            },
            "eligible_operations": eligible_operations,
            "skipped_operations": skipped_operations,
            "eligibility_report": eligibility_report,
            "required_most_work_package_ids": state["required_most_work_package_ids"],
            "process_decomposition": process,
            "process_route": process,
            "errors": state.get("errors") or [],
            "warnings": [
                *(state.get("warnings") or []),
                *(process.get("assumptions") or []),
            ],
            "blocking_reason": blocking_reason,
            "missing_outputs": state.get("missing_outputs") or [],
            "state": state,
        }

    triggered, skipped, failed = [], [], []
    work_packages = list(process.get("work_packages") or [])
    if only_work_package_id:
        only_work_package_id = _safe_part(
            only_work_package_id, "only_work_package_id"
        )
        work_packages = [
            item
            for item in work_packages
            if item.get("work_package_id") == only_work_package_id
        ]
        if not work_packages:
            raise ValueError(
                f"work_package_id {only_work_package_id} does not exist in process decomposition."
            )
    trigger_run_id = str(active_trigger_run_id or uuid.uuid4())
    for work_package in work_packages:
        work_package_id = work_package["work_package_id"]
        previous = state["most"].get(work_package_id) or {}
        if work_package.get("status") == "blocked":
            state["most"][work_package_id] = {
                **previous,
                **work_package,
                "status": "blocked",
            }
            skipped.append({"work_package_id": work_package_id, "status": "blocked", "reason": work_package.get("blocking_reason")})
            continue
        if not force and previous.get("status") in {
            "trigger_request_sending",
            "trigger_request_accepted",
            "awaiting_most_callback",
            "triggered",
            "received",
        }:
            skipped.append({"work_package_id": work_package_id, "status": previous.get("status"), "reason": "already_processed"})
            continue
        correlation_id = str(uuid.uuid4())
        payload = _most_trigger_payload(state, work_package, trigger_run_id)
        conversation_key = f"{project_code}:{product_id}:most:{work_package_id}:v1"
        sending_attempt = {
            "status": "trigger_request_sending",
            "requested_at": _now_iso(),
            "trigger_run_id": trigger_run_id,
        }
        state["most"][work_package_id] = {
            **previous,
            **work_package,
            "status": "trigger_request_sending",
            "lifecycle_status": "trigger_request_sending",
            "trigger_run_id": trigger_run_id,
            "conversation_key": conversation_key,
            "correlation_id": correlation_id,
            "trigger_payload": payload,
            "trigger_attempts": [
                *(previous.get("trigger_attempts") or []),
                sending_attempt,
            ],
            "save_path": payload["save_address"],
            "normalized_path": _relative(
                _normalized_most_output_path(project_code, product_id, work_package_id)
            ),
        }
        state["status"] = "most_triggering"
        state["current_step"] = "Step 3 MOST Assemblage"
        state["most"].update({
            "status": "trigger_request_sending",
            "lifecycle_status": "trigger_request_sending",
            "trigger_result": None,
            "trigger_attempts": [
                *(state["most"].get("trigger_attempts") or []),
                sending_attempt,
            ],
            "active_trigger_run_id": trigger_run_id,
        })
        _save_state(state)
        append_workflow_event(project_code, product_id, "most_trigger_requested", work_package_id=work_package_id, trigger_run_id=trigger_run_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="trigger_request_sending", save_path=payload["save_address"])
        trigger_result = _trigger(
            "CHATGPT_MOST_AGENT_ID",
            "MOST Assemblage",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            conversation_key,
            correlation_id,
            dry_run=dry_run,
        )
        accepted = trigger_result.get("status") in {"accepted", "dry_run"}
        completed_attempt = {
            "status": "trigger_request_accepted" if accepted else "trigger_request_failed",
            "completed_at": _now_iso(),
            "trigger_run_id": trigger_run_id,
            "http_status": trigger_result.get("http_status"),
            "request_correlation_id": trigger_result.get("request_correlation_id"),
        }
        entry = {
            **previous,
            **work_package,
            "status": "trigger_request_accepted" if accepted else "trigger_request_failed",
            "lifecycle_status": "awaiting_most_callback" if accepted else "trigger_request_failed",
            "trigger_run_id": trigger_run_id,
            "conversation_key": conversation_key,
            "correlation_id": correlation_id,
            "trigger_payload": payload,
            "trigger_result": trigger_result,
            "trigger_attempts": [
                *(previous.get("trigger_attempts") or []),
                sending_attempt,
                completed_attempt,
            ],
            "save_path": payload["save_address"],
            "normalized_path": _relative(_normalized_most_output_path(project_code, product_id, work_package_id)),
            "received_at": previous.get("received_at") if not force else None,
        }
        state["most"][work_package_id] = entry
        _save_state(state)
        summary = {"work_package_id": work_package_id, "status": "accepted" if accepted else "failed", "http_status": trigger_result.get("http_status"), "correlation_id": correlation_id, "trigger_run_id": trigger_run_id}
        if accepted:
            triggered.append(summary)
            append_workflow_event(project_code, product_id, "most_trigger_accepted", work_package_id=work_package_id, trigger_run_id=trigger_run_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="awaiting_most_callback")
        else:
            failed.append(summary)
            append_workflow_event(project_code, product_id, "most_trigger_failed", work_package_id=work_package_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="failed", http_status=trigger_result.get("http_status"))
    required_ids = state["required_most_work_package_ids"]
    state["missing_outputs"] = [f"most:{item}" for item in required_ids if (state["most"].get(item) or {}).get("status") != "received"]
    awaiting_existing = any(
        (state["most"].get(item) or {}).get("status")
        in {"trigger_request_sending", "trigger_request_accepted", "awaiting_most_callback", "triggered"}
        for item in required_ids
    )
    all_received = bool(required_ids) and all(
        (state["most"].get(item) or {}).get("status") == "received"
        for item in required_ids
    )
    if triggered:
        accepted_results = [
            (state["most"].get(summary["work_package_id"]) or {}).get("trigger_result")
            or {}
            for summary in triggered
        ]
        result_statuses = {item.get("status") for item in accepted_results}
        result_http_statuses = {item.get("http_status") for item in accepted_results}
        state["status"] = "most_triggered"
        state["current_step"] = "Step 3 MOST Assemblage"
        state["most"].update({
            "status": "trigger_request_accepted",
            "lifecycle_status": "awaiting_most_callback",
            "trigger_result": accepted_results[0] if len(accepted_results) == 1 else {
                "status": (
                    next(iter(result_statuses))
                    if len(result_statuses) == 1
                    else "mixed"
                ),
                "http_status": (
                    next(iter(result_http_statuses))
                    if len(result_http_statuses) == 1
                    else None
                ),
                "results": accepted_results,
            },
            "trigger_attempts": [
                item
                for summary in triggered
                for item in (
                    (state["most"].get(summary["work_package_id"]) or {}).get(
                        "trigger_attempts"
                    ) or []
                )
            ],
            "eligible_operations": eligible_operations,
            "skipped_operations": skipped_operations,
            "eligibility_report": eligibility_report,
            "active_trigger_run_id": trigger_run_id,
        })
    elif failed:
        failed_results = [
            (state["most"].get(summary["work_package_id"]) or {}).get("trigger_result")
            or {}
            for summary in failed
        ]
        state["status"] = "most_trigger_failed"
        state["current_step"] = "Step 3 MOST Assemblage"
        state["most"].update({
            "status": "trigger_request_failed",
            "lifecycle_status": "trigger_request_failed",
            "trigger_result": failed_results[0] if len(failed_results) == 1 else {
                "status": "failed",
                "results": failed_results,
            },
            "eligible_operations": eligible_operations,
            "skipped_operations": skipped_operations,
            "eligibility_report": eligibility_report,
            "active_trigger_run_id": trigger_run_id,
        })
    elif awaiting_existing:
        state["status"] = "most_triggered"
        state["current_step"] = "Step 3 MOST Assemblage"
        state["most"].update({
            "status": "trigger_request_accepted",
            "lifecycle_status": "awaiting_most_callback",
        })
    elif all_received:
        state["status"] = "most_received"
        state["current_step"] = "Step 4 Final Calculation"
        state["most"].update({
            "status": "most_received",
            "lifecycle_status": "most_received",
        })
    _save_state(state)
    if triggered:
        success, reason = True, None
    elif failed:
        success, reason = False, "trigger_failed"
    elif skipped:
        success, reason = True, "already_triggered"
    else:
        success, reason = True, "nothing_to_trigger"
    return {
        "success": success,
        "status": (
            "most_received"
            if all_received
            else
            "most_triggered"
            if triggered or awaiting_existing
            else "most_trigger_failed"
            if failed
            else "most_blocked"
        ),
        "lifecycle_status": (
            "awaiting_most_callback"
            if triggered or awaiting_existing
            else "trigger_request_failed"
            if failed
            else "most_received"
            if all_received
            else "most_blocked"
        ),
        "triggered": bool(triggered),
        "reason": reason,
        "project_code": project_code,
        "product_id": product_id,
        "triggered_work_packages": triggered,
        "skipped_work_packages": skipped,
        "failed_work_packages": failed,
        "most_triggers": [state["most"][item["work_package_id"]] for item in triggered],
        "most": {
            "status": state["most"].get("status"),
            "lifecycle_status": state["most"].get("lifecycle_status"),
            "trigger_result": state["most"].get("trigger_result"),
            "trigger_attempts": state["most"].get("trigger_attempts") or [],
        },
        "eligible_operations": eligible_operations,
        "skipped_operations": skipped_operations,
        "eligibility_report": eligibility_report,
        "required_most_work_package_ids": required_ids,
        "process_decomposition": process,
        "process_route": process,
        "errors": state.get("errors") or [],
        "warnings": [
            *(state.get("warnings") or []),
            *(process.get("assumptions") or []),
        ],
        "blocking_reason": None if triggered or awaiting_existing or all_received else reason,
        "missing_outputs": state.get("missing_outputs") or [],
        "state": state,
    }


def retry_most_work_package(
    project_code: str,
    product_id: str,
    work_package_id: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    product_id = str(product_id)
    work_package_id = _safe_part(work_package_id, "work_package_id")
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found.")
    entry = (state.get("most") or {}).get(work_package_id)
    if not isinstance(entry, dict) or not entry.get("work_package_id"):
        raise ValueError(
            f"work_package_id {work_package_id} does not exist in the active MOST route."
        )
    if entry.get("status") == "received":
        return {
            "success": False,
            "status": "most_retry_blocked",
            "reason": "work_package_already_received",
            "project_code": project_code,
            "product_id": product_id,
            "work_package_id": work_package_id,
            "state": state,
        }
    retry_count = int(entry.get("retry_count") or 0)
    if entry.get("status") in {
        "trigger_request_sending",
        "trigger_request_accepted",
        "awaiting_most_callback",
    } and retry_count > 0:
        return {
            "success": False,
            "status": "most_retry_blocked",
            "reason": "retry_already_active",
            "project_code": project_code,
            "product_id": product_id,
            "work_package_id": work_package_id,
            "active_trigger_run_id": entry.get("trigger_run_id"),
            "state": state,
        }
    failure = {
        "recorded_at": _now_iso(),
        "failure_reason": "missing_trigger_run_id",
        "failed_trigger_run_id": entry.get("trigger_run_id"),
        "status_before": entry.get("status"),
        "source": "controlled_retry_request",
    }
    entry.setdefault("writeback_failure_history", []).append(failure)
    entry.update({
        "status": "writeback_failed",
        "lifecycle_status": "most_writeback_failed",
        "retryable": True,
        "failure_reason": "missing_trigger_run_id",
        "retry_count": retry_count + 1,
    })
    state["most"][work_package_id] = entry
    state["most"].update({
        "status": "writeback_failed",
        "lifecycle_status": "most_writeback_failed",
        "retryable": True,
        "failure_reason": "missing_trigger_run_id",
    })
    state["status"] = "most_trigger_failed"
    state["current_step"] = "Step 3 MOST Assemblage"
    _save_state(state)
    append_workflow_event(
        project_code,
        product_id,
        "most_writeback_failed",
        work_package_id=work_package_id,
        trigger_run_id=entry.get("trigger_run_id"),
        failure_reason="missing_trigger_run_id",
        status_before=failure["status_before"],
        status_after="writeback_failed",
    )
    new_trigger_run_id = str(uuid.uuid4())
    return trigger_most_operations(
        project_code=project_code,
        product_id=product_id,
        dry_run=dry_run,
        force=True,
        only_work_package_id=work_package_id,
        active_trigger_run_id=new_trigger_run_id,
    )


def normalize_most_output(state: Dict[str, Any], work_package: Dict[str, Any], raw_json: Dict[str, Any]) -> Dict[str, Any]:
    def value(*keys: str) -> Any:
        return _output_value(raw_json, *keys)
    analysis_status = str(value("analysis_status", "status") or "assumption_based").lower()
    if analysis_status not in {"complete", "assumption_based", "blocked"}:
        analysis_status = "assumption_based"
    method = value("method", "most_method") or "engineering_estimate"
    if method not in {"BasicMOST", "MiniMOST", "engineering_estimate"}:
        method = "engineering_estimate"
    return {
        "schema_version": "1.0",
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        **classification_trace(state.get("choke_classification")),
        "work_package_id": work_package["work_package_id"],
        "operation_name": raw_json.get("operation_name") or work_package.get("operation_name"),
        "component_ids": raw_json.get("component_ids") if isinstance(raw_json.get("component_ids"), list) else work_package.get("component_ids") or [],
        "analysis_status": analysis_status,
        "method": method,
        "sequence_model": raw_json.get("sequence_model") if isinstance(raw_json.get("sequence_model"), list) else [],
        "tmus": value("tmus", "total_tmus"),
        "normal_time_seconds": value("normal_time_seconds", "cycle_time_seconds"),
        "allowance_percent": value("allowance_percent"),
        "standard_time_seconds": value("standard_time_seconds"),
        "pieces_per_hour": value("pieces_per_hour", "p_h"),
        "oee_percent": value("oee_percent", "oee"),
        "effective_pieces_per_hour": value("effective_pieces_per_hour"),
        "operator_count": value("operator_count"),
        "machine_count": value("machine_count"),
        "labor_cost_per_hour": value("labor_cost_per_hour"),
        "variable_overhead_per_hour": value("variable_overhead_per_hour"),
        "direct_labor_cost_per_piece": value("direct_labor_cost_per_piece", "dl_cost_per_piece"),
        "variable_overhead_cost_per_piece": value("variable_overhead_cost_per_piece", "voh_cost_per_piece"),
        "equipment": raw_json.get("equipment") if isinstance(raw_json.get("equipment"), list) else [],
        "assumptions": raw_json.get("assumptions") if isinstance(raw_json.get("assumptions"), list) else [],
        "unconfirmed_values": raw_json.get("unconfirmed_values") if isinstance(raw_json.get("unconfirmed_values"), list) else [],
        "required_confirmations": raw_json.get("required_confirmations") if isinstance(raw_json.get("required_confirmations"), list) else [],
    }


def save_most_output(
    project_code: str,
    product_id: str,
    work_package_id: str,
    raw_json: Any,
    trigger_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    work_package_id = _safe_part(work_package_id, "work_package_id")
    correlation_id = str(uuid.uuid4())
    state, state_path = _existing_state(project_code, product_id)
    status_before = (state or {}).get("status")
    append_workflow_event(project_code, product_id, "save_most_output_called", work_package_id=work_package_id, correlation_id=correlation_id, status_before=status_before, workflow_state_path=str(state_path) if state_path else None)
    try:
        if state is None:
            raise ValueError("Workflow state not found. MOST write-back cannot create a workflow.")
        if (state.get("bom") or {}).get("status") != "received":
            raise ValueError("BOM output must be received before MOST write-back.")
        if isinstance(raw_json, str):
            try:
                raw_json = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise ValueError("raw_json string must contain one valid JSON object.") from exc
        if not isinstance(raw_json, dict):
            raise ValueError("raw_json must be a JSON object or a string containing one JSON object.")
        returned_id = raw_json.get("work_package_id") or raw_json.get("most_scope_id")
        if returned_id not in [None, ""] and str(returned_id).strip() != work_package_id:
            raise ValueError("raw_json work_package_id does not match the tool work_package_id.")
        process = state.get("process_decomposition") or {}
        work_package = next((item for item in process.get("work_packages") or [] if item.get("work_package_id") == work_package_id), None)
        if not work_package:
            state.setdefault("most", {}).setdefault(
                "stale_callback_history", []
            ).append({
                "received_at": _now_iso(),
                "work_package_id": work_package_id,
                "trigger_run_id": str(trigger_run_id or ""),
                "reason": "stale_work_package_id",
            })
            _save_state(state)
            return {
                "success": False,
                "status": "stale_callback",
                "error_code": "stale_work_package_id",
                "message": "MOST callback work_package_id is not part of the active process route.",
                "project_code": project_code,
                "product_id": str(product_id),
                "work_package_id": work_package_id,
            }
        if work_package.get("status") == "blocked":
            raise ValueError(f"work_package_id {work_package_id} is blocked: {work_package.get('blocking_reason')}")
        existing_entry = (state.get("most") or {}).get(work_package_id) or {}
        expected_trigger_run_id = str(existing_entry.get("trigger_run_id") or "").strip()
        received_trigger_run_id = str(trigger_run_id or "").strip()
        if expected_trigger_run_id and not received_trigger_run_id:
            rejected = {
                "received_at": _now_iso(),
                "received_trigger_run_id": None,
                "expected_trigger_run_id": expected_trigger_run_id,
                "reason": "missing_trigger_run_id",
            }
            existing_entry.setdefault("stale_callback_history", []).append(rejected)
            existing_entry.update({
                "status": "writeback_failed",
                "lifecycle_status": "most_writeback_failed",
                "retryable": True,
                "failure_reason": "missing_trigger_run_id",
            })
            state["most"][work_package_id] = existing_entry
            state["most"].setdefault("stale_callback_history", []).append({
                **rejected,
                "work_package_id": work_package_id,
            })
            state["most"].update({
                "status": "writeback_failed",
                "lifecycle_status": "most_writeback_failed",
                "retryable": True,
                "failure_reason": "missing_trigger_run_id",
            })
            state["status"] = "most_trigger_failed"
            state["current_step"] = "Step 3 MOST Assemblage"
            _save_state(state)
            return {
                "success": False,
                "status": "rejected",
                "error_code": "missing_trigger_run_id",
                "message": "MOST callback is missing trigger_run_id for the current work package run.",
                "project_code": project_code,
                "product_id": str(product_id),
                "work_package_id": work_package_id,
            }
        if expected_trigger_run_id and received_trigger_run_id != expected_trigger_run_id:
            stale_callback = {
                "received_at": _now_iso(),
                "received_trigger_run_id": received_trigger_run_id,
                "expected_trigger_run_id": expected_trigger_run_id,
                "reason": "trigger_run_id_mismatch",
            }
            existing_entry.setdefault("stale_callback_history", []).append(stale_callback)
            state["most"][work_package_id] = existing_entry
            state["most"].setdefault("stale_callback_history", []).append({
                **stale_callback,
                "work_package_id": work_package_id,
            })
            _save_state(state)
            return {
                "success": False,
                "status": "stale_callback",
                "error_code": "trigger_run_id_mismatch",
                "message": "MOST callback belongs to a different trigger run.",
                "project_code": project_code,
                "product_id": str(product_id),
                "work_package_id": work_package_id,
            }
        normalized_bom = _load_normalized_bom(project_code, product_id)
        external_ids = {item["component_id"] for item in _required_external_components(normalized_bom, include_unconfirmed=True)}
        applicable = [item for item in work_package.get("component_ids") or [] if item in external_ids]
        missing_components = [item for item in applicable if (state.get("components", {}).get(item) or {}).get("status") != "received"]
        if missing_components:
            raise ValueError(f"Required component outputs are missing for MOST scope: {', '.join(missing_components)}")
        raw_path = _most_output_path(project_code, product_id, work_package_id)
        normalized_path = _normalized_most_output_path(project_code, product_id, work_package_id)
        normalized = normalize_most_output(state, work_package, raw_json)
        _write_json(raw_path, raw_json)
        _write_json(normalized_path, normalized)
        state.setdefault("most", {})
        existing = state["most"].get(work_package_id, {})
        state["most"][work_package_id] = {
            **existing,
            **work_package,
            "status": "received",
            "lifecycle_status": "most_received",
            "retryable": False,
            "failure_reason": None,
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
            "received_for_trigger_run_id": received_trigger_run_id or expected_trigger_run_id or None,
        }
        required = list(state.get("required_most_work_package_ids") or process.get("required_work_package_ids") or [])
        remaining = [item for item in required if (state["most"].get(item) or {}).get("status") != "received"]
        state["missing_outputs"] = [f"most:{item}" for item in remaining]
        if not remaining:
            state["status"] = "most_received"
            state["current_step"] = "Step 4 Final Calculation"
            state["most"].update({
                "status": "most_received",
                "lifecycle_status": "most_received",
                "retryable": False,
                "failure_reason": None,
            })
        elif state.get("status") != "most_triggered":
            state["status"] = "most_triggered"
            state["current_step"] = "Step 3 MOST Agent"
            state["most"].update({
                "status": "trigger_request_accepted",
                "lifecycle_status": "awaiting_most_callback",
            })
        _save_state(state)
        append_workflow_event(project_code, product_id, "save_most_output_completed", work_package_id=work_package_id, correlation_id=correlation_id, status_before=status_before, status_after=state.get("status"), raw_path=_relative(raw_path), normalized_path=_relative(normalized_path))
        if not remaining:
            append_workflow_event(project_code, product_id, "all_most_outputs_received", work_package_id=work_package_id, correlation_id=correlation_id, status_before=status_before, status_after="most_received")
        return {
            "success": True,
            "status": "saved",
            "project_code": project_code,
            "product_id": product_id,
            "work_package_id": work_package_id,
            "raw_most_saved": raw_path.exists(),
            "normalized_most_saved": normalized_path.exists(),
            "state_status_after": state.get("status"),
            "remaining_work_packages": remaining,
            "state": state,
        }
    except Exception as exc:
        append_workflow_event(project_code, product_id, "save_most_output_failed", work_package_id=work_package_id, correlation_id=correlation_id, status_before=status_before, error=str(exc))
        raise


def get_most_output(project_code: str, product_id: str, work_package_id: str) -> Dict[str, Any]:
    work_package_id = _safe_part(work_package_id, "work_package_id")
    raw_path = _most_output_path(project_code, product_id, work_package_id)
    normalized_path = _normalized_most_output_path(project_code, product_id, work_package_id)
    if not raw_path.exists() and not normalized_path.exists():
        return {"status": "missing", "project_code": project_code, "product_id": product_id, "work_package_id": work_package_id}
    return {
        "status": "found",
        "project_code": project_code,
        "product_id": product_id,
        "work_package_id": work_package_id,
        "raw_most": _read_json(raw_path, None),
        "normalized_most": _read_json(normalized_path, None),
        "paths": {"raw": _relative(raw_path), "normalized": _relative(normalized_path)},
    }


def get_most_outputs(project_code: str, product_id: str) -> Dict[str, Any]:
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found.")
    process = state.get("process_decomposition") or {}
    outputs = [get_most_output(project_code, product_id, item["work_package_id"]) for item in process.get("work_packages") or [] if item.get("status") != "blocked"]
    return {"status": "found", "project_code": project_code, "product_id": product_id, "most_outputs": outputs}


def _get_path(data: Any, path: List[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_value(data: Dict[str, Any], paths: List[List[str]]) -> Any:
    for path in paths:
        value = _get_path(data, path)
        if value not in [None, ""]:
            return value
    return None


def _float(value: Any) -> Optional[float]:
    if value in [None, ""] or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _saved_bom_quantity_map(raw_bom: Dict[str, Any]) -> Dict[str, float]:
    quantities: Dict[str, float] = {}
    for index, component in enumerate(_extract_component_list(raw_bom), start=1):
        component_id = _component_id(component, index)
        quantity = _float(
            component.get("quantity_per_product")
            or component.get("quantity")
            or component.get("qty")
            or component.get("quantity_value")
        )
        quantities[component_id] = quantity if quantity not in [None, 0] else 1.0
    return quantities


def _saved_bom_dimensional_map(raw_bom: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per-component dimensional/technical fields, kept distinct from any
    single "quantity_per_product" so BOM piece count, physical length,
    physical mass, and a supplier's priced quantity are never conflated.
    See services/choke_component_costing.py for how these feed cost calc."""
    dimensional: Dict[str, Dict[str, Any]] = {}
    for index, component in enumerate(_extract_component_list(raw_bom), start=1):
        component_id = _component_id(component, index)
        dimensional[component_id] = {
            "component_family": _component_family(component_id, component),
            **component_costing.extract_bom_dimensional_fields(component_id, component),
        }
    return dimensional


def _saved_component_cost(raw: Dict[str, Any]) -> Optional[float]:
    return _float(_first_value(raw, [
        ["normalized_cost", "material_cost_per_piece"],
        ["normalized_cost", "delivered_cost_per_piece"],
        ["recommended_offer", "supply_chain", "material_cost"],
        ["recommended_offer", "supply_chain", "delivered_cost"],
        ["recommended_offer", "material_cost"],
        ["recommended_offer", "delivered_cost"],
        ["material_cost_per_piece"],
        ["delivered_cost_per_piece"],
        ["material_cost"],
        ["delivered_cost"],
        ["cost_per_piece"],
    ]))


def _saved_component_currency(raw: Dict[str, Any]) -> str:
    return normalize_currency_code(_first_value(raw, [
        ["normalized_cost", "currency"],
        ["recommended_offer", "unit_price_currency"],
        ["recommended_offer", "price_currency"],
        ["recommended_offer", "offer_currency"],
        ["recommended_offer", "supply_chain", "currency"],
        ["recommended_offer", "currency"],
        ["currency"],
    ])) or ""


def _saved_transport_value(raw: Dict[str, Any], names: List[str]) -> float:
    paths = []
    for name in names:
        paths.extend([
            ["recommended_offer", "supply_chain", name],
            ["supply_chain", name],
            ["normalized_cost", name],
            [name],
        ])
    return _float(_first_value(raw, paths)) or 0.0


def _load_saved_component_outputs(project_code: str, product_id: str) -> List[Dict[str, Any]]:
    component_dir = _run_dir(project_code, product_id) / "agent_outputs" / "components"
    return [
        _normalize_component_output(path)
        for path in sorted(component_dir.glob("*.json"))
    ] if component_dir.exists() else []


def _load_saved_most_outputs(project_code: str, product_id: str) -> List[Dict[str, Any]]:
    most_dir = _run_dir(project_code, product_id) / "agent_outputs" / "most"
    return [
        _normalize_most_output(path)
        for path in sorted(most_dir.glob("*.json"))
    ] if most_dir.exists() else []


def _unit_data_for_final_calculation(
    state: Dict[str, Any],
    customer_input: Dict[str, Any],
    unit_data_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if unit_data_override:
        return unit_data_override
    unit_data = state.get("unit_data") or {}
    if unit_data:
        return unit_data
    manufacturing_strategy = state.get("manufacturing_strategy") or {}
    if not manufacturing_strategy:
        manufacturing_strategy = get_master_manufacturing_strategy(
            customer_input.get("product_line"),
            customer_input.get("product"),
            customer_input.get("customer_delivery_zone"),
        )
    return get_master_unit_data(manufacturing_strategy.get("production_plant"))


def _plant_unit_missing(unit_data: Dict[str, Any]) -> bool:
    required = [
        "dl_rate_operating_per_hour",
        "voh_rate_operating_per_hour",
        "foh_percent_dc",
        "fee_percent_dc",
        "open_hours_per_year",
        "operating_currency",
        "selling_currency",
    ]
    return any(unit_data.get(key) in [None, "", 0] for key in required)


def calculate_final_choke_costing_from_saved_outputs(
    project_code: str,
    product_id: str,
    unit_data_override: Optional[Dict[str, Any]] = None,
    fx_rates_override: Any = None,
    result_mode: str = "firm",
) -> Dict[str, Any]:
    result_mode = str(result_mode or "firm").strip().lower()
    if result_mode not in {"firm", "preliminary"}:
        raise ValueError("result_mode must be 'firm' or 'preliminary'.")
    warnings: List[str] = []
    missing_inputs: List[str] = []
    state = _load_state(project_code, product_id)
    input_file = state.get("input_file")
    customer_input = state.get("customer_input") or {}
    if input_file:
        try:
            loaded_input = _load_customer_input(input_file)
            customer_input = {**loaded_input, **customer_input}
        except Exception as exc:
            warnings.append(f"customer input could not be loaded from workflow state: {exc}")

    raw_bom = _read_json(_bom_raw_path(project_code, product_id), None)
    if raw_bom is None:
        missing_inputs.append("bom")
        raw_bom = {}
    normalized_bom = _read_json(_bom_normalized_path(project_code, product_id), {}) or {}
    dimensional_by_component = _saved_bom_dimensional_map(raw_bom)
    ferrite_fields = dimensional_by_component.get("ferrite_core") or {}
    ferrite_length_resolution = component_costing.resolve_ferrite_length_mm(
        normalized_bom, raw_bom
    )
    if ferrite_length_resolution.get("status") == "resolved":
        ferrite_fields["ferrite_length_mm"] = (
            ferrite_length_resolution["ferrite_length_mm"]
        )
    glue_fields = dimensional_by_component.get("glue")
    provisional_glue = None
    if glue_fields is not None and not any(
        glue_fields.get(field) not in (None, "")
        for field in ("weight_kg_per_product", "physical_mass_g_per_product")
    ):
        provisional_glue = component_costing.calculate_provisional_glue_consumption(
            ferrite_fields.get("ferrite_length_mm"),
            approved=bool(
                glue_fields.get("assumption_approved")
                or customer_input.get("glue_consumption_approved") is True
            ),
            source_field_path=ferrite_length_resolution.get(
                "source_field_path"
            ),
            source_evidence=ferrite_length_resolution.get("source_evidence"),
        )
        if provisional_glue.get("status") in {
            "resolved", "resolved_assumption"
        }:
            glue_fields["physical_mass_g_per_product"] = (
                provisional_glue["glue_mass_g_per_product"]
            )
            glue_fields["provisional_glue_consumption"] = provisional_glue

    component_outputs = _load_saved_component_outputs(project_code, product_id)
    if not component_outputs:
        missing_inputs.append("component_outputs")

    component_breakdown = []
    transport_breakdown = []
    material_cost_per_piece = Decimal("0")
    delivered_material_cost_per_piece = Decimal("0")
    transport_cost_per_piece = Decimal("0")
    unresolved_material_components: List[Dict[str, Any]] = []
    unresolved_logistics_adders: List[Dict[str, Any]] = []
    output_component_ids = set()
    unit_data = _unit_data_for_final_calculation(state, customer_input, unit_data_override)
    unit_data = {
        **unit_data,
        "operating_currency": normalize_currency_code(unit_data.get("operating_currency")),
        "selling_currency": normalize_currency_code(unit_data.get("selling_currency")),
    }
    project_currency = resolve_project_currency(customer_input.get("currency"), unit_data.get("selling_currency"))
    for component in component_outputs:
        raw = component.get("agent_raw_output") or component
        component_id = component.get("component_id") or raw.get("component_id")
        output_component_ids.add(component_id)
        bom_fields = dimensional_by_component.get(component_id, {})
        pricing_quantity_info = component_costing.resolve_component_pricing_quantity(
            component_id, bom_fields.get("component_family"), bom_fields, raw,
        )
        price_info = component_costing.resolve_unit_price(raw, target_currency=project_currency)
        source_material_result = component_costing.compute_component_material_cost(
            component_id, pricing_quantity_info, price_info,
        )
        if (
            source_material_result.get("status") == "calculated"
            and (price_info.get("fx") or {}).get("status") == "already_converted"
        ):
            material_result = {
                **source_material_result,
                "source_currency": (price_info["fx"] or {}).get("source_currency"),
                "currency": project_currency,
                "material_cost_per_product_source_currency": None,
                "converted_to_project_currency": True,
                "fx": price_info["fx"],
            }
        else:
            material_result = component_costing.convert_component_cost_to_project_currency(
                component_id, source_material_result, project_currency, fx_rates=fx_rates_override,
            )
        if material_result["status"] == "blocked":
            line_material_cost = None
            line_material_decimal = None
        else:
            line_material_cost = material_result["material_cost_per_product"]
            line_material_decimal = (
                Decimal(str(pricing_quantity_info.get("pricing_quantity")))
                * Decimal(str(price_info.get("unit_price")))
                if pricing_quantity_info.get("pricing_quantity") is not None
                and price_info.get("unit_price") is not None
                and normalize_currency_code(material_result.get("currency"))
                == normalize_currency_code(price_info.get("unit_price_currency"))
                else Decimal(str(line_material_cost))
            )
        delivered_result = component_costing.resolve_delivered_unit_cost(
            raw, project_currency, fx_rates=fx_rates_override,
        )
        delivered_basis = delivered_result.get("pricing_unit")
        pricing_unit = pricing_quantity_info.get("pricing_unit")
        pricing_quantity = pricing_quantity_info.get("pricing_quantity")
        delivered_basis_compatible = (
            delivered_result.get("status") == "calculated"
            and delivered_basis == pricing_unit
            and pricing_quantity is not None
        )
        if delivered_basis_compatible:
            line_delivered_decimal = (
                Decimal(str(pricing_quantity))
                * Decimal(
                    delivered_result.get("reported_delivered_unit_cost_exact")
                    or delivered_result.get("calculated_delivered_unit_cost_exact")
                    or str(delivered_result["delivered_cost_per_pricing_unit"])
                )
            )
            line_delivered_cost = float(line_delivered_decimal)
            line_transport_decimal = (
                max(
                    line_delivered_decimal - line_material_decimal,
                    Decimal("0"),
                )
                if line_material_cost is not None else None
            )
            line_transport = (
                float(line_transport_decimal)
                if line_transport_decimal is not None else None
            )
            logistics_source = delivered_result.get("calculation_source")
        else:
            line_delivered_cost = None
            line_transport = None
            logistics_source = delivered_result.get("calculation_source")
            if delivered_result.get("status") != "calculated":
                unresolved_logistics_adders.append({
                    "component_id": component_id,
                    "field": "delivered_cost",
                    "reason": delivered_result.get("reason"),
                    "reported_delivered_unit_cost": delivered_result.get(
                        "reported_delivered_unit_cost"
                    ),
                    "calculated_delivered_unit_cost": delivered_result.get(
                        "calculated_delivered_unit_cost"
                    ),
                    "reconciliation_difference": delivered_result.get(
                        "reconciliation_difference"
                    ),
                })
                missing_inputs.append(
                    f"component_outputs:{component_id}:"
                    f"{delivered_result.get('reason') or 'delivered_cost_unresolved'}"
                )
            elif pricing_quantity is not None and delivered_basis != pricing_unit:
                unresolved_logistics_adders.append({
                    "component_id": component_id,
                    "field": "delivered_cost",
                    "reason": "delivered_cost_pricing_unit_mismatch",
                    "reported_pricing_unit": delivered_basis,
                    "expected_pricing_unit": pricing_unit,
                })
                missing_inputs.append(
                    f"component_outputs:{component_id}:"
                    "delivered_cost_pricing_unit_mismatch"
                )

        for excluded in delivered_result.get("excluded_adders") or []:
            unresolved_logistics_adders.append({
                "component_id": component_id,
                **excluded,
                "covered_by_delivered_cost": False,
            })
        if (
            delivered_result.get("excluded_adders")
        ):
            missing_inputs.append(f"component_outputs:{component_id}:logistics_adders_unresolved")

        currency = _saved_component_currency(raw)
        canonical_currency = (
            delivered_result.get("delivered_cost_currency")
            if delivered_basis_compatible else material_result.get("currency")
        )
        canonical_fields = {
            "technical_quantity": pricing_quantity,
            "technical_quantity_unit": (
                f"{pricing_unit}/product" if pricing_unit else None
            ),
            "pricing_quantity": pricing_quantity,
            "pricing_unit": pricing_unit,
            "unit_cost": price_info.get("unit_price"),
            "currency": canonical_currency,
            "material_cost_per_piece": line_material_cost,
            "delivered_material_cost_per_piece": line_delivered_cost,
        }
        missing_canonical_fields = [
            name for name, value in canonical_fields.items()
            if value in (None, "")
        ]
        reconciliation_difference = delivered_result.get(
            "reconciliation_difference"
        )
        reconciliation_valid = (
            reconciliation_difference is not None
            and abs(Decimal(str(reconciliation_difference)))
            <= component_costing.DELIVERED_COST_RECONCILIATION_TOLERANCE
        )
        uses_provisional_glue = (
            component_id == "glue"
            and isinstance(
                bom_fields.get("provisional_glue_consumption"), dict
            )
        )
        component_status = (
            "resolved"
            if not missing_canonical_fields
            and material_result.get("status") == "calculated"
            and delivered_result.get("status") == "calculated"
            and reconciliation_valid
            else "blocked"
        )
        if component_status == "resolved" and uses_provisional_glue:
            component_status = (
                "resolved"
                if bom_fields["provisional_glue_consumption"].get("approved")
                else "resolved_assumption"
            )
            provisional_warning = (
                bom_fields["provisional_glue_consumption"].get("warning")
            )
            if provisional_warning:
                warnings.append(provisional_warning)
        component_blocking_reason = None
        if component_status == "blocked":
            component_blocking_reason = (
                material_result.get("reason")
                if material_result.get("status") != "calculated" else None
            ) or (
                delivered_result.get("reason")
                if delivered_result.get("status") != "calculated" else None
            ) or (
                "delivered_cost_reconciliation_mismatch"
                if not reconciliation_valid else None
            ) or (
                f"{missing_canonical_fields[0]}_missing"
                if missing_canonical_fields else "component_cost_unresolved"
            )
            missing_inputs.append(
                f"component_outputs:{component_id}:{component_blocking_reason}"
            )
            unresolved_material_components.append({
                "component_id": component_id,
                "reason": component_blocking_reason,
                "message": (
                    "Glue consumption per product required."
                    if component_id == "glue"
                    and component_blocking_reason
                    == "technical_quantity_unit_unknown"
                    else None
                ),
            })
        else:
            material_cost_per_piece += line_material_decimal
            delivered_material_cost_per_piece += line_delivered_decimal
            transport_cost_per_piece += line_transport_decimal
            warnings.extend(pricing_quantity_info.get("warnings") or [])

        component_breakdown.append({
            "component_id": component_id,
            "technical_quantity": pricing_quantity_info.get("pricing_quantity"),
            "technical_quantity_unit": (
                f"{pricing_quantity_info['pricing_unit']}/product"
                if pricing_quantity_info.get("pricing_unit") else None
            ),
            "pricing_quantity": pricing_quantity_info.get("pricing_quantity"),
            "pricing_unit": pricing_quantity_info.get("pricing_unit"),
            "pricing_quantity_basis": pricing_quantity_info.get("pricing_quantity_basis"),
            "unit_price": price_info.get("unit_price"),
            "original_unit_price": (price_info.get("normalized_offer") or {}).get("unit_price"),
            "original_currency": (price_info.get("normalized_offer") or {}).get("currency"),
            "converted_unit_price": (
                price_info.get("unit_price")
                if (price_info.get("fx") or {}).get("status") == "already_converted"
                else None
            ),
            "base_unit_cost": delivered_result.get("base_unit_cost"),
            "unit_material_or_delivered_cost": price_info.get("unit_price"),
            "material_cost_per_piece": line_material_cost,
            "material_cost_per_piece_exact": (
                format(line_material_decimal, "f")
                if line_material_decimal is not None else None
            ),
            "delivered_cost_per_pricing_unit": delivered_result.get(
                "delivered_cost_per_pricing_unit"
            ),
            "delivered_material_cost_per_piece": line_delivered_cost,
            "delivered_material_cost_per_piece_exact": (
                format(line_delivered_decimal, "f")
                if delivered_basis_compatible else None
            ),
            "transport_cost_per_piece": line_transport,
            "delivered_cost_source": delivered_result.get("calculation_source"),
            "included_adders": delivered_result.get("included_adders") or [],
            "excluded_adders": delivered_result.get("excluded_adders") or [],
            "adjustments": delivered_result.get("adjustments") or [],
            "delivered_cost_formula": delivered_result.get(
                "delivered_cost_formula"
            ),
            "calculated_delivered_unit_cost": delivered_result.get(
                "calculated_delivered_unit_cost"
            ),
            "calculated_delivered_unit_cost_exact": delivered_result.get(
                "calculated_delivered_unit_cost_exact"
            ),
            "reported_delivered_unit_cost": delivered_result.get(
                "reported_delivered_unit_cost"
            ),
            "reported_delivered_unit_cost_exact": delivered_result.get(
                "reported_delivered_unit_cost_exact"
            ),
            "reconciliation_difference": delivered_result.get(
                "reconciliation_difference"
            ),
            "reconciliation_difference_exact": delivered_result.get(
                "reconciliation_difference_exact"
            ),
            "rounding_policy": delivered_result.get("rounding_policy"),
            "logistics_source": logistics_source,
            "source_currency": currency,
            "currency": canonical_currency,
            "normalized_offer": price_info.get("normalized_offer"),
            "ap_terms": component_costing.resolve_component_ap_terms(raw),
            "fx": price_info.get("fx") or material_result.get("fx"),
            "warnings": pricing_quantity_info.get("warnings") or [],
            "assumption_details": (
                bom_fields.get("provisional_glue_consumption")
                if uses_provisional_glue else None
            ),
            "classification": "External",
            "source": "saved_component_json",
            "status": component_status,
            "blocking_reason": component_blocking_reason,
        })
        included_adders_per_product = []
        if pricing_quantity is not None:
            for adder in delivered_result.get("included_adders") or []:
                converted_value = adder.get("converted_value")
                included_adders_per_product.append({
                    **adder,
                    "cost_per_product": (
                        float(
                            Decimal(str(pricing_quantity))
                            * Decimal(str(converted_value))
                        )
                        if converted_value is not None else None
                    ),
                })
        transport_breakdown.append({
            "component_id": component_id,
            "pricing_quantity": pricing_quantity_info.get("pricing_quantity"),
            "pricing_unit": pricing_quantity_info.get("pricing_unit"),
            "included_adders": included_adders_per_product,
            "transport_cost_per_piece": line_transport,
            "currency": project_currency,
            "status": (
                "calculated"
                if component_status in {"resolved", "resolved_assumption"}
                else "blocked"
            ),
            "calculation_source": logistics_source,
            "excluded_adders": delivered_result.get("excluded_adders") or [],
        })

    for component_id in dimensional_by_component:
        if component_id not in output_component_ids and component_id not in {"lead_tin_plating", "tin_plating"}:
            warnings.append(f"no saved component JSON found for BOM component {component_id}")

    most_outputs = _load_saved_most_outputs(project_code, product_id)
    if not most_outputs:
        missing_inputs.append("most_outputs")

    if _plant_unit_missing(unit_data):
        missing_inputs.append("plant_unit_data")

    annual_quantity = customer_input.get("annual_quantity")
    dl_voh = calculate_dl_voh(most_outputs, unit_data, annual_quantity)
    if dl_voh.get("status") == "blocked":
        for item in dl_voh.get("missing_inputs") or []:
            if item in {
                "dl_rate_operating_per_hour",
                "voh_rate_operating_per_hour",
                "open_hours_per_year",
                "operating_currency/selling_currency",
                "fx_operating_to_selling",
            }:
                missing_inputs.append("plant_unit_data")
            else:
                missing_inputs.append(item)

    dl_cost = dl_voh.get("dl_cost_per_piece")
    voh_cost = dl_voh.get("voh_cost_per_piece")
    foh_percent = _float(unit_data.get("foh_percent_dc")) or 0.0
    fee_percent = _float(unit_data.get("fee_percent_dc")) or 0.0
    unique_missing = list(dict.fromkeys(missing_inputs))
    material_component_count = len(component_outputs)
    resolved_component_count = sum(
        1 for item in component_breakdown
        if item.get("status") in {"resolved", "resolved_assumption"}
    )
    completeness = {
        "resolved_component_count": resolved_component_count,
        "total_component_count": material_component_count,
        "unresolved_component_count": len(unresolved_material_components),
        "percentage": (
            round(resolved_component_count / material_component_count * 100, 2)
            if material_component_count else 0.0
        ),
    }

    # Olivier's preliminary plant-percentage formula:
    #   direct_cost = dl + voh + transport
    #   foh = direct_cost * foh_percent_dc / 100 ; fee = direct_cost * fee_percent_dc / 100
    # Only computed once every input is fully resolved (no blocked component,
    # no blocked MOST scope) so a "blocked" result never carries partial numbers.
    direct_cost = None
    foh_cost = None
    fee_cost = None
    manufacturing_cost = None
    core_blockers = [
        item for item in unique_missing
        if item in {"bom", "component_outputs", "most_outputs", "plant_unit_data"}
        or not str(item).startswith("component_outputs:")
    ]
    blocking_logistics = [
        item
        for item in unresolved_logistics_adders
        if not item.get("covered_by_delivered_cost")
    ]
    calculation_permitted = (
        not core_blockers
        and not blocking_logistics
        and dl_cost is not None
        and voh_cost is not None
        and (not unresolved_material_components or result_mode == "preliminary")
    )
    if calculation_permitted:
        direct_cost = dl_cost + voh_cost + float(transport_cost_per_piece)
        foh_cost = foh_percent / 100 * direct_cost
        fee_cost = fee_percent / 100 * direct_cost
        manufacturing_cost = direct_cost + foh_cost + fee_cost

    if core_blockers:
        result_status = "blocked"
    elif unresolved_material_components or blocking_logistics:
        result_status = (
            "preliminary_incomplete" if result_mode == "preliminary" else "blocked"
        )
    else:
        result_status = "calculated"

    assumption_components = [
        item["component_id"] for item in component_breakdown
        if item.get("status") == "resolved_assumption"
    ]
    technical_preliminary_status = (
        "blocked" if core_blockers else
        "resolved_assumption" if assumption_components else
        "calculated"
    )
    technical_firm_blockers = [
        *unique_missing,
        *(
            [f"component_outputs:{item}:assumption_approval_required"
             for item in assumption_components]
        ),
    ]
    technical_firm_status = (
        "blocked" if technical_firm_blockers else "calculated"
    )
    if result_mode == "firm" and assumption_components:
        result_status = "blocked"
    result_missing_inputs = list(dict.fromkeys([
        *unique_missing,
        *(
            [
                f"component_outputs:{item}:assumption_approval_required"
                for item in assumption_components
            ]
            if result_mode == "firm" else []
        ),
    ]))

    result = {
        "project_code": project_code,
        "product_id": product_id,
        **classification_trace(state.get("choke_classification")),
        "process_decomposition": state.get("process_decomposition") or {},
        "status": result_status,
        "result_mode": result_mode,
        "technical_preliminary_status": technical_preliminary_status,
        "technical_firm_status": technical_firm_status,
        "technical_firm_blockers": list(dict.fromkeys(technical_firm_blockers)),
        "ferrite_length_resolution": ferrite_length_resolution,
        "provisional_glue_consumption": provisional_glue,
        "commercially_usable": (
            result_status == "calculated"
            and all(
                (component.get("agent_raw_output") or component).get("commercially_usable")
                is True
                for component in component_outputs
            )
        ),
        "costing_method": "preliminary_plant_percentage_dc",
        "currency": project_currency or "",
        "material_cost_per_piece": (
            None if not component_outputs else float(material_cost_per_piece)
        ),
        "calculated_material_cost_for_resolved_components": (
            None if not component_outputs else float(material_cost_per_piece)
        ),
        "calculated_material_cost_exact": (
            None if not component_outputs else format(material_cost_per_piece, "f")
        ),
        "calculated_delivered_material_cost_for_resolved_components": (
            None if not component_outputs else float(delivered_material_cost_per_piece)
        ),
        "delivered_material_cost_per_piece": (
            None if not component_outputs else float(delivered_material_cost_per_piece)
        ),
        "calculated_delivered_material_cost_exact": (
            None if not component_outputs
            else format(delivered_material_cost_per_piece, "f")
        ),
        "transport_cost_per_piece": (
            None if not component_outputs else float(transport_cost_per_piece)
        ),
        "transport_cost_per_piece_exact": (
            None if not component_outputs else format(transport_cost_per_piece, "f")
        ),
        "base_material_cost_per_piece": (
            None if not component_outputs else float(material_cost_per_piece)
        ),
        "logistics_cost_per_piece": (
            None if not component_outputs else float(transport_cost_per_piece)
        ),
        "dl_cost_per_piece": dl_cost,
        "voh_cost_per_piece": voh_cost,
        "direct_cost_per_piece": direct_cost,
        "added_value_direct_cost_per_piece": direct_cost,
        "foh_percent_dc": foh_percent,
        "foh_cost_per_piece": foh_cost,
        "fee_percent_dc": fee_percent,
        "fee_cost_per_piece": fee_cost,
        "manufacturing_cost_per_piece": manufacturing_cost,
        "manufacturing_added_value_cost_per_piece": manufacturing_cost,
        "total_cost_before_commercial_items_per_piece": (
            None
            if manufacturing_cost is None
            else float(material_cost_per_piece) + manufacturing_cost
        ),
        "foh_basis": "added_value_direct_cost",
        "fee_basis": "added_value_direct_cost",
        "component_breakdown": component_breakdown,
        "transport_breakdown_by_component": transport_breakdown,
        "unresolved_material_components": unresolved_material_components,
        "unresolved_logistics_adders": unresolved_logistics_adders,
        "blocking_unresolved_logistics_adders": blocking_logistics,
        "material_completeness": completeness,
        "most_breakdown_by_scope": dl_voh.get("work_package_calculation") or [],
        "missing_inputs": result_missing_inputs,
        "warnings": list(dict.fromkeys([
            *warnings,
            *(
                ["Preliminary totals exclude unresolved component costs and are not quotation-ready."]
                if result_status == "preliminary_incomplete" else []
            ),
        ])),
    }
    output_path = _run_dir(project_code, product_id) / "final_choke_costing_result.json"
    result["save_path"] = _write_json(output_path, result)
    if _state_path(project_code, product_id).exists():
        state["workflow_status"] = state.get("status")
        state["bom_status"] = (state.get("bom") or {}).get("status")
        state["component_status"] = (
            "received"
            if component_outputs
            and all(
                item.get("status") == "resolved"
                for item in component_breakdown
                if item.get("component_id") != "glue" or result_mode == "firm"
            )
            else "partial"
        )
        state["most_status"] = (state.get("most") or {}).get("status")
        state["final_calculation_status"] = result_status
        state.setdefault("financial_status", "not_calculated")
        _save_state(state)
    return result


def _normalize_component_output(path: Path) -> Dict[str, Any]:
    raw = _read_json(path, {}) or {}
    component_id = raw.get("component_id") or path.stem
    delivered_cost = _float(_first_value(raw, [
        ["normalized_cost", "delivered_cost_per_piece"],
        ["recommended_offer", "supply_chain", "delivered_cost"],
        ["recommended_offer", "delivered_cost"],
        ["delivered_cost_per_piece"],
        ["delivered_cost"],
    ]))
    material_cost = _float(_first_value(raw, [
        ["normalized_cost", "material_cost_per_piece"],
        ["material_cost_per_piece"],
    ]))
    normalized_offer = component_costing.resolve_component_offer(raw)
    currency = normalized_offer.get("currency")
    normalized_cost = dict(raw.get("normalized_cost") or {})
    normalized_cost.update({
        "currency": currency or normalize_currency_code(normalized_cost.get("currency")) or "",
        "material_cost_per_piece": material_cost if material_cost is not None else delivered_cost,
        "delivered_cost_per_piece": delivered_cost,
        "commercially_usable": bool(normalized_cost.get("commercially_usable")),
        "missing_inputs": normalized_cost.get("missing_inputs") or [],
    })
    return {
        **raw,
        "component_id": component_id,
        "component_type": raw.get("component_type") or raw.get("component_family") or "",
        "normalized_cost": normalized_cost,
        "normalized_offer": normalized_offer,
        "agent_raw_output": raw,
    }


def _normalize_most_output(path: Path) -> Dict[str, Any]:
    """Adapts a saved MOST output into the shape calculate_dl_voh expects.
    MOST agent outputs commonly nest p_h/operator_percent/CAPEX/tooling
    fields under `station_library_summary` rather than at the operation's
    top level; every field here checks both locations. An operation that
    explicitly reports p_h=0 and operator_percent=0 (a station with no
    internal AVOCarbon operation) is preserved as explicit zero rather than
    being treated the same as a genuinely missing field — calculate_dl_voh
    is responsible for not blocking on that specific combination."""
    raw = _read_json(path, {}) or {}
    work_package_id = raw.get("work_package_id") or path.stem
    normalized_operation = raw.get("normalized_operation") or raw.get("operation_details") or raw
    cycle_time = _float(_first_value(normalized_operation, [["cycle_time_seconds"], ["operation_cycle_time_seconds"]]))
    p_h = _float(_first_value(normalized_operation, [
        ["p_h"], ["station_library_summary", "p_h"], ["rate_per_hour_instantaneous"],
    ]))
    parts_per_cycle = _float(_first_value(normalized_operation, [
        ["parts_per_cycle"], ["station_library_summary", "parts_per_cycle"], ["pieces_per_cycle"],
    ])) or 1.0
    if p_h is None and cycle_time not in [None, 0]:
        p_h = 3600 / cycle_time * parts_per_cycle
    output = {
        **raw,
        "work_package_id": work_package_id,
        "component_id": raw.get("component_id") or normalized_operation.get("component_id"),
        "operation_id": raw.get("operation_id") or normalized_operation.get("operation_id"),
        "operation_name": raw.get("operation_name") or normalized_operation.get("operation_name") or raw.get("operation"),
        "p_h": p_h,
        "oee": _first_value(normalized_operation, [
            ["oee"], ["oee_percent"], ["costing_oee_percent"], ["station_library_summary", "oee"],
        ]),
        "operator_percent": _first_value(normalized_operation, [
            ["operator_percent"], ["percent_operator"], ["station_library_summary", "operator_percent"],
        ]),
        "parts_per_cycle": parts_per_cycle,
        "generic_capex_eur": _first_value(normalized_operation, [
            ["generic_capex_eur"], ["generic_capex"], ["station_library_summary", "generic_capex_eur"],
        ]),
        "specific_capex_eur": _first_value(normalized_operation, [
            ["specific_capex_eur"], ["specific_capex"], ["station_library_summary", "specific_capex_eur"],
        ]),
        "tooling_cost_eur": _first_value(normalized_operation, [
            ["tooling_cost_eur"], ["tooling_cost"], ["station_library_summary", "tooling_cost_eur"],
        ]),
        "tooling_life_pieces": _first_value(normalized_operation, [
            ["tooling_life_pieces"], ["station_library_summary", "tooling_life_pieces"],
        ]),
        "tooling_type": _first_value(normalized_operation, [
            ["tooling_type"], ["station_library_summary", "tooling_type"],
        ]),
        "tooling_adder_per_piece_eur": _first_value(normalized_operation, [
            ["tooling_adder_per_piece_eur"], ["station_library_summary", "tooling_adder_per_piece_eur"],
        ]),
        "capex_currency": normalize_currency_code(_first_value(normalized_operation, [
            ["capex_currency"], ["station_library_summary", "capex_currency"],
        ])),
        "tooling_currency": normalize_currency_code(_first_value(normalized_operation, [
            ["tooling_currency"], ["station_library_summary", "tooling_currency"],
        ])),
        "agent_raw_output": raw,
    }
    return output


def calculate_from_real_outputs(project_code: str, product_id: str) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    input_file = state.get("input_file")
    if not input_file:
        raise ValueError("workflow_state input_file is missing")
    customer_input = _load_customer_input(input_file)
    if customer_input.get("annual_quantity") in [None, "", 0]:
        raise ValueError("Final costing needs annual_quantity.")
    raw_bom = _read_json(_bom_raw_path(project_code, product_id), None)
    if raw_bom is None:
        raise FileNotFoundError("BOM output is missing")

    component_dir = _run_dir(project_code, product_id) / "agent_outputs" / "components"
    most_dir = _run_dir(project_code, product_id) / "agent_outputs" / "most"
    component_outputs = [
        _normalize_component_output(path)
        for path in sorted(component_dir.glob("*.json"))
    ] if component_dir.exists() else []
    most_outputs = [
        _normalize_most_output(path)
        for path in sorted(most_dir.glob("*.json"))
    ] if most_dir.exists() else []

    envelope = run_choke_orchestration(
        customer_input,
        dry_run=True,
        trigger_agents=False,
        bom_json=raw_bom,
        component_cost_outputs=component_outputs,
        most_outputs=most_outputs,
        demo_override=False,
    )

    most_raw_by_id = {
        item.get("work_package_id"): item.get("agent_raw_output")
        for item in most_outputs
        if item.get("work_package_id")
    }
    for work_package in envelope.get("most_work_packages") or []:
        work_package_id = work_package.get("work_package_id")
        if work_package_id in most_raw_by_id:
            work_package["most_status"] = "available"
            work_package["agent_raw_output"] = most_raw_by_id[work_package_id]

    envelope["calculation_source"] = "real_sequential_agent_chain"
    envelope["workflow_state"] = state
    final_result = calculate_final_choke_costing_from_saved_outputs(project_code, product_id)
    envelope["final_choke_costing"] = final_result
    envelope.setdefault("financial_calculation", {}).update({
        "status": final_result.get("status"),
        "currency": final_result.get("currency"),
        "material_cost_per_piece": final_result.get("material_cost_per_piece"),
        "transport_cost_per_piece": final_result.get("transport_cost_per_piece"),
        "dl_cost_per_piece": final_result.get("dl_cost_per_piece"),
        "voh_cost_per_piece": final_result.get("voh_cost_per_piece"),
        "direct_cost_per_piece": final_result.get("direct_cost_per_piece"),
        "foh_percent_dc": final_result.get("foh_percent_dc"),
        "foh_cost_per_piece": final_result.get("foh_cost_per_piece"),
        "fee_percent_dc": final_result.get("fee_percent_dc"),
        "fee_cost_per_piece": final_result.get("fee_cost_per_piece"),
        "manufacturing_cost_per_piece": final_result.get("manufacturing_cost_per_piece"),
        "component_breakdown": final_result.get("component_breakdown"),
        "transport_breakdown_by_component": final_result.get("transport_breakdown_by_component"),
        "most_breakdown_by_scope": final_result.get("most_breakdown_by_scope"),
        "missing_inputs": final_result.get("missing_inputs"),
        "warnings": final_result.get("warnings"),
    })
    output_path = _run_dir(project_code, product_id) / "orchestration_result_real_agent_chain.json"
    envelope["orchestration_result_real_agent_chain_path"] = _write_json(output_path, envelope)

    financial_status = final_result.get("status")
    state["status"] = "calculated" if financial_status != "blocked" else "blocked"
    state["current_step"] = "Step 4 Cost Calculation"
    state["missing_outputs"] = final_result.get("missing_inputs") or []
    _save_state(state)
    return envelope
