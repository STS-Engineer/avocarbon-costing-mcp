import inspect

import server
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
    assert diagnostic["trigger_request_contract"]["drawing_delivery_mode"] == "signed_url"


def test_signed_url_runtime_instruction_is_explicit_and_correlated(monkeypatch):
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

    assert payload["payload"]["drawing_delivery_mode"] == "signed_url"
    assert payload["payload"]["drawing_filename"] == "original drawing.pdf"
    assert "Open drawing_file_url now" in instruction
    assert "Do not wait for ./user_files/" in instruction
    assert "another user message" in instruction
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

    assert diagnostic["drawing_delivery_mode"] == "signed_url"
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
