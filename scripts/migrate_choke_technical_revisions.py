"""Add technical revision metadata without deleting or triggering anything.

Dry-run is the default. Use --apply only after reviewing the report.
Legacy component/MOST outputs remain ``legacy_unverified`` unless their saved
input snapshot deterministically matches the current input revision.
"""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import choke_technical_revisions as revisions  # noqa: E402
from services.project_data_paths import COSTING_RUNS_DIR, atomic_write_json  # noqa: E402


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def output(path):
    return read_json(path, None)


def migrate_run(run_dir, apply=False):
    state_path = run_dir / "workflow_state.json"
    raw_path = run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
    normalized_path = run_dir / "bom_normalized.json"
    state = read_json(state_path, None)
    raw = read_json(raw_path, None)
    normalized = read_json(normalized_path, None)
    if not isinstance(state, dict) or not isinstance(raw, dict) or not isinstance(normalized, dict):
        return {"run_dir": str(run_dir), "status": "skipped", "reason": "required_current_files_missing"}

    normalized = revisions.attach_bom_revisions(raw, normalized)
    process = revisions.attach_process_revisions(
        state.get("process_decomposition") or {},
        normalized["technical_revision"],
        normalized,
    )
    component_dir = run_dir / "components_normalized"
    most_dir = run_dir / "most_normalized"
    component_reconciliation = revisions.reconcile_component_outputs(
        normalized,
        state,
        lambda component_id: output(component_dir / f"{component_id}.json"),
    )
    most_reconciliation = revisions.reconcile_most_outputs(
        process,
        state,
        lambda work_package_id: output(most_dir / f"{work_package_id}.json"),
    )
    report = {
        "run_dir": str(run_dir),
        "status": "would_migrate" if not apply else "migrated",
        "technical_revisions": {
            "raw_bom": normalized["source_raw_bom_revision"],
            "normalized_bom": normalized["technical_revision"],
            "process_decomposition": process["technical_revision"],
        },
        "legacy_component_outputs": [
            item["component_id"]
            for item in component_reconciliation["legacy_unverified_components"]
        ],
        "legacy_most_outputs": [
            item["work_package_id"]
            for item in most_reconciliation["legacy_unverified_work_packages"]
        ],
        "obsolete_component_outputs": [
            item["component_id"]
            for item in component_reconciliation["obsolete_components"]
        ],
        "obsolete_most_outputs": [
            item["work_package_id"]
            for item in most_reconciliation["obsolete_work_packages"]
        ],
    }
    if apply:
        state["process_decomposition"] = process
        state["technical_revisions"] = report["technical_revisions"]
        state["component_revision_reconciliation"] = component_reconciliation
        state["most_revision_reconciliation"] = most_reconciliation
        state.setdefault("migration_notes", []).append({
            "status": "technical_revision_metadata_added",
            "legacy_outputs_policy": "legacy_unverified_until_inputs_match",
        })
        atomic_write_json(normalized_path, normalized)
        atomic_write_json(state_path, state)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reports = []
    if COSTING_RUNS_DIR.exists():
        for state_path in COSTING_RUNS_DIR.glob("*/*/workflow_state.json"):
            reports.append(migrate_run(state_path.parent, apply=args.apply))
    print(json.dumps({
        "mode": "apply" if args.apply else "dry_run",
        "runs": reports,
        "files_deleted": 0,
        "agent_triggers": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
