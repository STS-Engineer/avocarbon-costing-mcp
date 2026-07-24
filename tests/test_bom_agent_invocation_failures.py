import io
import json
import socket
import urllib.error

from fastapi.testclient import TestClient

from app.main import app
from app.routers import choke_workflow_router
from services import choke_sequential_agent_workflow as workflow
from services import workspace_agent_client


class _Response:
    def __init__(self, status=202, body=b""):
        self.status = status
        self._body = body
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self._body


def _invoke(monkeypatch, urlopen):
    monkeypatch.setattr(workspace_agent_client.urllib.request, "urlopen", urlopen)
    return workspace_agent_client.trigger_workspace_agent(
        agent_id="agtch_test",
        access_token="test-token",
        input_text="test",
        conversation_key="P:X:bom",
        dry_run=False,
        timeout_seconds=0.01,
    )


def test_configuration_reports_missing_bom_agent_id():
    result = workspace_agent_client.workspace_agent_configuration(
        agent_id="",
        access_token="test-token",
    )

    assert result["status"] == "misconfigured"
    assert "CHATGPT_CHOKE_BOM_AGENT_ID" in result["missing_configuration"]


def test_configuration_reports_missing_access_token():
    result = workspace_agent_client.workspace_agent_configuration(
        agent_id="agtch_test",
        access_token="",
    )

    assert result["status"] == "misconfigured"
    assert "CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN" in result["missing_configuration"]


def test_workspace_api_401_and_403_are_failed(monkeypatch):
    for status in (401, 403):
        def raise_http_error(request, timeout, code=status):
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "rejected",
                {},
                io.BytesIO(json.dumps({"error": "rejected"}).encode()),
            )

        result = _invoke(monkeypatch, raise_http_error)
        assert result["status"] == "failed"
        assert result["http_status"] == status
        assert workflow._safe_trigger_failure(result)["code"] == (
            f"workspace_agent_http_{status}"
        )


def test_workspace_api_timeout_is_failed(monkeypatch):
    result = _invoke(
        monkeypatch,
        lambda request, timeout: (_ for _ in ()).throw(socket.timeout("late")),
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "timeout"
    assert workflow._safe_trigger_failure(result)["code"] == "workspace_agent_timeout"


def test_successful_agent_trigger_is_accepted(monkeypatch):
    result = _invoke(monkeypatch, lambda request, timeout: _Response())

    assert result["status"] == "accepted"
    assert result["http_status"] == 202


def test_unexpected_invocation_exception_becomes_failed_result(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "get_bom_agent_configuration_health",
        lambda: {
            "status": "configured",
            "agent_id_masked": "agtch_test",
            "token_present": True,
            "endpoint": "https://api.chatgpt.com/v1/workspace_agents/{agent_id}/trigger",
            "invocation_timeout_seconds": 30,
        },
    )
    monkeypatch.setattr(
        workflow,
        "_trigger",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = workflow._trigger_bom_agent_with_retries(
        project_code="P",
        product_id="X",
        input_text="test",
        dry_run=False,
        status_before="pending",
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "execution_exception"


def test_start_route_returns_failed_state_in_non_200_error(monkeypatch):
    state = {
        "project_code": "P",
        "product_id": "X",
        "status": "trigger_request_failed",
        "bom_status": "failed",
        "bom": {
            "status": "trigger_request_failed",
            "display_status": "failed",
            "safe_error": {
                "code": "workspace_agent_http_401",
                "message": "BOM Workspace Agent authorization was rejected.",
                "retryable": False,
            },
        },
    }
    monkeypatch.setattr(
        choke_workflow_router,
        "start_real_choke_workflow",
        lambda **kwargs: {"status": "trigger_request_failed", "state": state},
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/choke-workflow/start",
            json={"input_file": "input.json", "dry_run": False},
        )

    assert response.status_code == 502
    assert response.json()["detail"]["state"]["bom_status"] == "failed"


def test_retry_bom_only_returns_existing_received_state(monkeypatch):
    state = {
        "project_code": "P",
        "product_id": "X",
        "status": "bom_received",
        "bom": {"status": "received"},
        "components": {"core": {"status": "received"}},
        "most": {"wp": {"status": "received"}},
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *args: (state, None))

    result = workflow.retry_bom_agent("P", "X")

    assert result["skipped"] is True
    assert result["reason"] == "bom_already_received"
    assert state["components"]["core"]["status"] == "received"
    assert state["most"]["wp"]["status"] == "received"


def test_bom_agent_health_does_not_expose_token(monkeypatch):
    monkeypatch.setattr(
        choke_workflow_router,
        "get_bom_agent_configuration_health",
        lambda: {
            "status": "configured",
            "agent_id_masked": "agtch_test...last4",
            "token_present": True,
            "token_length": 42,
        },
    )

    response = choke_workflow_router.bom_agent_health()

    assert response["status"] == "configured"
    assert "access_token" not in json.dumps(response).lower()
