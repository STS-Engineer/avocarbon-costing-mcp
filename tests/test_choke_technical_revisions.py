from copy import deepcopy
from pathlib import Path

from services import choke_technical_revisions as revisions


def component(
    component_id,
    quantity,
    unit,
    route="external_component_costing_agent",
    **definition,
):
    return {
        "component_id": component_id,
        "component": f"Material {component_id}",
        "quantity_per_product": quantity,
        "quantity_unit": unit,
        "costing_route": route,
        "component_definition": {
            "quantity_per_product": quantity,
            "quantity_unit": unit,
            **definition,
        },
    }


def bom(subtype, components, note=None):
    return {
        "components": components,
        "choke_classification": {"choke_subtype": subtype},
        "raw_bom": {"note": note} if note else {},
    }


def process(source_bom_revision, packages, normalized_bom=None):
    payload = {
        "required_work_package_ids": [
            item["work_package_id"]
            for item in packages
            if item.get("status") != "blocked"
        ],
        "work_packages": packages,
    }
    return revisions.attach_process_revisions(
        payload, source_bom_revision, normalized_bom
    )


def package(identifier, operation, component_ids, status="confirmed"):
    return {
        "work_package_id": identifier,
        "operation_key": operation,
        "operation_name": operation.replace("_", " ").title(),
        "component_ids": component_ids,
        "status": status,
    }


def revised_bom(subtype, components, note=None):
    raw = {"bom": deepcopy(components), "note": note}
    return revisions.attach_bom_revisions(raw, bom(subtype, components, note))


def test_revision_is_deterministic_and_ignores_unrelated_raw_note():
    original = revised_bom(
        "rod_choke",
        [component("material_a", 1, "pc"), component("material_b", 0.2, "m")],
        "first note",
    )
    changed_note = revised_bom(
        "rod_choke",
        [component("material_a", 1, "pc"), component("material_b", 0.2, "m")],
        "different note",
    )
    assert original["technical_revision"] == changed_note["technical_revision"]
    assert (
        original["source_raw_bom_revision"]
        != changed_note["source_raw_bom_revision"]
    )


def test_current_process_is_only_required_work_package_authority():
    current_bom = revised_bom("fuse_choke", [component("material_a", 1, "pc")])
    current_process = process(
        current_bom["technical_revision"],
        [
            package("operation_a", "winding", ["material_a"]),
            package("operation_b", "soldering", ["material_a"]),
            package("operation_c", "optional_test", [], status="blocked"),
        ],
    )
    assert revisions.required_work_package_ids(current_process) == [
        "operation_a",
        "operation_b",
    ]


def test_removed_operation_is_obsolete_and_new_operation_is_missing():
    current_bom = revised_bom("fuse_choke", [component("material_a", 1, "pc")])
    old_process = process(
        current_bom["technical_revision"],
        [package("operation_old", "adhesive", ["material_a"])],
    )
    current_process = process(
        current_bom["technical_revision"],
        [package("operation_new", "soldering", ["material_a"])],
    )
    state = {
        "most": {
            "operation_old": {
                "status": "received",
                "component_ids": ["material_a"],
            }
        }
    }
    result = revisions.reconcile_most_outputs(
        current_process, state, lambda _: None
    )
    assert [item["work_package_id"] for item in result["missing_work_packages"]] == [
        "operation_new"
    ]
    assert [item["work_package_id"] for item in result["obsolete_work_packages"]] == [
        "operation_old"
    ]
    transition = revisions.revision_transition(
        current_bom, current_bom, old_process, current_process
    )
    assert transition["work_packages"]["removed"] == ["operation_old"]
    assert transition["work_packages"]["added"] == ["operation_new"]


def test_component_quantity_change_invalidates_only_dependent_component():
    previous = revised_bom(
        "rod_choke",
        [component("material_a", 1, "pc"), component("material_b", 2, "g")],
    )
    current = revised_bom(
        "rod_choke",
        [component("material_a", 1, "pc"), component("material_b", 3, "g")],
    )
    previous_process = process(
        previous["technical_revision"],
        [
            package("operation_a", "assembly", ["material_a"]),
            package("operation_b", "application", ["material_b"]),
        ],
        previous,
    )
    current_process = process(
        current["technical_revision"],
        [
            package("operation_a", "assembly", ["material_a"]),
            package("operation_b", "application", ["material_b"]),
        ],
        current,
    )
    transition = revisions.revision_transition(
        previous, current, previous_process, current_process
    )
    assert transition["components"]["changed"] == ["material_b"]
    assert transition["components"]["unchanged"] == ["material_a"]
    assert transition["work_packages"]["changed"] == ["operation_b"]
    assert transition["work_packages"]["unchanged"] == ["operation_a"]


def test_piece_mass_length_and_volume_quantities_convert_explicitly():
    cases = [
        (component("part", 2, "pc"), "pc", 2),
        (component("powder", 5, "g"), "kg", 0.005),
        (component("strand", 250, "mm"), "m", 0.25),
        (component("liquid", 3, "ml"), "l", 0.003),
        (component("paste", 4000, "mm3"), "cm3", 4),
    ]
    for source, pricing_unit, expected in cases:
        result = revisions.resolve_technical_quantity(source, pricing_unit)
        assert result["resolution_status"] == "confirmed"
        assert result["quantity"] == expected


def test_incompatible_quantity_blocks_in_firm_mode():
    result = revisions.resolve_technical_quantity(
        component("material", 2, "m"), "kg", mode="firm"
    )
    assert result["resolution_status"] == "blocked"
    assert result["quantity"] is None
    assert result["confirmation_questions"]


def test_configurable_geometry_conversion_has_no_component_name_dependency():
    source = component(
        "arbitrary_material",
        1.5,
        "m",
        diameter_mm=1.0,
        density_g_cm3=8.0,
    )
    result = revisions.resolve_technical_quantity(source, "kg")
    assert result["resolution_status"] == "confirmed"
    assert result["conversion"]["method"] == (
        "cylindrical_length_diameter_density_to_mass"
    )
    assert result["quantity"] > 0


def test_preliminary_assumption_is_traceable_and_firm_rejects_it():
    source = component("consumable", None, None)
    rule = {
        "approved": True,
        "value": 0.4,
        "unit": "g",
        "source": "approved engineering rule R-12",
        "formula": "validated application consumption",
        "confirmation_questions": ["Confirm production trial consumption."],
    }
    preliminary = revisions.resolve_technical_quantity(
        source, "kg", mode="preliminary", approved_assumption_rules=[rule]
    )
    firm = revisions.resolve_technical_quantity(
        source, "kg", mode="firm", approved_assumption_rules=[rule]
    )
    assert preliminary["resolution_status"] == "estimated"
    assert preliminary["quantity"] == 0.0004
    assert preliminary["assumptions"] == [rule]
    assert firm["resolution_status"] == "blocked"


def test_component_reconciliation_valid_stale_legacy_and_obsolete():
    current = revised_bom(
        "toroid_choke",
        [
            component("material_a", 1, "pc"),
            component("material_b", 0.1, "kg"),
            component("internal_part", 1, "pc", route="internal_manufacturing"),
        ],
    )
    component_map = {
        item["component_id"]: item for item in current["components"]
    }
    outputs = {
        "material_a": {
            "source_bom_revision": current["technical_revision"],
            "source_component_revision": component_map["material_a"][
                "technical_revision"
            ],
            "technical_revision": "output-a",
        },
        "material_b": {
            "source_bom_revision": "old-revision",
            "source_component_revision": "old-input",
        },
    }
    state = {
        "components": {
            "material_a": {"status": "received"},
            "material_b": {"status": "received"},
            "removed_material": {"status": "received"},
        }
    }
    result = revisions.reconcile_component_outputs(
        current, state, outputs.get
    )
    assert [item["component_id"] for item in result["valid_components"]] == [
        "material_a"
    ]
    assert [item["component_id"] for item in result["stale_components"]] == [
        "material_b"
    ]
    assert [item["component_id"] for item in result["obsolete_components"]] == [
        "removed_material"
    ]
    assert any(
        item["component_id"] == "internal_part"
        for item in result["blocked_components"]
    )


def test_legacy_output_is_unverified_unless_snapshot_matches():
    current = revised_bom("rod_choke", [component("material_a", 1, "pc")])
    current_component = current["components"][0]
    state = {"components": {"material_a": {"status": "received"}}}
    legacy = {"component_id": "material_a"}
    unverified = revisions.reconcile_component_outputs(
        current, state, lambda _: legacy
    )
    assert unverified["legacy_unverified_components"]
    verified = revisions.reconcile_component_outputs(
        current,
        state,
        lambda _: {
            **legacy,
            "source_component_snapshot": revisions.component_input_projection(
                current_component
            ),
        },
    )
    assert verified["valid_components"][0][
        "legacy_compatibility_verified"
    ] is True


def test_most_reconciliation_never_accepts_wrong_revision():
    current_bom = revised_bom("toroid_choke", [component("material_a", 1, "pc")])
    current_process = process(
        current_bom["technical_revision"],
        [
            package("operation_a", "toroidal_winding", ["material_a"]),
            package("operation_b", "electrical_testing", ["material_a"]),
        ],
    )
    packages = {
        item["work_package_id"]: item
        for item in current_process["work_packages"]
    }
    outputs = {
        "operation_a": {
            "source_process_revision": current_process["technical_revision"],
            "source_work_package_revision": packages["operation_a"][
                "technical_revision"
            ],
        },
        "operation_b": {
            "source_process_revision": "another-process",
            "source_work_package_revision": "different-work-package-input",
        },
    }
    result = revisions.reconcile_most_outputs(
        current_process, {"most": {}}, outputs.get
    )
    assert [item["work_package_id"] for item in result["valid_work_packages"]] == [
        "operation_a"
    ]
    assert [item["work_package_id"] for item in result["stale_work_packages"]] == [
        "operation_b"
    ]


def test_component_scheduler_only_automates_missing_and_stale():
    reconciliation = {
        "valid_components": [{"component_id": "valid"}],
        "missing_components": [{"component_id": "missing"}],
        "stale_components": [{"component_id": "stale"}],
        "legacy_unverified_components": [{"component_id": "legacy"}],
        "obsolete_components": [{"component_id": "obsolete"}],
        "blocked_components": [{"component_id": "blocked"}],
    }

    policy = revisions.component_scheduler_eligibility(reconciliation)

    assert policy == {
        "reuse_ids": ["valid"],
        "automatic_trigger_ids": ["missing", "stale"],
        "explicit_validation_or_regeneration_ids": ["legacy"],
        "never_trigger_ids": ["obsolete", "blocked"],
    }


def test_most_scheduler_only_automates_missing_and_stale():
    reconciliation = {
        "valid_work_packages": [{"work_package_id": "valid"}],
        "missing_work_packages": [{"work_package_id": "missing"}],
        "stale_work_packages": [{"work_package_id": "stale"}],
        "legacy_unverified_work_packages": [{"work_package_id": "legacy"}],
        "received_not_normalized_work_packages": [
            {"work_package_id": "not_normalized"}
        ],
        "obsolete_work_packages": [{"work_package_id": "obsolete"}],
        "blocked_work_packages": [{"work_package_id": "blocked"}],
    }

    policy = revisions.most_scheduler_eligibility(reconciliation)

    assert policy == {
        "reuse_ids": ["valid"],
        "automatic_trigger_ids": ["missing", "stale"],
        "explicit_validation_or_regeneration_ids": [
            "legacy",
            "not_normalized",
        ],
        "never_trigger_ids": ["obsolete", "blocked"],
    }


def test_migration_and_application_startup_never_trigger_revision_outputs():
    root = Path(revisions.__file__).resolve().parents[1]
    migration_source = (
        root / "scripts" / "migrate_choke_technical_revisions.py"
    ).read_text(encoding="utf-8")
    startup_source = "\n".join(
        (root / relative_path).read_text(encoding="utf-8")
        for relative_path in ("app/main.py", "server.py")
    )

    forbidden_calls = (
        "trigger_next_component_costing(",
        "trigger_most_operations(",
        "_trigger(",
        "trigger_workspace_agent(",
    )
    assert not [call for call in forbidden_calls if call in migration_source]
    assert "migrate_choke_technical_revisions" not in startup_source
    assert not [
        call
        for call in (
            "trigger_next_component_costing(",
            "trigger_most_operations(",
        )
        if call in startup_source
    ]


def test_revision_module_contains_no_example_identifiers():
    source = Path(revisions.__file__).read_text(encoding="utf-8")
    forbidden = (
        "24018-CHO-00",
        "300440157",
        "ferrite_core",
        "magnet_wire",
        "lead_tinning",
        "wp_10_wire_winding",
        "wp_20_glue_application",
        "wp_20_soldering_tinning",
    )
    assert not [item for item in forbidden if item in source]
