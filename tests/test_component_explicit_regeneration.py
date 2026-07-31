import copy
import json
from pathlib import Path

import app.routers.choke_workflow_router as router
import services.choke_sequential_agent_workflow as workflow


def _component(component_id: str, name: str):
    return {
        "component_id": component_id,
        "component": name,
        "category": "external",
        "external_component_type": name,
        "costing_route": "external_component_costing_agent",
        "quantity_per_product": 1,
        "quantity_unit": "pc/product",
        "component_definition": {"grade": "current"},
        "technical_revision": f"revision-{component_id}",
    }


def _context(tmp_path: Path, monkeypatch):
    components = [
        _component("ferrite_core", "Ferrite"),
        _component("magnet_wire", "Enameled wire"),
        _component("glue", "Glue"),
    ]
    normalized_bom = {
        "source_raw_bom_revision": "raw-current",
        "technical_revision": "bom-current",
        "components": components,
    }
    state = {
        "project_code": "24018-CHO-00",
        "product_id": "300440157",
        "status": "bom_received",
        "bom": {
            "status": "received",
            "trigger_run_id": "bom-current-run",
            "received_for_trigger_run_id": "bom-current-run",
            "drawing_input_snapshot": {"document_revision_hash": "drawing-current"},
        },
        "technical_revisions": {
            "raw_bom": "raw-current",
            "normalized_bom": "bom-current",
            "process_decomposition": "process-current",
        },
        "process_decomposition": {"technical_revision": "process-current"},
        "customer_input": {
            "product": "Rod Choke",
            "annual_quantity": 360000,
            "customer_delivery_zone": "India",
            "currency": "INR",
        },
        "production_plant": "Pune",
        "manufacturing_strategy": {"status": "found"},
        "unit_data": {"status": "found", "plant": "Pune"},
        "components": {
            component["component_id"]: {
                "status": "stale",
                "revision_status": "legacy_unverified",
                "revision_status_reason": "source_revision_metadata_missing",
                "stale_reason": "component_input_revision_changed",
                "trigger_run_id": f"old-{component['component_id']}",
            }
            for component in components
        },
    }
    state["components"]["lead_tinning"] = {
        "status": "obsolete_for_current_revision",
        "trigger_run_id": "old-lead",
    }
    raw_bom_path = tmp_path / "raw_bom.json"
    normalized_bom_path = tmp_path / "bom_normalized.json"
    raw_bom_path.write_text('{"bom":"unchanged"}', encoding="utf-8")
    normalized_bom_path.write_text(
        json.dumps(normalized_bom, sort_keys=True), encoding="utf-8"
    )
    old_raw_paths = {}
    old_normalized_paths = {}
    for component in components:
        component_id = component["component_id"]
        old_raw_paths[component_id] = tmp_path / f"{component_id}-raw.json"
        old_normalized_paths[component_id] = tmp_path / f"{component_id}-normalized.json"
        old_raw_paths[component_id].write_text(
            json.dumps({"component_id": component_id, "legacy": True}), encoding="utf-8"
        )
        old_normalized_paths[component_id].write_text(
            json.dumps({"component_id": component_id, "legacy": True}), encoding="utf-8"
        )

    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, tmp_path / "state.json"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: copy.deepcopy(normalized_bom))
    monkeypatch.setattr(workflow, "_bom_raw_path", lambda *_a, **_k: raw_bom_path)
    monkeypatch.setattr(workflow, "_bom_normalized_path", lambda *_a, **_k: normalized_bom_path)
    monkeypatch.setattr(workflow, "_component_output_path", lambda _p, _x, item: old_raw_paths[item])
    monkeypatch.setattr(
        workflow, "_normalized_component_output_path", lambda _p, _x, item: old_normalized_paths[item]
    )
    monkeypatch.setattr(workflow, "_run_dir", lambda *_a, **_k: tmp_path / "run")
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_refresh_resolved_customer_context", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_refresh_master_data_for_state", lambda *_a, **_k: {})
    monkeypatch.setattr(workflow, "_component_validation_response", lambda *_a, **_k: None)
    reconciliation = {
        "current_bom_revision": "bom-current",
        "current_components": [item["component_id"] for item in components],
        "valid_components": [],
        "missing_components": [],
        "stale_components": [],
        "blocked_components": [],
        "legacy_unverified_components": [
            {
                "component_id": item["component_id"],
                "status": "legacy_unverified",
                "status_reason": "source_revision_metadata_missing",
            }
            for item in components
        ],
        "obsolete_components": [{"component_id": "lead_tinning"}],
    }
    monkeypatch.setattr(
        workflow.technical_revisions,
        "reconcile_component_outputs",
        lambda *_a, **_k: reconciliation,
    )
    return state, normalized_bom, raw_bom_path, normalized_bom_path, old_raw_paths


def test_explicit_regeneration_triggers_current_legacy_components_only(tmp_path, monkeypatch):
    state, normalized_bom, raw_bom_path, normalized_bom_path, old_raw_paths = _context(
        tmp_path, monkeypatch
    )
    bom_before = (raw_bom_path.read_bytes(), normalized_bom_path.read_bytes(), copy.deepcopy(state["bom"]))
    old_outputs_before = {key: path.read_bytes() for key, path in old_raw_paths.items()}
    sent_payloads = []

    def accepted(_env, _name, input_text, *_args, **_kwargs):
        sent_payloads.append(json.loads(input_text))
        return {"status": "accepted", "http_status": 202, "request_correlation_id": "request"}

    monkeypatch.setattr(workflow, "_trigger", accepted)
    result = workflow.trigger_next_component_costing(
        "24018-CHO-00",
        "300440157",
        requested_by="api",
        explicit_regeneration=True,
    )

    assert result["triggered_count"] == 3
    assert result["skipped_count"] == 0
    assert {item["component_id"] for item in sent_payloads} == {
        "ferrite_core", "magnet_wire", "glue"
    }
    assert "lead_tinning" not in {item["component_id"] for item in sent_payloads}
    trigger_ids = {item["trigger_run_id"] for item in sent_payloads}
    assert len(trigger_ids) == 3
    for payload in sent_payloads:
        assert payload["raw_bom_revision"] == "raw-current"
        assert payload["normalized_bom_revision"] == "bom-current"
        assert payload["process_decomposition_revision"] == "process-current"
        assert payload["drawing_document_revision_hash"] == "drawing-current"
        assert payload["upstream_bom_trigger_run_id"] == "bom-current-run"
        assert payload["component_input_revision"] == f"revision-{payload['component_id']}"
        assert "save_component_output" in payload["instruction"]
    assert raw_bom_path.read_bytes() == bom_before[0]
    assert normalized_bom_path.read_bytes() == bom_before[1]
    assert state["bom"] == bom_before[2]
    assert {key: path.read_bytes() for key, path in old_raw_paths.items()} == old_outputs_before
    assert all(
        any(value for value in state["components"][item]["previous_output_archive"].values())
        for item in ("ferrite_core", "magnet_wire", "glue")
    )


def test_automatic_scheduler_still_skips_legacy_unverified(tmp_path, monkeypatch):
    _context(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(workflow, "_trigger", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = workflow.trigger_next_component_costing(
        "24018-CHO-00", "300440157", requested_by="system"
    )

    assert calls == []
    assert result["triggered_count"] == 0
    assert result["skipped_count"] == 3
    assert all(
        item["decision_reason"] == "compatibility_validation_or_explicit_regeneration_required"
        for item in result["items"]
    )


def test_rest_component_route_marks_request_as_explicit_regeneration(monkeypatch):
    captured = {}

    def fake_trigger(**kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(router, "trigger_next_component_costing", fake_trigger)
    response = router.trigger_components(router.TriggerStageRequest(
        project_code="P", product_id="X"
    ))

    assert response["status"] == "ok"
    assert captured["explicit_regeneration"] is True


def test_current_revision_callback_is_accepted_and_wrong_run_is_rejected(tmp_path, monkeypatch):
    state, normalized_bom, *_ = _context(tmp_path, monkeypatch)
    component = normalized_bom["components"][0]
    metadata = workflow._component_source_revision_metadata(state, normalized_bom, component)
    state["components"]["ferrite_core"].update({
        "trigger_run_id": "new-run",
        "source_revision_metadata": metadata,
    })

    wrong = workflow.save_component_output(
        "24018-CHO-00",
        "300440157",
        "ferrite_core",
        {"component_id": "ferrite_core", "classification": "External"},
        trigger_run_id="old-run",
    )
    assert wrong["status"] == "stale_callback"
    assert wrong["error_code"] == "trigger_run_id_mismatch"

    monkeypatch.setattr(workflow, "normalize_component_output", lambda *_a, **_k: {
        "technical_revision": "output-current",
        "source_bom_revision": "bom-current",
        "source_component_revision": "revision-ferrite_core",
        "source_revision_metadata": metadata,
        "pricing_completeness": {"status": "complete", "requires_regeneration": False},
    })
    monkeypatch.setattr(workflow, "_derive_revision_aware_status", lambda value: value)
    accepted = workflow.save_component_output(
        "24018-CHO-00",
        "300440157",
        "ferrite_core",
        {"component_id": "ferrite_core", "classification": "External"},
        trigger_run_id="new-run",
    )
    assert accepted["success"] is True
    assert state["components"]["ferrite_core"]["source_revision_metadata"] == metadata
