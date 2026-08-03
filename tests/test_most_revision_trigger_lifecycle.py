import copy
import json

import services.choke_sequential_agent_workflow as workflow
from services import choke_technical_revisions as revisions


WINDING = "wp_10_wire_winding"
GLUE = "wp_20_glue_application"


def _process():
    packages = [
        {
            "work_package_id": WINDING,
            "operation_id": "10",
            "operation_name": "Winding",
            "operation_key": "wire_winding",
            "operation_family": "wire_winding",
            "component_ids": ["magnet_wire", "ferrite_core"],
            "status": "confirmed",
            "technical_revision": "wp-winding-current",
        },
        {
            "work_package_id": GLUE,
            "operation_id": "20",
            "operation_name": "Glue application",
            "operation_key": "glue_application",
            "operation_family": "glue_application",
            "component_ids": ["glue"],
            "status": "confirmed",
            "technical_revision": "wp-glue-current",
        },
    ]
    return {
        "status": "created",
        "technical_revision": "process-current",
        "source_bom_revision": "bom-current",
        "required_work_package_ids": [WINDING, GLUE],
        "work_packages": packages,
        "operations": [
            *packages,
            {
                "operation_key": "curing_baking",
                "operation_name": "Curing / baking",
                "status": "needs_confirmation",
                "component_ids": ["glue"],
            },
        ],
        "excluded_operations": [],
    }


def _state():
    return {
        "project_code": "24018-CHO-00",
        "product_id": "300440157",
        "bom": {"status": "received"},
        "customer_input": {"annual_quantity": 360000, "product": "Rod Choke"},
        "production_plant": "Chennai",
        "unit_data": {"status": "found", "plant": "Chennai"},
        "required_external_component_ids": ["ferrite_core", "magnet_wire", "glue"],
        "components": {
            "ferrite_core": {"status": "received"},
            "magnet_wire": {"status": "received"},
            "glue": {"status": "received"},
        },
        "most": {
            WINDING: {
                "work_package_id": WINDING,
                "status": "received",
                "revision_status": "legacy_unverified",
                "revision_status_reason": "source_revision_metadata_missing",
            },
        },
    }


def _reconciliation():
    return {
        "process_revision": "process-current",
        "required_work_packages": [WINDING, GLUE],
        "valid_work_packages": [],
        "missing_work_packages": [{"work_package_id": GLUE, "status": "missing"}],
        "stale_work_packages": [],
        "blocked_work_packages": [],
        "received_not_normalized_work_packages": [],
        "legacy_unverified_work_packages": [{
            "work_package_id": WINDING,
            "status": "legacy_unverified",
            "status_reason": "source_revision_metadata_missing",
        }],
        "obsolete_work_packages": [],
    }


def _patch_context(monkeypatch, state):
    process = _process()
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "state.json"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: {
        "technical_revision": "bom-current",
        "components": [],
    })
    monkeypatch.setattr(workflow, "build_most_process_decomposition", lambda *_a, **_k: copy.deepcopy(process))
    monkeypatch.setattr(workflow.technical_revisions, "attach_process_revisions", lambda value, *_a, **_k: value)
    monkeypatch.setattr(workflow.technical_revisions, "reconcile_most_outputs", lambda *_a, **_k: _reconciliation())
    monkeypatch.setattr(workflow, "_archive_technical_revision", lambda *_a, **_k: "archive/process.json")
    monkeypatch.setattr(workflow, "_archive_previous_most_output", lambda *_a, **_k: {
        "raw": "archive/winding-raw.json",
        "normalized": "archive/winding-normalized.json",
    })
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_read_json", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "get_most_agent_configuration_health", lambda: {
        "status": "configured",
        "token_present": True,
        "access_token_present": True,
        "agent_id_prefix": "agtch_test",
        "agent_id_suffix": "test",
        "agent_id_configured": True,
        "agent_id_source": "process_environment",
        "access_token_source": "process_environment",
        "agent_id_matches_external_component": False,
    })


def test_explicit_most_trigger_regenerates_legacy_and_classifies_409(monkeypatch):
    state = _state()
    _patch_context(monkeypatch, state)
    calls = []

    def trigger(_env, _name, input_text, *_args, **_kwargs):
        payload = json.loads(input_text)
        calls.append(payload)
        if payload["work_package_id"] == WINDING:
            return {"status": "accepted", "http_status": 202, "request_correlation_id": "req-w"}
        return {
            "status": "failed",
            "http_status": 409,
            "request_correlation_id": "req-g",
            "response": {
                "error": {
                    "type": "conflict_error",
                    "code": "workspace_agent_unavailable",
                    "message": "The workspace agent trigger is not currently available.",
                },
            },
        }

    monkeypatch.setattr(workflow, "_trigger", trigger)
    result = workflow.trigger_most_operations(
        "24018-CHO-00",
        "300440157",
        explicit_regeneration=True,
    )

    assert {item["work_package_id"] for item in calls} == {WINDING, GLUE}
    assert result["candidate_count"] == 2
    assert result["triggered_count"] == 1
    assert result["failed_count"] == 1
    winding = state["most"][WINDING]
    assert winding["regenerated_for_current_revision"] is True
    assert winding["revision_status"] == "current_revision_pending"
    assert winding["legacy_output_archive"]["normalized"]
    glue = state["most"][GLUE]
    assert glue["status"] == "trigger_request_failed"
    assert glue["failure_code"] == "workspace_agent_unavailable"
    assert glue["retryable"] is True
    assert glue["failure_before_agent_execution"] is True
    assert glue["upstream_http_status"] == 409
    assert glue["upstream_request_id"] == "req-g"
    assert glue["upstream_error_type"] == "conflict_error"
    assert glue["upstream_error_code"] == "workspace_agent_unavailable"
    assert glue["upstream_error_message"] == (
        "The workspace agent trigger is not currently available."
    )
    assert glue["source_work_package_revision"] == "wp-glue-current"
    glue_payload = next(
        item for item in calls if item["work_package_id"] == GLUE
    )
    payload_bytes = json.dumps(
        glue_payload, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")
    assert glue["trigger_payload"] == glue_payload
    assert glue["trigger_diagnostic"]["payload_sha256"] == workflow.hashlib.sha256(
        payload_bytes
    ).hexdigest()
    assert glue["trigger_diagnostic"]["payload_size_bytes"] == len(payload_bytes)
    assert all(
        item.get("revision_status") != "obsolete_for_current_revision"
        for item in result["items"]
    )


def test_required_package_is_never_reconciled_as_obsolete():
    process = _process()
    state = {
        "most": {
            WINDING: {
                "work_package_id": WINDING,
                "status": "received",
                "revision_status": "obsolete_for_current_revision",
            },
            "historical_package": {
                "work_package_id": "historical_package",
                "status": "received",
            },
        },
    }
    result = revisions.reconcile_most_outputs(process, state, lambda _item: None)

    obsolete_ids = {
        item["work_package_id"] for item in result["obsolete_work_packages"]
    }
    assert WINDING not in obsolete_ids
    assert "historical_package" in obsolete_ids
    assert result["required_work_packages"] == [WINDING, GLUE]


def test_retry_keeps_package_id_and_uses_a_new_trigger_run_id(monkeypatch):
    state = _state()
    state["process_decomposition"] = _process()
    state["most"][WINDING].update({
        "status": "trigger_request_failed",
        "trigger_run_id": "old-run",
        "failure_code": "workspace_agent_unavailable",
        "retryable": True,
    })
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "state.json"))
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    captured = {}

    def retrigger(**kwargs):
        captured.update(kwargs)
        return {"status": "most_triggered"}

    monkeypatch.setattr(workflow, "trigger_most_operations", retrigger)
    workflow.retry_most_work_package("24018-CHO-00", "300440157", WINDING)

    assert captured["only_work_package_id"] == WINDING
    assert captured["active_trigger_run_id"] != "old-run"
    assert captured["explicit_regeneration"] is True
    assert state["most"][WINDING]["retry_history"][0]["previous_trigger_run_id"] == "old-run"


def test_most_configuration_is_secret_safe(monkeypatch):
    monkeypatch.setenv("CHATGPT_MOST_AGENT_ID", '  "agtch_most_agent_1234"  ')
    monkeypatch.setenv("CHATGPT_EXTERNAL_COMPONENT_AGENT_ID", "agtch_external")
    monkeypatch.setenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN", "secret-token")

    result = workflow.get_most_agent_configuration_health()

    assert result["status"] == "configured"
    assert result["agent_id_prefix"].startswith("agtch_")
    assert result["agent_id_suffix"] == "1234"
    assert result["token_present"] is True
    assert result["agent_id_source"] == "process_environment"
    assert result["access_token_source"] == "process_environment"
    assert "secret-token" not in json.dumps(result)


def test_raw_most_completeness_verifier_is_read_only(monkeypatch, tmp_path):
    raw_path = tmp_path / "raw_most_agent_output.json"
    raw_json = {
        field: [] if field in {
            "previous_operations",
            "components_by_operation",
            "quality_controls",
            "assumptions",
            "validation_questions",
        } else 1
        for field in workflow.MOST_NATIVE_EXPECTED_FIELDS
    }
    raw_json.update({
        "operation_name": "Glue application",
        "description": "Apply glue to the current choke assembly.",
        "tooling_cost_eur": 100,
        "tooling_life_pieces": 100000,
    })
    raw_path.write_text(json.dumps(raw_json, ensure_ascii=False), encoding="utf-8")
    before = raw_path.read_bytes()
    monkeypatch.setattr(workflow, "_most_output_path", lambda *_a, **_k: raw_path)

    result = workflow.verify_most_raw_output_completeness(
        "24018-CHO-00", "300440157", GLUE
    )

    assert result["status"] == "complete"
    assert result["complete"] is True
    assert result["missing_fields"] == []
    assert set(result["tooling_fields_present"]) == {
        "tooling_cost_eur",
        "tooling_life_pieces",
    }
    assert raw_path.read_bytes() == before
