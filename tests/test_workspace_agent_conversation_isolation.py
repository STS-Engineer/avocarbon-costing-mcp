import json

from services import workspace_agent_client as client


class _Response:
    def __init__(self, conversation_id):
        self._conversation_id = conversation_id
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return 202

    def read(self):
        return json.dumps(
            {"conversation_id": self._conversation_id}
        ).encode("utf-8")


def test_two_triggers_create_distinct_fresh_conversations(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        requests.append(
            {
                "body": body,
                "headers": dict(request.headers),
                "timeout": timeout,
            }
        )
        return _Response(f"conversation-{len(requests)}")

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)

    first = client.trigger_workspace_agent(
        "agtch_test",
        "token",
        '{"trigger_run_id":"run-a"}',
        conversation_key="P:X:bom",
        idempotency_key="P:X:bom",
        dry_run=False,
    )
    second = client.trigger_workspace_agent(
        "agtch_test",
        "token",
        '{"trigger_run_id":"run-b"}',
        conversation_key="P:X:bom",
        idempotency_key="P:X:bom",
        dry_run=False,
    )

    assert first["conversation_mode"] == second["conversation_mode"] == "new"
    assert first["invocation_id"] != second["invocation_id"]
    assert first["conversation_key"] != second["conversation_key"]
    assert first["idempotency_key"] != second["idempotency_key"]
    assert requests[0]["body"] is not requests[1]["body"]
    assert requests[0]["body"]["conversation_key"] != requests[1]["body"]["conversation_key"]


def test_fresh_request_contains_no_continuation_identifier(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response("conversation-new")

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)

    result = client.trigger_workspace_agent(
        "agtch_test",
        "token",
        "hello",
        conversation_key="shared-base",
        dry_run=False,
    )

    assert set(captured) == {"input", "conversation_key"}
    assert not client.CONTINUATION_FIELDS.intersection(captured)
    assert result["returned_conversation_id_audit"]["safe_suffix"] == "tion-new"


def test_returned_conversation_id_is_audit_only(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return _Response("conversation-from-prior-response")

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)

    client.trigger_workspace_agent(
        "agtch_test",
        "token",
        "first",
        conversation_key="base",
        dry_run=False,
    )
    client.trigger_workspace_agent(
        "agtch_test",
        "token",
        "second",
        conversation_key="base",
        dry_run=False,
    )

    assert all(
        "conversation-from-prior-response" not in request["conversation_key"]
        for request in requests
    )


def test_dry_run_also_allocates_fresh_invocation_identity():
    first = client.trigger_workspace_agent(
        "agtch_test",
        "",
        "first",
        conversation_key="P:X:most:wp",
        dry_run=True,
    )
    second = client.trigger_workspace_agent(
        "agtch_test",
        "",
        "second",
        conversation_key="P:X:most:wp",
        dry_run=True,
    )

    assert first["conversation_key"] != second["conversation_key"]
    assert first["invocation_id"] != second["invocation_id"]


def test_bom_retry_uses_a_new_conversation():
    initial = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"trigger_run_id":"initial"}',
        conversation_key="P:X:sequential:bom",
        dry_run=True,
    )
    retry = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"trigger_run_id":"retry"}',
        conversation_key="P:X:sequential:bom",
        dry_run=True,
    )

    assert initial["conversation_key"] != retry["conversation_key"]


def test_component_invocations_are_conversation_isolated():
    ferrite = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"component_id":"ferrite_core"}',
        conversation_key="P:X:component:ferrite_core:v1",
        dry_run=True,
    )
    wire = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"component_id":"magnet_wire"}',
        conversation_key="P:X:component:magnet_wire:v1",
        dry_run=True,
    )

    assert ferrite["conversation_key"] != wire["conversation_key"]


def test_most_work_packages_and_retry_are_conversation_isolated():
    winding = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"work_package_id":"wp_winding"}',
        conversation_key="P:X:most:wp_winding:v1",
        dry_run=True,
    )
    soldering = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"work_package_id":"wp_soldering"}',
        conversation_key="P:X:most:wp_soldering:v1",
        dry_run=True,
    )
    winding_retry = client.trigger_workspace_agent(
        "agtch_test",
        "",
        '{"work_package_id":"wp_winding"}',
        conversation_key="P:X:most:wp_winding:v1",
        dry_run=True,
    )

    assert len(
        {
            winding["conversation_key"],
            soldering["conversation_key"],
            winding_retry["conversation_key"],
        }
    ) == 3
