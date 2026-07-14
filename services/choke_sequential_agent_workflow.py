import json
import logging
import os
import re
import time
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
from services.project_data_paths import (
    BACKEND_ROOT,
    COSTING_RUNS_DIR,
    CUSTOMER_INPUT_DIR,
    DATA_ROOT,
    PROJECT_ROOT,
    data_reference_candidates,
    portable_data_reference,
    resolve_customer_input_path,
)
from services.workspace_agent_client import clean_agent_id, trigger_workspace_agent


BASE_DIR = BACKEND_ROOT
RUNS_DIR = COSTING_RUNS_DIR
logger = logging.getLogger(__name__)


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
    return (os.getenv("PUBLIC_BASE_URL") or request_base_url or "http://127.0.0.1:8000/").rstrip("/")


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
    return (
        f"{base_url}/api/choke-costing/files/"
        f"{quote(project_code, safe='')}/{quote(filename, safe='')}"
    )


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return _relative(path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(project_code: str, product_id: str) -> Path:
    return RUNS_DIR / _safe_part(project_code, "project_code") / _safe_part(product_id, "product_id")


def _state_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "workflow_state.json"


def _events_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "workflow_events.jsonl"


def _state_path_candidates(project_code: str, product_id: str) -> List[Path]:
    canonical = _state_path(project_code, product_id).resolve()
    reference = (
        Path("data")
        / "costing_runs"
        / _safe_part(project_code, "project_code")
        / _safe_part(product_id, "product_id")
        / "workflow_state.json"
    )
    return list(dict.fromkeys([canonical, *data_reference_candidates(reference)]))


def _bom_raw_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "bom" / "raw_bom_agent_output.json"


def _bom_normalized_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "bom_normalized.json"


def _component_output_path(project_code: str, product_id: str, component_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "components" / f"{_safe_part(component_id, 'component_id')}.json"


def _most_output_path(project_code: str, product_id: str, work_package_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "most" / f"{_safe_part(work_package_id, 'work_package_id')}.json"


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
    for candidate in _state_path_candidates(project_code, product_id):
        state = _read_json(candidate, None)
        if isinstance(state, dict):
            return state, candidate
    return None, None


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
    )
    if state is None:
        response = {
            "status": "not_found",
            "message": "Workflow state not found",
            "project_code": project_code,
            "product_id": product_id,
            "debug_url": f"/api/choke-workflow/debug/{project_code}/{product_id}",
            "debug_hint": f"Use /api/choke-workflow/debug/{project_code}/{product_id}",
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
    return state


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
        normalized_input.get("drawing_sas_url")
        or normalized_input.get("drawing_blob_url")
        or normalized_input.get("drawing_file_url")
        or generated_local_url
    )
    drawing_access_mode = normalized_input.get("drawing_access_mode")
    if not drawing_access_mode:
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
    append_workflow_event(
        project_code,
        product_id,
        "workflow_start_requested",
        input_file=customer_input["_input_file"],
        drawing_file_path=normalized_input.get("drawing_file_path"),
        drawing_file_url=bom_trigger.get("drawing_file_url"),
        status_before=status_before,
    )
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
    state = existing_state
    state.update({
        "input_file": customer_input["_input_file"],
        "drawing_file_path": normalized_input.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "status": workflow_status,
        "current_step": "Step 1 BOM Agent",
        "manufacturing_strategy": manufacturing_strategy,
        "unit_data": unit_data,
        "customer_input": normalized_input,
        "components": {},
        "most": {},
        "process_decomposition": None,
        "missing_outputs": ["bom"],
        "warnings": bom_trigger.get("warnings") or [],
    })
    state["bom"] = {
        "status": bom_status,
        "save_path": save_address,
        "drawing_file_path": bom_trigger.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "warnings": bom_trigger.get("warnings") or [],
        "trigger_result": trigger_result,
        "trigger_attempts": trigger_result.get("attempts") or [],
        "retryable": retryable_failure,
        "input_text": input_text,
    }
    if not accepted:
        state.setdefault("errors", []).append({"stage": "bom", "trigger_result": trigger_result})
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
        raw_bom.get("material_lines"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ["components", "lines", "line_items", "items", "materials", "bom"]:
                nested = candidate.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
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
        or component.get("designation")
        or component.get("description")
        or ""
    )


def _component_id(component: Dict[str, Any], index: int) -> str:
    explicit = (
        component.get("component_id")
        or component.get("component_code")
        or component.get("component_reference")
        or component.get("part_number")
        or component.get("id")
    )
    if explicit:
        return _slug(explicit, f"component_{index}")
    text = _component_text(component)
    if any(term in text for term in ["ferrite", "core", "magnetic"]):
        return "ferrite_core"
    if any(term in text for term in ["magnet wire", "copper wire", "enameled", "enamelled", "wire"]):
        return "magnet_wire"
    if any(term in text for term in ["tin", "tinning", "plating"]):
        return "lead_tin_plating"
    return _slug(_component_type(component), f"component_{index}")


def _external_costing_route(component: Dict[str, Any]) -> Optional[str]:
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
    for index, component in enumerate(_extract_component_list(raw_bom), start=1):
        component_id = _component_id(component, index)
        component_type = _component_type(component)
        route_type = _external_costing_route(component)
        is_external_costing = route_type in {"ferrite", "enameled_wire", "tin"}
        normalized = {
            "component_id": component_id,
            "component_type": component_type or route_type or "component",
            "component": (
                component.get("component")
                or component.get("product")
                or component.get("designation")
                or component.get("description")
                or component_type
                or route_type
                or "component"
            ),
            "category": (
                component.get("category")
                or component.get("component_category")
                or component.get("component_family")
                or component.get("family")
                or route_type
            ),
            "quantity_per_product": (
                component.get("quantity_per_product")
                or component.get("quantity_per_assembly")
                or component.get("quantity")
                or component.get("qty")
            ),
            "component_definition": component,
            "costing_route": "external_component_costing_agent" if is_external_costing else "not_external_agent",
            "external_component_type": route_type,
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


def _find_recursive_value(data: Any, keys: List[str]) -> Any:
    key_set = {key.lower() for key in keys}
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in key_set:
                scalar = _scalar_from_value(value)
                if scalar not in [None, "", [], {}]:
                    return scalar
        for value in data.values():
            nested = _find_recursive_value(value, keys)
            if nested not in [None, "", [], {}]:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _find_recursive_value(item, keys)
            if nested not in [None, "", [], {}]:
                return nested
    return None


def extract_bom_technical_fields(raw_bom: Any) -> Dict[str, Any]:
    quote_information = raw_bom.get("quote_information") if isinstance(raw_bom, dict) else {}
    quote_information = quote_information if isinstance(quote_information, dict) else {}

    def quote_value(keys: List[str]) -> Any:
        for key in keys:
            value = _scalar_from_value(quote_information.get(key))
            if value not in [None, "", [], {}]:
                return value
        return None

    return {
        "product_name": quote_value(["product_name", "product", "product_description"]) or _find_recursive_value(raw_bom, [
            "product_name",
            "product",
            "product_designation",
            "product_description",
            "choke_type",
            "product_type",
        ]),
        "part_number": quote_value(["part_number", "customer_part_number", "product_reference"]) or _find_recursive_value(raw_bom, [
            "part_number",
            "customer_part_number",
            "product_reference",
            "product_reference_number",
            "drawing_part_number",
            "item_number",
        ]),
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
    if not path:
        return {"status": "skipped", "reason": "customer input file not available"}

    current = _read_json(path, {}) or {}
    updates: Dict[str, Any] = {}

    product_name = extracted.get("product_name")
    part_number = extracted.get("part_number")
    if product_name:
        updates["product_name"] = product_name
        if not current.get("product"):
            updates["product"] = product_name
    if part_number:
        if not current.get("part_number"):
            updates["part_number"] = part_number
        current_product_id = str(current.get("product_id") or "")
        if not current_product_id or current_product_id.startswith("UNKNOWN-PART-"):
            updates["product_id"] = part_number
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
    if extracted_values or updates:
        _write_json(path, current)

    return {
        "status": "updated" if updates else ("extracted" if extracted_values else "no_fields_found"),
        "path": _relative(path),
        "updates": updates,
        "extracted": extracted_values,
    }


def _refresh_master_data_for_state(state: Dict[str, Any]) -> Dict[str, Any]:
    customer_input = state.get("customer_input") or {}
    product_line = customer_input.get("product_line") or "Chokes"
    product = customer_input.get("product")
    delivery_zone = customer_input.get("customer_delivery_zone")
    if product and delivery_zone:
        manufacturing_strategy = get_master_manufacturing_strategy(
            product_line,
            product,
            delivery_zone,
        )
        state["manufacturing_strategy"] = manufacturing_strategy
        state["unit_data"] = get_master_unit_data(manufacturing_strategy.get("production_plant"))
        return {
            "status": "refreshed",
            "manufacturing_strategy_source": manufacturing_strategy.get("source"),
            "production_plant": manufacturing_strategy.get("production_plant"),
            "unit_data_source": (state.get("unit_data") or {}).get("source"),
        }
    missing = []
    if not product:
        missing.append("product")
    if not delivery_zone:
        missing.append("delivery_zone")
    return {
        "status": "skipped",
        "missing_inputs": missing,
        "message": "Manufacturing strategy needs product and delivery_zone.",
    }


def save_bom_output(project_code: str, product_id: str, raw_json: Dict[str, Any]) -> Dict[str, Any]:
    existing_state, existing_state_path = _existing_state(project_code, product_id)
    status_before = (existing_state or {}).get("status")
    run_dir = _run_dir(project_code, product_id).resolve()
    state_path = _state_path(project_code, product_id).resolve()
    raw_path = _bom_raw_path(project_code, product_id)
    normalized_path = _bom_normalized_path(project_code, product_id)
    raw_keys = list(raw_json.keys()) if isinstance(raw_json, dict) else []
    called_debug = {
        "project_code_received": project_code,
        "product_id_received": product_id,
        "raw_json_type": type(raw_json).__name__,
        "raw_json_top_level_keys": raw_keys,
        "data_root": str(DATA_ROOT),
        "workflow_run_directory": str(run_dir),
        "workflow_state_path": str(state_path),
        "raw_bom_save_path": str(raw_path.resolve()),
        "normalized_bom_path": str(normalized_path.resolve()),
        "state_exists_before_save": existing_state is not None,
        "state_status_before_save": status_before,
    }
    logger.info("save_bom_output called: %s", json.dumps(called_debug, default=str))
    append_workflow_event(
        project_code,
        product_id,
        "save_bom_output_called",
        **called_debug,
        status_before=status_before,
    )
    _write_json(raw_path, raw_json)
    normalized = normalize_bom(raw_json)
    _write_json(normalized_path, normalized)

    state = existing_state if isinstance(existing_state, dict) else _load_state(project_code, product_id)
    if existing_state is None:
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
    }
    _apply_bom_received_precedence(state)
    state["technical_fields_extracted_from_bom"] = bool(customer_input_update.get("extracted"))
    state["technical_fields_from_bom"] = customer_input_update.get("extracted") or {}
    state["customer_input_update"] = customer_input_update
    state["master_data_refresh"] = master_data_refresh
    state["missing_outputs"] = [
        f"component:{item['component_id']}"
        for item in normalized.get("external_components") or []
    ]
    _save_state(state)
    component_ids = [
        item.get("component_id")
        for item in normalized.get("components") or []
        if item.get("component_id")
    ]
    debug = {
        "success": True,
        "tool": "save_bom_output",
        "project_code": project_code,
        "product_id": product_id,
        **called_debug,
        "state_status_after_save": state.get("status"),
        "raw_bom_saved": raw_path.exists(),
        "normalized_bom_saved": normalized_path.exists(),
        "component_ids": component_ids,
        "missing_outputs_after_save": state.get("missing_outputs") or [],
        "errors": [],
    }
    logger.info("save_bom_output completed: %s", json.dumps(debug, default=str))
    append_workflow_event(
        project_code,
        product_id,
        "save_bom_output_completed",
        workflow_state_path=str(state_path),
        raw_bom_save_path=str(raw_path.resolve()),
        normalized_bom_path=str(normalized_path.resolve()),
        state_exists_before_save=existing_state is not None,
        status_before=status_before,
        status_after=state.get("status"),
        component_ids=component_ids,
        missing_outputs=state.get("missing_outputs") or [],
    )
    return {
        **debug,
        "status": "saved",
        "normalized_bom": normalized,
        "state": state,
        "debug": debug,
        "state_merge": {
            "existing_state_found": existing_state_path is not None,
            "existing_state_path": str(existing_state_path) if existing_state_path else None,
            "saved_state_path": str(_state_path(project_code, product_id).resolve()),
        },
    }


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

    customer_input = {**(state.get("customer_input") or {}), **updates}
    customer_input["project_code"] = project_code
    customer_input.setdefault("workflow_product_id", product_id)
    state["customer_input"] = customer_input

    input_path = _customer_input_path_from_state(state)
    if input_path:
        stored_input = _read_json(input_path, {}) or {}
        stored_input.update(updates)
        stored_input["project_code"] = project_code
        stored_input.setdefault("workflow_product_id", product_id)
        _write_json(input_path, stored_input)

    state["master_data_refresh"] = _refresh_master_data_for_state(state)
    _save_state(state)
    return {
        "status": "updated",
        "project_code": project_code,
        "product_id": product_id,
        "updated_fields": updates,
        "customer_input": state["customer_input"],
        "state": state,
    }


def trigger_next_component_costing(project_code: str, product_id: str, dry_run: bool = False) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    if (state.get("bom") or {}).get("status") != "received":
        raise ValueError("BOM output must be received before triggering component costing.")
    normalized_bom = _load_normalized_bom(project_code, product_id)
    customer_input = state.get("customer_input") or {}
    master_data_refresh = _refresh_master_data_for_state(state)
    unit_data = state.get("unit_data") or {}
    missing_commercial_inputs = []
    if customer_input.get("annual_quantity") in [None, "", 0]:
        missing_commercial_inputs.append("annual_quantity")
    if not customer_input.get("customer_delivery_zone"):
        missing_commercial_inputs.append("customer_delivery_zone")
    if not customer_input.get("currency"):
        missing_commercial_inputs.append("currency")
    if missing_commercial_inputs:
        return {
            "status": "blocked",
            "missing_inputs": missing_commercial_inputs,
            "message": "Complete commercial fields before external component costing.",
            "state": state,
        }
    missing_stage_inputs = []
    if not customer_input.get("product"):
        missing_stage_inputs.append("product")
    if not unit_data.get("plant"):
        missing_stage_inputs.append("production_plant")
    if missing_stage_inputs:
        raise ValueError(
            "Component costing needs annual_quantity, delivery_zone, product and manufacturing strategy "
            f"before external component agents can be triggered. Missing: {', '.join(missing_stage_inputs)}"
        )
    triggers = []

    state["status"] = "components_triggering"
    state["current_step"] = "Step 2 External Component Costing Agent"
    state["master_data_refresh"] = master_data_refresh
    state.setdefault("components", {})

    for component in normalized_bom.get("external_components") or []:
        component_id = component["component_id"]
        if state["components"].get(component_id, {}).get("status") == "received":
            continue
        save_address = _relative(_component_output_path(project_code, product_id, component_id))
        payload = {
            "project_code": project_code,
            "product_id": product_id,
            "component_id": component_id,
            "component_type": component.get("external_component_type") or component.get("component_type"),
            "component_definition": component.get("component_definition"),
            "annual_quantity": customer_input.get("annual_quantity"),
            "destination_zone": customer_input.get("customer_delivery_zone"),
            "production_plant": unit_data.get("plant"),
            "reporting_currency": unit_data.get("selling_currency"),
            "save_address": save_address,
            "write_back_instruction": (
                "After producing the component costing JSON, you must call save_component_output."
            ),
        }
        input_text = _json_input_text(
            [
                "This is one external component only.",
                "Do not cost a complete choke or assembly.",
                "Use the component definition exactly extracted from the BOM Agent JSON.",
                "Return JSON only.",
                "After producing the component costing JSON, you must call save_component_output.",
            ],
            payload,
            save_address,
        )
        trigger_result = _trigger(
            "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
            "External Component Costing Agent",
            input_text,
            f"{project_code}:{product_id}:sequential:component:{component_id}",
            f"{project_code}:{product_id}:sequential:component:{component_id}:v1",
            dry_run=dry_run,
        )
        component_status = _trigger_status(trigger_result)
        state["components"][component_id] = {
            "status": component_status,
            "component_id": component_id,
            "component_type": component.get("external_component_type") or component.get("component_type"),
            "save_path": save_address,
            "trigger_result": trigger_result,
        }
        triggers.append(state["components"][component_id])
    state["missing_outputs"] = [
        f"component:{component_id}"
        for component_id, info in state["components"].items()
        if info.get("status") != "received"
    ]
    _save_state(state)
    return {"status": "components_triggered", "component_triggers": triggers, "state": state}


def save_component_output(project_code: str, product_id: str, component_id: str, raw_json: Dict[str, Any]) -> Dict[str, Any]:
    path = _component_output_path(project_code, product_id, component_id)
    _write_json(path, raw_json)
    state = _load_state(project_code, product_id)
    state.setdefault("components", {})
    existing = state["components"].get(component_id, {})
    state["components"][component_id] = {
        **existing,
        "status": "received",
        "component_id": component_id,
        "save_path": _relative(path),
        "received_at": _now_iso(),
    }
    required_components = list(state.get("components", {}).keys())
    received = [
        key for key, info in state["components"].items()
        if info.get("status") == "received"
    ]
    if required_components and set(required_components) <= set(received):
        state["status"] = "components_received"
        state["current_step"] = "Step 3 MOST Agent"
        state["missing_outputs"] = []
    else:
        state["missing_outputs"] = [
            f"component:{key}"
            for key in required_components
            if key not in received
        ]
    _save_state(state)
    return {"status": "saved", "component_id": component_id, "state": state}


def trigger_most_operations(project_code: str, product_id: str, dry_run: bool = False) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    if (state.get("bom") or {}).get("status") != "received":
        raise ValueError("BOM output must be received before triggering MOST.")
    missing_components = [
        key for key, info in (state.get("components") or {}).items()
        if info.get("status") != "received"
    ]
    if missing_components:
        raise ValueError(f"Component outputs must be received before MOST: {', '.join(missing_components)}")

    raw_bom = _read_json(_bom_raw_path(project_code, product_id), {}) or {}
    customer_input = state.get("customer_input") or {}
    unit_data = state.get("unit_data") or {}
    if customer_input.get("annual_quantity") in [None, "", 0]:
        raise ValueError("MOST/component-operation planning needs annual_quantity before operations can be triggered.")
    process = decompose_choke_process(raw_bom, customer_input)

    state["status"] = "most_triggering"
    state["current_step"] = "Step 3 MOST Agent"
    state.setdefault("most", {})
    triggers = []
    for work_package in process.get("work_packages") or []:
        work_package_id = work_package["work_package_id"]
        if state["most"].get(work_package_id, {}).get("status") == "received":
            continue
        save_address = _relative(_most_output_path(project_code, product_id, work_package_id))
        work_package = {**work_package, "save_address": save_address}
        payload = {
            "project_code": project_code,
            "product_id": product_id,
            "work_package_id": work_package_id,
            "component_id": work_package.get("component_id"),
            "operation_id": work_package.get("operation_id"),
            "operation_name": work_package.get("operation_name"),
            "technical_inputs": work_package,
            "annual_quantity": customer_input.get("annual_quantity"),
            "plant": unit_data.get("plant"),
            "save_address": save_address,
            "write_back_instruction": (
                "After producing the MOST operation JSON, you must call save_most_output."
            ),
        }
        input_text = _json_input_text(
            [
                "This is one component-operation work package only.",
                "Do not process the full product.",
                "Do not read SharePoint.",
                "Use the provided technical payload.",
                "Return JSON only.",
                "After producing the MOST operation JSON, you must call save_most_output.",
            ],
            payload,
            save_address,
        )
        trigger_result = _trigger(
            "CHATGPT_MOST_AGENT_ID",
            "MOST Assemblage",
            input_text,
            f"{project_code}:{product_id}:sequential:most:{work_package_id}",
            f"{project_code}:{product_id}:sequential:most:{work_package_id}:v1",
            dry_run=dry_run,
        )
        state["most"][work_package_id] = {
            "status": _trigger_status(trigger_result),
            "work_package_id": work_package_id,
            "component_id": work_package.get("component_id"),
            "operation_id": work_package.get("operation_id"),
            "operation_name": work_package.get("operation_name"),
            "save_path": save_address,
            "trigger_result": trigger_result,
        }
        triggers.append(state["most"][work_package_id])
    state["process_decomposition"] = process
    state["missing_outputs"] = [
        f"most:{work_package_id}"
        for work_package_id, info in state["most"].items()
        if info.get("status") != "received"
    ]
    _save_state(state)
    return {"status": "most_triggered", "most_triggers": triggers, "process_decomposition": process, "state": state}


def save_most_output(project_code: str, product_id: str, work_package_id: str, raw_json: Dict[str, Any]) -> Dict[str, Any]:
    path = _most_output_path(project_code, product_id, work_package_id)
    _write_json(path, raw_json)
    state = _load_state(project_code, product_id)
    state.setdefault("most", {})
    existing = state["most"].get(work_package_id, {})
    state["most"][work_package_id] = {
        **existing,
        "status": "received",
        "work_package_id": work_package_id,
        "save_path": _relative(path),
        "received_at": _now_iso(),
    }
    required = list(state.get("most", {}).keys())
    received = [
        key for key, info in state["most"].items()
        if info.get("status") == "received"
    ]
    if required and set(required) <= set(received):
        state["status"] = "most_received"
        state["current_step"] = "Step 4 Cost Calculation"
        state["missing_outputs"] = []
    else:
        state["missing_outputs"] = [
            f"most:{key}"
            for key in required
            if key not in received
        ]
    _save_state(state)
    return {"status": "saved", "work_package_id": work_package_id, "state": state}


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
    quantity_by_component = _saved_bom_quantity_map(raw_bom)

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
        quantity = quantity_by_component.get(component_id, 1.0)
        unit_material_cost = _saved_component_cost(raw)
        if unit_material_cost is None:
            missing_inputs.append(f"component_outputs:{component_id}:material_cost")
            line_material_cost = None
        else:
            line_material_cost = quantity * unit_material_cost
            material_cost_per_piece += line_material_cost
        transportation = _saved_transport_value(raw, ["transportation_cost", "transport_cost"])
        custom_duty = _saved_transport_value(raw, ["custom_duty_cost", "customs_duty_cost", "duty_cost"])
        forwarder = _saved_transport_value(raw, ["forwarder_cost", "forwarding_cost"])
        line_transport = quantity * (transportation + custom_duty + forwarder)
        transport_cost_per_piece += line_transport
        currency = _saved_component_currency(raw)
        component_breakdown.append({
            "component_id": component_id,
            "quantity_per_product": quantity,
            "unit_material_or_delivered_cost": unit_material_cost,
            "material_cost_per_piece": line_material_cost,
            "currency": currency,
            "source": "saved_component_json",
        })
        transport_breakdown.append({
            "component_id": component_id,
            "quantity_per_product": quantity,
            "transportation_cost": transportation,
            "custom_duty_cost": custom_duty,
            "forwarder_cost": forwarder,
            "transport_cost_per_piece": line_transport,
            "currency": currency,
        })

    for component_id in quantity_by_component:
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
    direct_cost = None
    foh_cost = None
    fee_cost = None
    manufacturing_cost = None
    if dl_cost is not None and voh_cost is not None:
        direct_cost = dl_cost + voh_cost + transport_cost_per_piece
        foh_cost = foh_percent / 100 * direct_cost
        fee_cost = fee_percent / 100 * direct_cost
        manufacturing_cost = direct_cost + foh_cost + fee_cost

    unique_missing = list(dict.fromkeys(missing_inputs))
    result = {
        "project_code": project_code,
        "product_id": product_id,
        "status": "blocked" if unique_missing else "calculated",
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
    raw = _read_json(path, {}) or {}
    work_package_id = raw.get("work_package_id") or path.stem
    normalized_operation = raw.get("normalized_operation") or raw.get("operation_details") or raw
    cycle_time = _float(_first_value(normalized_operation, [["cycle_time_seconds"], ["operation_cycle_time_seconds"]]))
    p_h = _float(_first_value(normalized_operation, [["p_h"], ["station_library_summary", "p_h"], ["rate_per_hour_instantaneous"]]))
    parts_per_cycle = _float(_first_value(normalized_operation, [["parts_per_cycle"], ["pieces_per_cycle"]])) or 1.0
    if p_h in [None, 0] and cycle_time not in [None, 0]:
        p_h = 3600 / cycle_time * parts_per_cycle
    output = {
        **raw,
        "work_package_id": work_package_id,
        "component_id": raw.get("component_id") or normalized_operation.get("component_id"),
        "operation_id": raw.get("operation_id") or normalized_operation.get("operation_id"),
        "operation_name": raw.get("operation_name") or normalized_operation.get("operation_name") or raw.get("operation"),
        "p_h": p_h,
        "oee": _first_value(normalized_operation, [["oee"], ["oee_percent"], ["costing_oee_percent"]]),
        "operator_percent": _first_value(normalized_operation, [["operator_percent"], ["percent_operator"]]),
        "parts_per_cycle": parts_per_cycle,
        "generic_capex_eur": _first_value(normalized_operation, [["generic_capex_eur"], ["generic_capex"]]),
        "specific_capex_eur": _first_value(normalized_operation, [["specific_capex_eur"], ["specific_capex"]]),
        "tooling_cost_eur": _first_value(normalized_operation, [["tooling_cost_eur"], ["tooling_cost"]]),
        "tooling_adder_per_piece_eur": _first_value(normalized_operation, [["tooling_adder_per_piece_eur"]]),
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
