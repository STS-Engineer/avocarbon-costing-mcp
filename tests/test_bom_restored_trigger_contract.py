import json

from services import choke_sequential_agent_workflow as workflow
from services import workspace_agent_client as client


class _Response:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return 202

    def read(self):
        return b""


def test_shared_client_can_preserve_last_working_request_identifiers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    result = client.trigger_workspace_agent(
        agent_id="agtch_test",
        access_token="test-token",
        input_text='{"trigger_run_id":"run-current"}',
        conversation_key="PROJECT:PRODUCT:sequential:bom",
        idempotency_key="PROJECT:PRODUCT:sequential:bom:run-current",
        dry_run=False,
        preserve_request_identifiers=True,
    )

    assert captured["body"] == {
        "input": '{"trigger_run_id":"run-current"}',
        "conversation_key": "PROJECT:PRODUCT:sequential:bom",
    }
    assert captured["headers"]["Idempotency-key"] == (
        "PROJECT:PRODUCT:sequential:bom:run-current"
    )
    assert ":invocation:" not in captured["body"]["conversation_key"]
    assert ":invocation:" not in captured["headers"]["Idempotency-key"]
    assert captured["timeout"] == 60.0
    assert result["status"] == "accepted"


def test_bom_flow_uses_restored_identifiers(monkeypatch):
    calls = []

    def fake_trigger(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return {"status": "dry_run"}

    monkeypatch.setattr(
        workflow,
        "get_bom_agent_configuration_health",
        lambda: {"status": "configured"},
    )
    monkeypatch.setattr(
        workflow,
        "require_bom_writeback_capability",
        lambda: {"save_bom_output_accepts_trigger_run_id": True},
    )
    monkeypatch.setattr(workflow, "_trigger", fake_trigger)

    result = workflow._trigger_bom_agent_with_retries(
        "P",
        "X",
        '{"trigger_run_id":"bom-run"}',
        dry_run=True,
        status_before="created",
        trigger_run_id="bom-run",
    )

    assert result["status"] == "dry_run"
    assert calls[0]["args"][3] == "P:X:sequential:bom:bom-run"
    assert calls[0]["args"][4] == "P:X:sequential:bom:bom-run"
    assert calls[0]["kwargs"]["preserve_request_identifiers"] is True


def test_each_logical_bom_invocation_gets_unique_identifiers():
    first = workflow._bom_invocation_identifiers("P", "X", "run-one")
    second = workflow._bom_invocation_identifiers("P", "X", "run-two")

    assert first == {
        "conversation_key": "P:X:sequential:bom:run-one",
        "idempotency_key": "P:X:sequential:bom:run-one",
    }
    assert second == {
        "conversation_key": "P:X:sequential:bom:run-two",
        "idempotency_key": "P:X:sequential:bom:run-two",
    }
    assert first["conversation_key"] != second["conversation_key"]
    assert first["idempotency_key"] != second["idempotency_key"]


def test_bounded_http_attempts_reuse_one_bom_invocation(monkeypatch):
    calls = []
    results = iter(
        [
            {
                "status": "failed",
                "http_status": 503,
                "error_type": "http_error",
            },
            {
                "status": "failed",
                "http_status": 503,
                "error_type": "http_error",
            },
            {"status": "accepted", "http_status": 202},
        ]
    )

    def fake_trigger(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return next(results)

    def fake_prepare(
        project_code,
        product_id,
        normalized_input,
        request_base_url,
        attempt_number,
        trigger_run_id,
        conversation_key,
        idempotency_key,
        workflow_status_before,
        current_step_before,
    ):
        return {
            "status": "ready",
            "trigger_run_id": trigger_run_id,
            "conversation_key": conversation_key,
            "idempotency_key": idempotency_key,
            "input_text": '{"trigger_run_id":"run-one"}',
            "pdf_url_check": {"success": True},
        }

    monkeypatch.setattr(
        workflow,
        "get_bom_agent_configuration_health",
        lambda: {"status": "configured"},
    )
    monkeypatch.setattr(
        workflow,
        "require_bom_writeback_capability",
        lambda: {"save_bom_output_accepts_trigger_run_id": True},
    )
    monkeypatch.setattr(workflow, "_bom_trigger_max_attempts", lambda: 3)
    monkeypatch.setattr(workflow, "_trigger_backoff_seconds", lambda *_: 0)
    monkeypatch.setattr(workflow.time, "sleep", lambda *_: None)
    monkeypatch.setattr(workflow, "_trigger", fake_trigger)
    monkeypatch.setattr(workflow, "_prepare_automatic_bom_retry", fake_prepare)
    monkeypatch.setattr(workflow, "_existing_state", lambda *_: (None, None))
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *a, **k: None)

    result = workflow._trigger_bom_agent_with_retries(
        "P",
        "X",
        '{"trigger_run_id":"run-one"}',
        dry_run=False,
        status_before="created",
        trigger_run_id="run-one",
        retry_context={"normalized_input": {}},
    )

    assert result["status"] == "accepted"
    assert len(calls) == 3
    assert {call["args"][3] for call in calls} == {
        "P:X:sequential:bom:run-one"
    }
    assert {call["args"][4] for call in calls} == {
        "P:X:sequential:bom:run-one"
    }
    assert {attempt["trigger_run_id"] for attempt in result["attempts"]} == {
        "run-one"
    }


def test_other_agent_flow_keeps_fresh_identifier_behavior(monkeypatch):
    calls = []

    def fake_trigger_workspace_agent(**kwargs):
        calls.append(kwargs)
        return {"status": "dry_run"}

    monkeypatch.setattr(
        workflow,
        "trigger_workspace_agent",
        fake_trigger_workspace_agent,
    )
    monkeypatch.setenv("CHATGPT_EXTERNAL_COMPONENT_AGENT_ID", "agtch_component")

    workflow._trigger(
        "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
        "",
        '{"component_id":"ferrite"}',
        "P:X:component:ferrite:v1",
        "P:X:component:ferrite:v1:run",
        dry_run=True,
    )

    assert calls[0]["preserve_request_identifiers"] is False
