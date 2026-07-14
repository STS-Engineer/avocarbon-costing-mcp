import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.workspace_agent_client import trigger_workspace_agent


class FakeResponse:
    headers = {"x-request-id": "request-test-123"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getcode(self):
        return 202

    def read(self):
        return b'{"id":"run-test","status":"accepted"}'


def main():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    with patch.dict(os.environ, {
        "CHATGPT_WORKSPACE_AGENT_TRIGGER_BASE_URL": " https://api.chatgpt.com/v1/workspace_agents/ ",
    }), patch("urllib.request.urlopen", fake_urlopen):
        result = trigger_workspace_agent(
            agent_id=" agtch_test123456789/trigger\n",
            access_token=" test-token\n",
            input_text=' {"project_code":"TEST"} ',
            conversation_key=" test-conversation ",
            idempotency_key="test-idempotency",
            dry_run=False,
        )

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "https://api.chatgpt.com/v1/workspace_agents/agtch_test123456789/trigger"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["Idempotency-key"] == "test-idempotency"
    assert set(body) == {"input", "conversation_key"}
    assert body["input"] == '{"project_code":"TEST"}'
    assert body["conversation_key"] == "test-conversation"
    assert result["http_status"] == 202
    assert result["request_correlation_id"] == "request-test-123"
    print("PASS trigger request matches the known working URL, headers and JSON schema")


if __name__ == "__main__":
    main()
