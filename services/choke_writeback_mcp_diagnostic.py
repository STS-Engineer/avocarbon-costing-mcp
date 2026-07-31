import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from services.project_data_paths import get_build_time, get_git_commit
from services.public_url_service import get_public_rest_base_url


WRITEBACK_REQUIRED_FIELDS = {
    "save_bom_output": {"project_code", "product_id", "trigger_run_id", "raw_json"},
    "save_component_output": {
        "project_code", "product_id", "component_id", "trigger_run_id", "raw_json",
    },
    "save_most_output": {
        "project_code", "product_id", "work_package_id", "most_scope_id",
        "trigger_run_id", "raw_json",
    },
}
DRAWING_REQUIRED_FIELDS = {"project_code", "product_id", "trigger_run_id"}

# Backward-compatible documented contract. Runtime validation below deliberately
# reads FastMCP's registered catalog instead of trusting this declaration.
WRITEBACK_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "save_bom_output": {
        "required": ["project_code", "product_id", "trigger_run_id", "raw_json"],
    },
    "save_component_output": {
        "required": [
            "project_code", "product_id", "component_id", "trigger_run_id", "raw_json",
        ],
    },
    "save_most_output": {
        "required": [
            "project_code", "product_id", "work_package_id", "most_scope_id",
            "trigger_run_id", "raw_json",
        ],
    },
    "get_choke_drawing": {
        "required": ["project_code", "product_id", "trigger_run_id"],
    },
}


def _runtime_tool_schemas() -> Dict[str, Dict[str, Any]]:
    # Imported lazily to avoid a server <-> diagnostic import cycle.
    from server import mcp

    return {
        tool.name: dict(tool.parameters)
        for tool in mcp._tool_manager.list_tools()
    }


def _schema_sha256(schema: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        schema, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _deployment_version() -> str:
    return str(
        os.getenv("DEPLOYMENT_VERSION")
        or os.getenv("APP_VERSION")
        or os.getenv("BUILD_BUILDNUMBER")
        or get_build_time()
        or "unknown"
    ).strip()


def get_mcp_schema_fingerprints() -> Dict[str, Any]:
    schemas = _runtime_tool_schemas()
    generated_at = datetime.now(timezone.utc).isoformat()
    actions = []
    errors = []
    for action_name in (
        "save_bom_output",
        "save_component_output",
        "save_most_output",
    ):
        schema = schemas.get(action_name)
        expected = WRITEBACK_REQUIRED_FIELDS[action_name]
        actual_required = set((schema or {}).get("required") or [])
        properties = set((schema or {}).get("properties") or {})
        missing_required = sorted(expected - actual_required)
        missing_properties = sorted(expected - properties)
        valid = schema is not None and not missing_required and not missing_properties
        if not valid:
            errors.append({
                "action_name": action_name,
                "missing_required_fields": missing_required,
                "missing_properties": missing_properties,
            })
        actions.append({
            "action_name": action_name,
            "required_fields": sorted(actual_required),
            "property_fields": sorted(properties),
            "schema_sha256": _schema_sha256(schema or {}),
            "schema_valid": valid,
            "missing_required_fields": missing_required,
        })
    drawing_schema = schemas.get("get_choke_drawing")
    drawing_required = set((drawing_schema or {}).get("required") or [])
    drawing_properties = set((drawing_schema or {}).get("properties") or {})
    drawing_valid = (
        drawing_schema is not None
        and DRAWING_REQUIRED_FIELDS <= drawing_required
        and DRAWING_REQUIRED_FIELDS <= drawing_properties
    )
    return {
        "status": "ok" if not errors and drawing_valid else "configuration_error",
        "deployment_version": _deployment_version(),
        "server_commit": get_git_commit(),
        "generated_at": generated_at,
        "actions": actions,
        "get_choke_drawing": {
            "action_name": "get_choke_drawing",
            "required_fields": sorted(drawing_required),
            "property_fields": sorted(drawing_properties),
            "schema_sha256": _schema_sha256(drawing_schema or {}),
            "schema_valid": drawing_valid,
        },
        "errors": errors + ([] if drawing_valid else [{
            "action_name": "get_choke_drawing",
            "missing_required_fields": sorted(DRAWING_REQUIRED_FIELDS - drawing_required),
        }]),
    }


def validate_runtime_writeback_schemas(raise_on_error: bool = False) -> Dict[str, Any]:
    diagnostic = get_mcp_schema_fingerprints()
    if raise_on_error and diagnostic["status"] != "ok":
        raise RuntimeError(
            "MCP action schema mismatch: "
            + json.dumps(diagnostic["errors"], ensure_ascii=True, sort_keys=True)
        )
    return diagnostic


def get_writeback_mcp_connectivity_diagnostic() -> Dict[str, Any]:
    public_rest_base = get_public_rest_base_url()
    fingerprint = get_mcp_schema_fingerprints()
    by_name = {item["action_name"]: item for item in fingerprint["actions"]}
    schemas = _runtime_tool_schemas()
    save_schema = schemas.get("save_bom_output")
    save_most_schema = schemas.get("save_most_output")
    return {
        "status": fingerprint["status"],
        "mcp_url": f"{public_rest_base}/mcp" if public_rest_base else "/mcp",
        "exposed_tools": sorted(schemas),
        "save_bom_output_exists": save_schema is not None,
        "save_bom_output_schema_valid": by_name["save_bom_output"]["schema_valid"],
        "save_bom_output_schema": save_schema,
        "save_most_output_exists": save_most_schema is not None,
        "save_most_output_schema_valid": by_name["save_most_output"]["schema_valid"],
        "save_most_output_schema": save_most_schema,
        "get_choke_drawing_exists": fingerprint["get_choke_drawing"]["schema_valid"],
        "schema_fingerprints": fingerprint,
        "authentication": {
            "server_auth_mode": os.getenv("MCP_AUTH_TYPE") or "not_configured",
            "secret_values_exposed": False,
        },
        "health_check": {
            "status": "ok" if fingerprint["status"] == "ok" else "configuration_error",
            "mode": "runtime_fastmcp_catalog",
            "write_performed": False,
        },
    }


def get_bom_agent_capability_diagnostic() -> Dict[str, Any]:
    connectivity = get_writeback_mcp_connectivity_diagnostic()
    schema = connectivity.get("save_bom_output_schema") or {}
    required = set(schema.get("required") or [])
    accepts_trigger_run_id = "trigger_run_id" in required
    return {
        "status": "ok" if connectivity.get("save_bom_output_schema_valid") else "configuration_error",
        "drawing_delivery_mode": "mcp_embedded_resource",
        "attachment_file_reference_present": False,
        "get_choke_drawing_available": bool(connectivity.get("get_choke_drawing_exists")),
        "save_bom_output_available": bool(connectivity.get("save_bom_output_exists")),
        "save_bom_output_accepts_trigger_run_id": accepts_trigger_run_id,
        "save_bom_output_required_fields": sorted(required),
        "published_agent_version": (
            os.getenv("CHATGPT_CHOKE_BOM_AGENT_PUBLISHED_VERSION") or "unknown"
        ),
        "conversation_mode": "new",
        "conversation_strategy": "project_product_trigger_run_id",
        "diagnostic_source": "runtime_fastmcp_catalog",
    }


def require_bom_writeback_capability() -> Dict[str, Any]:
    diagnostic = get_bom_agent_capability_diagnostic()
    if (
        not diagnostic["save_bom_output_available"]
        or not diagnostic["save_bom_output_accepts_trigger_run_id"]
        or not diagnostic["get_choke_drawing_available"]
    ):
        raise RuntimeError(
            "Published BOM Agent MCP capability is incompatible: "
            "save_bom_output must require trigger_run_id and get_choke_drawing must be registered."
        )
    return diagnostic
