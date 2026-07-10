import logging
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import anyio
import psycopg2
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from psycopg2.extras import Json, RealDictCursor
from starlette.requests import Request
from starlette.responses import JSONResponse

from services import agent_writeback_service

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("avocarbon-costing-mcp")

DATABASE_URL = os.getenv("DATABASE_URL")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "streamable-http")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", os.getenv("PORT", "8000")))

mcp = FastMCP(
    "AVOCarbon Costing MCP",
    host=MCP_HOST,
    port=MCP_PORT,
    sse_path="/sse",
    message_path="/messages/",
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
)


@contextmanager
def db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is required.")

    conn = None
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
            connect_timeout=10,
            sslmode=os.getenv("PGSSLMODE", "require"),
        )
        yield conn
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def success(data: Any = None, message: Optional[str] = None, **extra):
    result = {"success": True}
    if message:
        result["message"] = message
    if data is not None:
        result["data"] = json_safe(data)
    result.update({k: json_safe(v) for k, v in extra.items()})
    return result


def error(message: str, exc: Optional[Exception] = None):
    result = {"success": False, "error": message}
    if exc:
        result["details"] = str(exc)
    return result


def _save_agent_json_traceability(
    project_code: str,
    product_id: str,
    output_type: str,
    object_id: str,
    agent_name: str,
    status: str,
    raw_json: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Best-effort traceability write into agent_json_records when that table exists.

    The table has had a few shapes during this prototype, so this helper maps the
    requested choke write-back metadata onto whichever compatible columns exist.
    Traceability failures never block the workflow write-back.
    """
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'agent_json_records'
                    """
                )
                columns = {row["column_name"] for row in cur.fetchall()}
                if not columns:
                    return {
                        "status": "skipped",
                        "reason": "agent_json_records table is not available",
                    }

                metadata_payload = {
                    "project_code": project_code,
                    "product_id": product_id,
                    "output_type": output_type,
                    "object_id": object_id,
                    "agent_name": agent_name,
                    "status": status,
                    "raw_json": raw_json,
                }
                values_by_column = {
                    "project_code": project_code,
                    "product_id": product_id,
                    "product_reference": product_id,
                    "output_type": output_type,
                    "json_type": output_type,
                    "object_id": object_id,
                    "source_agent": agent_name,
                    "agent_name": agent_name,
                    "validation_status": status,
                    "status": status,
                    "payload": Json(metadata_payload),
                    "raw_json": Json(raw_json),
                }
                insert_columns = [
                    column
                    for column in values_by_column
                    if column in columns
                ]
                if "project_code" not in insert_columns:
                    return {
                        "status": "skipped",
                        "reason": "agent_json_records has no project_code column",
                    }
                placeholders = ", ".join(["%s"] * len(insert_columns))
                column_sql = ", ".join(insert_columns)
                values = [values_by_column[column] for column in insert_columns]
                cur.execute(
                    f"""
                    INSERT INTO agent_json_records ({column_sql})
                    VALUES ({placeholders})
                    RETURNING *
                    """,
                    values,
                )
                row = cur.fetchone()
        return {
            "status": "saved",
            "table": "agent_json_records",
            "record": json_safe(dict(row)) if row else None,
        }
    except Exception as exc:
        logger.warning("agent_json_records traceability save skipped: %s", exc)
        return {
            "status": "skipped",
            "reason": str(exc),
        }


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def root_info(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "AVOCarbon Costing MCP",
        "message": "Use /mcp from ChatGPT or another MCP client.",
        "mcp_endpoint": "/mcp",
        "health_endpoint": "/health",
        "transport": MCP_TRANSPORT,
        "registered_tools": len(mcp._tool_manager._tools),
    })


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "AVOCarbon Costing MCP",
        "database_configured": bool(DATABASE_URL),
        "transport": MCP_TRANSPORT,
        "registered_tools": len(mcp._tool_manager._tools),
    })


@mcp.tool()
def list_database_tables() -> Dict[str, Any]:
    """List tables in the costing PostgreSQL database."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
                rows = [dict(r) for r in cur.fetchall()]
        return success(rows, count=len(rows))
    except Exception as exc:
        logger.exception("list_database_tables failed")
        return error("Failed to list database tables.", exc)


@mcp.tool()
def get_project_context(project_id: int) -> Dict[str, Any]:
    """Get project, customer, zone, products, documents, BOM, components and offers."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*, c.customer_name, z.zone_name AS delivery_zone
                    FROM projects p
                    JOIN customers c ON c.customer_id = p.customer_id
                    JOIN zones z ON z.zone_id = p.delivery_zone_id
                    WHERE p.project_id = %s
                """, (project_id,))
                project = cur.fetchone()

                if not project:
                    return error(f"Project {project_id} not found.")

                cur.execute("""
                    SELECT pp.*, pr.product_name, pl.product_line_name
                    FROM project_products pp
                    JOIN products pr ON pr.product_id = pp.product_id
                    JOIN product_lines pl ON pl.product_line_id = pr.product_line_id
                    WHERE pp.project_id = %s
                    ORDER BY pp.project_product_id
                """, (project_id,))
                products = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT *
                    FROM documents
                    WHERE related_entity_type = 'project'
                      AND related_entity_id = %s
                    ORDER BY uploaded_at DESC
                """, (project_id,))
                documents = [dict(r) for r in cur.fetchall()]

                project_product_ids = [p["project_product_id"] for p in products]
                bom_lines = []
                router_operations = []

                if project_product_ids:
                    cur.execute("""
                        SELECT
                            b.*,
                            c.component_code,
                            c.component_description,
                            c.technology,
                            c.total_weight_grams
                        FROM bill_of_materials b
                        JOIN components c ON c.component_id = b.component_id
                        WHERE b.project_product_id = ANY(%s)
                        ORDER BY b.project_product_id, b.bom_id
                    """, (project_product_ids,))
                    bom_lines = [dict(r) for r in cur.fetchall()]

                    cur.execute("""
                        SELECT *
                        FROM router_operations
                        WHERE project_product_id = ANY(%s)
                        ORDER BY project_product_id, operation_number
                    """, (project_product_ids,))
                    router_operations = [dict(r) for r in cur.fetchall()]

        return success({
            "project": dict(project),
            "products": products,
            "documents": documents,
            "bom_lines": bom_lines,
            "router_operations": router_operations,
        })
    except Exception as exc:
        logger.exception("get_project_context failed")
        return error("Failed to get project context.", exc)


@mcp.tool()
def get_project_product_bom(project_product_id: int) -> Dict[str, Any]:
    """Get BOM for one project product."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        b.bom_id,
                        b.project_product_id,
                        b.component_id,
                        c.component_code,
                        c.component_description,
                        c.technology,
                        c.total_weight_grams,
                        b.quantity_per_product
                    FROM bill_of_materials b
                    JOIN components c ON c.component_id = b.component_id
                    WHERE b.project_product_id = %s
                    ORDER BY b.bom_id
                """, (project_product_id,))
                rows = [dict(r) for r in cur.fetchall()]
        return success(rows, count=len(rows))
    except Exception as exc:
        logger.exception("get_project_product_bom failed")
        return error("Failed to get project product BOM.", exc)


@mcp.tool()
def search_material_prices(
    material_family: Optional[str] = None,
    generic_material: Optional[str] = None,
    trade_name: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Search internal material price references."""
    if limit < 1 or limit > 200:
        return error("limit must be between 1 and 200.")

    try:
        conditions = []
        params = []

        if material_family:
            conditions.append("material_family ILIKE %s")
            params.append(f"%{material_family}%")

        if generic_material:
            conditions.append("generic_material ILIKE %s")
            params.append(f"%{generic_material}%")

        if trade_name:
            conditions.append("trade_name ILIKE %s")
            params.append(f"%{trade_name}%")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT *
                    FROM material_price_references
                    {where}
                    ORDER BY material_family, generic_material, trade_name
                    LIMIT %s
                """, params + [limit])
                rows = [dict(r) for r in cur.fetchall()]

        return success(rows, count=len(rows))
    except Exception as exc:
        logger.exception("search_material_prices failed")
        return error("Failed to search material prices.", exc)


@mcp.tool()
def create_or_update_component(
    component_code: Optional[str],
    component_description: str,
    technology: str,
    total_weight_grams: Optional[float] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a component by component_code if provided."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                existing = None

                if component_code:
                    cur.execute("""
                        SELECT *
                        FROM components
                        WHERE component_code = %s
                        LIMIT 1
                    """, (component_code,))
                    existing = cur.fetchone()

                if existing:
                    cur.execute("""
                        UPDATE components
                        SET component_description = %s,
                            technology = %s,
                            total_weight_grams = %s,
                            status = COALESCE(%s, status)
                        WHERE component_id = %s
                        RETURNING *
                    """, (
                        component_description,
                        technology,
                        total_weight_grams,
                        status,
                        existing["component_id"],
                    ))
                    row = dict(cur.fetchone())
                    return success(row, "Component updated.", action="updated")

                cur.execute("""
                    INSERT INTO components
                    (component_code, component_description, technology, total_weight_grams, status)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                """, (
                    component_code,
                    component_description,
                    technology,
                    total_weight_grams,
                    status,
                ))
                row = dict(cur.fetchone())

        return success(row, "Component created.", action="created")
    except Exception as exc:
        logger.exception("create_or_update_component failed")
        return error("Failed to create or update component.", exc)


@mcp.tool()
def create_or_update_bom_line(
    project_product_id: int,
    component_id: int,
    quantity_per_product: float,
) -> Dict[str, Any]:
    """Create or update BOM line for a project product/component pair."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM bill_of_materials
                    WHERE project_product_id = %s
                      AND component_id = %s
                    LIMIT 1
                """, (project_product_id, component_id))
                existing = cur.fetchone()

                if existing:
                    cur.execute("""
                        UPDATE bill_of_materials
                        SET quantity_per_product = %s
                        WHERE bom_id = %s
                        RETURNING *
                    """, (quantity_per_product, existing["bom_id"]))
                    row = dict(cur.fetchone())
                    return success(row, "BOM line updated.", action="updated")

                cur.execute("""
                    INSERT INTO bill_of_materials
                    (project_product_id, component_id, quantity_per_product)
                    VALUES (%s, %s, %s)
                    RETURNING *
                """, (project_product_id, component_id, quantity_per_product))
                row = dict(cur.fetchone())

        return success(row, "BOM line created.", action="created")
    except Exception as exc:
        logger.exception("create_or_update_bom_line failed")
        return error("Failed to create or update BOM line.", exc)


@mcp.tool()
def get_component_offers(component_id: int) -> Dict[str, Any]:
    """Get supplier offers for one component."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        o.*,
                        s.supplier_name,
                        s.supplier_country,
                        s.is_internal_group,
                        c.currency_code AS purchasing_currency_code
                    FROM component_offers o
                    JOIN suppliers s ON s.supplier_id = o.supplier_id
                    JOIN currencies c ON c.currency_id = o.purchasing_currency_id
                    WHERE o.component_id = %s
                    ORDER BY o.created_at DESC
                """, (component_id,))
                rows = [dict(r) for r in cur.fetchall()]
        return success(rows, count=len(rows))
    except Exception as exc:
        logger.exception("get_component_offers failed")
        return error("Failed to get component offers.", exc)


@mcp.tool()
def update_project_status(project_id: int, status: str) -> Dict[str, Any]:
    """Update project status."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE projects
                    SET status = %s
                    WHERE project_id = %s
                    RETURNING *
                """, (status, project_id))
                row = cur.fetchone()

        if not row:
            return error(f"Project {project_id} not found.")

        return success(dict(row), "Project status updated.")
    except Exception as exc:
        logger.exception("update_project_status failed")
        return error("Failed to update project status.", exc)


class _GetMcpInterceptMiddleware:
    def __init__(self, app, mcp_path: str = "/mcp") -> None:
        self._app = app
        self._mcp_path = mcp_path

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == self._mcp_path
            and not scope.get("query_string", b"")
        ):
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "result": {
                        "status": "ok",
                        "message": "MCP server ready. POST to /mcp.",
                    },
                    "id": None,
                },
                status_code=200,
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


async def _run_http_async() -> None:
    import uvicorn

    app = mcp.streamable_http_app()
    patched = _GetMcpInterceptMiddleware(app, mcp_path="/mcp")

    config = uvicorn.Config(
        patched,
        host=MCP_HOST,
        port=MCP_PORT,
        log_level="info",
    )

    await uvicorn.Server(config).serve()

@mcp.tool()
def find_project_product(project_code: str, product_reference: str):
    """Find project_product_id from project name/RFQ number and product/customer reference."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        p.project_id,
                        p.project_name,
                        pp.project_product_id,
                        pr.product_name,
                        pp.customer_part_number
                    FROM projects p
                    JOIN project_products pp ON pp.project_id = p.project_id
                    JOIN products pr ON pr.product_id = pp.product_id
                    WHERE p.project_name ILIKE %s
                      AND (
                        pp.customer_part_number ILIKE %s
                        OR pr.product_name ILIKE %s
                      )
                    LIMIT 10
                """, (
                    f"%{project_code}%",
                    f"%{product_reference}%",
                    f"%{product_reference}%"
                ))
                rows = [dict(r) for r in cur.fetchall()]

        return success(rows, count=len(rows))
    except Exception as exc:
        logger.exception("find_project_product failed")
        return error("Failed to find project product.", exc)


@mcp.tool()
def save_bom_output(
    project_code: str,
    product_id: str,
    raw_json: Dict[str, Any],
) -> Dict[str, Any]:
    """
    The agent must call this tool at the end of its analysis. The backend workflow will not continue until this tool is called.

    Save the final Choke BOM Analyzer JSON to the backend workflow. This is equivalent
    to POST /api/choke-workflow/save-bom-output.
    """
    try:
        from services.choke_sequential_agent_workflow import save_bom_output as workflow_save_bom_output

        workflow_response = workflow_save_bom_output(
            project_code=project_code,
            product_id=product_id,
            raw_json=raw_json,
        )
        traceability = _save_agent_json_traceability(
            project_code=project_code,
            product_id=product_id,
            output_type="bom",
            object_id="bom",
            agent_name="choke_bom_agent",
            status="received",
            raw_json=raw_json,
        )
        if isinstance(workflow_response, dict):
            workflow_response["traceability"] = traceability
        return workflow_response
    except Exception as exc:
        logger.exception("save_bom_output failed")
        return error("Failed to save BOM output.", exc)


@mcp.tool()
def save_component_output(
    project_code: str,
    product_id: str,
    component_id: str,
    raw_json: Dict[str, Any],
) -> Dict[str, Any]:
    """
    The agent must call this tool at the end of its analysis. The backend workflow will not continue until this tool is called.

    Save one final External Component Costing Agent JSON to the backend workflow. This
    is equivalent to POST /api/choke-workflow/save-component-output.
    """
    try:
        from services.choke_sequential_agent_workflow import save_component_output as workflow_save_component_output

        workflow_response = workflow_save_component_output(
            project_code=project_code,
            product_id=product_id,
            component_id=component_id,
            raw_json=raw_json,
        )
        traceability = _save_agent_json_traceability(
            project_code=project_code,
            product_id=product_id,
            output_type="component",
            object_id=component_id,
            agent_name="external_component_costing_agent",
            status="received",
            raw_json=raw_json,
        )
        if isinstance(workflow_response, dict):
            workflow_response["traceability"] = traceability
        return workflow_response
    except Exception as exc:
        logger.exception("save_component_output failed")
        return error("Failed to save component output.", exc)


@mcp.tool()
def save_most_output(
    project_code: str,
    product_id: str,
    raw_json: Dict[str, Any],
    most_scope_id: Optional[str] = None,
    work_package_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    The agent must call this tool at the end of its analysis. The backend workflow will not continue until this tool is called.

    Save one final MOST operation JSON to the backend workflow. This is equivalent to
    POST /api/choke-workflow/save-most-output.
    """
    try:
        scope_id = most_scope_id or work_package_id
        if not scope_id:
            return error("most_scope_id or work_package_id is required.")
        from services.choke_sequential_agent_workflow import save_most_output as workflow_save_most_output

        workflow_response = workflow_save_most_output(
            project_code=project_code,
            product_id=product_id,
            work_package_id=scope_id,
            raw_json=raw_json,
        )
        traceability = _save_agent_json_traceability(
            project_code=project_code,
            product_id=product_id,
            output_type="most",
            object_id=scope_id,
            agent_name="most_assemblage_agent",
            status="received",
            raw_json=raw_json,
        )
        if isinstance(workflow_response, dict):
            workflow_response["traceability"] = traceability
        return workflow_response
    except Exception as exc:
        logger.exception("save_most_output failed")
        return error("Failed to save MOST output.", exc)


@mcp.tool()
def get_choke_workflow_status(project_code: str, product_id: str) -> Dict[str, Any]:
    """
    Read the Choke workflow status after agent write-back.

    This is equivalent to GET /api/choke-workflow/status/{project_code}/{product_id}.
    """
    try:
        from services.choke_sequential_agent_workflow import get_workflow_state

        return get_workflow_state(project_code=project_code, product_id=product_id)
    except Exception as exc:
        logger.exception("get_choke_workflow_status failed")
        return error("Failed to get workflow status.", exc)


@mcp.tool()
def get_workflow_status(project_code: str, product_id: str) -> Dict[str, Any]:
    """Backward-compatible alias for get_choke_workflow_status."""
    return get_choke_workflow_status(project_code=project_code, product_id=product_id)


@mcp.tool()
def calculate_choke_from_saved_outputs(project_code: str, product_id: str) -> Dict[str, Any]:
    """
    Calculate final choke result from saved individual BOM, component and MOST outputs.

    Uses the backend calculation function behind /api/choke-workflow/calculate-from-real-outputs,
    including Olivier transport, direct cost, FOH, FEE and manufacturing-cost formulas.
    """
    try:
        from services.choke_sequential_agent_workflow import calculate_final_choke_costing_from_saved_outputs

        return calculate_final_choke_costing_from_saved_outputs(project_code=project_code, product_id=product_id)
    except Exception as exc:
        logger.exception("calculate_choke_from_saved_outputs failed")
        return error("Failed to calculate choke from saved outputs.", exc)


@mcp.tool()
def import_agent_costing_package(payload: dict):
    """
    Import agent-generated costing JSON into PostgreSQL.
    Creates project, product, project_product, components, BOM and routing operations.
    """
    try:
        project = payload.get("project", {})
        product = payload.get("project_product", {})
        components = payload.get("components", [])
        bom_lines = payload.get("bill_of_materials", [])
        operations = payload.get("router_operations", [])

        project_code = project.get("project_code")
        customer_name = project.get("customer")
        product_name = product.get("product_name")
        customer_reference = product.get("customer_reference")

        if not project_code:
            return error("project.project_code is required.")
        if not customer_name:
            return error("project.customer is required.")
        if not product_name:
            return error("project_product.product_name is required.")

        with db_connection() as conn:
            with conn.cursor() as cur:

                # 1. Customer
                cur.execute("""
                    SELECT customer_id FROM customers
                    WHERE customer_name ILIKE %s
                    LIMIT 1
                """, (customer_name,))
                row = cur.fetchone()

                if row:
                    customer_id = row["customer_id"]
                else:
                    cur.execute("""
                        INSERT INTO customers (customer_name)
                        VALUES (%s)
                        RETURNING customer_id
                    """, (customer_name,))
                    customer_id = cur.fetchone()["customer_id"]
                # Delivery zone
                delivery_zone_name = project.get("delivery_zone") or "Europe"

                cur.execute("""
                    SELECT zone_id
                    FROM zones
                    WHERE zone_name ILIKE %s
                    LIMIT 1
                """, (delivery_zone_name,))
                row = cur.fetchone()

                if row:
                    delivery_zone_id = row["zone_id"]
                else:
                    cur.execute("""
                        SELECT zone_id
                        FROM zones
                        ORDER BY zone_id
                        LIMIT 1
                    """)
                    row = cur.fetchone()

                    if not row:
                        return error("No delivery zone found in zones table.")

                    delivery_zone_id = row["zone_id"]
                # 2. Project
                cur.execute("""
                    SELECT project_id FROM projects
                    WHERE project_name ILIKE %s
                    LIMIT 1
                """, (project_code,))
                row = cur.fetchone()

                if row:
                    project_id = row["project_id"]
                else:
                    cur.execute("""
                        INSERT INTO projects (project_name, customer_id, delivery_zone_id, status)
                        VALUES (%s, %s, %s, %s)
                        RETURNING project_id
                    """, (project_code, customer_id, delivery_zone_id, "created_from_agent"))
                    project_id = cur.fetchone()["project_id"]

                # 3. Product line fallback
                cur.execute("""
                    SELECT product_line_id FROM product_lines
                    ORDER BY product_line_id
                    LIMIT 1
                """)
                product_line_id = cur.fetchone()["product_line_id"]

                # 4. Product
                cur.execute("""
                    SELECT product_id FROM products
                    WHERE product_name ILIKE %s
                    LIMIT 1
                """, (product_name,))
                row = cur.fetchone()

                if row:
                    product_id = row["product_id"]
                else:
                    cur.execute("""
                        INSERT INTO products (product_name, product_line_id, roce_target_percent)
                        VALUES (%s, %s, %s)
                        RETURNING product_id
                    """, (product_name, product_line_id, 0))
                    product_id = cur.fetchone()["product_id"]

                # 5. Project product
                cur.execute("""
                    SELECT project_product_id FROM project_products
                    WHERE project_id = %s
                      AND product_id = %s
                    LIMIT 1
                """, (project_id, product_id))
                row = cur.fetchone()

                if row:
                    project_product_id = row["project_product_id"]
                else:
                    cur.execute("""
                        INSERT INTO project_products
                        (project_id, product_id, customer_part_number, annual_volume)
                        VALUES (%s, %s, %s, %s)
                        RETURNING project_product_id
                    """, (
                        project_id,
                        product_id,
                        customer_reference,
                        project.get("annual_quantity")
                    ))
                    project_product_id = cur.fetchone()["project_product_id"]

                component_id_by_code = {}

                # 6. Components
                for component in components:
                    code = component.get("component_code")
                    description = component.get("component_name") or component.get("component_description")
                    technology = component.get("component_family") or component.get("component_type")
                    weight = component.get("net_weight")

                    if not code:
                        continue

                    cur.execute("""
                        SELECT component_id FROM components
                        WHERE component_code = %s
                        LIMIT 1
                    """, (code,))
                    row = cur.fetchone()

                    if row:
                        component_id = row["component_id"]
                        cur.execute("""
                            UPDATE components
                            SET component_description = COALESCE(%s, component_description),
                                technology = COALESCE(%s, technology),
                                total_weight_grams = COALESCE(%s, total_weight_grams)
                            WHERE component_id = %s
                        """, (
                            description,
                            technology,
                            float(weight) * 1000 if weight else None,
                            component_id
                        ))
                    else:
                        cur.execute("""
                            INSERT INTO components
                            (component_code, component_description, technology, total_weight_grams, status)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING component_id
                        """, (
                            code,
                            description,
                            technology,
                            float(weight) * 1000 if weight else None,
                            "created_from_agent"
                        ))
                        component_id = cur.fetchone()["component_id"]

                    component_id_by_code[code] = component_id

                # 7. BOM
                for bom in bom_lines:
                    code = bom.get("component_code")
                    qty = bom.get("quantity_per_parent")

                    component_id = component_id_by_code.get(code)
                    if not component_id:
                        continue

                    cur.execute("""
                        SELECT bom_id FROM bill_of_materials
                        WHERE project_product_id = %s
                          AND component_id = %s
                        LIMIT 1
                    """, (project_product_id, component_id))
                    row = cur.fetchone()

                    if row:
                        cur.execute("""
                            UPDATE bill_of_materials
                            SET quantity_per_product = %s
                            WHERE bom_id = %s
                        """, (qty, row["bom_id"]))
                    else:
                        cur.execute("""
                            INSERT INTO bill_of_materials
                            (project_product_id, component_id, quantity_per_product)
                            VALUES (%s, %s, %s)
                        """, (project_product_id, component_id, qty))

                # 8. Routing operations
                for op in operations:
                    cur.execute("""
                        SELECT router_operation_id FROM router_operations
                        WHERE project_product_id = %s
                          AND operation_number = %s
                        LIMIT 1
                    """, (project_product_id, op.get("operation_sequence")))
                    row = cur.fetchone()

                    if row:
                        cur.execute("""
                            UPDATE router_operations
                            SET operation_description = %s,
                                cycle_time_seconds = %s,
                                gross_strokes_per_hour = %s,
                                pieces_per_stroke = %s,
                                generic_capex = %s,
                                specific_capex = %s,
                                tooling_cost = %s
                            WHERE router_operation_id = %s
                        """, (
                            op.get("description"),
                            op.get("cycle_time_seconds"),
                            op.get("gross_strokes_per_hour"),
                            op.get("pieces_per_stroke"),
                            op.get("generic_capex"),
                            op.get("specific_capex"),
                            op.get("tooling_cost"),
                            row["router_operation_id"]
                        ))
                    else:
                        cur.execute("""
                            INSERT INTO router_operations
                            (
                                project_product_id,
                                operation_number,
                                operation_description,
                                cycle_time_seconds,
                                gross_strokes_per_hour,
                                pieces_per_stroke,
                                generic_capex,
                                specific_capex,
                                tooling_cost
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            project_product_id,
                            op.get("operation_sequence"),
                            op.get("description"),
                            op.get("cycle_time_seconds"),
                            op.get("gross_strokes_per_hour"),
                            op.get("pieces_per_stroke"),
                            op.get("generic_capex"),
                            op.get("specific_capex"),
                            op.get("tooling_cost")
                        ))

        return success({
            "project_id": project_id,
            "project_product_id": project_product_id,
            "components_processed": len(component_id_by_code),
            "bom_lines_processed": len(bom_lines),
            "routing_operations_processed": len(operations)
        }, "Agent costing package imported.")

    except Exception as exc:
        logger.exception("import_agent_costing_package failed")
        return error("Failed to import agent costing package.", exc)
@mcp.tool()
def save_choke_bom_result(
    project_code: str,
    product_id: str,
    agent_name: str,
    raw_json: Dict[str, Any],
    save_to_database: bool = False,
) -> Dict[str, Any]:
    """
    Use this tool to save your final JSON output. Always call this tool before finishing.

    Saves the final Choke BOM Analyzer JSON to the backend costing run folder and updates
    the write-back status file.
    """
    try:
        return agent_writeback_service.save_choke_bom_result(
            project_code=project_code,
            product_id=product_id,
            agent_name=agent_name,
            raw_json=raw_json,
            save_to_database=save_to_database,
        )
    except Exception as exc:
        logger.exception("save_choke_bom_result failed")
        return error("Failed to save choke BOM result.", exc)


@mcp.tool()
def save_component_costing_result(
    project_code: str,
    product_id: str,
    component_id: str,
    component_type: str,
    agent_name: str,
    raw_json: Dict[str, Any],
    save_to_database: bool = False,
) -> Dict[str, Any]:
    """
    Use this tool to save your final JSON output. Always call this tool before finishing.

    Saves one External Component Costing Agent JSON output to the backend costing run
    folder and updates the write-back status file.
    """
    try:
        return agent_writeback_service.save_component_costing_result(
            project_code=project_code,
            product_id=product_id,
            component_id=component_id,
            component_type=component_type,
            agent_name=agent_name,
            raw_json=raw_json,
            save_to_database=save_to_database,
        )
    except Exception as exc:
        logger.exception("save_component_costing_result failed")
        return error("Failed to save component costing result.", exc)


@mcp.tool()
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
    """
    Use this tool to save your final JSON output. Always call this tool before finishing.

    Saves one MOST operation JSON output to the backend costing run folder and updates
    the write-back status file.
    """
    try:
        return agent_writeback_service.save_most_operation_result(
            project_code=project_code,
            product_id=product_id,
            work_package_id=work_package_id,
            component_id=component_id,
            operation_id=operation_id,
            operation_name=operation_name,
            agent_name=agent_name,
            raw_json=raw_json,
            save_to_database=save_to_database,
        )
    except Exception as exc:
        logger.exception("save_most_operation_result failed")
        return error("Failed to save MOST operation result.", exc)


@mcp.tool()
def get_costing_run_status(project_code: str, product_id: str) -> Dict[str, Any]:
    """Read the saved agent-output status for one Choke costing run."""
    try:
        return agent_writeback_service.get_costing_run_status(
            project_code=project_code,
            product_id=product_id,
        )
    except Exception as exc:
        logger.exception("get_costing_run_status failed")
        return error("Failed to get costing run status.", exc)


@mcp.tool()
def calculate_choke_from_saved_agent_outputs(
    project_code: str,
    product_id: str,
    input_file: str,
) -> Dict[str, Any]:
    """Calculate Choke preliminary costing from JSON files saved by write-back tools."""
    try:
        return agent_writeback_service.calculate_choke_from_saved_agent_outputs(
            project_code=project_code,
            product_id=product_id,
            input_file=input_file,
        )
    except Exception as exc:
        logger.exception("calculate_choke_from_saved_agent_outputs failed")
        return error("Failed to calculate from saved agent outputs.", exc)


@mcp.tool()
def store_agent_json(
    project_code: str,
    json_type: str,
    payload: dict,
    product_reference: str = None,
    source_agent: str = None,
    validation_status: str = "draft"
):
    """
    Store an agent-generated JSON payload in PostgreSQL.

    json_type examples:
    - project_validation
    - component_json
    - bom_json
    - router_operation_json
    - costing_json
    """
    try:
        if not project_code:
            return error("project_code is required.")
        if not json_type:
            return error("json_type is required.")
        if not payload:
            return error("payload is required.")

        import json

        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS agent_json_records (
                        agent_json_record_id SERIAL PRIMARY KEY,
                        project_code TEXT NOT NULL,
                        product_reference TEXT NULL,
                        json_type TEXT NOT NULL,
                        source_agent TEXT NULL,
                        validation_status TEXT NULL,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMP DEFAULT NOW(),
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)

                cur.execute("""
                    INSERT INTO agent_json_records
                    (
                        project_code,
                        product_reference,
                        json_type,
                        source_agent,
                        validation_status,
                        payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING agent_json_record_id
                """, (
                    project_code,
                    product_reference,
                    json_type,
                    source_agent,
                    validation_status,
                    json.dumps(payload)
                ))

                record_id = cur.fetchone()["agent_json_record_id"]

        return success({
            "agent_json_record_id": record_id,
            "project_code": project_code,
            "product_reference": product_reference,
            "json_type": json_type,
            "validation_status": validation_status
        }, "Agent JSON stored successfully.")

    except Exception as exc:
        logger.exception("store_agent_json failed")
        return error("Failed to store agent JSON.", exc)
if __name__ == "__main__":
    logger.info("=" * 48)
    logger.info("Starting AVOCarbon Costing MCP")
    logger.info("Python    = %s", sys.version)
    logger.info("TRANSPORT = %s", MCP_TRANSPORT)
    logger.info("HOST      = %s", MCP_HOST)
    logger.info("PORT      = %s", MCP_PORT)
    logger.info("=" * 48)

    if MCP_TRANSPORT == "streamable-http":
        anyio.run(_run_http_async)
    else:
        mcp.run(transport=MCP_TRANSPORT)
