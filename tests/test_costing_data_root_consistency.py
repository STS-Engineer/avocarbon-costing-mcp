from services import agent_writeback_service as writeback
from services import choke_sequential_agent_workflow as workflow
from services import project_data_paths as paths


def test_all_component_workflow_consumers_share_canonical_run_dir(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(paths, "DATA_ROOT_RAW", str(tmp_path))
    expected = (tmp_path / "costing_runs" / "P" / "X").resolve()
    assert paths.resolve_costing_run_dir("P", "X") == expected
    assert paths.get_workflow_run_paths("P", "X")["run_dir"] == expected
    assert workflow._run_dir("P", "X") == expected
    assert writeback._run_dir("P", "X") == expected

    diagnostics = paths.costing_run_storage_diagnostics("P", "X")
    assert diagnostics["configured_data_root"] == str(tmp_path.resolve())
    assert diagnostics["resolved_run_directory"] == str(expected)
    assert diagnostics["final_result_path"] == str(
        (expected / "final_choke_costing_result.json").resolve()
    )


def test_azure_default_data_root_is_persistent_home_data(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "mcp-costing")
    monkeypatch.setenv("HOME", "/home")
    assert paths._default_data_root().as_posix() == (
        "/home/data/avocarbon-costing"
    )
