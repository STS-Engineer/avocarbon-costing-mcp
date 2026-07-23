import json
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

    assert state["status"] == "awaiting_bom_callback"
    assert state["bom"]["lifecycle_status"] == "awaiting_bom_callback"
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


def test_missing_trigger_run_id_callback_is_rejected(monkeypatch):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)

    try:
        result = workflow.save_bom_output("P", "X", {"bom": []})
        assert result["error_code"] == "missing_trigger_run_id"
        assert not (tmp_path / "raw.json").exists()
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_wrong_trigger_run_id_is_recorded_as_stale(monkeypatch):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)

    try:
        result = workflow.save_bom_output(
            "P", "X", {"bom": []}, trigger_run_id="run-old"
        )
        assert result["status"] == "stale_callback"
        assert state["stale_bom_callbacks"][0]["received_trigger_run_id"] == "run-old"
        assert not (tmp_path / "raw.json").exists()
    finally:
        shutil.rmtree(tmp_path.parent, ignore_errors=True)


def test_valid_current_run_callback_completes_and_normalizes(monkeypatch):
    tmp_path = _test_dir()
    state = _waiting_state()
    _patch_writeback(monkeypatch, tmp_path, state)

    try:
        result = workflow.save_bom_output(
            "P", "X", {"bom": []}, trigger_run_id="run-current"
        )
        assert result["success"] is True
        assert state["status"] == "bom_received"
        assert state["bom"]["callback_status"] == "bom_received"
        assert state["bom"]["normalization_status"] == "bom_normalized"
        assert state["bom"]["received_for_trigger_run_id"] == "run-current"
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
    schemas = dict(mcp_diagnostic.WRITEBACK_TOOL_SCHEMAS)
    schemas.pop("save_bom_output")
    monkeypatch.setattr(mcp_diagnostic, "WRITEBACK_TOOL_SCHEMAS", schemas)

    result = mcp_diagnostic.get_writeback_mcp_connectivity_diagnostic()

    assert result["status"] == "configuration_error"
    assert result["save_bom_output_exists"] is False
    assert result["health_check"]["write_performed"] is False
