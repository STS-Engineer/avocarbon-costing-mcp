import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from services.project_data_paths import (
    get_workflow_run_paths,
    resolve_existing_data_reference,
)


def _read_state(project_code: str, product_id: str) -> tuple[Dict[str, Any], Path]:
    state_path = get_workflow_run_paths(project_code, product_id)["workflow_state_path"]
    if not state_path.exists() or not state_path.is_file():
        raise ValueError("Workflow state not found for the requested project and product.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("Workflow state is not a JSON object.")
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


def _record_event(
    project_code: str,
    product_id: str,
    event: str,
    **details: Any,
) -> None:
    try:
        from services.choke_sequential_agent_workflow import append_workflow_event

        append_workflow_event(project_code, product_id, event, **details)
    except Exception:
        # File delivery must not fail merely because diagnostic logging failed.
        pass


def get_current_choke_drawing(
    project_code: str,
    product_id: str,
    trigger_run_id: str,
    *,
    include_bytes: bool = True,
    record_events: bool = True,
) -> Dict[str, Any]:
    received_run_id = str(trigger_run_id or "").strip()
    if record_events:
        _record_event(
            project_code,
            product_id,
            "drawing_mcp_action_called",
            trigger_run_id_received=received_run_id,
        )
    state, state_path = _read_state(project_code, product_id)
    expected_run_id = str((state.get("bom") or {}).get("trigger_run_id") or "").strip()
    if not received_run_id:
        if record_events:
            _record_event(project_code, product_id, "drawing_mcp_action_rejected", reason="missing_trigger_run_id")
        raise ValueError("trigger_run_id is required for current drawing retrieval.")
    if not expected_run_id or received_run_id != expected_run_id:
        if record_events:
            _record_event(
                project_code,
                product_id,
                "drawing_mcp_action_rejected",
                reason="stale_or_mismatched_trigger_run_id",
                trigger_run_id_expected=expected_run_id,
                trigger_run_id_received=received_run_id,
            )
        raise ValueError("trigger_run_id does not match the active BOM workflow run.")
    reference = _drawing_reference(state)
    drawing_path = resolve_existing_data_reference(reference) if reference else None
    if drawing_path is None or not drawing_path.is_file():
        if record_events:
            _record_event(project_code, product_id, "drawing_mcp_delivery_failed", reason="drawing_file_missing")
        raise ValueError("Current workflow drawing PDF is unavailable.")
    if drawing_path.suffix.lower() != ".pdf":
        if record_events:
            _record_event(project_code, product_id, "drawing_mcp_delivery_failed", reason="drawing_not_pdf")
        raise ValueError("Current workflow drawing is not a PDF.")
    content = drawing_path.read_bytes()
    if not content or not content.startswith(b"%PDF"):
        if record_events:
            _record_event(project_code, product_id, "drawing_mcp_delivery_failed", reason="drawing_invalid_pdf")
        raise ValueError("Current workflow drawing is empty or has an invalid PDF signature.")
    checksum = hashlib.sha256(content).hexdigest()
    manifest = _manifest_item(state, drawing_path)
    expected_checksum = str(manifest.get("checksum_sha256") or "").strip().lower()
    if expected_checksum and checksum.lower() != expected_checksum:
        if record_events:
            _record_event(project_code, product_id, "drawing_mcp_delivery_failed", reason="drawing_checksum_mismatch")
        raise ValueError("Current workflow drawing checksum does not match its upload manifest.")
    metadata = {
        "success": True,
        "project_code": project_code,
        "product_id": product_id,
        "trigger_run_id": received_run_id,
        "filename": manifest.get("original_filename") or drawing_path.name,
        "stored_filename": drawing_path.name,
        "mime_type": "application/pdf",
        "checksum_sha256": checksum,
        "source_revision": f"drawing-sha256:{checksum}",
        "file_size": len(content),
        "workflow_state_path": str(state_path),
        "drawing_delivery_mode": "mcp_embedded_resource",
    }
    if record_events:
        _record_event(
            project_code,
            product_id,
            "drawing_mcp_delivered",
            trigger_run_id=received_run_id,
            filename=metadata["filename"],
            checksum_sha256=checksum,
            file_size=len(content),
            source_revision=metadata["source_revision"],
        )
    if include_bytes:
        metadata["pdf_bytes"] = content
    return metadata
