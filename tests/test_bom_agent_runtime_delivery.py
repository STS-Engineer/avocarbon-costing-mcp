import asyncio
import inspect

import server
from app.routers.choke_workflow_router import (
    SaveBomOutputRequest,
    SaveComponentOutputRequest,
    SaveMostOutputRequest,
)
from services import choke_sequential_agent_workflow as workflow
from services import choke_writeback_mcp_diagnostic as mcp_diagnostic
from services.workspace_agent_client import (
    WORKSPACE_AGENT_TRIGGER_BODY_FIELDS,
    workspace_agent_configuration,
)


def _input():
    return {
        "project_code": "P-100",
        "product_id": "PART-200",
        "product": "Fuse choke",
        "drawing_file_path": "data/customer_inputs/P-100/original drawing.pdf",
        "drawing_reference": "original drawing.pdf",
    }


def test_workspace_trigger_contract_has_no_attachment_field(monkeypatch):
    monkeypatch.setenv("CHATGPT_CHOKE_BOM_AGENT_ID", "agtch_test")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN", "token")
    diagnostic = workspace_agent_configuration()

    assert WORKSPACE_AGENT_TRIGGER_BODY_FIELDS == {"input", "conversation_key"}
    assert diagnostic["trigger_request_contract"]["file_attachments_supported"] is False
    assert diagnostic["trigger_request_contract"]["drawing_delivery_mode"] == "mcp_embedded_resource"


def test_mcp_drawing_runtime_instruction_is_explicit_and_correlated(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_drawing_file_url_from_path",
        lambda *_: "https://backend.example/api/choke-costing/agent-files/P-100/original.pdf?token=secret",
    )
    payload = workflow._build_bom_trigger_payload(
        "P-100",
        "PART-200",
        _input(),
        trigger_run_id="run-current",
    )
    instruction = payload["payload"]["instruction"]

    assert payload["payload"]["drawing_delivery_mode"] == "mcp_embedded_resource"
    assert payload["payload"]["drawing_filename"] == "original drawing.pdf"
    assert "First call get_choke_drawing" in instruction
    assert "Analyze only the current PDF returned by that MCP action" in instruction
    assert "drawing_file_url is diagnostic metadata only" in instruction
    assert "BOM_INPUT_FILE_UNAVAILABLE" in instruction
    assert "Never use an archived BOM" in instruction
    assert "project_code exactly as 'P-100'" in instruction
    assert "product_id exactly as 'PART-200'" in instruction
    assert "trigger_run_id exactly as 'run-current'" in instruction
    assert "call save_bom_output exactly once" in instruction
    assert "complete BOM JSON object as raw_json" in instruction
    assert "run-old" not in payload["input_text"]


def test_runtime_instruction_never_reuses_old_trigger_id(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "_drawing_file_url_from_path",
        lambda *_: "https://backend.example/drawing.pdf?token=fresh",
    )
    first = workflow._build_bom_trigger_payload(
        "P-100", "PART-200", _input(), trigger_run_id="run-old"
    )
    second = workflow._build_bom_trigger_payload(
        "P-100", "PART-200", _input(), trigger_run_id="run-new"
    )

    assert "run-old" in first["input_text"]
    assert "run-new" in second["input_text"]
    assert "run-old" not in second["input_text"]


def test_save_bom_output_schema_requires_trigger_run_id():
    diagnostic = mcp_diagnostic.get_bom_agent_capability_diagnostic()

    assert diagnostic["drawing_delivery_mode"] == "mcp_embedded_resource"
    assert diagnostic["get_choke_drawing_available"] is True
    assert diagnostic["save_bom_output_available"] is True
    assert diagnostic["save_bom_output_accepts_trigger_run_id"] is True
    assert diagnostic["save_bom_output_required_fields"] == [
        "product_id",
        "project_code",
        "raw_json",
        "trigger_run_id",
    ]
    runtime_signature = inspect.signature(server.save_bom_output)
    assert runtime_signature.parameters["trigger_run_id"].default is inspect.Parameter.empty


def test_registered_mcp_writeback_schemas_are_strict_and_scoped():
    tools = {
        tool.name: tool.inputSchema
        for tool in asyncio.run(server.mcp.list_tools())
        if tool.name in {
            "get_choke_drawing",
            "save_bom_output",
            "save_component_output",
            "save_most_output",
        }
    }

    drawing_schema = tools["get_choke_drawing"]
    assert set(drawing_schema["properties"]) == {
        "project_code",
        "product_id",
        "trigger_run_id",
    }
    assert set(drawing_schema["required"]) == {
        "project_code",
        "product_id",
        "trigger_run_id",
    }

    bom_schema = tools["save_bom_output"]
    assert set(bom_schema["properties"]) == {
        "project_code",
        "product_id",
        "trigger_run_id",
        "raw_json",
    }
    assert set(bom_schema["required"]) == {
        "project_code",
        "product_id",
        "trigger_run_id",
        "raw_json",
    }
    assert bom_schema["properties"]["trigger_run_id"] == {
        "title": "Trigger Run Id",
        "type": "string",
    }

    component_schema = tools["save_component_output"]
    assert set(component_schema["properties"]) == {
        "project_code",
        "product_id",
        "component_id",
        "trigger_run_id",
        "raw_json",
    }
    assert set(component_schema["required"]) == {
        "project_code",
        "product_id",
        "component_id",
        "trigger_run_id",
        "raw_json",
    }

    most_schema = tools["save_most_output"]
    assert set(most_schema["properties"]) == {
        "project_code",
        "product_id",
        "trigger_run_id",
        "raw_json",
        "most_scope_id",
        "work_package_id",
    }
    assert set(most_schema["required"]) == {
        "project_code",
        "product_id",
        "work_package_id",
        "most_scope_id",
        "trigger_run_id",
        "raw_json",
    }


def test_rest_bom_writeback_schema_requires_trigger_run_id():
    model_schema = getattr(
        SaveBomOutputRequest,
        "model_json_schema",
        SaveBomOutputRequest.schema,
    )()

    assert "trigger_run_id" in model_schema["required"]
    assert model_schema["properties"]["trigger_run_id"]["type"] == "string"


def test_rest_component_and_most_writeback_schemas_match_mcp_catalog():
    component_schema = getattr(
        SaveComponentOutputRequest,
        "model_json_schema",
        SaveComponentOutputRequest.schema,
    )()
    most_schema = getattr(
        SaveMostOutputRequest,
        "model_json_schema",
        SaveMostOutputRequest.schema,
    )()

    assert set(component_schema["required"]) == {
        "project_code", "product_id", "component_id", "trigger_run_id", "raw_json"
    }
    assert component_schema["properties"]["trigger_run_id"]["type"] == "string"
    assert set(most_schema["required"]) == {
        "project_code", "product_id", "work_package_id", "most_scope_id",
        "trigger_run_id", "raw_json",
    }
    assert most_schema["properties"]["trigger_run_id"]["type"] == "string"


def test_invocation_fails_before_agent_when_schema_is_incompatible(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "get_bom_agent_configuration_health",
        lambda: {"status": "configured"},
    )
    monkeypatch.setattr(
        workflow,
        "require_bom_writeback_capability",
        lambda: (_ for _ in ()).throw(RuntimeError("trigger_run_id missing")),
    )
    monkeypatch.setattr(
        workflow,
        "_trigger",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("Workspace Agent must not be invoked")
        ),
    )

    result = workflow._trigger_bom_agent_with_retries(
        "P-100",
        "PART-200",
        '{"trigger_run_id":"run-current"}',
        dry_run=False,
        status_before="created",
        trigger_run_id="run-current",
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "bom_writeback_schema_incompatible"
    assert result["retryable"] is False
    assert result["attempts"] == []


def test_each_refresh_regenerates_the_signed_file_url(monkeypatch):
    urls = iter(
        [
            "https://backend.example/drawing.pdf?token=initial",
            "https://backend.example/drawing.pdf?token=retry-1",
            "https://backend.example/drawing.pdf?token=retry-2",
        ]
    )
    monkeypatch.setattr(workflow, "_drawing_file_url_from_path", lambda *_: next(urls))
    trigger = workflow._build_bom_trigger_payload(
        "P-100", "PART-200", _input(), trigger_run_id="run-current"
    )

    initial = trigger["drawing_file_url"]
    workflow._refresh_bom_trigger_signed_url(trigger)
    retry_one = trigger["drawing_file_url"]
    workflow._refresh_bom_trigger_signed_url(trigger)
    retry_two = trigger["drawing_file_url"]

    assert len({initial, retry_one, retry_two}) == 3
