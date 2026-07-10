import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.choke_orchestrator import run_choke_orchestration


BASE_DIR = Path(__file__).resolve().parents[1]
RUNS_DIR = BASE_DIR / "data" / "costing_runs"
CUSTOMER_INPUT_DIR = BASE_DIR / "data" / "customer_inputs"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_part(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{field_name} must not contain path separators.")
    return text


def _run_dir(project_code: str, product_id: str) -> Path:
    project = _safe_part(project_code, "project_code")
    product = _safe_part(product_id, "product_id")
    return RUNS_DIR / project / product


def _relative(path: Path) -> str:
    return path.resolve().relative_to(BASE_DIR.resolve()).as_posix()


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return _relative(path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _status_path(project_code: str, product_id: str) -> Path:
    return _run_dir(project_code, product_id) / "agent_outputs" / "status.json"


def _load_status(project_code: str, product_id: str) -> Dict[str, Any]:
    status = _read_json(_status_path(project_code, product_id), None)
    if isinstance(status, dict):
        status.setdefault("project_code", project_code)
        status.setdefault("product_id", product_id)
        status.setdefault("bom", {"status": "missing"})
        status.setdefault("components", {})
        status.setdefault("most", {})
        return status
    return {
        "project_code": project_code,
        "product_id": product_id,
        "bom": {"status": "missing"},
        "components": {},
        "most": {},
        "created_at": _now_iso(),
    }


def _save_status(project_code: str, product_id: str, status: Dict[str, Any]) -> str:
    status["updated_at"] = _now_iso()
    return _write_json(_status_path(project_code, product_id), status)


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except Exception:
        pass

    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _optional_database_write(
    project_code: str,
    product_id: str,
    output_type: str,
    output_key: str,
    agent_name: str,
    raw_json: Any,
    path: str,
    save_to_database: bool = False,
) -> Dict[str, Any]:
    if not save_to_database:
        return {"status": "skipped", "reason": "save_to_database is false"}

    _load_env()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return {"status": "skipped", "reason": "DATABASE_URL is not configured"}

    try:
        import psycopg2

        conn = psycopg2.connect(
            database_url,
            connect_timeout=10,
            sslmode=os.getenv("PGSSLMODE", "require"),
        )
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS costing_agent_outputs (
                            costing_agent_output_id SERIAL PRIMARY KEY,
                            project_code TEXT NOT NULL,
                            product_id TEXT NOT NULL,
                            output_type TEXT NOT NULL,
                            output_key TEXT NOT NULL,
                            agent_name TEXT NULL,
                            raw_json JSONB NOT NULL,
                            file_path TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT NOW(),
                            updated_at TIMESTAMP DEFAULT NOW(),
                            UNIQUE (project_code, product_id, output_type, output_key)
                        )
                        """
                    )
                    cur.execute(
                        """
                        INSERT INTO costing_agent_outputs
                        (
                            project_code,
                            product_id,
                            output_type,
                            output_key,
                            agent_name,
                            raw_json,
                            file_path
                        )
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (project_code, product_id, output_type, output_key)
                        DO UPDATE SET
                            agent_name = EXCLUDED.agent_name,
                            raw_json = EXCLUDED.raw_json,
                            file_path = EXCLUDED.file_path,
                            updated_at = NOW()
                        """,
                        (
                            project_code,
                            product_id,
                            output_type,
                            output_key,
                            agent_name,
                            json.dumps(raw_json, ensure_ascii=False, default=str),
                            path,
                        ),
                    )
            return {"status": "saved", "table": "costing_agent_outputs"}
        finally:
            conn.close()
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def save_choke_bom_result(
    project_code: str,
    product_id: str,
    agent_name: str,
    raw_json: Dict[str, Any],
    save_to_database: bool = False,
) -> Dict[str, Any]:
    run_dir = _run_dir(project_code, product_id)
    path = run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
    relative_path = _write_json(path, raw_json)

    status = _load_status(project_code, product_id)
    status["bom"] = {
        "status": "received",
        "agent_name": agent_name,
        "path": relative_path,
        "received_at": _now_iso(),
    }
    _save_status(project_code, product_id, status)

    workflow_update: Dict[str, Any] = {"status": "skipped"}
    try:
        from services.choke_sequential_agent_workflow import save_bom_output

        workflow_update = save_bom_output(project_code, product_id, raw_json)
    except Exception as exc:
        workflow_update = {"status": "failed", "error": str(exc)}

    db_result = _optional_database_write(
        project_code,
        product_id,
        "bom",
        "bom",
        agent_name,
        raw_json,
        relative_path,
        save_to_database=save_to_database,
    )
    return {
        "status": "saved",
        "output_type": "bom",
        "path": relative_path,
        "next_step": "backend_can_trigger_component_costing",
        "workflow_update": workflow_update,
        "database": db_result,
    }


def save_component_costing_result(
    project_code: str,
    product_id: str,
    component_id: str,
    component_type: str,
    agent_name: str,
    raw_json: Dict[str, Any],
    save_to_database: bool = False,
) -> Dict[str, Any]:
    safe_component_id = _safe_part(component_id, "component_id")
    run_dir = _run_dir(project_code, product_id)
    path = run_dir / "agent_outputs" / "components" / f"{safe_component_id}.json"
    relative_path = _write_json(path, raw_json)

    status = _load_status(project_code, product_id)
    status.setdefault("components", {})
    status["components"][safe_component_id] = {
        "status": "received",
        "component_id": safe_component_id,
        "component_type": component_type,
        "agent_name": agent_name,
        "path": relative_path,
        "received_at": _now_iso(),
    }
    _save_status(project_code, product_id, status)

    workflow_update: Dict[str, Any] = {"status": "skipped"}
    try:
        from services.choke_sequential_agent_workflow import save_component_output

        workflow_update = save_component_output(project_code, product_id, safe_component_id, raw_json)
    except Exception as exc:
        workflow_update = {"status": "failed", "error": str(exc)}

    db_result = _optional_database_write(
        project_code,
        product_id,
        "component",
        safe_component_id,
        agent_name,
        raw_json,
        relative_path,
        save_to_database=save_to_database,
    )
    return {
        "status": "saved",
        "output_type": "component",
        "component_id": safe_component_id,
        "path": relative_path,
        "next_step": "backend_can_trigger_next_component_or_most",
        "workflow_update": workflow_update,
        "database": db_result,
    }


def save_most_operation_result(
    project_code: str,
    product_id: str,
    work_package_id: str,
    component_id: str,
    operation_id: str,
    operation_name: str,
    agent_name: str,
    raw_json: Dict[str, Any],
    save_to_database: bool = False,
) -> Dict[str, Any]:
    safe_work_package_id = _safe_part(work_package_id, "work_package_id")
    run_dir = _run_dir(project_code, product_id)
    path = run_dir / "agent_outputs" / "most" / f"{safe_work_package_id}.json"
    relative_path = _write_json(path, raw_json)

    status = _load_status(project_code, product_id)
    status.setdefault("most", {})
    status["most"][safe_work_package_id] = {
        "status": "received",
        "work_package_id": safe_work_package_id,
        "component_id": component_id,
        "operation_id": operation_id,
        "operation_name": operation_name,
        "agent_name": agent_name,
        "path": relative_path,
        "received_at": _now_iso(),
    }
    _save_status(project_code, product_id, status)

    workflow_update: Dict[str, Any] = {"status": "skipped"}
    try:
        from services.choke_sequential_agent_workflow import save_most_output

        workflow_update = save_most_output(project_code, product_id, safe_work_package_id, raw_json)
    except Exception as exc:
        workflow_update = {"status": "failed", "error": str(exc)}

    db_result = _optional_database_write(
        project_code,
        product_id,
        "most",
        safe_work_package_id,
        agent_name,
        raw_json,
        relative_path,
        save_to_database=save_to_database,
    )
    return {
        "status": "saved",
        "output_type": "most",
        "work_package_id": safe_work_package_id,
        "path": relative_path,
        "next_step": "backend_can_trigger_next_operation_or_calculate",
        "workflow_update": workflow_update,
        "database": db_result,
    }


def _planned_outputs(project_code: str, product_id: str) -> Dict[str, List[str]]:
    plan_path = _run_dir(project_code, product_id) / "orchestration_result.json"
    envelope = _read_json(plan_path, {}) or {}
    orchestration = envelope.get("agent_orchestration") or {}
    component_ids = [
        str(item.get("component_id"))
        for item in orchestration.get("component_agent_calls") or []
        if item.get("component_id")
    ]
    work_package_ids = [
        str(item.get("work_package_id"))
        for item in orchestration.get("most_agent_calls") or []
        if item.get("work_package_id")
    ]
    return {"components": component_ids, "most": work_package_ids}


def get_costing_run_status(project_code: str, product_id: str) -> Dict[str, Any]:
    status = _load_status(project_code, product_id)
    planned = _planned_outputs(project_code, product_id)

    bom_received = (status.get("bom") or {}).get("status") == "received"
    components = status.get("components") or {}
    most = status.get("most") or {}
    components_received = [
        key for key, value in components.items()
        if isinstance(value, dict) and value.get("status") == "received"
    ]
    most_received = [
        key for key, value in most.items()
        if isinstance(value, dict) and value.get("status") == "received"
    ]

    def is_received(planned_key: str, received_keys: List[str]) -> bool:
        planned_text = str(planned_key)
        return any(
            received == planned_text
            or received.endswith(f"-{planned_text}")
            or planned_text.endswith(f"-{received}")
            for received in received_keys
        )

    missing_outputs = []
    if not bom_received:
        missing_outputs.append("bom")
    for component_id in planned.get("components") or []:
        if not is_received(component_id, components_received):
            missing_outputs.append(f"component:{component_id}")
    for work_package_id in planned.get("most") or []:
        if not is_received(work_package_id, most_received):
            missing_outputs.append(f"most:{work_package_id}")

    return {
        "project_code": project_code,
        "product_id": product_id,
        "bom_received": bom_received,
        "components_received": components_received,
        "most_received": most_received,
        "missing_outputs": missing_outputs,
        "status": status,
    }


def _get_path(data: Any, path: List[str]) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_value(data: Dict[str, Any], paths: List[List[str]]) -> Any:
    for path in paths:
        value = _get_path(data, path)
        if value not in [None, ""]:
            return value
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if value in [None, ""] or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".").replace("%", "")
    try:
        return float(text)
    except ValueError:
        import re

        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None


def _normalize_component_output(path: Path) -> Dict[str, Any]:
    raw_json = _read_json(path, {}) or {}
    component_id = (
        raw_json.get("component_id")
        or raw_json.get("component_reference")
        or path.stem
    )
    component_type = (
        raw_json.get("component_type")
        or raw_json.get("component_family")
        or raw_json.get("family")
        or ""
    )
    delivered_cost = _coerce_float(_first_value(raw_json, [
        ["normalized_cost", "delivered_cost_per_piece"],
        ["recommended_offer", "supply_chain", "delivered_cost"],
        ["recommended_offer", "delivered_cost"],
        ["delivered_cost_per_piece"],
        ["delivered_cost"],
        ["recommended_offer", "selling_price_converted_per_unit"],
        ["recommended_offer", "selling_price_per_unit"],
    ]))
    material_cost = _coerce_float(_first_value(raw_json, [
        ["normalized_cost", "material_cost_per_piece"],
        ["material_cost_per_piece"],
    ]))
    currency = _first_value(raw_json, [
        ["normalized_cost", "currency"],
        ["recommended_offer", "reporting_currency"],
        ["recommended_offer", "purchasing_currency"],
        ["currency"],
    ])
    normalized_cost = dict(raw_json.get("normalized_cost") or {})
    normalized_cost.update({
        "currency": currency or normalized_cost.get("currency") or "",
        "material_cost_per_piece": material_cost if material_cost is not None else delivered_cost,
        "delivered_cost_per_piece": delivered_cost,
        "tooling_cost": normalized_cost.get("tooling_cost") or raw_json.get("tooling_cost"),
        "commercially_usable": bool(normalized_cost.get("commercially_usable")),
        "missing_inputs": normalized_cost.get("missing_inputs") or [],
    })
    return {
        **raw_json,
        "component_id": component_id,
        "component_type": component_type,
        "normalized_cost": normalized_cost,
        "raw_json": raw_json,
    }


def _normalize_most_output(path: Path) -> Dict[str, Any]:
    raw_json = _read_json(path, {}) or {}
    work_package_id = raw_json.get("work_package_id") or path.stem
    p_h = _coerce_float(_first_value(raw_json, [
        ["p_h"],
        ["station_library_summary", "p_h"],
        ["rate_per_hour_instantaneous"],
    ]))
    cycle_time = _coerce_float(_first_value(raw_json, [
        ["cycle_time_seconds"],
        ["operation_cycle_time_seconds"],
    ]))
    parts_per_cycle = _coerce_float(_first_value(raw_json, [
        ["parts_per_cycle"],
        ["pieces_per_cycle"],
    ])) or 1.0
    if p_h in [None, 0] and cycle_time not in [None, 0]:
        p_h = 3600 / cycle_time * parts_per_cycle

    normalized = {
        "work_package_id": work_package_id,
        "component_id": raw_json.get("component_id"),
        "operation_id": raw_json.get("operation_id"),
        "operation_name": raw_json.get("operation_name") or raw_json.get("operation"),
        "p_h": p_h,
        "cycle_time_seconds": cycle_time,
        "oee": _first_value(raw_json, [["oee"], ["oee_percent"], ["costing_oee_percent"]]),
        "operator_percent": _first_value(raw_json, [["operator_percent"], ["percent_operator"]]),
        "parts_per_cycle": parts_per_cycle,
        "generic_capex_eur": _first_value(raw_json, [["generic_capex_eur"], ["generic_capex"]]),
        "specific_capex_eur": _first_value(raw_json, [["specific_capex_eur"], ["specific_capex"]]),
        "tooling_cost_eur": _first_value(raw_json, [["tooling_cost_eur"], ["tooling_cost"]]),
        "tooling_life_pieces": _first_value(raw_json, [
            ["tooling_life_pieces"],
            ["tooling_life_parts"],
            ["tooling_lifetime_parts"],
        ]),
        "tooling_adder_per_piece_eur": _first_value(raw_json, [["tooling_adder_per_piece_eur"]]),
        "agent_raw_output": raw_json,
        "raw_json": raw_json,
    }
    return {**raw_json, **normalized}


def _load_customer_input(input_file: str) -> Dict[str, Any]:
    path = Path(input_file)
    if path.is_absolute():
        candidate = path
    else:
        candidate = BASE_DIR / path
    candidate = candidate.resolve()
    allowed_root = CUSTOMER_INPUT_DIR.resolve()
    if allowed_root not in candidate.parents and candidate != allowed_root:
        raise ValueError("input_file must be inside data/customer_inputs")
    if not candidate.exists():
        raise FileNotFoundError(f"Customer input file not found: {input_file}")
    return _read_json(candidate, {}) or {}


def calculate_choke_from_saved_agent_outputs(
    project_code: str,
    product_id: str,
    input_file: str,
) -> Dict[str, Any]:
    run_dir = _run_dir(project_code, product_id)
    bom_path = run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
    component_dir = run_dir / "agent_outputs" / "components"
    most_dir = run_dir / "agent_outputs" / "most"

    customer_input = _load_customer_input(input_file)
    bom_json = _read_json(bom_path, None)
    component_outputs = [
        _normalize_component_output(path)
        for path in sorted(component_dir.glob("*.json"))
    ] if component_dir.exists() else []
    most_outputs = [
        _normalize_most_output(path)
        for path in sorted(most_dir.glob("*.json"))
    ] if most_dir.exists() else []

    envelope = run_choke_orchestration(
        customer_input,
        dry_run=True,
        trigger_agents=False,
        bom_json=bom_json,
        component_cost_outputs=component_outputs,
        most_outputs=most_outputs,
        demo_override=False,
    )

    most_raw_by_id = {
        item.get("work_package_id"): item.get("raw_json")
        for item in most_outputs
        if item.get("work_package_id")
    }
    for work_package in envelope.get("most_work_packages") or []:
        work_package_id = work_package.get("work_package_id")
        if work_package_id in most_raw_by_id:
            work_package["most_status"] = "available"
            work_package["agent_raw_output"] = most_raw_by_id[work_package_id]

    envelope["saved_agent_outputs"] = {
        "bom_path": _relative(bom_path) if bom_path.exists() else None,
        "component_paths": [
            _relative(path) for path in sorted(component_dir.glob("*.json"))
        ] if component_dir.exists() else [],
        "most_paths": [
            _relative(path) for path in sorted(most_dir.glob("*.json"))
        ] if most_dir.exists() else [],
    }
    envelope["calculation_source"] = "saved_agent_outputs"

    output_path = run_dir / "orchestration_result_from_saved_agent_outputs.json"
    envelope["orchestration_result_from_saved_agent_outputs_path"] = _write_json(output_path, envelope)
    return envelope
