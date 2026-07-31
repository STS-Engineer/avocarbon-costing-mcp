from datetime import datetime, timedelta, timezone

import services.choke_sequential_agent_workflow as workflow


def _normalized_bom():
    return workflow.normalize_bom({
        "product_name": "Fuse Choke",
        "components": [{
            "component_id": "ferrite_core",
            "component_name": "Ferrite core",
            "quantity_per_product": 1,
            "quantity_unit": "pc/product",
            "status": "confirmed",
        }],
    })


def _state():
    return {
        "project_code": "REVISION-TEST",
        "product_id": "PRODUCT-1",
        "status": "most_received",
        "bom": {"status": "received"},
        "customer_input": {
            "product": "Fuse Choke",
            "annual_quantity": 100000,
            "customer_delivery_zone": "Europe",
            "currency": "EUR",
        },
        "production_plant": "SAME",
        "unit_data": {"status": "found", "plant": "SAME"},
        "components": {
            "ferrite_core": {
                "status": "received",
                "requires_regeneration": False,
                "trigger_run_id": "old-run",
                "trigger_result": {
                    "status": "accepted",
                    "http_status": 202,
                    "response": {"conversation_url": "https://example.invalid/old"},
                },
            }
        },
    }


def _stale_component_reconciliation(normalized):
    component = normalized["components"][0]
    return {
        "current_bom_revision": normalized.get("technical_revision"),
        "current_components": ["ferrite_core"],
        "valid_components": [],
        "missing_components": [],
        "stale_components": [{
            "component_id": "ferrite_core",
            "status": "stale",
            "status_reason": "component_input_revision_changed",
            "source_component_revision": component.get("technical_revision"),
        }],
        "blocked_components": [],
        "legacy_unverified_components": [],
        "obsolete_components": [],
    }


def _patch_component_context(monkeypatch, state, normalized):
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "state.json"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: normalized)
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_refresh_resolved_customer_context", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_refresh_master_data_for_state", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_component_validation_response", lambda *_a, **_k: None)
    monkeypatch.setattr(workflow, "_read_json", lambda *_a, **_k: {})
    reconciliation = _stale_component_reconciliation(normalized)
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_component_outputs",
        lambda *_a, **_k: reconciliation,
    )


def test_stale_component_creates_new_audited_attempt_and_ignores_legacy_flag(monkeypatch):
    state = _state()
    normalized = _normalized_bom()
    _patch_component_context(monkeypatch, state, normalized)
    sent = []

    def accepted(*args, **kwargs):
        sent.append((args, kwargs))
        return {
            "status": "accepted",
            "http_status": 202,
            "request_correlation_id": "request-1",
            "response": {"conversation_url": "https://example.invalid/new"},
        }

    monkeypatch.setattr(workflow, "_trigger", accepted)
    result = workflow.trigger_next_component_costing(
        "REVISION-TEST", "PRODUCT-1", requested_by="test"
    )

    assert len(sent) == 1, result
    assert result["candidate_count"] == 1
    assert result["triggered_count"] == 1
    item = result["items"][0]
    assert item["revision_status"] == "stale"
    assert item["scheduler_action"] == "triggered"
    assert item["http_request_attempted"] is True
    current = state["components"]["ferrite_core"]
    assert current["latest_trigger_attempt"]["final_outcome"] == "waiting_for_callback"
    assert current["latest_trigger_attempt"]["conversation_url"].endswith("/new")
    assert current["trigger_history"][0]["conversation_url"].endswith("/old")
    assert current["latest_trigger_attempt"]["conversation_url"] != current["trigger_history"][0]["conversation_url"]
    assert state["latest_orchestration_invocation"]["requested_by"] == "test"


def test_failure_before_http_is_never_reported_as_triggered(monkeypatch):
    state = _state()
    normalized = _normalized_bom()
    _patch_component_context(monkeypatch, state, normalized)
    monkeypatch.setattr(workflow, "_trigger", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("not sent")))

    result = workflow.trigger_next_component_costing("REVISION-TEST", "PRODUCT-1")

    assert result["triggered_count"] == 0
    assert result["failed_count"] == 1
    assert result["items"][0]["scheduler_action"] == "failed_before_http"
    assert result["items"][0]["http_request_attempted"] is False
    assert state["components"]["ferrite_core"]["latest_trigger_attempt"]["final_outcome"] == "failed_before_http"


def test_current_status_ignores_historical_received_component_and_most(monkeypatch):
    state = _state()
    normalized = _normalized_bom()
    process = {
        "status": "created",
        "technical_revision": "process-new",
        "source_bom_revision": normalized.get("technical_revision"),
        "required_work_package_ids": ["wp_winding"],
        "operations": [{
            "operation_id": "OP10",
            "operation_name": "Winding",
            "operation_key": "wire_winding",
            "status": "confirmed",
            "component_ids": ["ferrite_core"],
        }],
        "excluded_operations": [],
        "work_packages": [{
            "work_package_id": "wp_winding",
            "operation_id": "OP10",
            "operation_key": "wire_winding",
            "status": "confirmed",
            "component_ids": ["ferrite_core"],
            "technical_revision": "wp-new",
        }],
    }
    state["process_decomposition"] = process
    state["most"] = {"wp_winding": {"status": "received"}}
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: normalized)
    monkeypatch.setattr(workflow, "_read_json", lambda *_a, **_k: {})
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_component_outputs",
        lambda *_a, **_k: _stale_component_reconciliation(normalized),
    )
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_most_outputs",
        lambda *_a, **_k: {
            "valid_work_packages": [],
            "missing_work_packages": [],
            "stale_work_packages": [{
                "work_package_id": "wp_winding",
                "status": "stale",
                "status_reason": "work_package_input_revision_changed",
            }],
            "blocked_work_packages": [],
            "legacy_unverified_work_packages": [],
            "received_not_normalized_work_packages": [],
            "obsolete_work_packages": [],
        },
    )

    workflow._derive_revision_aware_status(state)

    assert state["component_status"] == "stale"
    assert state["most_status"] == "stale"
    assert state["workflow_status"] == "component_costing_required"


def test_callback_timeout_is_exposed_on_latest_attempt(monkeypatch):
    state = _state()
    old_deadline = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    state["components"]["ferrite_core"].update({
        "status": "trigger_request_accepted",
        "latest_trigger_attempt": {
            "trigger_run_id": "current-run",
            "callback_deadline": old_deadline,
            "final_outcome": "waiting_for_callback",
        },
        "trigger_history": [{
            "trigger_run_id": "current-run",
            "callback_deadline": old_deadline,
            "final_outcome": "waiting_for_callback",
        }],
    })

    workflow._apply_agent_callback_timeouts(state)

    attempt = state["components"]["ferrite_core"]["latest_trigger_attempt"]
    assert attempt["callback_status"] == "callback_timeout"
    assert attempt["final_outcome"] == "callback_timeout"
    assert state["components"]["ferrite_core"]["retryable"] is True


def test_stale_most_package_creates_fresh_attempt_for_current_revision(monkeypatch):
    state = _state()
    normalized = _normalized_bom()
    process = {
        "status": "created",
        "technical_revision": "process-current",
        "source_bom_revision": normalized.get("technical_revision"),
        "required_work_package_ids": ["wp_winding"],
        "operations": [{
            "operation_id": "OP10",
            "operation_name": "Winding",
            "operation_key": "wire_winding",
            "status": "confirmed",
            "component_ids": ["ferrite_core"],
        }],
        "excluded_operations": [],
        "work_packages": [{
            "work_package_id": "wp_winding",
            "operation_id": "OP10",
            "operation_name": "Winding",
            "operation_key": "wire_winding",
            "status": "confirmed",
            "component_ids": ["ferrite_core"],
            "technical_revision": "wp-current",
            "annual_quantity": 100000,
            "production_plant": "SAME",
            "technical_inputs": {},
        }],
    }
    state["process_decomposition"] = process
    state["required_external_component_ids"] = ["ferrite_core"]
    state["most"] = {
        "wp_winding": {
            "status": "received",
            "trigger_result": {
                "status": "accepted",
                "response": {"conversation_url": "https://example.invalid/old-most"},
            },
        }
    }
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "state.json"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: normalized)
    monkeypatch.setattr(workflow, "build_most_process_decomposition", lambda *_a, **_k: process)
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_read_json", lambda *_a, **_k: {})
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_component_outputs",
        lambda *_a, **_k: {
            "valid_components": [{"component_id": "ferrite_core", "status": "valid"}],
            "missing_components": [],
            "stale_components": [],
            "blocked_components": [],
            "legacy_unverified_components": [],
            "obsolete_components": [],
        },
    )
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_most_outputs",
        lambda *_a, **_k: {
            "valid_work_packages": [],
            "missing_work_packages": [],
            "stale_work_packages": [{
                "work_package_id": "wp_winding",
                "status": "stale",
                "status_reason": "work_package_input_revision_changed",
            }],
            "blocked_work_packages": [],
            "legacy_unverified_work_packages": [],
            "received_not_normalized_work_packages": [],
            "obsolete_work_packages": [],
        },
    )
    sent = []

    def accepted(*args, **kwargs):
        sent.append((args, kwargs))
        return {
            "status": "accepted",
            "http_status": 202,
            "request_correlation_id": "most-request-1",
            "response": {"conversation_url": "https://example.invalid/new-most"},
        }

    monkeypatch.setattr(workflow, "_trigger", accepted)
    result = workflow.trigger_most_operations(
        "REVISION-TEST", "PRODUCT-1", requested_by="test"
    )

    assert len(sent) == 1, result
    assert result["triggered_count"] == 1
    assert result["items"][0]["revision_status"] == "stale"
    entry = state["most"]["wp_winding"]
    assert entry["latest_trigger_attempt"]["source_work_package_revision"] == state["process_decomposition"]["work_packages"][0]["technical_revision"]
    assert entry["latest_trigger_attempt"]["final_outcome"] == "waiting_for_callback"
    assert entry["latest_trigger_attempt"]["conversation_url"].endswith("/new-most")
    assert entry["trigger_history"][0]["conversation_url"].endswith("/old-most")
