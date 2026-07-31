import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from mcp.types import EmbeddedResource, TextContent

import server
from services import choke_drawing_mcp_service as drawing_service
from services.choke_writeback_mcp_diagnostic import (
    get_mcp_schema_fingerprints,
    validate_runtime_writeback_schemas,
)


def _workflow(tmp_path: Path, monkeypatch, *, trigger_run_id: str = "run-current"):
    drawing = tmp_path / "current drawing.pdf"
    drawing.write_bytes(b"%PDF-1.7\ncurrent drawing\n%%EOF")
    state_path = tmp_path / "workflow_state.json"
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
                "checksum_sha256": hashlib.sha256(drawing.read_bytes()).hexdigest(),
            }],
        },
        "bom": {
            "trigger_run_id": trigger_run_id,
            "drawing_file_path": str(drawing),
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

    content = server.get_choke_drawing("P-100", "PART-200", "run-current")
    assert isinstance(content[0], TextContent)
    assert isinstance(content[1], EmbeddedResource)
    assert content[1].resource.mimeType == "application/pdf"


def test_missing_and_stale_trigger_ids_cannot_read_current_pdf(tmp_path, monkeypatch):
    _workflow(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="required"):
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "")
    with pytest.raises(ValueError, match="does not match"):
        drawing_service.get_current_choke_drawing("P-100", "PART-200", "run-stale")


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
