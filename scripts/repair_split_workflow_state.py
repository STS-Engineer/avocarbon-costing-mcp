import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import normalize_bom
from services.project_data_paths import (
    get_legacy_workflow_state_paths,
    get_workflow_run_paths,
)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _started_score(state: Dict[str, Any]) -> int:
    fields = [
        "input_file",
        "drawing_file_path",
        "drawing_file_url",
        "customer_input",
        "created_at",
    ]
    return sum(state.get(field) not in [None, "", {}, []] for field in fields)


def _bom_score(state: Dict[str, Any]) -> int:
    bom = state.get("bom") or {}
    return sum([
        state.get("status") == "bom_received",
        bom.get("status") == "received",
        bool(bom.get("save_path")),
        bool(bom.get("normalized_path")),
        bool(bom.get("received_at")),
    ])


def _merge_events(source_paths: List[Path], destination: Path, apply: bool) -> int:
    lines = []
    seen = set()
    for state_path in source_paths:
        events_path = state_path.parent / "workflow_events.jsonl"
        if not events_path.exists():
            continue
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
    if apply and lines:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def repair_split_workflow_state(
    project_code: str,
    product_id: str,
    apply: bool = False,
) -> Dict[str, Any]:
    paths = get_workflow_run_paths(project_code, product_id)
    canonical_state_path = paths["workflow_state_path"]
    state_paths = []
    if canonical_state_path.exists():
        state_paths.append(canonical_state_path)
    state_paths.extend(get_legacy_workflow_state_paths(project_code, product_id))
    state_paths = list(dict.fromkeys(path.resolve() for path in state_paths))
    states = [(path, _read_json(path)) for path in state_paths]
    states = [(path, state) for path, state in states if state]

    report = {
        "project_code": project_code,
        "product_id": product_id,
        "mode": "apply" if apply else "dry_run",
        "states_found": [str(path) for path, _ in states],
        "selected_canonical_path": str(canonical_state_path),
        "fields_merged": [],
        "files_copied": [],
        "state_files_archived": [],
        "final_status": "not_found",
    }
    if not states:
        return report

    started_path, started_state = max(states, key=lambda item: _started_score(item[1]))
    bom_path, bom_state = max(states, key=lambda item: _bom_score(item[1]))
    merged = dict(started_state)
    fields_to_preserve = [
        "input_file",
        "drawing_file_path",
        "drawing_file_url",
        "drawing_access_mode",
        "drawing_blob_url",
        "drawing_sas_url",
        "customer_input",
        "created_at",
    ]
    for field in fields_to_preserve:
        value = started_state.get(field)
        if value not in [None, "", {}, []]:
            merged[field] = value
            report["fields_merged"].append(field)

    started_bom = dict(started_state.get("bom") or {})
    received_bom = dict(bom_state.get("bom") or {})
    merged_bom = {**started_bom, **received_bom}
    if started_bom.get("trigger_result"):
        merged_bom["trigger_result"] = started_bom["trigger_result"]
    if started_bom.get("trigger_attempts"):
        merged_bom["trigger_attempts"] = started_bom["trigger_attempts"]
    merged_bom.update({"status": "received", "retryable": False})
    merged["bom"] = merged_bom
    merged["project_code"] = project_code
    merged["product_id"] = product_id
    merged["status"] = "bom_received"
    merged["current_step"] = "Step 2 External Component Costing Agent"
    merged["writeback_created_state_without_start"] = False
    merged["retryable"] = False
    merged["retry_available"] = False

    for source_path, _ in states:
        if source_path.parent == paths["run_dir"]:
            continue
        if apply:
            shutil.copytree(source_path.parent, paths["run_dir"], dirs_exist_ok=True)
        report["files_copied"].append(
            {"from": str(source_path.parent), "to": str(paths["run_dir"])}
        )

    raw_path = paths["raw_bom_path"]
    normalized_path = paths["normalized_bom_path"]
    if not raw_path.exists() or not normalized_path.exists():
        for source_path, _ in states:
            source_raw = source_path.parent / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
            source_normalized = source_path.parent / "bom_normalized.json"
            if apply and source_raw.exists() and not raw_path.exists():
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_raw, raw_path)
            if apply and source_normalized.exists() and not normalized_path.exists():
                normalized_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_normalized, normalized_path)

    normalized = _read_json(normalized_path) if normalized_path.exists() else {}
    if not normalized and raw_path.exists():
        normalized = normalize_bom(_read_json(raw_path))
        if apply:
            _write_json(normalized_path, normalized)
    component_ids = list(dict.fromkeys(
        item.get("component_id")
        for item in normalized.get("external_components") or []
        if isinstance(item, dict) and item.get("component_id")
    ))
    if not component_ids:
        component_ids = list(dict.fromkeys(
            value.split(":", 1)[1]
            for value in bom_state.get("missing_outputs") or []
            if isinstance(value, str) and value.startswith("component:")
        ))
    merged["missing_outputs"] = [f"component:{item}" for item in component_ids]
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    report["fields_merged"].extend([
        "bom",
        "status",
        "current_step",
        "missing_outputs",
    ])
    report["fields_merged"] = list(dict.fromkeys(report["fields_merged"]))

    report["workflow_events_merged"] = _merge_events(
        [path for path, _ in states],
        paths["workflow_events_path"],
        apply,
    )
    if apply:
        _write_json(canonical_state_path, merged)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        for source_path, _ in states:
            if source_path == canonical_state_path or not source_path.exists():
                continue
            archived = source_path.with_name(f"workflow_state.migrated-{stamp}.json")
            source_path.replace(archived)
            report["state_files_archived"].append(str(archived))
    report["final_status"] = merged["status"]
    report["final_state_preview"] = {
        "status": merged.get("status"),
        "current_step": merged.get("current_step"),
        "input_file": merged.get("input_file"),
        "drawing_file_path": merged.get("drawing_file_path"),
        "bom_status": (merged.get("bom") or {}).get("status"),
        "missing_outputs": merged.get("missing_outputs"),
        "writeback_created_state_without_start": merged.get(
            "writeback_created_state_without_start"
        ),
    }
    report["started_state_source"] = str(started_path)
    report["bom_state_source"] = str(bom_path)
    return report


def parse_args():
    parser = argparse.ArgumentParser(description="Repair split Choke workflow state files.")
    parser.add_argument("--project-code", required=True)
    parser.add_argument("--product-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    report = repair_split_workflow_state(
        project_code=args.project_code,
        product_id=args.product_id,
        apply=args.apply,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["final_status"] != "not_found" else 1


if __name__ == "__main__":
    raise SystemExit(main())
