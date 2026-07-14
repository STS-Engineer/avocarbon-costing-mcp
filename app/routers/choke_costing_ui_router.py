import json
import os
import re
from html import escape
from datetime import datetime
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
    uploaded_pdf_path,
    validate_agent_file_token,
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
from services.choke_sequential_agent_workflow import append_workflow_event


BASE_DIR = BACKEND_ROOT
RESULTS_DIR = COSTING_RUNS_DIR

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
    original = Path(filename or "drawing.pdf").name
    stem = _safe_slug(Path(original).stem, "drawing")
    suffix = Path(original).suffix.lower()
    if suffix != ".pdf":
        raise HTTPException(status_code=400, detail="drawing_pdf must be a PDF file")
    return f"{stem}{suffix}"


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
    base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url)
    return base_url.rstrip("/")


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

    customer = field("customer")
    customer_delivery_zone = field("customer_delivery_zone")
    annual_quantity = number_field("annual_quantity")
    currency = field("currency")
    drawing_pdf = form.get("drawing_pdf")
    missing = []
    if drawing_pdf is None or not getattr(drawing_pdf, "filename", ""):
        missing.append("drawing_pdf")
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

    created_timestamp = _timestamp()
    project_code = field("project_code") or f"RFQ-{created_timestamp}"
    product_id_input = field("product_id")
    part_number_input = field("part_number")
    product_id = product_id_input or part_number_input or f"UNKNOWN-PART-{created_timestamp}"

    safe_project_code = _safe_slug(project_code, "project")
    safe_product_id = _safe_slug(product_id, "product")
    drawing_reference = None
    drawing_file_path = None

    filename = _safe_upload_filename(drawing_pdf.filename)
    upload_dir = CUSTOMER_INPUT_DIR / "uploads" / safe_project_code
    upload_path = upload_dir / filename
    upload_dir.mkdir(parents=True, exist_ok=True)
    content = await drawing_pdf.read()
    upload_path.write_bytes(content)
    drawing_reference = Path(drawing_pdf.filename).name
    drawing_file_path = _relative_to_base(upload_path)
    drawing_file_url_local = get_public_file_url(request, drawing_file_path)
    drawing_agent_proxy_url = get_agent_file_url(request, drawing_file_path)
    drawing_file_url = drawing_agent_proxy_url or drawing_file_url_local
    drawing_blob_url = None
    drawing_sas_url = None
    drawing_access_mode = "backend_signed_proxy" if drawing_agent_proxy_url else "local"
    warnings = []
    azure_upload_result = {
        "status": "not_configured",
        "message": "AZURE_STORAGE_CONNECTION_STRING is not configured",
    }
    if is_azure_blob_configured():
        azure_upload_result = upload_file_to_blob(
            upload_path,
            project_code,
            original_filename=Path(drawing_pdf.filename).name,
        )
        if azure_upload_result.get("status") == "uploaded":
            drawing_blob_url = azure_upload_result.get("blob_url")
            drawing_sas_url = azure_upload_result.get("sas_url")
            drawing_file_url = drawing_agent_proxy_url or drawing_sas_url or drawing_blob_url
            drawing_access_mode = "backend_signed_proxy" if drawing_agent_proxy_url else (
                "azure_blob_sas" if drawing_sas_url else "azure_blob"
            )
        else:
            warnings.append(
                "Azure Blob upload failed; using local PDF URL fallback. "
                "Cloud Workspace Agents cannot access localhost URLs."
            )
    else:
        warnings.append(
            "Azure Blob is not configured; using local PDF URL fallback. "
            "Cloud Workspace Agents cannot access localhost URLs."
        )

    customer_input = {
        "project_code": project_code,
        "customer": customer,
        "final_customer": field("final_customer"),
        "product_line": field("product_line", "Chokes"),
        "product": field("product"),
        "product_id": product_id,
        "workflow_product_id": product_id,
        "part_number": part_number_input,
        "drawing_reference": drawing_reference,
        "drawing_file_path": drawing_file_path,
        "drawing_file_url_local": drawing_file_url_local,
        "drawing_agent_proxy_url": drawing_agent_proxy_url,
        "drawing_file_url": drawing_file_url,
        "drawing_blob_url": drawing_blob_url,
        "drawing_sas_url": drawing_sas_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_azure_upload": azure_upload_result,
        "drawing_original_filename": Path(drawing_pdf.filename).name,
        "customer_delivery_zone": customer_delivery_zone,
        "annual_quantity": annual_quantity,
        "currency": currency,
        "target_price": number_field("target_price"),
        "sop_date": field("sop_date"),
        "technical_fields_pending_bom": not all([
            field("product"),
            product_id_input,
            part_number_input,
        ]),
        "technical_fields_extracted_from_bom": False,
        "warnings": warnings,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

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
        if not validate_agent_file_token(project_code, filename, token):
            raise HTTPException(status_code=403, detail="Invalid or expired Agent file token")
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
