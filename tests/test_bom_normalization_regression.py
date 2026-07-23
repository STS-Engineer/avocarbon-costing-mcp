"""Regression tests for the Choke sequential workflow BOM -> MOST decomposition
pipeline.

These lock down the fix for a regression where a BOM Agent output shape using
bare numeric row ids (e.g. "id": 1) instead of semantic component ids caused:
  - component_name to be a numeric row index instead of a descriptive name
  - a zero-quantity, not-retained glue line to be misclassified as "ferrite"
    and wrongly required for external costing
  - process decomposition to find no ferrite/wire/tin/glue components at all
    (since the components dict was keyed by "1"/"4" instead of canonical ids)
  - trigger-most to silently no-op with an ambiguous HTTP 200
"""

from pathlib import Path

import services.choke_sequential_agent_workflow as workflow


def _state(**overrides):
    state = {
        "project_code": "TEST-PROJECT",
        "product_id": "TEST-PRODUCT",
        "customer_input": {"annual_quantity": 600000, "product": "Fuse Choke"},
        "production_plant": "Kunshan",
        "unit_data": {"status": "found", "plant": "Kunshan"},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# A. Component normalization: descriptive name, not a numeric row id
# ---------------------------------------------------------------------------

def test_component_name_prefers_descriptive_field_over_numeric_row_id():
    raw_bom = {
        "components": [
            {
                "id": 1,
                "component": 1,
                "product_designation": "Noyau ferrite cylindrique",
                "component_family": "ferrite",
                "quantity_per_product": 1,
            },
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    component = normalized["components"][0]

    assert component["component"] == "Noyau ferrite cylindrique"
    assert component["component"] != "1"
    assert component["component_id"] == "ferrite_core"


def test_component_id_falls_back_to_component_id_when_no_descriptive_text():
    raw_bom = {"components": [{"id": 7, "quantity_per_product": 1}]}
    normalized = workflow.normalize_bom(raw_bom)
    component = normalized["components"][0]
    # No descriptive field at all: falls back to the (slugged) explicit id.
    assert component["component"] == "7"


# ---------------------------------------------------------------------------
# B. Required external component filtering: exclude zero-qty / not-retained
# ---------------------------------------------------------------------------

def test_glue_zero_quantity_not_retained_is_excluded_from_required_components():
    raw_bom = {
        "components": [
            {
                "id": 4,
                "component": 4,
                "product_designation": "Colle de fixation ferrite",
                "component_family": "ferrite",
                "quantity": 0,
                "specification": "Non retenue selon plan analyse",
            },
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    component = normalized["components"][0]

    assert component["component_id"] == "glue"
    assert component["excluded_not_required"] is True
    assert component["costing_route"] == "not_external_agent"

    required = workflow._required_external_components(normalized)
    assert required == []


def test_glue_component_family_is_not_ferrite():
    raw_bom = {
        "components": [
            {
                "id": 4,
                "product_designation": "Colle de fixation ferrite",
                "component_family": "ferrite",
                "quantity": 1,
            },
        ]
    }
    normalized = workflow.normalize_bom(raw_bom)
    component = normalized["components"][0]
    assert component["component_id"] == "glue"
    assert component["category"] == "glue"
    assert component["category"] != "ferrite"


# ---------------------------------------------------------------------------
# C. Preserve all valid non-zero BOM components across field-name variations
# ---------------------------------------------------------------------------

def _full_choke_bom():
    return {
        "components": [
            {
                "id": 1,
                "component": 1,
                "product_designation": "Noyau ferrite cylindrique",
                "component_family": "ferrite",
                "quantity_per_product": 1,
            },
            {
                "id": 2,
                "designation": "Fil cuivre emaille (magnet wire)",
                "quantity_per_product": 1,
                "turns": 13,
            },
            {
                "id": 3,
                "description": "Etamage plomb des broches",
                "quantity_per_product": 1,
            },
            {
                "id": 4,
                "component": 4,
                "product_designation": "Colle de fixation ferrite",
                "component_family": "ferrite",
                "quantity": 0,
                "specification": "Non retenue selon plan analyse",
            },
        ]
    }


def test_wire_and_tin_field_name_variants_survive_normalization():
    normalized = workflow.normalize_bom(_full_choke_bom())
    component_ids = {item["component_id"] for item in normalized["components"]}
    assert component_ids == {"ferrite_core", "magnet_wire", "lead_tinning", "glue"}

    required_ids = {item["component_id"] for item in workflow._required_external_components(normalized)}
    # Glue is present in the BOM (for traceability) but excluded from costing.
    assert required_ids == {"ferrite_core", "magnet_wire", "lead_tinning"}


# ---------------------------------------------------------------------------
# E. Process decomposition robustness
# ---------------------------------------------------------------------------

def test_process_decomposition_generates_work_packages_for_valid_bom():
    normalized = workflow.normalize_bom(_full_choke_bom())
    process = workflow.build_most_process_decomposition(_state(), normalized)

    assert process["status"] == "created"
    work_package_ids = {item["work_package_id"] for item in process["work_packages"]}
    # Winding and the explicitly described lead-tinning operation are
    # confirmed. Core assembly still needs confirmation; packaging is not invented.
    assert work_package_ids == {"wp_10_wire_winding", "wp_30_soldering_tinning"}
    assert "electrical_test" not in {item["operation_key"] for item in process["operations"]}
    assert "glue_application" not in {item["operation_key"] for item in process["operations"]}
    assert process["blocked_reason"] is None
    assert process["missing_inputs"] == []


def test_process_decomposition_blocked_reports_reason_and_missing_inputs():
    raw_bom = {"components": [{"id": 99, "description": "Unrelated hardware clip", "quantity_per_product": 1}]}
    normalized = workflow.normalize_bom(raw_bom)
    process = workflow.build_most_process_decomposition(_state(), normalized)

    assert process["status"] == "blocked"
    assert process["work_packages"] == []
    assert process["blocked_reason"]
    assert process["missing_inputs"]


def test_process_decomposition_blocked_when_bom_totally_empty():
    normalized = workflow.normalize_bom({"components": []})
    process = workflow.build_most_process_decomposition(_state(), normalized)

    assert process["status"] == "blocked"
    assert process["missing_inputs"] == ["confirmed_process_operations"]


# ---------------------------------------------------------------------------
# D. Product identity: assembly product must not be overwritten by a
#    component's own descriptive field.
# ---------------------------------------------------------------------------

def test_product_name_is_not_derived_from_a_bom_line_item():
    raw_bom = {
        # No assembly-level product/product_name/summary field anywhere -
        # only a per-component "product_designation".
        "components": [
            {
                "id": 1,
                "product_designation": "Noyau ferrite cylindrique",
                "quantity_per_product": 1,
            },
        ]
    }
    extracted = workflow.extract_bom_technical_fields(raw_bom)
    assert extracted["product_name"] is None


def test_product_name_still_resolved_from_assembly_level_fields():
    raw_bom = {
        "summary": {"product_name": "Fuse choke"},
        "components": [
            {
                "id": 1,
                "product_designation": "Noyau ferrite cylindrique",
                "quantity_per_product": 1,
            },
        ],
    }
    extracted = workflow.extract_bom_technical_fields(raw_bom)
    assert extracted["product_name"] == "Fuse choke"


def test_customer_input_product_is_not_downgraded_when_bom_has_no_product_evidence():
    state = _state(customer_input={"annual_quantity": 600000, "product": "Fuse choke"})
    raw_bom = {
        "components": [
            {
                "id": 1,
                "product_designation": "Noyau ferrite cylindrique",
                "quantity_per_product": 1,
            },
        ]
    }
    extracted = workflow.extract_bom_technical_fields(raw_bom)
    update = workflow._update_customer_input_from_bom(state, extracted)
    assert update["updates"].get("product") is None
    assert update["updates"].get("product_name") is None
    assert state["customer_input"]["product"] == "Fuse choke"


# ---------------------------------------------------------------------------
# F. trigger-most: explicit response when decomposition is blocked, and when
#    work packages exist (triggering + idempotent skip of already-received).
# ---------------------------------------------------------------------------

def _patch_common(monkeypatch, state, normalized_bom):
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "FAKE_PATH"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: normalized_bom)
    monkeypatch.setattr(workflow, "_save_state", lambda s: s)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})


def test_trigger_most_returns_explicit_reason_when_decomposition_blocked(monkeypatch):
    normalized_bom = workflow.normalize_bom({"components": []})
    state = _state(
        bom={"status": "received"},
        required_external_component_ids=[],
        components={},
    )
    _patch_common(monkeypatch, state, normalized_bom)

    result = workflow.trigger_most_operations("TEST-PROJECT", "TEST-PRODUCT", dry_run=True)

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["reason"] == "no_confirmed_operations"
    assert result["blocked_reason"]
    assert result["status"] == "most_blocked"
    assert state["status"] == "most_blocked"
    assert state["most"]["status"] == "most_blocked"
    assert result["triggered_work_packages"] == []


def test_trigger_most_triggers_pending_packages_and_moves_state(monkeypatch):
    normalized_bom = workflow.normalize_bom(_full_choke_bom())
    required_ids = [item["component_id"] for item in workflow._required_external_components(normalized_bom)]
    state = _state(
        bom={"status": "received"},
        required_external_component_ids=required_ids,
        components={cid: {"status": "received"} for cid in required_ids},
    )
    _patch_common(monkeypatch, state, normalized_bom)
    monkeypatch.setattr(
        workflow,
        "_trigger",
        lambda *_a, **_k: {"status": "dry_run", "http_status": 200, "response": {}},
    )

    result = workflow.trigger_most_operations("TEST-PROJECT", "TEST-PRODUCT", dry_run=True)

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["reason"] is None
    assert result["status"] == "most_triggered"
    triggered_ids = {item["work_package_id"] for item in result["triggered_work_packages"]}
    assert triggered_ids == {"wp_10_wire_winding", "wp_30_soldering_tinning"}
    assert state["status"] == "most_triggered"
    assert state["most"]["lifecycle_status"] == "awaiting_most_callback"
    assert result["most"]["trigger_result"]["status"] == "dry_run"
    assert result["most"]["trigger_result"]["http_status"] == 200


def test_trigger_most_skips_already_received_work_packages_idempotently(monkeypatch):
    normalized_bom = workflow.normalize_bom(_full_choke_bom())
    required_ids = [item["component_id"] for item in workflow._required_external_components(normalized_bom)]
    state = _state(
        bom={"status": "received"},
        required_external_component_ids=required_ids,
        components={cid: {"status": "received"} for cid in required_ids},
        most={"wp_10_wire_winding": {"status": "received"}},
    )
    _patch_common(monkeypatch, state, normalized_bom)

    trigger_calls = []

    def fake_trigger(*args, **kwargs):
        trigger_calls.append(args)
        return {"status": "dry_run", "http_status": 200, "response": {}}

    monkeypatch.setattr(workflow, "_trigger", fake_trigger)

    result = workflow.trigger_most_operations("TEST-PROJECT", "TEST-PRODUCT", dry_run=True)

    skipped_ids = {item["work_package_id"] for item in result["skipped_work_packages"]}
    assert "wp_10_wire_winding" in skipped_ids


def test_rod_choke_unit_bearing_quantities_create_only_evidence_based_winding():
    normalized_bom = workflow.normalize_bom({
        "bill_of_material": [
            {
                "component_id": "ferrite_core",
                "poste": "Ferrite",
                "produit_designation": "Ferrite core rod",
                "quantite": "2 pcs",
            },
            {
                "component_id": "magnet_wire",
                "poste": "Fil",
                "produit_designation": "Copper wire AIEW",
                "quantite": "1 bobinage / piece",
            },
            {
                "component_id": "lead_tinning",
                "poste": "Etamage",
                "produit_designation": "Tin coating on copper wire",
                "quantite": "2 zones potentielles",
                "status": "to_confirm",
            },
        ],
    })
    state = _state(
        customer_input={"product": "Rod Choke", "annual_quantity": 360000},
        choke_classification={
            "choke_family": "choke",
            "choke_subtype": "rod_choke",
            "raw_detected_product_name": "Rod Choke",
        },
    )

    process = workflow.build_most_process_decomposition(state, normalized_bom)

    assert process["status"] == "created"
    assert [item["operation_key"] for item in process["work_packages"]] == ["wire_winding"]
    assert process["work_packages"][0]["component_ids"] == [
        "magnet_wire",
        "ferrite_core",
    ]
    assert process["exact_historical_profile_match"] is None
    assert all(item["component_type"] == "finished_choke_operation" for item in process["work_packages"])


def test_most_request_is_persisted_before_workspace_http_call(monkeypatch):
    normalized_bom = workflow.normalize_bom(_full_choke_bom())
    required_ids = [
        item["component_id"]
        for item in workflow._required_external_components(normalized_bom)
    ]
    state = _state(
        product_id=300440157,
        bom={"status": "received"},
        required_external_component_ids=required_ids,
        components={cid: {"status": "received"} for cid in required_ids},
    )
    persisted = []
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "FAKE_PATH"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: normalized_bom)
    monkeypatch.setattr(
        workflow,
        "_save_state",
        lambda value: persisted.append({
            "status": value["status"],
            "most_statuses": {
                key: item.get("status")
                for key, item in value["most"].items()
                if isinstance(item, dict) and item.get("work_package_id")
            },
        }) or value,
    )
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})

    def accepted_trigger(*_args, **_kwargs):
        assert persisted
        assert "trigger_request_sending" in persisted[-1]["most_statuses"].values()
        return {"status": "accepted", "http_status": 202}

    monkeypatch.setattr(workflow, "_trigger", accepted_trigger)

    result = workflow.trigger_most_operations(
        "TEST-PROJECT", "300440157", dry_run=False
    )

    assert result["status"] == "most_triggered"
    assert result["lifecycle_status"] == "awaiting_most_callback"
    assert result["state"]["product_id"] == "300440157"
    assert all(
        isinstance(item["trigger_run_id"], str)
        for item in result["triggered_work_packages"]
    )


def test_duplicate_most_trigger_is_blocked_while_awaiting_callback(monkeypatch):
    normalized_bom = workflow.normalize_bom(_full_choke_bom())
    process = workflow.build_most_process_decomposition(_state(), normalized_bom)
    required_ids = [
        item["component_id"]
        for item in workflow._required_external_components(normalized_bom)
    ]
    most = {
        item["work_package_id"]: {
            **item,
            "status": "trigger_request_accepted",
            "lifecycle_status": "awaiting_most_callback",
        }
        for item in process["work_packages"]
    }
    state = _state(
        status="most_triggered",
        bom={"status": "received"},
        required_external_component_ids=required_ids,
        components={cid: {"status": "received"} for cid in required_ids},
        most=most,
    )
    _patch_common(monkeypatch, state, normalized_bom)
    monkeypatch.setattr(
        workflow,
        "_trigger",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate trigger")),
    )

    result = workflow.trigger_most_operations(
        "TEST-PROJECT", "TEST-PRODUCT", dry_run=False
    )

    assert result["status"] == "most_triggered"
    assert result["reason"] == "already_triggered"
    assert result["triggered_work_packages"] == []


def test_most_callback_requires_current_trigger_run_id(monkeypatch):
    work_package = {
        "work_package_id": "wp_10_wire_winding",
        "status": "confirmed",
        "component_ids": ["ferrite_core", "magnet_wire"],
    }
    state = _state(
        bom={"status": "received"},
        process_decomposition={"work_packages": [work_package]},
        most={
            "wp_10_wire_winding": {
                **work_package,
                "trigger_run_id": "most-run-current",
                "status": "trigger_request_accepted",
            },
        },
    )
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "FAKE_PATH"))
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})

    missing = workflow.save_most_output(
        "TEST-PROJECT", "TEST-PRODUCT", "wp_10_wire_winding", {}
    )
    stale = workflow.save_most_output(
        "TEST-PROJECT",
        "TEST-PRODUCT",
        "wp_10_wire_winding",
        {},
        trigger_run_id="most-run-old",
    )

    assert missing["error_code"] == "missing_trigger_run_id"
    assert stale["error_code"] == "trigger_run_id_mismatch"
    assert state["most"]["wp_10_wire_winding"]["stale_callbacks"]


def test_valid_most_callback_completes_current_trigger_run(monkeypatch):
    work_package = {
        "work_package_id": "wp_10_wire_winding",
        "status": "confirmed",
        "component_ids": ["ferrite_core", "magnet_wire"],
    }
    state = _state(
        status="most_triggered",
        bom={"status": "received"},
        process_decomposition={
            "work_packages": [work_package],
            "required_work_package_ids": ["wp_10_wire_winding"],
        },
        required_most_work_package_ids=["wp_10_wire_winding"],
        components={
            "ferrite_core": {"status": "received"},
            "magnet_wire": {"status": "received"},
        },
        most={
            "wp_10_wire_winding": {
                **work_package,
                "trigger_run_id": "most-run-current",
                "status": "trigger_request_accepted",
            },
        },
    )
    raw_path = Path("data/test_runs/most_callback_test/raw.json")
    normalized_path = Path("data/test_runs/most_callback_test/normalized.json")
    writes = {}
    monkeypatch.setattr(workflow, "_existing_state", lambda *_a, **_k: (state, "FAKE_PATH"))
    monkeypatch.setattr(workflow, "_load_normalized_bom", lambda *_a, **_k: {"components": []})
    monkeypatch.setattr(workflow, "_most_output_path", lambda *_a, **_k: raw_path)
    monkeypatch.setattr(
        workflow, "_normalized_most_output_path", lambda *_a, **_k: normalized_path
    )
    monkeypatch.setattr(
        workflow, "_write_json", lambda path, value: writes.__setitem__(str(path), value)
    )
    monkeypatch.setattr(workflow, "_save_state", lambda value: value)
    monkeypatch.setattr(workflow, "append_workflow_event", lambda *_a, **_k: {})

    result = workflow.save_most_output(
        "TEST-PROJECT",
        "TEST-PRODUCT",
        "wp_10_wire_winding",
        {
            "work_package_id": "wp_10_wire_winding",
            "operation_name": "Winding",
            "cycle_time_seconds": 12,
        },
        trigger_run_id="most-run-current",
    )

    assert result["success"] is True
    assert result["state_status_after"] == "most_received"
    assert state["most"]["wp_10_wire_winding"]["received_for_trigger_run_id"] == (
        "most-run-current"
    )
    assert str(raw_path) in writes
    assert str(normalized_path) in writes
