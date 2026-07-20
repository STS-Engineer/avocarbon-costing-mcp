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

import services.choke_sequential_agent_workflow as workflow


def _state(**overrides):
    state = {
        "project_code": "TEST-PROJECT",
        "product_id": "TEST-PRODUCT",
        "customer_input": {"annual_quantity": 600000},
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
    # wp_50_electrical_test is conditional on explicit technical evidence
    # (Phase 6 G) and is not part of the default routing.
    assert work_package_ids == {
        "wp_10_ferrite_handling",
        "wp_20_wire_winding",
        "wp_30_lead_tinning",
        "wp_60_visual_inspection_packaging",
    }
    assert "wp_50_electrical_test" not in work_package_ids
    # Glue is excluded (zero qty / not retained) so no gluing work package is created.
    assert "wp_40_glue_application_baking" not in work_package_ids
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
    assert process["missing_inputs"] == ["normalized BOM has no components"]


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
    assert result["reason"] == "process_decomposition_blocked"
    assert result["blocked_reason"]
    assert result["status"] == "no_most_triggered"
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
    assert triggered_ids == {
        "wp_10_ferrite_handling",
        "wp_20_wire_winding",
        "wp_30_lead_tinning",
        "wp_60_visual_inspection_packaging",
    }
    assert state["status"] == "most_triggered"


def test_trigger_most_skips_already_received_work_packages_idempotently(monkeypatch):
    normalized_bom = workflow.normalize_bom(_full_choke_bom())
    required_ids = [item["component_id"] for item in workflow._required_external_components(normalized_bom)]
    state = _state(
        bom={"status": "received"},
        required_external_component_ids=required_ids,
        components={cid: {"status": "received"} for cid in required_ids},
        most={"wp_10_ferrite_handling": {"status": "received"}},
    )
    _patch_common(monkeypatch, state, normalized_bom)

    trigger_calls = []

    def fake_trigger(*args, **kwargs):
        trigger_calls.append(args)
        return {"status": "dry_run", "http_status": 200, "response": {}}

    monkeypatch.setattr(workflow, "_trigger", fake_trigger)

    result = workflow.trigger_most_operations("TEST-PROJECT", "TEST-PRODUCT", dry_run=True)

    skipped_ids = {item["work_package_id"] for item in result["skipped_work_packages"]}
    assert "wp_10_ferrite_handling" in skipped_ids
    triggered_ids = {item["work_package_id"] for item in result["triggered_work_packages"]}
    assert "wp_10_ferrite_handling" not in triggered_ids
