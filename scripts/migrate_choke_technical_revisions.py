"""Add Choke technical revision metadata without deleting or triggering.

Dry-run is the default. Use --apply only after reviewing the report.
Legacy component/MOST outputs remain ``legacy_unverified`` unless their saved
input snapshot deterministically matches the current input revision.
"""

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import choke_technical_revisions as revisions  # noqa: E402
from services.project_data_paths import (  # noqa: E402
    atomic_write_json,
    get_data_root,
)


DEFAULT_TEST_RUN_PATTERNS = (
    "*-TEST-*",
    "BOM-RETRY-*",
    "BOM-STATE-*",
    "BOM-UX-*",
    "FINAL-CALC-TEST-*",
    "MCP-REST-*",
    "RFQ-WRITEBACK-MCP-TEST-*",
    "WRITEBACK-DEBUG-*",
    "WRITEBACK-TEST-*",
)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def output(path: Path):
    return read_json(path, None)


def _skipped(run_dir: Path, reason: str) -> Dict[str, Any]:
    return {
        "run_dir": str(run_dir),
        "status": "skipped",
        "reason": reason,
        "files_created_or_updated": 0,
        "files_deleted": 0,
        "agent_triggers": 0,
    }


def migrate_run(run_dir: Path, apply: bool = False) -> Dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / "workflow_state.json"
    raw_path = run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
    normalized_path = run_dir / "bom_normalized.json"
    if not state_path.is_file():
        return _skipped(run_dir, "workflow_state_missing")
    if not raw_path.is_file():
        return _skipped(run_dir, "raw_bom_missing")
    if not normalized_path.is_file():
        return _skipped(run_dir, "normalized_bom_missing")

    try:
        state = read_json(state_path, None)
        raw = read_json(raw_path, None)
        normalized = read_json(normalized_path, None)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return {
            "run_dir": str(run_dir),
            "status": "failed",
            "reason": "json_read_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "files_created_or_updated": 0,
            "files_deleted": 0,
            "agent_triggers": 0,
        }
    if not isinstance(state, dict):
        return _skipped(run_dir, "workflow_state_missing")
    if not isinstance(raw, dict):
        return _skipped(run_dir, "raw_bom_missing")
    if not isinstance(normalized, dict):
        return _skipped(run_dir, "normalized_bom_missing")
    if not isinstance(state.get("process_decomposition"), dict) or not state.get(
        "process_decomposition"
    ):
        return _skipped(run_dir, "process_decomposition_missing")

    try:
        normalized = revisions.attach_bom_revisions(raw, normalized)
        process = revisions.attach_process_revisions(
            state["process_decomposition"],
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
    except Exception as exc:
        return {
            "run_dir": str(run_dir),
            "status": "failed",
            "reason": "revision_metadata_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "files_created_or_updated": 0,
            "files_deleted": 0,
            "agent_triggers": 0,
        }

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
        "files_created_or_updated": 2 if apply else 0,
        "files_deleted": 0,
        "agent_triggers": 0,
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


def _resolve_data_root(data_root: Optional[str | Path]) -> Path:
    if data_root is None:
        return get_data_root(create=False).resolve()
    configured = Path(data_root).expanduser()
    if not configured.is_absolute():
        configured = Path.cwd() / configured
    return configured.resolve()


def _matches_exclusion(
    project_code: str,
    product_id: str,
    patterns: Sequence[str],
) -> bool:
    candidates = (project_code, product_id, f"{project_code}/{product_id}")
    return any(
        fnmatch.fnmatchcase(candidate.upper(), pattern.upper())
        for pattern in patterns
        for candidate in candidates
    )


def _candidate_run_dirs(
    costing_runs_root: Path,
    project_code: Optional[str],
    product_id: Optional[str],
) -> tuple[List[Path], Optional[Dict[str, Any]]]:
    if project_code:
        project_dir = costing_runs_root / project_code
        if not project_dir.is_dir():
            return [], _skipped(project_dir, "project_directory_missing")
        if product_id:
            product_dir = project_dir / product_id
            if not product_dir.is_dir():
                return [], _skipped(product_dir, "product_directory_missing")
            return [product_dir], None
        products = sorted(path for path in project_dir.iterdir() if path.is_dir())
        if not products:
            return [], _skipped(project_dir, "product_directory_missing")
        return products, None

    if not costing_runs_root.is_dir():
        missing = costing_runs_root / ("*" if not product_id else f"*/{product_id}")
        return [], _skipped(missing, "project_directory_missing")
    if product_id:
        products = sorted(
            path
            for path in costing_runs_root.glob(f"*/{product_id}")
            if path.is_dir()
        )
        if not products:
            return [], _skipped(
                costing_runs_root / "*" / product_id,
                "product_directory_missing",
            )
        return products, None
    return sorted(
        product_dir
        for project_dir in costing_runs_root.iterdir()
        if project_dir.is_dir()
        for product_dir in project_dir.iterdir()
        if product_dir.is_dir()
    ), None


def _summary(
    reports: Iterable[Dict[str, Any]],
    *,
    total_scanned: Optional[int] = None,
) -> Dict[str, int]:
    reports = list(reports)
    migrated_reports = [
        item for item in reports if item.get("status") in {"would_migrate", "migrated"}
    ]
    return {
        "total_scanned": len(reports) if total_scanned is None else total_scanned,
        "would_migrate": sum(item.get("status") == "would_migrate" for item in reports),
        "migrated": sum(item.get("status") == "migrated" for item in reports),
        "skipped": sum(item.get("status") == "skipped" for item in reports),
        "failed": sum(item.get("status") == "failed" for item in reports),
        "legacy_component_output_count": sum(
            len(item.get("legacy_component_outputs") or [])
            for item in migrated_reports
        ),
        "legacy_most_output_count": sum(
            len(item.get("legacy_most_outputs") or []) for item in migrated_reports
        ),
        "obsolete_component_output_count": sum(
            len(item.get("obsolete_component_outputs") or [])
            for item in migrated_reports
        ),
        "obsolete_most_output_count": sum(
            len(item.get("obsolete_most_outputs") or [])
            for item in migrated_reports
        ),
        "files_created_or_updated": sum(
            int(item.get("files_created_or_updated") or 0) for item in reports
        ),
        "files_deleted": 0,
        "agent_triggers": 0,
    }


def run_migration(
    *,
    data_root: Optional[str | Path] = None,
    project_code: Optional[str] = None,
    product_id: Optional[str] = None,
    apply: bool = False,
    exclude_test_runs: bool = False,
    test_run_patterns: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    resolved_data_root = _resolve_data_root(data_root)
    costing_runs_root = resolved_data_root / "costing_runs"
    run_dirs, missing_target = _candidate_run_dirs(
        costing_runs_root, project_code, product_id
    )
    reports: List[Dict[str, Any]] = []
    patterns = tuple(DEFAULT_TEST_RUN_PATTERNS) + tuple(test_run_patterns or ())
    for run_dir in run_dirs:
        run_project_code = run_dir.parent.name
        run_product_id = run_dir.name
        if exclude_test_runs and _matches_exclusion(
            run_project_code, run_product_id, patterns
        ):
            reports.append(_skipped(run_dir, "excluded_test_run"))
            continue
        reports.append(migrate_run(run_dir, apply=apply))

    filtered = bool(project_code or product_id)
    target_not_found = filtered and not run_dirs
    if missing_target is not None:
        reports.append(missing_target)
    result = {
        "status": "target_not_found" if target_not_found else "complete",
        "mode": "apply" if apply else "dry_run",
        "project_code": project_code,
        "product_id": product_id,
        "searched_data_root": str(resolved_data_root),
        "costing_runs_root": str(costing_runs_root),
        "exclude_test_runs": exclude_test_runs,
        "test_run_patterns": list(patterns) if exclude_test_runs else [],
        "summary": _summary(reports, total_scanned=len(run_dirs)),
        "runs": reports,
    }
    return result


def _write_report(report_file: str | Path, payload: Dict[str, Any]) -> Path:
    path = Path(report_file).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return atomic_write_json(path.resolve(), payload)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--project-code")
    parser.add_argument("--product-id")
    parser.add_argument("--data-root")
    parser.add_argument("--exclude-test-runs", action="store_true")
    parser.add_argument(
        "--test-run-pattern",
        action="append",
        default=[],
        help="Additional case-insensitive fnmatch pattern; may be repeated.",
    )
    parser.add_argument("--report-file")
    args = parser.parse_args(argv)
    result = run_migration(
        data_root=args.data_root,
        project_code=args.project_code,
        product_id=args.product_id,
        apply=args.apply,
        exclude_test_runs=args.exclude_test_runs,
        test_run_patterns=args.test_run_pattern,
    )
    if args.report_file:
        report_path = _write_report(args.report_file, result)
        result["report_file"] = str(report_path)
    print(json.dumps(result, indent=2))
    return 2 if result["status"] == "target_not_found" else 0


if __name__ == "__main__":
    raise SystemExit(main())
