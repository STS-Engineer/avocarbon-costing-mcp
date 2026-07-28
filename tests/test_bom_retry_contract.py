import copy
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import choke_workflow_router
from services import choke_sequential_agent_workflow as workflow


def _state():
    return {
        "project_code": "P",
        "product_id": "X",
        "status": "most_received",
        "bom_status": "failed_retryable",
        "current_step": "Step 4 Final Calculation",
        "customer_input": {"drawing_file_path": "drawing.pdf"},
        "bom": {
            "status": "failed_retryable",
            "lifecycle_status": "failed_retryable",
            "trigger_run_id": "old-run",
            "save_path": "bom.json",
        },
        "components": {"core": {"status": "received", "value": 1}},
        "most": {"wp": {"status": "received", "p_h": 100}},
        "missing_outputs": [],
    }


def _patch_retry(monkeypatch, state, trigger_result):
    saved = []

    monkeypatch.setattr(
        workflow,
        "_existing_state",
        lambda *_args: (state, Path("state.json")),
    )
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_log_bom_lifecycle", lambda *_a, **_k: None)
    monkeypatch.setattr(
        workflow,
        "_save_state",
        lambda value: saved.append(copy.deepcopy(value)) or value,
    )
    monkeypatch.setattr(
        workflow,
        "_build_bom_trigger_payload",
        lambda *_a, **kwargs: {
            "input_text": f'{{"trigger_run_id":"{kwargs["trigger_run_id"]}"}}',
            "save_address": "bom.json",
            "trigger_run_id": kwargs["trigger_run_id"],
            "drawing_file_url": "https://example.test/drawing.pdf",
            "drawing_agent_proxy_url": "https://example.test/drawing.pdf",
            "drawing_sas_url": None,
            "drawing_access_mode": "backend_signed_proxy",
        },
    )
    monkeypatch.setattr(
        workflow,
        "_refresh_bom_trigger_signed_url",
        lambda trigger, *_args: trigger,
    )
    monkeypatch.setattr(
        workflow,
        "_validate_and_select_drawing_url",
        lambda _trigger: {
            "success": True,
            "selected": {
                "access_mode": "backend_signed_proxy",
                "validation": {"success": True},
            },
            "candidate_validations": [],
        },
    )
    calls = []

    def trigger(**kwargs):
        calls.append(kwargs)
        return {**trigger_result, "trigger_run_id": kwargs["trigger_run_id"]}

    monkeypatch.setattr(workflow, "_trigger_bom_agent_with_retries", trigger)
    return saved, calls


def test_accepted_retry_preserves_downstream_state(monkeypatch):
    state = _state()
    before_components = copy.deepcopy(state["components"])
    before_most = copy.deepcopy(state["most"])
    saved, calls = _patch_retry(
        monkeypatch,
        state,
        {
            "status": "accepted",
            "http_status": 202,
            "retryable": False,
            "attempts": [{"attempt_number": 1, "http_status": 202}],
        },
    )

    result = workflow._retry_bom_agent_locked("P", "X")

    assert len(calls) == 1
    assert result["success"] is True
    assert result["status"] == "most_received"
    assert result["bom_status"] == "triggered"
    assert result["bom"]["status"] == "triggered"
    assert result["bom"]["lifecycle_status"] == "awaiting_writeback"
    assert result["state"]["components"] == before_components
    assert result["state"]["most"] == before_most
    assert all(item["status"] == "most_received" for item in saved)
    assert calls[0]["trigger_run_id"] == result["bom"]["trigger_run_id"]


@pytest.mark.parametrize(
    ("http_status", "expected_bom_status", "expected_http"),
    [
        (503, "failed_retryable", 503),
        (401, "failed_non_retryable", 401),
        (403, "failed_non_retryable", 403),
    ],
)
def test_retry_failure_is_non_200_and_preserves_global_status(
    monkeypatch,
    http_status,
    expected_bom_status,
    expected_http,
):
    state = _state()
    retryable = http_status == 503
    _patch_retry(
        monkeypatch,
        state,
        {
            "status": "failed",
            "http_status": http_status,
            "retryable": retryable,
            "attempts": [
                {
                    "attempt_number": 1,
                    "http_status": http_status,
                    "failure_timestamp": "2026-07-28T00:00:00+00:00",
                }
            ],
        },
    )

    result = workflow._retry_bom_agent_locked("P", "X")

    assert result["success"] is False
    assert result["status"] == "most_received"
    assert result["bom_status"] == expected_bom_status
    with pytest.raises(HTTPException) as raised:
        choke_workflow_router._raise_trigger_failure(result)
    assert raised.value.status_code == expected_http
    assert raised.value.detail["state"]["status"] == "most_received"
    assert raised.value.detail["state"]["bom_status"] == expected_bom_status
