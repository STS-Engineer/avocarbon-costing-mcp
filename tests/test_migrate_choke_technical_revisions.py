import copy
import json
from pathlib import Path

from scripts import migrate_choke_technical_revisions as migration


def _write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(data_root: Path, project_code: str, product_id: str) -> Path:
    run_dir = data_root / "costing_runs" / project_code / product_id
    component = {
        "component_id": "core",
        "component": "Core",
        "quantity_per_product": 1,
        "quantity_unit": "pc",
        "costing_route": "external_component_costing_agent",
        "component_definition": {
            "quantity_per_product": 1,
            "quantity_unit": "pc",
        },
    }
    work_package = {
        "work_package_id": "wp_winding",
        "operation_key": "winding",
        "operation_name": "Winding",
        "component_ids": ["core"],
        "status": "confirmed",
    }
    _write(
        run_dir / "workflow_state.json",
        {
            "components": {
                "core": {"status": "received"},
                "removed_core": {"status": "received"},
            },
            "most": {
                "wp_winding": {"status": "received"},
                "wp_removed": {"status": "received"},
            },
            "process_decomposition": {
                "status": "ready",
                "required_work_package_ids": ["wp_winding"],
                "work_packages": [work_package],
            },
        },
    )
    _write(
        run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json",
        {"bom": [component]},
    )
    _write(
        run_dir / "bom_normalized.json",
        {
            "components": [component],
            "choke_classification": {"choke_subtype": "fuse_choke"},
        },
    )
    _write(run_dir / "components_normalized" / "core.json", {"component_id": "core"})
    _write(run_dir / "most_normalized" / "wp_winding.json", {"work_package_id": "wp_winding"})
    return run_dir


def test_project_only_filtering(tmp_path):
    selected = _make_run(tmp_path, "PROJECT-A", "PRODUCT-1")
    _make_run(tmp_path, "PROJECT-B", "PRODUCT-2")

    result = migration.run_migration(data_root=tmp_path, project_code="PROJECT-A")

    assert [Path(item["run_dir"]) for item in result["runs"]] == [selected]
    assert result["summary"]["would_migrate"] == 1


def test_product_only_filtering(tmp_path):
    selected_a = _make_run(tmp_path, "PROJECT-A", "SHARED-PRODUCT")
    selected_b = _make_run(tmp_path, "PROJECT-B", "SHARED-PRODUCT")
    _make_run(tmp_path, "PROJECT-C", "OTHER")

    result = migration.run_migration(data_root=tmp_path, product_id="SHARED-PRODUCT")

    assert {Path(item["run_dir"]) for item in result["runs"]} == {
        selected_a,
        selected_b,
    }


def test_combined_filtering(tmp_path):
    selected = _make_run(tmp_path, "PROJECT-A", "PRODUCT-1")
    _make_run(tmp_path, "PROJECT-A", "PRODUCT-2")

    result = migration.run_migration(
        data_root=tmp_path,
        project_code="PROJECT-A",
        product_id="PRODUCT-1",
    )

    assert [Path(item["run_dir"]) for item in result["runs"]] == [selected]


def test_target_not_found_reports_specific_reason(tmp_path):
    result = migration.run_migration(
        data_root=tmp_path,
        project_code="ABSENT",
        product_id="NOT-HERE",
    )

    assert result["status"] == "target_not_found"
    assert result["project_code"] == "ABSENT"
    assert result["product_id"] == "NOT-HERE"
    assert result["searched_data_root"] == str(tmp_path.resolve())
    assert result["summary"]["total_scanned"] == 0
    assert result["runs"][0]["reason"] == "project_directory_missing"


def test_test_run_exclusion_is_opt_in_and_configurable(tmp_path):
    test_run = _make_run(tmp_path, "BOM-RETRY-123", "PRODUCT")
    custom_run = _make_run(tmp_path, "DIAGNOSTIC-123", "PRODUCT")
    production_run = _make_run(tmp_path, "PROJECT-A", "PRODUCT")

    unfiltered = migration.run_migration(data_root=tmp_path)
    excluded = migration.run_migration(
        data_root=tmp_path,
        exclude_test_runs=True,
        test_run_patterns=["DIAGNOSTIC-*"],
    )

    assert unfiltered["summary"]["would_migrate"] == 3
    excluded_by_path = {Path(item["run_dir"]): item for item in excluded["runs"]}
    assert excluded_by_path[test_run]["reason"] == "excluded_test_run"
    assert excluded_by_path[custom_run]["reason"] == "excluded_test_run"
    assert excluded_by_path[production_run]["status"] == "would_migrate"


def test_summary_counts_and_agent_trigger_invariant(tmp_path):
    _make_run(tmp_path, "PROJECT-A", "PRODUCT")

    result = migration.run_migration(data_root=tmp_path)
    summary = result["summary"]

    assert summary == {
        "total_scanned": 1,
        "would_migrate": 1,
        "migrated": 0,
        "skipped": 0,
        "failed": 0,
        "legacy_component_output_count": 1,
        "legacy_most_output_count": 1,
        "obsolete_component_output_count": 1,
        "obsolete_most_output_count": 1,
        "files_created_or_updated": 0,
        "files_deleted": 0,
        "agent_triggers": 0,
    }
    assert all(item["agent_triggers"] == 0 for item in result["runs"])


def test_dry_run_does_not_mutate_files(tmp_path):
    run_dir = _make_run(tmp_path, "PROJECT-A", "PRODUCT")
    before = {
        path.relative_to(run_dir): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }

    result = migration.run_migration(data_root=tmp_path)

    after = {
        path.relative_to(run_dir): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert result["mode"] == "dry_run"
    assert before == after
    assert result["summary"]["files_created_or_updated"] == 0
    assert result["summary"]["files_deleted"] == 0


def test_apply_is_limited_to_filtered_target(tmp_path):
    selected = _make_run(tmp_path, "PROJECT-A", "PRODUCT-1")
    untouched = _make_run(tmp_path, "PROJECT-B", "PRODUCT-2")
    untouched_before = {
        path.relative_to(untouched): copy.deepcopy(path.read_bytes())
        for path in untouched.rglob("*")
        if path.is_file()
    }

    result = migration.run_migration(
        data_root=tmp_path,
        project_code="PROJECT-A",
        product_id="PRODUCT-1",
        apply=True,
    )

    selected_state = json.loads((selected / "workflow_state.json").read_text(encoding="utf-8"))
    untouched_after = {
        path.relative_to(untouched): path.read_bytes()
        for path in untouched.rglob("*")
        if path.is_file()
    }
    assert result["summary"]["migrated"] == 1
    assert result["summary"]["files_created_or_updated"] == 2
    assert "technical_revisions" in selected_state
    assert untouched_before == untouched_after
    assert result["summary"]["agent_triggers"] == 0


def test_missing_current_files_have_distinct_skip_reasons(tmp_path):
    run_dir = tmp_path / "costing_runs" / "PROJECT" / "PRODUCT"
    run_dir.mkdir(parents=True)
    assert migration.migrate_run(run_dir)["reason"] == "workflow_state_missing"
    _write(run_dir / "workflow_state.json", {"process_decomposition": {"status": "ready"}})
    assert migration.migrate_run(run_dir)["reason"] == "raw_bom_missing"
    _write(run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json", {})
    assert migration.migrate_run(run_dir)["reason"] == "normalized_bom_missing"
    _write(run_dir / "bom_normalized.json", {})
    _write(run_dir / "workflow_state.json", {})
    assert migration.migrate_run(run_dir)["reason"] == "process_decomposition_missing"


def test_report_file_is_written_outside_costing_runs(tmp_path):
    _make_run(tmp_path, "PROJECT-A", "PRODUCT")
    report_path = tmp_path / "migration_reports" / "report.json"
    result = migration.run_migration(data_root=tmp_path)

    written = migration._write_report(report_path, result)

    assert written == report_path.resolve()
    assert json.loads(report_path.read_text(encoding="utf-8")) == result
    assert not report_path.is_relative_to(tmp_path / "costing_runs")
