import os
from typing import Any, Dict

from services.public_url_service import get_public_rest_base_url


WRITEBACK_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "save_bom_output": {
        "description": "Save the final Choke BOM Analyzer JSON to the backend workflow",
        "required": ["project_code", "product_id", "trigger_run_id", "raw_json"],
        "properties": {
            "project_code": "string",
            "product_id": "string",
            "trigger_run_id": "string",
            "raw_json": "object",
        },
    },
    "save_component_output": {
        "description": "Save one final External Component Costing Agent JSON to the backend workflow",
        "required": ["project_code", "product_id", "component_id", "raw_json"],
    },
    "save_most_output": {
        "description": "Save one final MOST operation JSON to the backend workflow",
        "required": [
            "project_code",
            "product_id",
            "work_package_id",
            "trigger_run_id",
            "raw_json",
        ],
        "properties": {
            "project_code": "string",
            "product_id": "string",
            "work_package_id": "string",
            "trigger_run_id": "string",
            "raw_json": ["object", "string"],
        },
    },
    "get_choke_workflow_status": {
        "description": "Read the current Choke workflow status",
        "required": ["project_code", "product_id"],
    },
}


def get_writeback_mcp_connectivity_diagnostic() -> Dict[str, Any]:
    public_rest_base = get_public_rest_base_url()
    save_schema = WRITEBACK_TOOL_SCHEMAS.get("save_bom_output")
    save_most_schema = WRITEBACK_TOOL_SCHEMAS.get("save_most_output")
    required = set((save_schema or {}).get("required") or [])
    schema_valid = {
        "project_code",
        "product_id",
        "trigger_run_id",
        "raw_json",
    }.issubset(required)
    return {
        "status": "ok" if schema_valid else "configuration_error",
        "mcp_url": f"{public_rest_base}/mcp" if public_rest_base else "/mcp",
        "exposed_tools": sorted(WRITEBACK_TOOL_SCHEMAS),
        "save_bom_output_exists": save_schema is not None,
        "save_bom_output_schema_valid": schema_valid,
        "save_bom_output_schema": save_schema,
        "save_most_output_exists": save_most_schema is not None,
        "save_most_output_schema_valid": {
            "project_code",
            "product_id",
            "work_package_id",
            "trigger_run_id",
            "raw_json",
        }.issubset(set((save_most_schema or {}).get("required") or [])),
        "save_most_output_schema": save_most_schema,
        "authentication": {
            "server_auth_mode": os.getenv("MCP_AUTH_TYPE") or "not_configured",
            "secret_values_exposed": False,
        },
        "health_check": {
            "status": "ok",
            "mode": "registration_and_schema_only",
            "write_performed": False,
        },
    }
