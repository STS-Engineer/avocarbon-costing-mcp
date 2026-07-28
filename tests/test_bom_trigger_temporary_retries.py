import copy

import pytest

from services import choke_sequential_agent_workflow as workflow


def _configured():
    return {
        "status": "configured",
        "missing_configuration": [],
        "agent_id_masked": "agtch_test...",
        "token_present": True,
        "endpoint": "https://api.chatgpt.com/v1/workspace_agents/{agent_id}/trigger",
        "invocation_timeout_seconds": 60,
    }


def _run_retry_sequence(monkeypatch, responses):
    state = {
        "project_code": "P",
        "product_id": "X",
        "status": "trigger_request_sending",
        "bom": {"status": "trigger_request_sending", "trigger_run_id": "run-1"},
        "components": {"existing": {"status": "received"}},
        "most": {"existing": {"status": "received"}},
    }
    saved = []
    calls = []
    prepared_ids = iter(["run-2", "run-3"])

    monkeypatch.setattr(workflow, "get_bom_agent_configuration_health", _configured)
    monkeypatch.setattr(workflow, "_existing_state", lambda *_: (copy.deepcopy(state), None))

    def save(updated):
        state.clear()
        state.update(copy.deepcopy(updated))
        saved.append(copy.deepcopy(updated))
        return updated

    def prepare(*_args):
        run_id = next(prepared_ids)
        state["bom"]["trigger_run_id"] = run_id
        return {
            "status": "ready",
            "trigger_run_id": run_id,
            "input_text": f'{{"trigger_run_id":"{run_id}"}}',
            "pdf_url_check": {"success": True},
        }

    def trigger(*args, **_kwargs):
        calls.append(args[2])
        return responses[len(calls) - 1]

    monkeypatch.setattr(workflow, "_save_state", save)
    monkeypatch.setattr(workflow, "_prepare_automatic_bom_retry", prepare)
    monkeypatch.setattr(workflow, "_trigger", trigger)
    monkeypatch.setattr(workflow, "_log_bom_lifecycle", lambda *_a, **_k: None)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: None)
    monkeypatch.setattr(workflow.time, "sleep", lambda *_: None)
    monkeypatch.setenv("WORKSPACE_AGENT_TRIGGER_INITIAL_BACKOFF_SECONDS", "0")

    result = workflow._trigger_bom_agent_with_retries(
        project_code="P",
        product_id="X",
        input_text='{"trigger_run_id":"run-1"}',
        dry_run=False,
        status_before="created",
        trigger_run_id="run-1",
        retry_context={"normalized_input": {"drawing_file_path": "drawing.pdf"}},
    )
    return result, calls, state, saved


@pytest.mark.parametrize("temporary_status", [502, 503])
def test_temporary_gateway_failure_then_success(monkeypatch, temporary_status):
    result, calls, state, _ = _run_retry_sequence(
        monkeypatch,
        [
            {
                "status": "failed",
                "http_status": temporary_status,
                "error_type": "http_error",
                "request_correlation_id": "req-failed",
            },
            {"status": "accepted", "http_status": 202, "request_correlation_id": "req-ok"},
        ],
    )

    assert result["status"] == "accepted"
    assert result["trigger_run_id"] == "run-2"
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert state["components"]["existing"]["status"] == "received"
    assert state["most"]["existing"]["status"] == "received"


def test_429_respects_retry_after(monkeypatch):
    result, calls, _, _ = _run_retry_sequence(
        monkeypatch,
        [
            {
                "status": "failed",
                "http_status": 429,
                "error_type": "http_error",
                "retry_after_seconds": 7,
            },
            {"status": "accepted", "http_status": 202},
        ],
    )

    assert result["status"] == "accepted"
    assert result["attempts"][0]["retry_after_seconds"] == 7
    assert result["attempts"][0]["next_retry_seconds"] == 7
    assert len(calls) == 2


def test_all_three_attempts_unavailable_is_retryable(monkeypatch):
    result, calls, state, _ = _run_retry_sequence(
        monkeypatch,
        [
            {"status": "failed", "http_status": 503, "error_type": "http_error"},
            {"status": "failed", "http_status": 502, "error_type": "http_error"},
            {"status": "failed", "http_status": 504, "error_type": "http_error"},
        ],
    )

    assert result["status"] == "failed"
    assert result["retryable"] is True
    assert result["max_attempts"] == 3
    assert [item["trigger_run_id"] for item in result["attempts"]] == [
        "run-1",
        "run-2",
        "run-3",
    ]
    assert len(calls) == 3
    assert state["status"] == "failed_retryable"
    assert state["bom"]["accepted_at"] is None


def test_401_is_not_retried(monkeypatch):
    result, calls, state, _ = _run_retry_sequence(
        monkeypatch,
        [{"status": "failed", "http_status": 401, "error_type": "http_error"}],
    )

    assert result["retryable"] is False
    assert len(calls) == 1
    assert state["status"] == "failed_non_retryable"


def test_duplicate_retry_is_blocked_while_trigger_request_is_active(monkeypatch):
    state = {
        "project_code": "P",
        "product_id": "X",
        "status": "retrying_trigger",
        "bom": {
            "status": "retrying_trigger",
            "lifecycle_status": "retrying_trigger",
        },
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *_: (state, None))
    monkeypatch.setattr(
        workflow,
        "_build_bom_trigger_payload",
        lambda *_a, **_k: pytest.fail("duplicate retry must not build a payload"),
    )

    result = workflow._retry_bom_agent_locked("P", "X")

    assert result["skipped"] is True
    assert result["reason"] == "bom_trigger_request_in_progress"
