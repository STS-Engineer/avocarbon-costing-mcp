import json
import os
import re
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
from services.workspace_agent_client import trigger_workspace_agent


BASE_DIR = Path(__file__).resolve().parents[1]
CUSTOMER_INPUT_DIR = BASE_DIR / "data" / "customer_inputs"
RUNS_DIR = BASE_DIR / "data" / "costing_runs"


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
    return path.resolve().relative_to(BASE_DIR.resolve()).as_posix()


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


def _bom_raw_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "bom" / "raw_bom_agent_output.json"


def _bom_normalized_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "bom_normalized.json"


def _component_output_path(project_code: str, product_id: str, component_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "components" / f"{_safe_part(component_id, 'component_id')}.json"


def _most_output_path(project_code: str, product_id: str, work_package_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "most" / f"{_safe_part(work_package_id, 'work_package_id')}.json"


def _load_customer_input(input_file: str) -> Dict[str, Any]:
    path = Path(input_file)
    candidate = path if path.is_absolute() else BASE_DIR / path
    candidate = candidate.resolve()
    allowed_root = CUSTOMER_INPUT_DIR.resolve()
    if allowed_root not in candidate.parents and candidate != allowed_root:
        raise ValueError("input_file must be inside data/customer_inputs")
    if not candidate.exists():
        raise FileNotFoundError(f"Customer input file not found: {input_file}")
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
    state = _read_json(_state_path(project_code, product_id), None)
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


def _save_state(state: Dict[str, Any]) -> Dict[str, Any]:
    state["updated_at"] = _now_iso()
    _write_json(_state_path(state["project_code"], state["product_id"]), state)
    return state


def get_workflow_state(project_code: str, product_id: str) -> Dict[str, Any]:
    return _load_state(project_code, product_id)


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
        "After producing the BOM JSON, you must call the write-back tool save_bom_output. "
        "Do not only return the JSON in chat. The backend workflow will not continue until "
        "save_bom_output is called."
    )
    payload = {
        "project_code": project_code,
        "product_id": product_id,
        "product_family": normalized_input.get("product_line") or "Chokes",
        "product_line": normalized_input.get("product_line") or "Chokes",
        "drawing_reference": normalized_input.get("drawing_reference"),
        "drawing_file_path": drawing_file_path,
        "drawing_file_url": drawing_file_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_blob_url": normalized_input.get("drawing_blob_url"),
        "drawing_sas_url": normalized_input.get("drawing_sas_url"),
        "drawing_original_filename": normalized_input.get("drawing_original_filename"),
        "annual_quantity": normalized_input.get("annual_quantity"),
        "delivery_zone": normalized_input.get("customer_delivery_zone"),
        "customer_input": normalized_input,
        "save_address": save_address,
        "writeback_instructions": writeback_instruction,
        "write_back_instruction": writeback_instruction,
        "warnings": warnings,
    }
    input_text = _json_input_text(
        [
            (
                "Analyze the PDF drawing available at drawing_file_url. This is the source of truth "
                "for technical BOM extraction. Extract part number, drawing number, product name, "
                "revision, ferrite, wire, tin/plating, glue/locking clues, dimensions, manufacturing "
                "operations, assumptions and points to confirm. Return structured BOM JSON only."
            ),
            "Trigger only the BOM analysis step.",
            "Do not calculate final product price.",
            "Do not trigger component costing or MOST.",
            "Return JSON only.",
            (
                "After producing the BOM JSON, you must call the write-back tool save_bom_output. "
                "Do not only return the JSON in chat. The backend workflow will not continue until "
                "save_bom_output is called."
            ),
        ],
        payload,
        save_address,
    )
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
        input_path = BASE_DIR / customer_input["_input_file"]
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
    trigger_result = _trigger(
        "CHATGPT_CHOKE_BOM_AGENT_ID",
        "Choke BOM Analyzer",
        input_text,
        f"{project_code}:{product_id}:sequential:bom",
        f"{project_code}:{product_id}:sequential:bom:v1",
        dry_run=dry_run,
    )
    bom_status = _trigger_status(trigger_result)
    state = _load_state(project_code, product_id)
    state.update({
        "input_file": customer_input["_input_file"],
        "drawing_file_path": normalized_input.get("drawing_file_path"),
        "drawing_file_url": bom_trigger.get("drawing_file_url"),
        "drawing_access_mode": bom_trigger.get("drawing_access_mode"),
        "drawing_blob_url": bom_trigger.get("drawing_blob_url"),
        "drawing_sas_url": bom_trigger.get("drawing_sas_url"),
        "drawing_url_is_local": _is_local_url(bom_trigger.get("drawing_file_url")),
        "status": "bom_triggered" if bom_status == "triggered" else "blocked",
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
    }
    if bom_status == "failed":
        state.setdefault("errors", []).append({"stage": "bom", "trigger_result": trigger_result})
    _save_state(state)
    return {
        "message": "BOM Agent triggered first. Waiting for BOM output write-back.",
        "state": state,
        "trigger_report": {
            "bom": state["bom"],
            "components_triggered": [],
            "most_triggered": [],
        },
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
        raw_bom.get("bill_of_material"),
        raw_bom.get("bill_of_materials"),
        raw_bom.get("material_lines"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ["components", "lines", "items", "materials"]:
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
    return None


def normalize_bom(raw_bom: Dict[str, Any]) -> Dict[str, Any]:
    components = []
    external_components = []
    for index, component in enumerate(_extract_component_list(raw_bom), start=1):
        component_id = _component_id(component, index)
        component_type = _component_type(component)
        route_type = _external_costing_route(component)
        normalized = {
            "component_id": component_id,
            "component_type": component_type or route_type or "component",
            "quantity_per_product": (
                component.get("quantity_per_product")
                or component.get("quantity")
                or component.get("qty")
            ),
            "component_definition": component,
            "costing_route": "external_component_costing_agent" if route_type in {"ferrite", "enameled_wire"} else "not_external_agent",
            "external_component_type": route_type,
        }
        components.append(normalized)
        if route_type in {"ferrite", "enameled_wire"}:
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
    path = Path(input_file)
    candidate = path if path.is_absolute() else BASE_DIR / path
    candidate = candidate.resolve()
    allowed_root = CUSTOMER_INPUT_DIR.resolve()
    if allowed_root not in candidate.parents and candidate != allowed_root:
        return None
    return candidate if candidate.exists() else None


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
    raw_path = _bom_raw_path(project_code, product_id)
    _write_json(raw_path, raw_json)
    normalized = normalize_bom(raw_json)
    normalized_path = _bom_normalized_path(project_code, product_id)
    _write_json(normalized_path, normalized)

    state = _load_state(project_code, product_id)
    extracted = extract_bom_technical_fields(raw_json)
    customer_input_update = _update_customer_input_from_bom(state, extracted)
    if customer_input_update.get("status") in {"updated", "extracted"}:
        input_path = _customer_input_path_from_state(state)
        if input_path:
            state["customer_input"] = _read_json(input_path, state.get("customer_input") or {}) or {}
    master_data_refresh = _refresh_master_data_for_state(state)
    state["status"] = "bom_received"
    state["current_step"] = "Step 2 External Component Costing Agent"
    state["bom"] = {
        "status": "received",
        "save_path": _relative(raw_path),
        "normalized_path": _relative(normalized_path),
        "trigger_result": (state.get("bom") or {}).get("trigger_result"),
        "received_at": _now_iso(),
    }
    state["technical_fields_extracted_from_bom"] = bool(customer_input_update.get("extracted"))
    state["technical_fields_from_bom"] = customer_input_update.get("extracted") or {}
    state["customer_input_update"] = customer_input_update
    state["master_data_refresh"] = master_data_refresh
    state["missing_outputs"] = [
        f"component:{item['component_id']}"
        for item in normalized.get("external_components") or []
    ]
    _save_state(state)
    return {"status": "saved", "normalized_bom": normalized, "state": state}


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


def trigger_next_component_costing(project_code: str, product_id: str, dry_run: bool = False) -> Dict[str, Any]:
    state = _load_state(project_code, product_id)
    if (state.get("bom") or {}).get("status") != "received":
        raise ValueError("BOM output must be received before triggering component costing.")
    normalized_bom = _load_normalized_bom(project_code, product_id)
    customer_input = state.get("customer_input") or {}
    master_data_refresh = _refresh_master_data_for_state(state)
    unit_data = state.get("unit_data") or {}
    missing_stage_inputs = []
    if customer_input.get("annual_quantity") in [None, "", 0]:
        missing_stage_inputs.append("annual_quantity")
    if not customer_input.get("customer_delivery_zone"):
        missing_stage_inputs.append("delivery_zone")
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
    output_path = _run_dir(project_code, product_id) / "orchestration_result_real_agent_chain.json"
    envelope["orchestration_result_real_agent_chain_path"] = _write_json(output_path, envelope)

    financial_status = (envelope.get("financial_calculation") or {}).get("status")
    state["status"] = "calculated" if financial_status != "blocked" else "blocked"
    state["current_step"] = "Step 4 Cost Calculation"
    state["missing_outputs"] = envelope.get("missing_inputs") or []
    _save_state(state)
    return envelope
