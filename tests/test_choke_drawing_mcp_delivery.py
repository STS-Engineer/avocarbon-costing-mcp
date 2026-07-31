import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from mcp.types import EmbeddedResource, TextContent

import server
from services import choke_drawing_mcp_service as drawing_service
from services import choke_sequential_agent_workflow as workflow
from services.choke_writeback_mcp_diagnostic import (
    get_mcp_schema_fingerprints,
    validate_runtime_writeback_schemas,
)


def _workflow(tmp_path: Path, monkeypatch, *, trigger_run_id: str = "run-current"):
    drawing = tmp_path / "current drawing.pdf"
    drawing.write_bytes(b"%PDF-1.7\ncurrent drawing\n%%EOF")
    state_path = tmp_path / "workflow_state.json"
    checksum = hashlib.sha256(drawing.read_bytes()).hexdigest()
    state_path.write_text(json.dumps({
        "project_code": "P-100",
        "product_id": "PART-200",
        "drawing_file_path": str(drawing),
        "customer_input": {
            "drawing_file_path": str(drawing),
            "attachment_manifest": [{
                "original_filename": "customer drawing.pdf",
                "stored_path": str(drawing),
                "mime_type": "application/pdf",
                "file_size": drawing.stat().st_size,
                "checksum_sha256": checksum,
            }],
        },
        "bom": {
            "trigger_run_id": trigger_run_id,
            "drawing_file_path": str(drawing),
            "drawing_input_snapshot": {
                "project_code": "P-100",
                "product_id": "PART-200",
                "trigger_run_id": trigger_run_id,
                "document_id": checksum[:16],
                "stored_path": str(drawing),
                "filename": "customer drawing.pdf",
                "stored_filename": drawing.name,
                "mime_type": "application/pdf",
                "revision": f"sha256:{checksum}",
                "document_revision_hash": checksum,
                "file_size": drawing.stat().st_size,
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        drawing_service,
        "get_workflow_run_paths",
        lambda project_code, product_id: {"workflow_state_path": state_path},
    )
    monkeypatch.setattr(
        drawing_service,
        "resolve_existing_data_reference",
        lambda reference: Path(reference).resolve() if reference else None,
    )
    monkeypatch.setattr(drawing_service, "_record_event", lambda *_a, **_k: None)
    return drawing


def test_actual_mcp_catalog_requires_correlated_bom_writeback_and_drawing_action():
    tools = {tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())}

    assert set(tools["save_bom_output"]["required"]) == {
        "project_code", "product_id", "trigger_run_id", "raw_json"
    }
    assert set(tools["get_choke_drawing"]["required"]) == {
        "project_code", "product_id", "trigger_run_id"
    }
    assert set(tools["save_component_output"]["required"]) == {
        "project_code", "product_id", "component_id", "trigger_run_id", "raw_json"
    }
    assert set(tools["save_most_output"]["required"]) == {
        "project_code", "product_id", "work_package_id", "most_scope_id",
        "trigger_run_id", "raw_json"
    }
    assert tools["save_component_output"]["properties"]["trigger_run_id"]["type"] == "string"
    assert tools["save_most_output"]["properties"]["trigger_run_id"]["type"] == "string"
    assert tools["save_most_output"]["properties"]["raw_json"]["type"] == "object"
    assert get_mcp_schema_fingerprints()["status"] == "ok"


def test_schema_validation_fails_for_runtime_catalog_mismatch(monkeypatch):
    import services.choke_writeback_mcp_diagnostic as diagnostic

    schemas = diagnostic._runtime_tool_schemas()
    schemas["save_bom_output"] = {
        **schemas["save_bom_output"],
        "required": ["project_code", "product_id", "raw_json"],
    }
    monkeypatch.setattr(diagnostic, "_runtime_tool_schemas", lambda: schemas)
    with pytest.raises(RuntimeError, match="trigger_run_id"):
        validate_runtime_writeback_schemas(raise_on_error=True)


def test_current_trigger_returns_only_current_pdf_as_embedded_resource(tmp_path, monkeypatch):
    drawing = _workflow(tmp_path, monkeypatch)

    result = drawing_service.get_current_choke_drawing(
        "P-100", "PART-200", "run-current"
    )
    assert result["pdf_bytes"] == drawing.read_bytes()
    assert result["filename"] == "customer drawing.pdf"
    assert result["mime_type"] == "application/pdf"
    assert result["checksum_sha256"] == hashlib.sha256(drawing.read_bytes()).hexdigest()
    assert result["source_revision"].startswith("drawing-sha256:")
    assert result["status"] == "available"
    assert result["document_id"] == result["document_revision_hash"][:16]
    assert "download_url" in result
    assert "expires_at" in result

    content = server.get_choke_drawing("P-100", "PART-200", "run-current")
    assert isinstance(content[0], TextContent)
    assert isinstance(content[1], EmbeddedResource)
    assert content[1].resource.mimeType == "application/pdf"


def test_missing_and_stale_trigger_ids_cannot_read_current_pdf(tmp_path, monkeypatch):
    _workflow(tmp_path, monkeypatch)

    with pytest.raises(drawing_service.DrawingAccessError, match="required") as missing:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "")
    assert missing.value.error_code == "TRIGGER_RUN_NOT_FOUND"
    with pytest.raises(drawing_service.DrawingAccessError, match="does not match") as stale:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-stale")
    assert stale.value.error_code == "STALE_TRIGGER_RUN"


def test_wrong_project_or_product_cannot_access_another_workflow(tmp_path, monkeypatch):
    drawing = _workflow(tmp_path, monkeypatch)
    missing_state = tmp_path / "missing" / "workflow_state.json"
    monkeypatch.setattr(
        drawing_service,
        "get_workflow_run_paths",
        lambda project_code, product_id: {
            "workflow_state_path": (
                tmp_path / "workflow_state.json"
                if (project_code, product_id) == ("P-100", "PART-200")
                else missing_state
            )
        },
    )

    with pytest.raises(ValueError, match="not found"):
        drawing_service.get_current_choke_drawing("P-OTHER", "PART-200", "run-current")
    assert drawing.exists()


def test_unlinked_and_unsupported_documents_return_explicit_codes(tmp_path, monkeypatch):
    drawing = _workflow(tmp_path, monkeypatch)
    state_path = tmp_path / "workflow_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["customer_input"]["attachment_manifest"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(drawing_service.DrawingAccessError) as unlinked:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-current")
    assert unlinked.value.error_code == "DRAWING_NOT_LINKED_TO_PRODUCT"

    unsupported = tmp_path / "current drawing.txt"
    unsupported.write_bytes(drawing.read_bytes())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["drawing_file_path"] = str(unsupported)
    state["customer_input"]["drawing_file_path"] = str(unsupported)
    state["bom"]["drawing_file_path"] = str(unsupported)
    state["bom"]["drawing_input_snapshot"]["stored_path"] = str(unsupported)
    state["customer_input"]["attachment_manifest"] = [{
        "attachment_id": state["bom"]["drawing_input_snapshot"]["document_id"],
        "original_filename": unsupported.name,
        "stored_path": str(unsupported),
        "checksum_sha256": hashlib.sha256(unsupported.read_bytes()).hexdigest(),
    }]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(drawing_service.DrawingAccessError) as file_type:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-current")
    assert file_type.value.error_code == "UNSUPPORTED_FILE_TYPE"


def test_access_failure_and_mcp_error_payload_are_structured(tmp_path, monkeypatch):
    _workflow(tmp_path, monkeypatch)
    monkeypatch.setattr(
        drawing_service,
        "_read_pdf",
        lambda _path: (_ for _ in ()).throw(
            drawing_service.DrawingAccessError("DRAWING_ACCESS_FAILED", "read failed")
        ),
    )
    with pytest.raises(drawing_service.DrawingAccessError) as access:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-current")
    assert access.value.error_code == "DRAWING_ACCESS_FAILED"

    content = server.get_choke_drawing("P-100", "PART-200", "run-current")
    payload = json.loads(content[0].text)
    assert payload["success"] is False
    assert payload["error_code"] == "DRAWING_ACCESS_FAILED"


def test_missing_and_revised_drawings_are_rejected(tmp_path, monkeypatch):
    drawing = _workflow(tmp_path, monkeypatch)
    drawing.unlink()
    with pytest.raises(drawing_service.DrawingAccessError) as missing:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-current")
    assert missing.value.error_code == "DRAWING_NOT_FOUND"

    drawing = _workflow(tmp_path, monkeypatch)
    drawing.write_bytes(b"%PDF-1.7\nrevised drawing\n%%EOF")
    with pytest.raises(drawing_service.DrawingAccessError) as revised:
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-current")
    assert revised.value.error_code == "DRAWING_REVISION_MISMATCH"


def test_snapshot_capture_is_bound_to_manifest_and_active_trigger(tmp_path, monkeypatch):
    drawing = _workflow(tmp_path, monkeypatch)
    snapshot = drawing_service.capture_choke_drawing_snapshot(
        "P-100", "PART-200", "run-current"
    )
    assert snapshot["document_id"] == hashlib.sha256(drawing.read_bytes()).hexdigest()[:16]
    assert snapshot["stored_path"] == str(drawing)
    assert snapshot["trigger_run_id"] == "run-current"


def test_mcp_drawing_then_bom_writeback_uses_same_trigger_id(tmp_path, monkeypatch):
    _workflow(tmp_path, monkeypatch)
    delivered = server.get_choke_drawing("P-100", "PART-200", "run-current")
    assert isinstance(delivered[1], EmbeddedResource)
    received = {}

    def fake_save_bom_output(**kwargs):
        received.update(kwargs)
        return {"success": True, "status": "saved", **kwargs}

    monkeypatch.setattr(workflow, "save_bom_output", fake_save_bom_output)
    monkeypatch.setattr(server, "_save_agent_json_traceability", lambda **_kwargs: {})
    result = server.save_bom_output(
        "P-100",
        "PART-200",
        "run-current",
        {"bom": []},
    )
    assert result["success"] is True
    assert received["trigger_run_id"] == "run-current"


def test_runtime_instruction_forbids_archived_fallback():
    from services.choke_sequential_agent_workflow import (
        _build_bom_runtime_instruction,
        _combine_drawing_preflights,
    )

    instruction = _build_bom_runtime_instruction("P-100", "PART-200", "run-current")
    assert "get_choke_drawing" in instruction
    assert "BOM_INPUT_FILE_UNAVAILABLE" in instruction
    assert "Never use an archived BOM" in instruction
    assert "do not call save_bom_output" in instruction

    combined = _combine_drawing_preflights(
        {"success": True, "mime_type": "application/pdf", "file_size": 42},
        {"success": False, "http_status": 403, "error_code": "drawing_url_forbidden"},
    )
    assert combined["success"] is True
    assert combined["delivery_mode"] == "mcp_embedded_resource"
    assert combined["pdf_network_access_blocked"] is True


def test_drawing_retrieval_failure_blocks_before_writeback_instruction():
    from services.choke_sequential_agent_workflow import (
        _build_bom_runtime_instruction,
        _combine_drawing_preflights,
    )

    combined = _combine_drawing_preflights(
        {"success": False, "error_code": "BOM_INPUT_FILE_UNAVAILABLE"},
        {"success": False, "http_status": 403},
    )
    instruction = _build_bom_runtime_instruction("P", "X", "run-current")
    assert combined["success"] is False
    assert combined["error_code"] == "BOM_INPUT_FILE_UNAVAILABLE"
    assert "do not call save_bom_output" in instruction
