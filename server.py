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
from psycopg2.extras import RealDictCursor
from starlette.requests import Request
from starlette.responses import JSONResponse

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