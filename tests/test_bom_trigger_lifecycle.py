import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services import choke_sequential_agent_workflow as workflow
from services import choke_writeback_mcp_diagnostic as mcp_diagnostic
from services import workspace_agent_client


class _Response:
    def __init__(self, body=b"", status=202, headers=None):
        self._body = body
        self._status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self._status

    def read(self):
        return self._body


def _trigger(monkeypatch, body=b""):
    monkeypatch.setattr(
        workspace_agent_client.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(body=body),
    )
    return workspace_agent_client.trigger_workspace_agent(
        agent_id="agtch_test",
        access_token="token",
        input_text="test",
        dry_run=False,
    )


def test_202_with_empty_body_is_accepted_without_conversation_assumption(monkeypatch):
    result = _trigger(monkeypatch)

    assert result["status"] == "accepted"
    assert result["http_status"] == 202
    assert result["response"] is None
    assert result["conversation_url_verified"] is False


def test_202_optional_metadata_remains_unverified_diagnostic(monkeypatch):
    result = _trigger(
        monkeypatch,
        json.dumps({"conversation_url": "https://example.invalid/conversation"}).encode(),
    )

    assert result["status"] == "accepted"
    assert result["response"]["conversation_url"]
    assert result["conversation_url_verified"] is False


def test_conversation_url_audit_is_safe():
    result = workflow._safe_conversation_url_audit(
        "https://chatgpt.com/g/g-example/c/abcdef123456?secret=not-logged"
    )

    assert result == {
        "conversation_url_returned": True,
        "conversation_url_host": "chatgpt.com",
        "conversation_url_path_suffix": "abcdef123456",
    }
    assert "secret" not in json.dumps(result)


def test_accepted_trigger_logs_safe_conversation_diagnostic(monkeypatch, caplog):
    monkeypatch.setattr(workflow, "_load_env", lambda: None)
    monkeypatch.setattr(
        workflow,
        "get_bom_agent_configuration_health",
        lambda: {
            "status": "configured",
            "agent_id_masked": "agtch_...test",
            "token_present": True,
            "endpoint": "https://api.chatgpt.com/v1/workspace_agents/{agent_id}/trigger",
            "invocation_timeout_seconds": 30,
        },
    )
    monkeypatch.setattr(
        workflow,
        "require_bom_writeback_capability",
        lambda: {"save_bom_output_accepts_trigger_run_id": True},
    )
    monkeypatch.setattr(workflow, "_bom_trigger_max_attempts", lambda: 1)
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (None, None))
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        workflow,
        "_trigger",
        lambda *args, **kwargs: {
            "status": "accepted",
            "http_status": 202,
            "request_correlation_id": "request-safe",
            "response": {
                "conversation_url": (
                    "https://chatgpt.com/g/g-example/c/abcdef123456?secret=not-logged"
                )
            },
        },
    )
    caplog.set_level(logging.INFO, logger=workflow.__name__)

    result = workflow._trigger_bom_agent_with_retries(
        "P",
        "X",
        '{"trigger_run_id":"run-current"}',
        dry_run=False,
        status_before="created",
        trigger_run_id="run-current",
    )

    assert result["status"] == "accepted"
    assert '"event": "trigger_accepted"' in caplog.text
    assert '"request_id": "request-safe"' in caplog.text
    assert '"conversation_url_returned": true' in caplog.text
    assert '"conversation_url_path_suffix": "abcdef123456"' in caplog.text
    assert "not-logged" not in caplog.text


def test_accepted_status_waits_for_callback(monkeypatch):
    monkeypatch.setenv("BOM_CALLBACK_TIMEOUT_SECONDS", "900")
    now = datetime.now(timezone.utc)
    state = {
        "status": "trigger_request_accepted",
        "bom": {
            "status": "trigger_request_accepted",
            "lifecycle_status": "trigger_request_accepted",
            "accepted_at": now.isoformat(),
            "trigger_result": {"status": "accepted", "http_status": 202},
        },
    }

    workflow._apply_bom_callback_waiting_state(state, now=now + timedelta(seconds=10))

    assert state["status"] == "awaiting_writeback"
    assert state["bom"]["lifecycle_status"] == "awaiting_writeback"
    assert state["bom"]["retryable"] is False
    assert state["message"] == "Agent request accepted and queued. Waiting for BOM output."


def test_callback_timeout_and_duplicate_retry_block(monkeypatch):
    monkeypatch.setenv("BOM_CALLBACK_TIMEOUT_SECONDS", "60")
    now = datetime.now(timezone.utc)
    waiting = {
        "project_code": "P",
        "product_id": "X",
        "status": "awaiting_bom_callback",
        "bom": {
            "status": "awaiting_bom_callback",
            "lifecycle_status": "awaiting_bom_callback",
            "accepted_at": now.isoformat(),
            "trigger_result": {"status": "accepted", "http_status": 202},
        },
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (waiting, Path("state.json")))

    retry = workflow.retry_bom_agent("P", "X")
    assert retry["skipped"] is True
    assert retry["reason"] == "bom_callback_wait_still_active"

    workflow._apply_bom_callback_waiting_state(
        waiting,
        now=now + timedelta(seconds=61),
    )
    assert waiting["status"] == "bom_callback_timeout"
    assert waiting["bom"]["retryable"] is True
    assert waiting["bom"]["lifecycle_status"] == "failed"
    assert waiting["bom"]["failure_code"] == "callback_timeout"
    assert waiting["bom"]["safe_error"]["code"] == "callback_timeout"


def _patch_writeback(monkeypatch, tmp_path, state):
    raw_path = tmp_path / "raw.json"
    normalized_path = tmp_path / "normalized.json"
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, state_path))
    monkeypatch.setattr(workflow, "_run_dir", lambda *args: tmp_path)
    monkeypatch.setattr(workflow, "_state_path", lambda *args: state_path)
    monkeypatch.setattr(workflow, "_bom_raw_path", lambda *args: raw_path)
    monkeypatch.setattr(workflow, "_bom_normalized_path", lambda *args: normalized_path)
    monkeypatch.setattr(
        workflow,
        "workflow_path_diagnostics",
        lambda *args: {"project_code": "P", "product_id": "X"},
    )
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "classify_choke", lambda *args: {})
    monkeypatch.setattr(
        workflow,
        "normalize_bom",
        lambda *args: {"components": [{"component_id": "ferrite_core"}]},
    )
    monkeypatch.setattr(workflow, "extract_bom_technical_fields", lambda *args: {})
    monkeypatch.setattr(
        workflow,
        "_update_customer_input_from_bom",
        lambda *args: {"status": "skipped", "extracted": {}},
    )
    monkeypatch.setattr(workflow, "_refresh_master_data_for_state", lambda *args: {})
    monkeypatch.setattr(workflow, "build_choke_process_route", lambda *args: {})
    monkeypatch.setattr(workflow, "_required_external_components", lambda *args: [])


def _waiting_state():
    return {
        "project_code": "P",
        "product_id": "X",
        "status": "awaiting_bom_callback",
        "customer_input": {},
        "bom": {
            "status": "awaiting_bom_callback",
            "lifecycle_status": "awaiting_bom_callback",
            "trigger_run_id": "run-current",
            "trigger_result": {"status": "accepted", "http_status": 202},
        },
        "components": {},
        "most": {},
        "errors": [],
    }


def _test_dir():
    path = Path("test_artifacts_bom_lifecycle") / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_missing_trigger_run_id_callback_is_rejected(monkeypatch, caplog):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)

    try:
        caplog.set_level(logging.INFO, logger=workflow.__name__)
        result = workflow.save_bom_output("P", "X", "", {"bom": []})
        assert result["error_code"] == "missing_trigger_run_id"
        assert not (tmp_path / "raw.json").exists()
        assert '"event": "callback_rejected"' in caplog.text
        assert '"rejection_reason": "missing_trigger_run_id"' in caplog.text
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_wrong_trigger_run_id_is_recorded_as_stale(monkeypatch):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)
    raw_path = tmp_path / "raw.json"
    normalized_path = tmp_path / "normalized.json"
    raw_path.write_text('{"existing":"raw"}', encoding="utf-8")
    normalized_path.write_text('{"existing":"normalized"}', encoding="utf-8")
    state_before = json.loads(json.dumps(state))

    try:
        result = workflow.save_bom_output(
            "P", "X", "run-old", {"bom": []}
        )
        assert result["status"] == "stale_callback"
        assert state["stale_bom_callbacks"][0]["received_trigger_run_id"] == "run-old"
        assert raw_path.read_text(encoding="utf-8") == '{"existing":"raw"}'
        assert normalized_path.read_text(encoding="utf-8") == '{"existing":"normalized"}'
        assert state["bom"] == state_before["bom"]
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_valid_current_run_callback_completes_and_normalizes(monkeypatch, caplog):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)

    try:
        caplog.set_level(logging.INFO, logger=workflow.__name__)
        result = workflow.save_bom_output(
            "P", "X", "run-current", {"bom": []}
        )
        assert result["success"] is True
        assert state["status"] == "bom_received"
        assert state["bom"]["callback_status"] == "bom_received"
        assert state["bom"]["normalization_status"] == "bom_normalized"
        assert state["bom"]["received_for_trigger_run_id"] == "run-current"
        assert '"event": "callback_accepted"' in caplog.text
        assert '"event": "raw_bom_saved"' in caplog.text
        assert '"event": "normalized_bom_saved"' in caplog.text
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_trigger_payload_requires_correlated_writeback():
    result = workflow._build_bom_trigger_payload(
        "P",
        "X",
        {"drawing_file_url": "https://example.invalid/test.pdf", "drawing_access_mode": "diagnostic_url"},
        trigger_run_id="run-123",
    )

    assert result["payload"]["trigger_run_id"] == "run-123"
    assert "trigger_run_id" in result["payload"]["instruction"]


def test_tool_not_attached_produces_configuration_diagnostic(monkeypatch):
    schemas = mcp_diagnostic._runtime_tool_schemas()
    schemas.pop("save_bom_output")
    monkeypatch.setattr(mcp_diagnostic, "_runtime_tool_schemas", lambda: schemas)

    result = mcp_diagnostic.get_writeback_mcp_connectivity_diagnostic()

    assert result["status"] == "configuration_error"
    assert result["save_bom_output_exists"] is False
    assert result["health_check"]["write_performed"] is False
