import json
import logging
import os
import re
import hashlib
import mimetypes
from html import escape
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from services.azure_blob_storage_service import (
    is_azure_blob_configured,
    upload_file_to_blob,
)
from services.agent_file_proxy_service import (
    build_agent_file_url,
    inspect_agent_file_token,
    uploaded_pdf_path,
)
from services.choke_orchestrator import run_choke_orchestration
from services.project_data_paths import (
    BACKEND_ROOT,
    COSTING_RUNS_DIR,
    CUSTOMER_INPUT_DIR,
    CustomerInputFileNotFound,
    atomic_write_json,
    portable_data_reference,
    resolve_customer_input_path,
)
from services.public_url_service import get_public_rest_base_url
from services.choke_sequential_agent_workflow import append_workflow_event
from services.customer_input_extraction import (
    SUPPORTED_CUSTOMER_INPUT_EXTENSIONS,
    apply_resolution_to_customer_input,
    extract_customer_input_package,
)


BASE_DIR = BACKEND_ROOT
RESULTS_DIR = COSTING_RUNS_DIR
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Choke Costing UI"])


class ChokeCostingRunRequest(BaseModel):
    input_file: str = Field(..., description="Path under data/customer_inputs")
    mode: str = Field("instant", description="instant or trigger_agents")


def _safe_customer_input_path(input_file: str) -> Path:
    try:
        return resolve_customer_input_path(input_file)
    except CustomerInputFileNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.details) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _relative_to_base(path: Path) -> str:
    return portable_data_reference(path)


def _safe_slug(value: Any, fallback: str = "input") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_upload_filename(filename: str) -> str:
    original = Path(filename or "attachment").name
    stem = _safe_slug(Path(original).stem, "attachment")
    suffix = Path(original).suffix.lower()
    if suffix not in SUPPORTED_CUSTOMER_INPUT_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_CUSTOMER_INPUT_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported attachment type. Allowed: {allowed}")
    return f"{stem}{suffix}"


def _unique_upload_path(upload_dir: Path, filename: str) -> Path:
    candidate = upload_dir / filename
    counter = 2
    while candidate.exists():
        candidate = upload_dir / f"{Path(filename).stem}__{counter}{Path(filename).suffix}"
        counter += 1
    return candidate


def _attachment_role(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "drawing_specification"
    if suffix in {".xlsx", ".xlsm"}:
        return "commercial_rfq_workbook"
    if suffix == ".csv":
        return "commercial_data"
    return "supporting_attachment"


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


def _public_base_url(request: Request) -> str:
    _load_env()
    return get_public_rest_base_url(str(request.base_url))


def get_public_file_url(request: Request, drawing_file_path: str) -> str | None:
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
    return f"{_public_base_url(request)}/api/choke-costing/files/{project_code}/{filename}"


def get_agent_file_url(request: Request, drawing_file_path: str) -> str | None:
    if not drawing_file_path:
        return None
    parts = str(drawing_file_path).replace("\\", "/").split("/")
    try:
        upload_index = parts.index("uploads")
        project_code = parts[upload_index + 1]
        filename = parts[upload_index + 2]
    except (ValueError, IndexError):
        return None
    try:
        expiry_seconds = max(7200, int(os.getenv("AGENT_FILE_URL_EXPIRY_SECONDS", "14400")))
        return build_agent_file_url(
            _public_base_url(request),
            project_code,
            filename,
            expiry_seconds=expiry_seconds,
        )
    except (RuntimeError, ValueError):
        return None


def _load_json_file(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid JSON in {path.name}: {exc}",
        ) from exc


def _trigger_statuses(envelope: Dict[str, Any]) -> Dict[str, Any]:
    orchestration = envelope.get("agent_orchestration") or {}
    bom_agent = orchestration.get("bom_agent") or {}
    return {
        "bom": {
            "status": bom_agent.get("status"),
            "agent_id": bom_agent.get("agent_id"),
            "save_address": bom_agent.get("save_address"),
            "trigger_result": bom_agent.get("trigger_result"),
        },
        "components": [
            {
                "component_id": item.get("component_id"),
                "status": item.get("status"),
                "agent_id": item.get("agent_id"),
                "save_address": item.get("save_address"),
                "trigger_result": item.get("trigger_result"),
            }
            for item in orchestration.get("component_agent_calls") or []
        ],
        "most": [
            {
                "work_package_id": item.get("work_package_id"),
                "component_id": item.get("component_id"),
                "operation_id": item.get("operation_id"),
                "operation_name": item.get("operation_name"),
                "status": item.get("status"),
                "agent_id": item.get("agent_id"),
                "save_address": item.get("save_address"),
                "trigger_result": item.get("trigger_result"),
            }
            for item in orchestration.get("most_agent_calls") or []
        ],
    }


@router.get("/api/choke-costing/customer-inputs")
def list_customer_inputs(request: Request):
    items = []
    for path in sorted(CUSTOMER_INPUT_DIR.glob("*.json")):
        payload = _load_json_file(path)
        drawing_file_url = payload.get("drawing_file_url") or get_public_file_url(
            request,
            payload.get("drawing_file_path"),
        )
        items.append({
            "id": path.stem,
            "file": _relative_to_base(path),
            "project_code": payload.get("project_code"),
            "customer": payload.get("customer"),
            "product": payload.get("product"),
            "product_id": payload.get("product_id"),
            "workflow_product_id": payload.get("workflow_product_id") or payload.get("product_id"),
            "part_number": payload.get("part_number"),
            "customer_delivery_zone": payload.get("customer_delivery_zone"),
            "annual_quantity": payload.get("annual_quantity"),
            "drawing_file_path": payload.get("drawing_file_path"),
            "drawing_file_url": drawing_file_url,
            "drawing_file_url_local": payload.get("drawing_file_url_local"),
            "drawing_agent_proxy_url": payload.get("drawing_agent_proxy_url"),
            "drawing_access_mode": payload.get("drawing_access_mode"),
            "drawing_blob_url": payload.get("drawing_blob_url"),
            "drawing_sas_url": payload.get("drawing_sas_url"),
            "drawing_azure_upload": payload.get("drawing_azure_upload"),
            "warnings": payload.get("warnings") or [],
            "drawing_original_filename": payload.get("drawing_original_filename"),
            "technical_fields_extracted_from_bom": payload.get("technical_fields_extracted_from_bom") is True,
            "attachment_manifest": payload.get("attachment_manifest") or [],
            "customer_input_resolution": payload.get("customer_input_resolution") or {},
            "component_costing_ready": (
                (payload.get("customer_input_resolution") or {}).get("component_costing_ready") is True
            ),
        })
    return items


@router.post("/api/choke-costing/customer-inputs/create")
async def create_customer_input(request: Request):
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid multipart/form-data payload: {exc}",
        ) from exc

    def field(name: str, default: Any = None) -> Any:
        value = form.get(name)
        if value in [None, ""]:
            return default
        return str(value).strip()

    def number_field(name: str):
        value = field(name)
        if value in [None, ""]:
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{name} must be numeric") from exc

    created_timestamp = _timestamp()
    provisional_project_code = field("project_code") or f"RFQ-{created_timestamp}"
    product_id_input = field("product_id")
    part_number_input = field("part_number")
    provisional_product_id = product_id_input or part_number_input or f"UNKNOWN-PART-{created_timestamp}"

    uploads = []
    seen_upload_objects = set()
    for form_key in ["drawing_pdf", "attachments", "files"]:
        for upload in form.getlist(form_key):
            if not getattr(upload, "filename", "") or id(upload) in seen_upload_objects:
                continue
            seen_upload_objects.add(id(upload))
            uploads.append(upload)
    if not uploads:
        raise HTTPException(status_code=400, detail="At least one customer-input attachment is required.")

    upload_dir = CUSTOMER_INPUT_DIR / "uploads" / _safe_slug(provisional_project_code, "project")
    upload_dir.mkdir(parents=True, exist_ok=True)
    attachment_manifest = []
    uploaded_paths: Dict[str, Path] = {}
    uploaded_at = datetime.now(timezone.utc).isoformat()
    for upload in uploads:
        safe_filename = _safe_upload_filename(upload.filename)
        content = await upload.read()
        checksum = hashlib.sha256(content).hexdigest()
        upload_path = None
        for existing_path in upload_dir.iterdir():
            if not existing_path.is_file() or existing_path.suffix.lower() not in SUPPORTED_CUSTOMER_INPUT_EXTENSIONS:
                continue
            if hashlib.sha256(existing_path.read_bytes()).hexdigest() == checksum:
                upload_path = existing_path
                break
        reused_existing = upload_path is not None
        if upload_path is None:
            upload_path = _unique_upload_path(upload_dir, safe_filename)
            upload_path.write_bytes(content)
        stored_path = _relative_to_base(upload_path)
        manifest_item = {
            "attachment_id": checksum[:16],
            "original_filename": Path(upload.filename).name,
            "stored_filename": upload_path.name,
            "stored_path": stored_path,
            "mime_type": getattr(upload, "content_type", None) or mimetypes.guess_type(upload.filename)[0] or "application/octet-stream",
            "file_size": len(content),
            "checksum_sha256": checksum,
            "uploaded_at": uploaded_at,
            "source_role": _attachment_role(upload.filename),
            "reused_existing_file": reused_existing,
        }
        attachment_manifest.append(manifest_item)
        uploaded_paths[stored_path] = upload_path

    explicit_values = {
        "project_code": field("project_code"),
        "customer": field("customer"),
        "final_customer": field("final_customer"),
        "product_line": field("product_line"),
        "product": field("product"),
        "product_id": product_id_input,
        "part_number": part_number_input,
        "customer_delivery_zone": field("customer_delivery_zone"),
        "annual_quantity": number_field("annual_quantity"),
        "quotation_currency": field("quotation_currency") or field("currency"),
        "target_price": number_field("target_price"),
        "target_price_currency": field("target_price_currency"),
        "sop_date": field("sop_date"),
    }
    explicit_fields = [key for key, value in explicit_values.items() if value not in [None, ""]]
    structured_input = {
        **explicit_values,
        "product_line": explicit_values.get("product_line") or "Chokes",
        "_explicit_user_fields": explicit_fields,
    }
    extraction = extract_customer_input_package(structured_input, attachment_manifest)
    customer_input = apply_resolution_to_customer_input(structured_input, extraction)
    project_code = customer_input.get("project_code") or provisional_project_code
    resolved_part_number = customer_input.get("part_number")
    product_id = str(product_id_input or resolved_part_number or provisional_product_id).strip()
    customer_input.update({
        "project_code": project_code,
        "product_id": product_id,
        "workflow_product_id": product_id,
        "attachment_manifest": attachment_manifest,
        "customer_input_resolution": extraction,
        "resolved_customer_context": extraction,
        "_explicit_user_fields": explicit_fields,
    })

    drawing_candidates = [item for item in attachment_manifest if Path(item["stored_filename"]).suffix.lower() == ".pdf"]
    preferred_drawing = str(customer_input.get("drawing_reference") or "").lower()
    primary_drawing = next(
        (item for item in drawing_candidates if item["original_filename"].lower() == preferred_drawing),
        drawing_candidates[0] if drawing_candidates else None,
    )
    drawing_file_path = primary_drawing.get("stored_path") if primary_drawing else None
    drawing_reference = primary_drawing.get("original_filename") if primary_drawing else customer_input.get("drawing_reference")
    drawing_file_url_local = get_public_file_url(request, drawing_file_path) if drawing_file_path else None
    drawing_agent_proxy_url = get_agent_file_url(request, drawing_file_path) if drawing_file_path else None
    drawing_file_url = drawing_agent_proxy_url or drawing_file_url_local
    drawing_blob_url = None
    drawing_sas_url = None
    drawing_access_mode = "backend_signed_proxy" if drawing_agent_proxy_url else "local" if drawing_file_path else "missing"
    warnings = list(extraction.get("warnings") or [])
    azure_upload_result = {"status": "not_configured", "message": "AZURE_STORAGE_CONNECTION_STRING is not configured"}
    if primary_drawing and is_azure_blob_configured():
        primary_path = uploaded_paths[primary_drawing["stored_path"]]
        azure_upload_result = upload_file_to_blob(primary_path, project_code, original_filename=primary_drawing["original_filename"])
        if azure_upload_result.get("status") == "uploaded":
            drawing_blob_url = azure_upload_result.get("blob_url")
            drawing_sas_url = azure_upload_result.get("sas_url")
            drawing_file_url = drawing_agent_proxy_url or drawing_sas_url or drawing_blob_url
            drawing_access_mode = "backend_signed_proxy" if drawing_agent_proxy_url else "azure_blob_sas" if drawing_sas_url else "azure_blob"
        else:
            warnings.append("Azure Blob upload failed; using the backend file URL fallback.")
    elif primary_drawing and not is_azure_blob_configured():
        warnings.append("Azure Blob is not configured; using the backend PDF URL fallback.")

    customer_input.update({
        "drawing_reference": drawing_reference,
        "drawing_file_path": drawing_file_path,
        "drawing_file_url_local": drawing_file_url_local,
        "drawing_agent_proxy_url": drawing_agent_proxy_url,
        "drawing_file_url": drawing_file_url,
        "drawing_blob_url": drawing_blob_url,
        "drawing_sas_url": drawing_sas_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_azure_upload": azure_upload_result,
        "drawing_original_filename": drawing_reference,
        "technical_fields_pending_bom": not all([customer_input.get("product"), customer_input.get("part_number")]),
        "technical_fields_extracted_from_bom": False,
        "warnings": list(dict.fromkeys(warnings)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    safe_project_code = _safe_slug(project_code, "project")
    safe_product_id = _safe_slug(product_id, "product")

    output_path = CUSTOMER_INPUT_DIR / f"{safe_project_code}_{safe_product_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, customer_input)
    append_workflow_event(
        project_code,
        product_id,
        "customer_input_saved",
        input_file=_relative_to_base(output_path),
        customer_input_path=str(output_path.resolve()),
        drawing_file_path=drawing_file_path,
        drawing_file_url=drawing_file_url,
        status_after="saved",
    )
    return {
        "status": "saved",
        "input_file": _relative_to_base(output_path),
        "customer_input": customer_input,
        "attachment_manifest": attachment_manifest,
        "resolved_fields": extraction.get("resolved_fields") or [],
        "missing_fields": extraction.get("missing_fields") or [],
        "conflicts": extraction.get("conflicts") or [],
        "warnings": customer_input.get("warnings") or [],
        "component_costing_ready": extraction.get("component_costing_ready") is True,
    }


@router.get("/api/choke-costing/files/{project_code}/{filename}")
def get_uploaded_drawing_file(project_code: str, filename: str):
    if project_code != Path(project_code).name or filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files can be served")

    upload_root = (CUSTOMER_INPUT_DIR / "uploads").resolve()
    candidate = (upload_root / project_code / filename).resolve()
    if upload_root not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Uploaded drawing PDF not found")
    return FileResponse(
        candidate,
        media_type="application/pdf",
        filename=filename,
    )


@router.get("/api/choke-costing/agent-files/{project_code}/{filename}")
def get_agent_drawing_file(
    project_code: str,
    filename: str,
    token: str = Query(..., min_length=10),
):
    try:
        token_result = inspect_agent_file_token(project_code, filename, token)
        logger.info(
            "Agent PDF token check project=%s file=%s path=%s expires=%s current=%s method=GET valid=%s reason=%s",
            project_code,
            filename,
            token_result.get("normalized_relative_path"),
            token_result.get("expires_at_utc"),
            token_result.get("current_utc"),
            token_result.get("valid"),
            token_result.get("reason"),
        )
        if not token_result.get("valid"):
            detail = {
                "message": "Agent file token rejected",
                "reason": token_result.get("reason"),
                "normalized_relative_path": token_result.get("normalized_relative_path"),
                "expires_at_utc": token_result.get("expires_at_utc"),
                "current_utc": token_result.get("current_utc"),
            }
            raise HTTPException(status_code=403, detail=detail)
        candidate = uploaded_pdf_path(project_code, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Uploaded drawing PDF not found")
    return FileResponse(
        candidate,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/api/choke-costing/run")
def run_choke_costing(request: ChokeCostingRunRequest):
    input_path = _safe_customer_input_path(request.input_file)
    customer_input = _load_json_file(input_path)
    mode = (request.mode or "instant").strip().lower()

    if mode == "instant":
        envelope = run_choke_orchestration(
            customer_input,
            full_demo_mode=True,
            dry_run=True,
            trigger_agents=False,
            demo_override=True,
        )
        message = "Instant costing calculation completed using available backend/saved/preliminary outputs."
        envelope["ui_status_label"] = "Instant calculation completed"
        envelope["message"] = message
        return {
            "mode": mode,
            "message": message,
            "envelope": envelope,
        }

    if mode == "trigger_agents":
        envelope = run_choke_orchestration(
            customer_input,
            full_demo_mode=False,
            dry_run=False,
            trigger_agents=True,
            demo_override=True,
        )
        envelope["trigger_statuses"] = _trigger_statuses(envelope)
        message = "Real agents triggered. Waiting for MCP/write-back to receive final agent JSON outputs."
        envelope["ui_status_label"] = "Real agents triggered - waiting for output write-back"
        envelope["message"] = message
        return {
            "mode": mode,
            "message": message,
            "envelope": envelope,
        }

    raise HTTPException(status_code=400, detail="mode must be instant or trigger_agents")


@router.get("/api/choke-costing/result/{project_code}/{product_id}")
def get_choke_costing_result(project_code: str, product_id: str):
    path = RESULTS_DIR / project_code / product_id / "orchestration_result.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Result not found at data/costing_runs/{project_code}/{product_id}/orchestration_result.json",
        )
    return _load_json_file(path)


@router.get("/choke-costing", response_class=HTMLResponse)
def choke_costing_page():
    path = BASE_DIR / "app" / "static" / "choke_costing.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Choke costing page not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.get("/api/choke-costing/docs/writeback-setup", response_class=HTMLResponse)
def choke_writeback_setup_guide():
    path = BASE_DIR / "docs" / "choke_agent_writeback_setup.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Write-back setup guide not found")
    body = escape(path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Choke Agent Write-back Setup</title>"
        "<style>body{font:14px/1.5 Segoe UI,Arial,sans-serif;max-width:980px;margin:32px auto;"
        "padding:0 20px;color:#172033;background:#f8fbff}pre{white-space:pre-wrap;background:#fff;"
        "border:1px solid #d9e2ef;border-radius:8px;padding:18px}</style></head>"
        f"<body><pre>{body}</pre></body></html>"
    )
