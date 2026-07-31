import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from services.agent_file_proxy_service import (
    build_agent_file_url,
    inspect_signed_url_expiry,
)
from services.project_data_paths import (
    get_workflow_run_paths,
    resolve_existing_data_reference,
)
from services.public_url_service import get_public_rest_base_url


class DrawingAccessError(ValueError):
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)


def _read_state(project_code: str, product_id: str) -> tuple[Dict[str, Any], Path]:
    state_path = get_workflow_run_paths(project_code, product_id)["workflow_state_path"]
    if not state_path.exists() or not state_path.is_file():
        raise DrawingAccessError(
            "TRIGGER_RUN_NOT_FOUND",
            "Workflow state was not found for the requested project and product.",
        )
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DrawingAccessError(
            "DRAWING_ACCESS_FAILED",
            "Workflow state could not be read.",
        ) from exc
    if not isinstance(state, dict):
        raise DrawingAccessError("DRAWING_ACCESS_FAILED", "Workflow state is not a JSON object.")
    return state, state_path


def _drawing_reference(state: Dict[str, Any]) -> Optional[str]:
    bom = state.get("bom") or {}
    customer = state.get("customer_input") or {}
    references = (
        bom.get("drawing_file_path"),
        state.get("drawing_file_path"),
        customer.get("drawing_file_path"),
    )
    return next((str(item).strip() for item in references if str(item or "").strip()), None)


def _manifest_item(state: Dict[str, Any], drawing_path: Path) -> Dict[str, Any]:
    customer = state.get("customer_input") or {}
    for item in customer.get("attachment_manifest") or []:
        if not isinstance(item, dict):
            continue
        reference = item.get("stored_path")
        resolved = resolve_existing_data_reference(reference) if reference else None
        if resolved and resolved.resolve() == drawing_path.resolve():
            return item
    return {}


def _record_event(project_code: str, product_id: str, event: str, **details: Any) -> None:
    try:
        from services.choke_sequential_agent_workflow import append_workflow_event

        append_workflow_event(project_code, product_id, event, **details)
    except Exception:
        pass


def _validate_active_trigger(
    state: Dict[str, Any],
    trigger_run_id: str,
) -> str:
    received = str(trigger_run_id or "").strip()
    expected = str((state.get("bom") or {}).get("trigger_run_id") or "").strip()
    if not received or not expected:
        raise DrawingAccessError(
            "TRIGGER_RUN_NOT_FOUND",
            "An active BOM trigger_run_id is required for drawing retrieval.",
        )
    if received != expected:
        raise DrawingAccessError(
            "STALE_TRIGGER_RUN",
            "trigger_run_id does not match the active BOM workflow run.",
        )
    return received


def _read_pdf(drawing_path: Path) -> bytes:
    if drawing_path.suffix.lower() != ".pdf":
        raise DrawingAccessError("UNSUPPORTED_FILE_TYPE", "The linked workflow document is not a PDF.")
    try:
        content = drawing_path.read_bytes()
    except OSError as exc:
        raise DrawingAccessError("DRAWING_ACCESS_FAILED", "The linked drawing could not be read.") from exc
    if not content:
        raise DrawingAccessError("DRAWING_ACCESS_FAILED", "The linked drawing is empty.")
    if not content.startswith(b"%PDF"):
        raise DrawingAccessError("UNSUPPORTED_FILE_TYPE", "The linked document has an invalid PDF signature.")
    return content


def capture_choke_drawing_snapshot(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
) -> Dict[str, Any]:
    """Capture the exact current drawing revision before an Agent trigger is sent."""
    state, _ = _read_state(project_code, product_id)
    active_run_id = _validate_active_trigger(state, trigger_run_id)
    reference = _drawing_reference(state)
    if not reference:
        raise DrawingAccessError(
            "DRAWING_NOT_LINKED_TO_PRODUCT",
            "No drawing is linked to the requested workflow product.",
        )
    drawing_path = resolve_existing_data_reference(reference)
    if drawing_path is None or not drawing_path.is_file():
        raise DrawingAccessError("DRAWING_NOT_FOUND", "The linked workflow drawing was not found.")
    content = _read_pdf(drawing_path)
    manifest = _manifest_item(state, drawing_path)
    if not manifest:
        raise DrawingAccessError(
            "DRAWING_NOT_LINKED_TO_PRODUCT",
            "The drawing is not present in this workflow product's attachment manifest.",
        )
    checksum = hashlib.sha256(content).hexdigest()
    manifest_checksum = str(manifest.get("checksum_sha256") or "").strip().lower()
    if manifest_checksum and manifest_checksum != checksum:
        raise DrawingAccessError(
            "DRAWING_REVISION_MISMATCH",
            "The drawing content does not match its linked attachment revision.",
        )
    document_id = str(manifest.get("attachment_id") or checksum[:16]).strip()
    return {
        "project_code": project_code,
        "product_id": product_id,
        "trigger_run_id": active_run_id,
        "document_id": document_id,
        "stored_path": str(manifest.get("stored_path") or reference),
        "filename": manifest.get("original_filename") or drawing_path.name,
        "stored_filename": drawing_path.name,
        "mime_type": "application/pdf",
        "revision": manifest.get("uploaded_at") or f"sha256:{checksum}",
        "document_revision_hash": checksum,
        "file_size": len(content),
    }


def _fresh_download_metadata(project_code: str, filename: str) -> Dict[str, Any]:
    try:
        download_url = build_agent_file_url(
            get_public_rest_base_url(),
            project_code,
            filename,
            expiry_seconds=3600,
        )
        expiry = inspect_signed_url_expiry(download_url)
        return {
            "download_url": download_url,
            "expires_at": expiry.get("expires_at_utc"),
        }
    except (RuntimeError, ValueError):
        return {"download_url": None, "expires_at": None}


def get_current_choke_drawing(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
    *,
    include_bytes: bool = True,
    record_events: bool = True,
) -> Dict[str, Any]:
    if record_events:
        _record_event(
            project_code,
            product_id,
            "drawing_mcp_action_called",
            trigger_run_id_received=str(trigger_run_id or "").strip(),
        )
    try:
        state, state_path = _read_state(project_code, product_id)
        active_run_id = _validate_active_trigger(state, trigger_run_id)
        snapshot = (state.get("bom") or {}).get("drawing_input_snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("document_revision_hash"):
            raise DrawingAccessError(
                "TRIGGER_RUN_NOT_FOUND",
                "The active BOM trigger has no persisted drawing input snapshot.",
            )
        if str(snapshot.get("trigger_run_id") or "").strip() != active_run_id:
            raise DrawingAccessError(
                "STALE_TRIGGER_RUN",
                "The drawing snapshot belongs to a different BOM trigger run.",
            )
        current_reference = _drawing_reference(state)
        snapshot_reference = str(snapshot.get("stored_path") or "").strip()
        if not current_reference or not snapshot_reference:
            raise DrawingAccessError(
                "DRAWING_NOT_LINKED_TO_PRODUCT",
                "The active workflow product has no linked drawing snapshot.",
            )
        current_path = resolve_existing_data_reference(current_reference)
        drawing_path = resolve_existing_data_reference(snapshot_reference)
        if current_path is None or drawing_path is None:
            raise DrawingAccessError("DRAWING_NOT_FOUND", "The linked workflow drawing was not found.")
        if current_path.resolve() != drawing_path.resolve():
            raise DrawingAccessError(
                "DRAWING_REVISION_MISMATCH",
                "The workflow drawing changed after this BOM trigger was created.",
            )
        if not drawing_path.is_file():
            raise DrawingAccessError("DRAWING_NOT_FOUND", "The linked workflow drawing was not found.")
        content = _read_pdf(drawing_path)
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != str(snapshot.get("document_revision_hash") or "").strip().lower():
            raise DrawingAccessError(
                "DRAWING_REVISION_MISMATCH",
                "The drawing revision no longer matches the active trigger input snapshot.",
            )
        manifest = _manifest_item(state, drawing_path)
        if not manifest or str(manifest.get("attachment_id") or checksum[:16]) != str(snapshot.get("document_id")):
            raise DrawingAccessError(
                "DRAWING_NOT_LINKED_TO_PRODUCT",
                "The drawing snapshot is not linked to the requested workflow product.",
            )
        metadata = {
            "success": True,
            "status": "available",
            "project_code": project_code,
            "product_id": product_id,
            "trigger_run_id": active_run_id,
            "document_id": snapshot["document_id"],
            "filename": snapshot["filename"],
            "mime_type": "application/pdf",
            "revision": snapshot["revision"],
            "document_revision_hash": checksum,
            "checksum_sha256": checksum,
            "source_revision": f"drawing-sha256:{checksum}",
            "file_size": len(content),
            "workflow_state_path": str(state_path),
            "drawing_delivery_mode": "mcp_embedded_resource",
            **_fresh_download_metadata(project_code, drawing_path.name),
        }
        if record_events:
            _record_event(
                project_code,
                product_id,
                "drawing_mcp_delivered",
                trigger_run_id=active_run_id,
                document_id=metadata["document_id"],
                filename=metadata["filename"],
                document_revision_hash=checksum,
                file_size=len(content),
            )
        if include_bytes:
            metadata["pdf_bytes"] = content
        return metadata
    except DrawingAccessError as exc:
        if record_events:
            _record_event(
                project_code,
                product_id,
                "drawing_mcp_action_rejected",
                trigger_run_id_received=str(trigger_run_id or "").strip(),
                error_code=exc.error_code,
            )
        raise
