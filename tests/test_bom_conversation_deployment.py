import inspect

from app import main as app_main
from services import choke_sequential_agent_workflow as workflow
from services.choke_writeback_mcp_diagnostic import (
    get_bom_agent_capability_diagnostic,
)


def test_authoritative_bom_key_builders_include_trigger_run_id():
    expected = "P:X:sequential:bom:run-current"

    assert workflow._bom_conversation_key("P", "X", "run-current") == expected
    assert workflow._bom_idempotency_key("P", "X", "run-current") == expected


def test_production_workflow_has_no_fixed_bom_conversation_fallback():
    source = inspect.getsource(workflow)

    assert 'f"{project_code}:{product_id}:sequential:bom"' not in source
    assert source.count(
        'f"{project_code}:{product_id}:sequential:bom:{trigger_run_id}"'
    ) == 1


def test_safe_conversation_audit_proves_trigger_suffix():
    audit = workflow._conversation_key_audit(
        "P:X:sequential:bom:run-current",
        "run-current",
    )

    assert audit["conversation_key_suffix"] == "run-current"
    assert audit["expected_conversation_key_suffix"] == "run-current"
    assert audit["conversation_key_matches_trigger_run_id"] is True
    assert len(audit["conversation_key_hash"]) == 12


def test_capability_reports_trigger_correlated_conversation_strategy():
    diagnostic = get_bom_agent_capability_diagnostic()

    assert diagnostic["conversation_strategy"] == (
        "project_product_trigger_run_id"
    )


def test_version_endpoint_reports_deployment_strategy(monkeypatch):
    monkeypatch.setattr(app_main, "get_git_commit", lambda: "commit-test")
    monkeypatch.setattr(app_main, "get_build_time", lambda: "2026-07-29T12:00:00Z")

    assert app_main.api_version() == {
        "git_commit": "commit-test",
        "build_time": "2026-07-29T12:00:00Z",
        "bom_conversation_strategy": "project_product_trigger_run_id",
    }
