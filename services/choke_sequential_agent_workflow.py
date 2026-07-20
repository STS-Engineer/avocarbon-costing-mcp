import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.choke_financial_calculation import calculate_dl_voh
from services.choke_orchestrator import run_choke_orchestration
from services.choke_process_decomposition import decompose_choke_process
from services.costing_master_data_service import (
    get_master_manufacturing_strategy,
    get_master_unit_data,
)
from services.customer_input_schema import normalize_customer_input
from services.manufacturing_strategy import resolve_canonical_product
from services.agent_file_proxy_service import build_agent_file_url, verify_agent_pdf_url
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
from services.workspace_agent_client import clean_agent_id, trigger_workspace_agent


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
    "and raw_json containing the complete native MOST JSON object. "
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
    "In recommended_offer, always provide unit_price, unit_price_currency, and "
    "unit_price_basis (for example CNY/kg, CNY/pc, or CNY/m) describing exactly what "
    "one unit of unit_price_basis represents. Never state a price without its basis. "
    "For each of transportation_cost_per_piece, customs_cost_per_piece, and "
    "forwarder_cost_per_piece that you provide, also provide the matching "
    "transportation_cost_basis, customs_cost_basis, and forwarder_cost_basis field "
    "(for example CNY/kg, CNY/pc, CNY/shipment, or percentage_of_component_value). "
    "Do not report a technical length or mass as a piece quantity."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        "blocked",
    }
    if state.get("status") not in advanced_statuses:
        state["status"] = "bom_received"
        state["current_step"] = "Step 2 External Component Costing Agent"
    state["retry_available"] = False
    state["retryable"] = False
    state["errors"] = remaining_errors
    state["historical_errors"] = historical_errors
    state["bom"] = {
        **existing_bom,
        "status": "received",
        "retryable": False,
        "retry_available": False,
        **({"trigger_result": trigger_result} if trigger_result else {}),
    }
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
    normalized_reference = (
        (state.get("bom") or {}).get("normalized_path")
        or _relative(_bom_normalized_path(project_code, product_id))
    )
    normalized_exists = any(
        path.exists() for path in data_reference_candidates(normalized_reference)
    )
    if (state.get("bom") or {}).get("status") == "received" or normalized_exists:
        was_inconsistent = (
            state.get("status") != "bom_received"
            or (state.get("bom") or {}).get("retryable") is not False
            or state.get("retry_available") is not False
            or any(_is_resolved_bom_trigger_error(error) for error in state.get("errors") or [])
        )
        _apply_bom_received_precedence(state)
        if was_inconsistent:
            _save_state(state)
    if state.get("status") in {"created", "pending"} and raw_exists:
        state["diagnostic_warning"] = "Raw BOM exists but state was not updated correctly."
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
    max_attempts = 1 if dry_run else _positive_int_env("WORKSPACE_AGENT_TRIGGER_MAX_ATTEMPTS", 3)
    backoffs = _trigger_backoff_seconds()
    attempts = []
    last_result: Dict[str, Any] = {}
    idempotency_key = f"{project_code}:{product_id}:sequential:bom:{uuid.uuid4()}"

    for attempt_number in range(1, max_attempts + 1):
        result = _trigger(
            "CHATGPT_CHOKE_BOM_AGENT_ID",
            "Choke BOM Analyzer",
            input_text,
            f"{project_code}:{product_id}:sequential:bom",
            idempotency_key,
            dry_run=dry_run,
        )
        last_result = result or {}
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
                status_after="bom_triggered",
            )
            break
        if not has_next_attempt:
            append_workflow_event(
                project_code,
                product_id,
                "bom_trigger_failed_retryable" if retryable else "bom_trigger_failed_non_retryable",
                attempt_number=attempt_number,
                http_status=last_result.get("http_status"),
                status_before=status_before,
                status_after="bom_trigger_failed_retryable" if retryable else "blocked",
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
) -> Dict[str, Any]:
    _load_env()
    save_address = _relative(_bom_raw_path(project_code, product_id))
    drawing_file_path = normalized_input.get("drawing_file_path")
    generated_local_url = _drawing_file_url_from_path(drawing_file_path, request_base_url)
    drawing_file_url = (
        normalized_input.get("drawing_agent_proxy_url")
        or generated_local_url
        or normalized_input.get("drawing_file_url")
        or normalized_input.get("drawing_sas_url")
        or normalized_input.get("drawing_blob_url")
    )
    drawing_access_mode = normalized_input.get("drawing_access_mode")
    if normalized_input.get("drawing_agent_proxy_url") or generated_local_url:
        drawing_access_mode = "backend_signed_proxy"
    elif not drawing_access_mode:
        if normalized_input.get("drawing_sas_url"):
            drawing_access_mode = "azure_blob_sas"
        elif normalized_input.get("drawing_blob_url"):
            drawing_access_mode = "azure_blob"
        elif drawing_file_url:
            drawing_access_mode = "local"
        else:
            drawing_access_mode = "missing"
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
        "Analyze the drawing according to your permanent agent instructions and call "
        "save_bom_output with the complete BOM JSON."
    )
    payload = {
        "project_code": project_code,
        "product_id": product_id,
        "drawing_file_url": drawing_file_url,
        "drawing_agent_proxy_url": normalized_input.get("drawing_agent_proxy_url") or generated_local_url,
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
        "drawing_agent_proxy_url": normalized_input.get("drawing_agent_proxy_url") or generated_local_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_blob_url": normalized_input.get("drawing_blob_url"),
        "drawing_sas_url": normalized_input.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(drawing_file_url),
        "warnings": warnings,
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
) -> Dict[str, Any]:
    ensure_workflow_storage_ready()
    customer_input = _load_customer_input(input_file)
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

    manufacturing_strategy = get_master_manufacturing_strategy(
        normalized_input.get("product_line"),
        normalized_input.get("product"),
        normalized_input.get("customer_delivery_zone"),
    )
    unit_data = get_master_unit_data(manufacturing_strategy.get("production_plant"))
    path_diagnostics = workflow_path_diagnostics(project_code, product_id)
    logger.info("workflow start path: %s", json.dumps(path_diagnostics, default=str))
    run_dir = _run_dir(project_code, product_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    bom_trigger = _build_bom_trigger_payload(
        project_code,
        product_id,
        normalized_input,
        request_base_url=request_base_url,
    )
    input_text = bom_trigger["input_text"]
    save_address = bom_trigger["save_address"]
    existing_state = _load_state(project_code, product_id)
    status_before = existing_state.get("status")
    state = existing_state
    state.update({
        "input_file": customer_input["_input_file"],
        "drawing_file_path": normalized_input.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_agent_proxy_url": bom_trigger.get("drawing_agent_proxy_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "status": "starting",
        "current_step": "Step 1 BOM Agent",
        "manufacturing_strategy": manufacturing_strategy,
        "unit_data": unit_data,
        "customer_input": normalized_input,
        "components": state.get("components") or {},
        "most": state.get("most") or {},
        "process_decomposition": state.get("process_decomposition"),
        "missing_outputs": ["bom"],
        "warnings": bom_trigger.get("warnings") or [],
    })
    state["bom"] = {
        **dict(state.get("bom") or {}),
        "status": "pending",
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
        "trigger_attempts": [],
        "retryable": False,
        "input_text": input_text,
    }
    _save_state(state)
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

    pdf_url_check = {"success": True, "skipped": bool(dry_run)}
    if not dry_run:
        pdf_url_check = verify_agent_pdf_url(bom_trigger.get("drawing_file_url"))
        if not pdf_url_check.get("success"):
            persisted_state["status"] = "blocked"
            persisted_state["bom"] = {
                **dict(persisted_state.get("bom") or {}),
                "status": "failed",
                "retryable": False,
                "pdf_url_check": pdf_url_check,
            }
            persisted_state.setdefault("errors", []).append({
                "stage": "bom_pdf_access",
                "message": "Agent PDF proxy validation failed; Workspace Agent was not triggered.",
                "details": pdf_url_check,
            })
            _save_state(persisted_state)
            return {
                "message": "Agent PDF proxy validation failed; Workspace Agent was not triggered.",
                "status": "blocked",
                "state": persisted_state,
                "canonical_workflow_state_path": str(persisted_path),
                "workflow_state_exists_before_trigger": True,
                "data_root": str(DATA_ROOT),
                "pdf_url_check": pdf_url_check,
            }
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
        "bom_triggered"
        if accepted
        else "bom_trigger_failed_retryable"
        if retryable_failure
        else "blocked"
    )
    bom_status = (
        "triggered"
        if accepted
        else "trigger_failed_retryable"
        if retryable_failure
        else "failed"
    )
    latest_state, _ = _existing_state(project_code, product_id)
    state = latest_state or persisted_state
    if (state.get("bom") or {}).get("status") != "received":
        state["status"] = workflow_status
        state["current_step"] = "Step 1 BOM Agent"
        state["bom"] = {
            **dict(state.get("bom") or {}),
            "status": bom_status,
            "trigger_result": trigger_result,
            "trigger_attempts": trigger_result.get("attempts") or [],
            "retryable": retryable_failure,
            "pdf_url_check": pdf_url_check,
        }
        if not accepted:
            state.setdefault("errors", []).append({"stage": "bom", "trigger_result": trigger_result})
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
        "message": "BOM Agent triggered first. Waiting for BOM output write-back.",
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
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise FileNotFoundError("Workflow state not found. Start the workflow before retrying the BOM Agent.")
    status_before = state.get("status")
    append_workflow_event(
        project_code,
        product_id,
        "retry_bom_requested",
        status_before=status_before,
        workflow_state_path=str(_state_path(project_code, product_id).resolve()),
    )

    customer_input = dict(state.get("customer_input") or {})
    existing_bom = dict(state.get("bom") or {})
    for key in [
        "drawing_file_path",
        "drawing_file_url",
        "drawing_access_mode",
        "drawing_blob_url",
        "drawing_sas_url",
    ]:
        value = state.get(key) or existing_bom.get(key)
        if value not in [None, ""]:
            customer_input[key] = value
    customer_input.setdefault("project_code", project_code)
    customer_input.setdefault("workflow_product_id", product_id)
    customer_input.setdefault("product_id", product_id)
    bom_trigger = _build_bom_trigger_payload(project_code, product_id, customer_input)
    if not bom_trigger.get("drawing_file_url"):
        raise ValueError("BOM Agent retry requires drawing_file_url in workflow state or customer_input.")

    input_text = existing_bom.get("input_text") or bom_trigger["input_text"]
    trigger_result = _trigger_bom_agent_with_retries(
        project_code=project_code,
        product_id=product_id,
        input_text=input_text,
        dry_run=False,
        status_before=status_before,
    )
    accepted = trigger_result.get("status") == "accepted"
    retryable_failure = not accepted and trigger_result.get("retryable") is True
    state["status"] = (
        "bom_triggered"
        if accepted
        else "bom_trigger_failed_retryable"
        if retryable_failure
        else "blocked"
    )
    state["current_step"] = "Step 1 BOM Agent"
    state["missing_outputs"] = ["bom"]
    state["bom"] = {
        **existing_bom,
        "status": (
            "triggered"
            if accepted
            else "trigger_failed_retryable"
            if retryable_failure
            else "failed"
        ),
        "retryable": retryable_failure,
        "trigger_result": trigger_result,
        "trigger_attempts": trigger_result.get("attempts") or [],
        "input_text": input_text,
        "save_path": existing_bom.get("save_path") or bom_trigger.get("save_address"),
    }
    _save_state(state)
    return {
        "status": state["status"],
        "project_code": project_code,
        "product_id": product_id,
        "bom": state["bom"],
        "trigger_attempts": state["bom"]["trigger_attempts"],
        "state": state,
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


def normalize_bom(raw_bom: Dict[str, Any]) -> Dict[str, Any]:
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
            "excluded_not_required": excluded_not_required,
        }
        components.append(normalized)
        if is_external_costing:
            external_components.append(normalized)
    return {
        "status": "normalized",
        "components": components,
        "external_components": external_components,
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
    if extracted.get("drawing_number"):
        updates["drawing_reference"] = extracted["drawing_number"]

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
    allow_create_without_start: bool = False,
) -> Dict[str, Any]:
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
        status_before = (existing_state or {}).get("status")
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

        normalized = normalize_bom(raw_json)
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
        master_data_refresh = _refresh_master_data_for_state(state)
        existing_bom = dict(state.get("bom") or {})
        state["bom"] = {
            **existing_bom,
            "status": "received",
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
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
            process = decompose_choke_process(raw_bom or {}, state.get("customer_input") or {})
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
        "target_price",
        "sop_date",
        "product",
        "product_name",
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

    customer_input = {**(state.get("customer_input") or {}), **updates}
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
        "state": state,
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
    return {
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        "component_id": component_id,
        "component_name": component.get("component"),
        "component_family": component.get("external_component_type") or component.get("category"),
        "classification": "External",
        "product": customer_input.get("product"),
        "product_line": customer_input.get("product_line") or "Chokes",
        "annual_quantity": customer_input.get("annual_quantity"),
        "destination_zone": customer_input.get("customer_delivery_zone"),
        "production_plant": state.get("production_plant") or (state.get("unit_data") or {}).get("plant"),
        "reporting_currency": customer_input.get("currency") or (state.get("unit_data") or {}).get("selling_currency"),
        "bom_quantity_per_product": component.get("quantity_per_product"),
        "technical_specification": component_definition,
        "drawing_reference": customer_input.get("drawing_reference") or customer_input.get("drawing_number"),
        "bom_source_path": (state.get("bom") or {}).get("normalized_path"),
        "manufacturing_strategy": state.get("manufacturing_strategy") or {},
        "save_address": _relative(_component_output_path(state["project_code"], state["product_id"], component_id)),
        "instruction": COMPONENT_COSTING_INSTRUCTION,
    }


def _component_validation_response(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    customer_input = state.get("customer_input") or {}
    direct = []
    for key in ["annual_quantity", "customer_delivery_zone", "currency", "product"]:
        if customer_input.get(key) in [None, "", 0]:
            direct.append(key)
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
            "available_product_candidates": (
                (state.get("product_resolution") or {}).get("candidates")
                or (state.get("manufacturing_strategy") or {}).get("available_product_candidates")
                or []
            ),
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
        if not force and previous.get("status") in {"triggered", "received", "failed"}:
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
    return {
        "schema_version": "1.0",
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        "component_id": bom_component["component_id"],
        "component_name": raw_json.get("component_name") or bom_component.get("component"),
        "component_family": raw_json.get("component_family") or bom_component.get("external_component_type") or bom_component.get("category"),
        "classification": "External",
        "analysis_status": analysis_status,
        "quantity_per_product": bom_component.get("quantity_per_product"),
        "annual_quantity": customer_input.get("annual_quantity"),
        "destination_zone": customer_input.get("customer_delivery_zone"),
        "reporting_currency": customer_input.get("currency") or (state.get("unit_data") or {}).get("selling_currency"),
        "technical_specification": raw_json.get("technical_specification") or bom_component.get("component_definition") or {},
        "cost_basis": cost_basis,
        "recommended_offer": normalized_offer,
        "fx": raw_json.get("fx") if isinstance(raw_json.get("fx"), list) else [],
        "material_indexation": raw_json.get("material_indexation") if isinstance(raw_json.get("material_indexation"), list) else [],
        "productivity": raw_json.get("productivity") if isinstance(raw_json.get("productivity"), list) else [],
        "assumptions": raw_json.get("assumptions") if isinstance(raw_json.get("assumptions"), list) else [],
        "unconfirmed_values": raw_json.get("unconfirmed_values") if isinstance(raw_json.get("unconfirmed_values"), list) else [],
        "required_confirmations": raw_json.get("required_confirmations") if isinstance(raw_json.get("required_confirmations"), list) else [],
        "commercially_usable": raw_json.get("commercially_usable") is True,
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
        state["components"][component_id] = {
            **existing,
            "status": "received",
            "component_id": component_id,
            "component_name": bom_component.get("component"),
            "component_family": bom_component.get("external_component_type") or bom_component.get("category"),
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
        }
        required_ids = list(state.get("required_external_component_ids") or [])
        if not required_ids:
            required_ids = [item["component_id"] for item in _required_external_components(normalized_bom)]
        remaining = [item for item in required_ids if (state["components"].get(item) or {}).get("status") != "received"]
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


def _most_work_package(
    state: Dict[str, Any],
    work_package_id: str,
    operation_name: str,
    operation_family: str,
    components: List[Dict[str, Any]],
    status: str = "pending",
    blocking_reason: Optional[str] = None,
) -> Dict[str, Any]:
    technical_inputs: Dict[str, Any] = {}
    for component in components:
        technical_inputs.update(
            _most_component_technical_data(state["project_code"], state["product_id"], component)
        )
    return {
        "work_package_id": work_package_id,
        "operation_name": operation_name,
        "operation_family": operation_family,
        "component_ids": [item["component_id"] for item in components],
        "quantity_per_product": 1,
        "technical_inputs": technical_inputs,
        "production_plant": state.get("production_plant") or (state.get("unit_data") or {}).get("plant"),
        "annual_quantity": (state.get("customer_input") or {}).get("annual_quantity"),
        "status": status,
        "blocking_reason": blocking_reason,
    }


def _electrical_test_required(state: Dict[str, Any], normalized_bom: Dict[str, Any]) -> bool:
    """Electrical test is only added to the routing when there is explicit
    technical evidence for it (a customer/BOM requirement) — the MOST Agent
    does not get to invent it, and it is not part of the default routing.
    A customer flag takes priority; otherwise fall back to scanning the raw
    BOM text for an explicit requirement statement."""
    customer_input = state.get("customer_input") or {}
    flag = customer_input.get("electrical_test_required")
    if flag is not None:
        return bool(flag)
    raw_bom_text = json.dumps(normalized_bom.get("raw_bom") or {}, ensure_ascii=False, default=str).lower()
    return any(term in raw_bom_text for term in [
        "electrical_test_required",
        "electrical test required",
        "requires electrical test",
        "100% electrical test",
    ])


# Conditional operation catalog for the Fuse choke product family: the
# backend selects which of these apply from normalized BOM component
# families and technical evidence; the MOST Agent then estimates only the
# selected operations rather than proposing the overall routing itself.
_MOST_OPERATION_CATALOG = [
    {
        "operation_id": "wp_10_ferrite_handling",
        "operation_name": "Ferrite handling",
        "operation_family": "material_handling",
        "applies": lambda ctx: bool(ctx["ferrite"]),
        "components": lambda ctx: [ctx["ferrite"]],
    },
    {
        "operation_id": "wp_20_wire_winding",
        "operation_name": "Wire winding",
        "operation_family": "winding",
        "applies": lambda ctx: bool(ctx["wire"]),
        "components": lambda ctx: [item for item in [ctx["wire"], ctx["ferrite"]] if item],
    },
    {
        "operation_id": "wp_30_lead_tinning",
        "operation_name": "Lead tinning",
        "operation_family": "tinning",
        "applies": lambda ctx: bool(ctx["tin"]),
        "components": lambda ctx: [ctx["tin"]],
    },
    {
        "operation_id": "wp_40_glue_application_baking",
        "operation_name": "Glue application and baking",
        "operation_family": "gluing_baking",
        "applies": lambda ctx: bool(ctx["glue"]),
        "components": lambda ctx: [item for item in [ctx["glue"], ctx["ferrite"]] if item],
        "is_blocked": lambda ctx: bool(ctx["glue"]) and _component_status_is_unconfirmed(ctx["glue"]),
        "blocking_reason": "Glue requirement must be confirmed before MOST analysis.",
    },
    {
        "operation_id": "wp_50_electrical_test",
        "operation_name": "Electrical test",
        "operation_family": "quality_test",
        "applies": lambda ctx: bool(ctx["electrical_test_required"] and (ctx["ferrite"] or ctx["wire"])),
        "components": lambda ctx: [item for item in [ctx["ferrite"], ctx["wire"], ctx["tin"]] if item],
    },
    {
        "operation_id": "wp_60_visual_inspection_packaging",
        "operation_name": "Visual inspection and packaging",
        "operation_family": "inspection_packaging",
        "applies": lambda ctx: any([ctx["ferrite"], ctx["wire"], ctx["tin"], ctx["glue"]]),
        "components": lambda ctx: [item for item in [ctx["ferrite"], ctx["wire"], ctx["tin"]] if item],
    },
]


def build_most_process_decomposition(state: Dict[str, Any], normalized_bom: Dict[str, Any]) -> Dict[str, Any]:
    all_components = normalized_bom.get("components") or []
    # Components the BOM itself marked as not required (zero quantity, "not
    # retained", ...) must not spawn a MOST work package even though they are
    # still recorded in the normalized BOM for traceability.
    components = {
        item["component_id"]: item
        for item in all_components
        if item.get("component_id") and not item.get("excluded_not_required")
    }
    context = {
        "ferrite": components.get("ferrite_core"),
        "wire": components.get("magnet_wire"),
        "tin": components.get("lead_tinning"),
        "glue": components.get("glue"),
        "electrical_test_required": _electrical_test_required(state, normalized_bom),
    }
    ferrite, wire, tin, glue = context["ferrite"], context["wire"], context["tin"], context["glue"]

    packages: List[Dict[str, Any]] = []
    for entry in _MOST_OPERATION_CATALOG:
        if not entry["applies"](context):
            continue
        entry_components = entry["components"](context)
        is_blocked = entry.get("is_blocked", lambda ctx: False)(context)
        packages.append(_most_work_package(
            state,
            entry["operation_id"],
            entry["operation_name"],
            entry["operation_family"],
            entry_components,
            status="blocked" if is_blocked else "pending",
            blocking_reason=entry.get("blocking_reason") if is_blocked else None,
        ))

    if packages:
        return {
            "status": "created",
            "work_packages": packages,
            "required_work_package_ids": [item["work_package_id"] for item in packages if item.get("status") != "blocked"],
            "blocked_work_package_ids": [item["work_package_id"] for item in packages if item.get("status") == "blocked"],
            "blocked_reason": None,
            "missing_inputs": [],
        }

    missing_inputs = []
    if not any([ferrite, wire, tin, glue]):
        if all_components:
            missing_inputs.append(
                "normalized BOM has components but none match a recognized material "
                "family (ferrite/wire/tin/glue); check component_id/family classification"
            )
        else:
            missing_inputs.append("normalized BOM has no components")
    blocked_reason = "missing_required_components" if missing_inputs else "no_valid_work_packages"
    logger.warning(
        "build_most_process_decomposition blocked for %s/%s: blocked_reason=%s missing_inputs=%s component_ids=%s",
        state.get("project_code"),
        state.get("product_id"),
        blocked_reason,
        missing_inputs,
        list(components.keys()),
    )
    return {
        "status": "blocked",
        "work_packages": [],
        "required_work_package_ids": [],
        "blocked_work_package_ids": [],
        "blocked_reason": blocked_reason,
        "missing_inputs": missing_inputs,
    }


def _most_trigger_payload(state: Dict[str, Any], work_package: Dict[str, Any]) -> Dict[str, Any]:
    work_package_id = work_package["work_package_id"]
    return {
        "project_code": state["project_code"],
        "product_id": state["product_id"],
        "work_package_id": work_package_id,
        "most_scope_id": work_package_id,
        "operation_name": work_package.get("operation_name"),
        "component_ids": work_package.get("component_ids") or [],
        "technical_inputs": work_package.get("technical_inputs") or {},
        "annual_quantity": work_package.get("annual_quantity"),
        "production_plant": work_package.get("production_plant"),
        "unit_data": state.get("unit_data") or {},
        "save_address": _relative(_most_output_path(state["project_code"], state["product_id"], work_package_id)),
        "instruction": MOST_WRITEBACK_INSTRUCTION,
    }


def trigger_most_operations(
    project_code: str,
    product_id: str,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    state, _ = _existing_state(project_code, product_id)
    if state is None:
        raise ValueError("Workflow state not found. Start the workflow before triggering MOST.")
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
    state["process_decomposition"] = process
    state["required_most_work_package_ids"] = process.get("required_work_package_ids") or []
    state.setdefault("most", {})

    if process.get("status") == "blocked":
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
            "status": "no_most_triggered",
            "triggered": False,
            "reason": "process_decomposition_blocked",
            "blocked_reason": process.get("blocked_reason"),
            "missing_inputs": process.get("missing_inputs") or [],
            "project_code": project_code,
            "product_id": product_id,
            "triggered_work_packages": [],
            "skipped_work_packages": [],
            "failed_work_packages": [],
            "most_triggers": [],
            "required_most_work_package_ids": state["required_most_work_package_ids"],
            "process_decomposition": process,
            "state": state,
        }

    triggered, skipped, failed = [], [], []
    for work_package in process.get("work_packages") or []:
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
        if not force and previous.get("status") in {"triggered", "received", "failed"}:
            skipped.append({"work_package_id": work_package_id, "status": previous.get("status"), "reason": "already_processed"})
            continue
        correlation_id = str(uuid.uuid4())
        payload = _most_trigger_payload(state, work_package)
        conversation_key = f"{project_code}:{product_id}:most:{work_package_id}:v1"
        append_workflow_event(project_code, product_id, "most_trigger_requested", work_package_id=work_package_id, correlation_id=correlation_id, status_before=previous.get("status"), save_path=payload["save_address"])
        trigger_result = _trigger(
            "CHATGPT_MOST_AGENT_ID",
            "MOST Assemblage",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            conversation_key,
            correlation_id,
            dry_run=dry_run,
        )
        accepted = trigger_result.get("status") in {"accepted", "dry_run"}
        entry = {
            **previous,
            **work_package,
            "status": "triggered" if accepted else "failed",
            "conversation_key": conversation_key,
            "correlation_id": correlation_id,
            "trigger_payload": payload,
            "trigger_result": trigger_result,
            "save_path": payload["save_address"],
            "normalized_path": _relative(_normalized_most_output_path(project_code, product_id, work_package_id)),
            "received_at": previous.get("received_at") if not force else None,
        }
        state["most"][work_package_id] = entry
        summary = {"work_package_id": work_package_id, "status": "accepted" if accepted else "failed", "http_status": trigger_result.get("http_status"), "correlation_id": correlation_id}
        if accepted:
            triggered.append(summary)
            append_workflow_event(project_code, product_id, "most_trigger_accepted", work_package_id=work_package_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="triggered")
        else:
            failed.append(summary)
            append_workflow_event(project_code, product_id, "most_trigger_failed", work_package_id=work_package_id, correlation_id=correlation_id, status_before=previous.get("status"), status_after="failed", http_status=trigger_result.get("http_status"))
    required_ids = state["required_most_work_package_ids"]
    state["missing_outputs"] = [f"most:{item}" for item in required_ids if (state["most"].get(item) or {}).get("status") != "received"]
    if triggered:
        state["status"] = "most_triggered"
        state["current_step"] = "Step 3 MOST Agent"
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
        "status": "most_triggered" if triggered else ("most_trigger_failed" if failed else "no_most_triggered"),
        "triggered": bool(triggered),
        "reason": reason,
        "project_code": project_code,
        "product_id": product_id,
        "triggered_work_packages": triggered,
        "skipped_work_packages": skipped,
        "failed_work_packages": failed,
        "most_triggers": [state["most"][item["work_package_id"]] for item in triggered],
        "required_most_work_package_ids": required_ids,
        "process_decomposition": process,
        "state": state,
    }


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


def save_most_output(project_code: str, product_id: str, work_package_id: str, raw_json: Dict[str, Any]) -> Dict[str, Any]:
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
        if not isinstance(raw_json, dict):
            raise ValueError("raw_json must be a JSON object.")
        returned_id = raw_json.get("work_package_id") or raw_json.get("most_scope_id")
        if returned_id not in [None, ""] and str(returned_id).strip() != work_package_id:
            raise ValueError("raw_json work_package_id does not match the tool work_package_id.")
        process = state.get("process_decomposition") or {}
        work_package = next((item for item in process.get("work_packages") or [] if item.get("work_package_id") == work_package_id), None)
        if not work_package:
            raise ValueError(f"work_package_id {work_package_id} does not exist in process decomposition.")
        if work_package.get("status") == "blocked":
            raise ValueError(f"work_package_id {work_package_id} is blocked: {work_package.get('blocking_reason')}")
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
            "save_path": _relative(raw_path),
            "normalized_path": _relative(normalized_path),
            "received_at": _now_iso(),
        }
        required = list(state.get("required_most_work_package_ids") or process.get("required_work_package_ids") or [])
        remaining = [item for item in required if (state["most"].get(item) or {}).get("status") != "received"]
        state["missing_outputs"] = [f"most:{item}" for item in remaining]
        if not remaining:
            state["status"] = "most_received"
            state["current_step"] = "Step 4 Final Calculation"
        elif state.get("status") != "most_triggered":
            state["status"] = "most_triggered"
            state["current_step"] = "Step 3 MOST Agent"
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
        raw_qty = component.get("quantity_per_product") or component.get("quantity") or component.get("qty")
        dimensional[component_id] = {
            "component_family": _component_family(component_id, component),
            "weight_kg_per_product": component.get("weight_kg_per_product") or component.get("weight_kg") or component.get("part_weight_kg"),
            "physical_mass_g_per_product": component.get("mass_g_per_product") or component.get("physical_mass_g_per_product"),
            "physical_length_mm_per_product": (
                component.get("developed_length_mm")
                or component.get("wire_length_mm")
                or component.get("total_length_mm")
                or component.get("physical_length_mm_per_product")
            ),
            "diameter_mm": component.get("diameter_mm") or component.get("wire_diameter_mm") or component.get("wire_diameter"),
            "bom_count_per_product": component.get("bom_count_per_product"),
            "quantity_per_product": _float(raw_qty),
            "quantity_unit": component.get("quantity_unit") or component.get("unit"),
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
    return _first_value(raw, [
        ["normalized_cost", "currency"],
        ["recommended_offer", "supply_chain", "currency"],
        ["recommended_offer", "currency"],
        ["currency"],
    ]) or ""


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
) -> Dict[str, Any]:
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
    dimensional_by_component = _saved_bom_dimensional_map(raw_bom)

    component_outputs = _load_saved_component_outputs(project_code, product_id)
    if not component_outputs:
        missing_inputs.append("component_outputs")

    component_breakdown = []
    transport_breakdown = []
    material_cost_per_piece = 0.0
    transport_cost_per_piece = 0.0
    output_component_ids = set()
    for component in component_outputs:
        raw = component.get("agent_raw_output") or component
        component_id = component.get("component_id") or raw.get("component_id")
        output_component_ids.add(component_id)
        bom_fields = dimensional_by_component.get(component_id, {})
        pricing_quantity_info = component_costing.resolve_component_pricing_quantity(
            component_id, bom_fields.get("component_family"), bom_fields, raw,
        )
        price_info = component_costing.resolve_unit_price(raw)
        material_result = component_costing.compute_component_material_cost(
            component_id, pricing_quantity_info, price_info,
        )
        if material_result["status"] == "blocked":
            missing_inputs.append(f"component_outputs:{component_id}:{material_result['reason']}")
            line_material_cost = None
        else:
            line_material_cost = material_result["material_cost_per_product"]
            material_cost_per_piece += line_material_cost

        transport_result = component_costing.compute_component_transport_cost(
            component_id, raw, pricing_quantity_info, line_material_cost,
        )
        if transport_result["status"] == "blocked":
            missing_inputs.append(f"component_outputs:{component_id}:{transport_result['reason']}")
            line_transport = None
        else:
            line_transport = transport_result["transport_cost_per_product"]
            transport_cost_per_piece += line_transport
        currency = _saved_component_currency(raw)
        component_breakdown.append({
            "component_id": component_id,
            "pricing_quantity": pricing_quantity_info.get("pricing_quantity"),
            "pricing_unit": pricing_quantity_info.get("pricing_unit"),
            "pricing_quantity_basis": pricing_quantity_info.get("pricing_quantity_basis"),
            "unit_material_or_delivered_cost": price_info.get("unit_price"),
            "material_cost_per_piece": line_material_cost,
            "currency": currency,
            "source": "saved_component_json",
            "status": material_result["status"],
        })
        transport_breakdown.append({
            "component_id": component_id,
            "pricing_quantity": pricing_quantity_info.get("pricing_quantity"),
            "pricing_unit": pricing_quantity_info.get("pricing_unit"),
            "logistics_breakdown": transport_result.get("logistics_breakdown"),
            "transport_cost_per_piece": line_transport,
            "currency": currency,
            "status": transport_result["status"],
        })

    for component_id in dimensional_by_component:
        if component_id not in output_component_ids and component_id not in {"lead_tin_plating", "tin_plating"}:
            warnings.append(f"no saved component JSON found for BOM component {component_id}")

    most_outputs = _load_saved_most_outputs(project_code, product_id)
    if not most_outputs:
        missing_inputs.append("most_outputs")

    unit_data = _unit_data_for_final_calculation(state, customer_input, unit_data_override)
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

    # Olivier's preliminary plant-percentage formula:
    #   direct_cost = dl + voh + transport
    #   foh = direct_cost * foh_percent_dc / 100 ; fee = direct_cost * fee_percent_dc / 100
    # Only computed once every input is fully resolved (no blocked component,
    # no blocked MOST scope) so a "blocked" result never carries partial numbers.
    direct_cost = None
    foh_cost = None
    fee_cost = None
    manufacturing_cost = None
    if not unique_missing and dl_cost is not None and voh_cost is not None:
        direct_cost = dl_cost + voh_cost + transport_cost_per_piece
        foh_cost = foh_percent / 100 * direct_cost
        fee_cost = fee_percent / 100 * direct_cost
        manufacturing_cost = direct_cost + foh_cost + fee_cost

    result = {
        "project_code": project_code,
        "product_id": product_id,
        "status": "blocked" if unique_missing else "calculated",
        "costing_method": "preliminary_plant_percentage_dc",
        "currency": unit_data.get("selling_currency") or next(
            (item.get("currency") for item in component_breakdown if item.get("currency")),
            "",
        ),
        "material_cost_per_piece": None if "component_outputs" in unique_missing else material_cost_per_piece,
        "transport_cost_per_piece": None if "component_outputs" in unique_missing else transport_cost_per_piece,
        "dl_cost_per_piece": dl_cost,
        "voh_cost_per_piece": voh_cost,
        "direct_cost_per_piece": direct_cost,
        "foh_percent_dc": foh_percent,
        "foh_cost_per_piece": foh_cost,
        "fee_percent_dc": fee_percent,
        "fee_cost_per_piece": fee_cost,
        "manufacturing_cost_per_piece": manufacturing_cost,
        "component_breakdown": component_breakdown,
        "transport_breakdown_by_component": transport_breakdown,
        "most_breakdown_by_scope": dl_voh.get("work_package_calculation") or [],
        "missing_inputs": unique_missing,
        "warnings": list(dict.fromkeys(warnings)),
    }
    output_path = _run_dir(project_code, product_id) / "final_choke_costing_result.json"
    result["save_path"] = _write_json(output_path, result)
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
    currency = _first_value(raw, [
        ["normalized_cost", "currency"],
        ["recommended_offer", "supply_chain", "currency"],
        ["recommended_offer", "reporting_currency"],
        ["recommended_offer", "purchasing_currency"],
        ["recommended_offer", "currency"],
        ["currency"],
    ])
    normalized_cost = dict(raw.get("normalized_cost") or {})
    normalized_cost.update({
        "currency": currency or normalized_cost.get("currency") or "",
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
