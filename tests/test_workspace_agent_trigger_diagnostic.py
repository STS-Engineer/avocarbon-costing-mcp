import io
import json
import urllib.error

import pytest

from services import workspace_agent_trigger_diagnostic as diagnostic


class FakeResponse:
    def __init__(self, status=202, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def configured_environment(monkeypatch):
    monkeypatch.setenv(
        "CHATGPT_CHOKE_BOM_AGENT_ID",
        "agtch_1234567890abcdef",
    )
    monkeypatch.setenv(
        "CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN",
        "test-token-never-returned",
    )
    monkeypatch.setenv(
        "CHATGPT_WORKSPACE_AGENT_TRIGGER_BASE_URL",
        "https://api.chatgpt.com/v1/workspace_agents",
    )


def test_minimal_request_omits_conversation_key(monkeypatch):
    captured = []

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        return FakeResponse(
            headers={"x-request-id": "req_123", "Retry-After": "4"}
        )

    monkeypatch.setattr(diagnostic.urllib.request, "urlopen", fake_urlopen)

    result = diagnostic.run_raw_workspace_trigger(
        input_text=diagnostic.MINIMAL_DIAGNOSTIC_INPUT,
    )

    assert len(captured) == 1
    request, timeout = captured[0]
    assert timeout == 30
    assert request.full_url.endswith(
        "/v1/workspace_agents/agtch_1234567890abcdef/trigger"
    )
    assert json.loads(request.data) == {
        "input": diagnostic.MINIMAL_DIAGNOSTIC_INPUT
    }
    assert result["http_status"] == 202
    assert result["classification"] == "accepted"
    assert result["response_headers"] == {"x-request-id": "req_123"}
    assert result["retry_after"] == "4"
    assert "test-token" not in json.dumps(result)


def test_unique_conversation_key_is_the_only_optional_field(monkeypatch):
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request)
        return FakeResponse()

    monkeypatch.setattr(diagnostic.urllib.request, "urlopen", fake_urlopen)

    diagnostic.run_raw_workspace_trigger(
        input_text=diagnostic.MINIMAL_DIAGNOSTIC_INPUT,
        conversation_key="diagnostic-unique",
    )

    assert len(captured) == 1
    assert json.loads(captured[0].data) == {
        "input": diagnostic.MINIMAL_DIAGNOSTIC_INPUT,
        "conversation_key": "diagnostic-unique",
    }


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, b'{"error":"unauthorized"}', "authentication"),
        (403, b'{"error":"forbidden"}', "permission"),
        (404, b'{"error":"channel not found"}', "api_channel"),
        (409, b'{"error":"channel conflict"}', "api_channel"),
        (
            409,
            b'{"error":"trigger currently unavailable"}',
            "temporary service failure",
        ),
        (429, b'{"error":"quota exceeded"}', "quota/credits"),
        (429, b'{"error":"try again"}', "temporary service failure"),
        (503, b'{"error":"unavailable"}', "temporary service failure"),
        (400, b'{"error":"bad input"}', "invalid endpoint/payload"),
    ],
)
def test_failure_classification(monkeypatch, status, body, expected):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "error",
            {"x-request-id": "req_failure"},
            io.BytesIO(body),
        )

    monkeypatch.setattr(diagnostic.urllib.request, "urlopen", fake_urlopen)

    result = diagnostic.run_raw_workspace_trigger(
        input_text=diagnostic.MINIMAL_DIAGNOSTIC_INPUT
    )

    assert result["classification"] == expected
    assert result["http_status"] == status
    assert result["response_headers"] == {"x-request-id": "req_failure"}


def test_admin_result_contains_only_safe_contract_fields(monkeypatch):
    monkeypatch.setattr(
        diagnostic.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(
            headers={"x-request-id": "req_admin"}
        ),
    )

    result = diagnostic.run_minimal_trigger_diagnostic()

    assert result == {
        "configured": True,
        "minimal_trigger_http_status": 202,
        "request_id": "req_admin",
        "retry_after": None,
        "agent_id_suffix": "90abcdef",
        "endpoint_host": "api.chatgpt.com",
        "classification": "accepted",
        "checked_at": result["checked_at"],
    }
