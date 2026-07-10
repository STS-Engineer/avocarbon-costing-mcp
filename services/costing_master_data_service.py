import os
import re
from decimal import Decimal
from pathlib import Path

from services.manufacturing_strategy import select_manufacturing_strategy
from services.unit_table_service import get_unit_data


BASE_DIR = Path(__file__).resolve().parents[1]


def _load_env():
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


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _normalize_match(value):
    text = str(value or "").lower().replace("/", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = []
    for token in text.split():
        if token.endswith("ies") and len(token) > 4:
            token = f"{token[:-3]}y"
        elif token.endswith("s") and len(token) > 3:
            token = token[:-1]
        tokens.append(token)
    return " ".join(tokens)


def _json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_dict(row):
    return {key: _json_safe(value) for key, value in dict(row).items()}


def _get_engine(env_name, label):
    _load_env()
    database_url = os.getenv(env_name)
    if not database_url:
        return None, f"{env_name} is not configured"
    try:
        from sqlalchemy import create_engine

        return create_engine(database_url, pool_pre_ping=True), None
    except Exception as exc:
        return None, f"SQLAlchemy unavailable or {label} engine creation failed: {exc}"


def _get_master_engine():
    return _get_engine("KPI_DB_FINAL_URL", "KPI_DB_Final")


def get_master_connection_mode():
    _load_env()
    master_database = (
        "KPI_DB_FINAL_URL"
        if os.getenv("KPI_DB_FINAL_URL")
        else "unavailable"
    )
    return {
        "backend_database": "DATABASE_URL",
        "master_database": master_database,
        "mcp_note": (
            "MCP 21 06 26 targets KPI_DB_Final but is not directly accessible "
            "from Python unless an MCP client or KPI_DB_FINAL_URL is configured."
        ),
    }


def _execute_mappings(conn, sql, params=None):
    from sqlalchemy import text

    return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]


def _execute_scalar(conn, sql, params=None):
    from sqlalchemy import text

    return conn.execute(text(sql), params or {}).scalar()


def _table_exists(conn, table_name):
    return bool(_execute_scalar(
        conn,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = :table_name
        )
        """,
        {"table_name": table_name},
    ))


def _columns(conn, table_name):
    rows = _execute_mappings(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name
        ORDER BY ordinal_position
        """,
        {"table_name": table_name},
    )
    return [row["column_name"] for row in rows]


def _first_available(row, candidates):
    for candidate in candidates:
        if row.get(candidate) not in [None, ""]:
            return row.get(candidate)
    return None


def get_product_catalog_from_db():
    engine, error = _get_master_engine()
    if not engine:
        return {"status": "not_available", "source": "KPI_DB_Final", "message": error}

    try:
        with engine.connect() as conn:
            missing_tables = [
                table for table in ["product_line", "product"] if not _table_exists(conn, table)
            ]
            if missing_tables:
                return {
                    "status": "not_available",
                    "source": "KPI_DB_Final",
                    "missing_tables": missing_tables,
                }

            product_lines = [_row_dict(row) for row in _execute_mappings(
                conn,
                "SELECT * FROM public.product_line ORDER BY 1",
            )]
            products = [_row_dict(row) for row in _execute_mappings(
                conn,
                "SELECT * FROM public.product ORDER BY 1",
            )]
            return {
                "status": "found",
                "source": "KPI_DB_Final.product_line/product",
                "product_lines": product_lines,
                "products": products,
            }
    except Exception as exc:
        return {
            "status": "not_available",
            "source": "KPI_DB_Final",
            "message": f"Product catalog DB lookup failed: {exc}",
        }


def get_unit_data_from_db(production_plant):
    engine, error = _get_master_engine()
    if not engine:
        return {"status": "not_available", "source": "KPI_DB_Final.unit", "message": error}
    if not str(production_plant or "").strip():
        return {
            "status": "not_available",
            "source": "KPI_DB_Final.unit",
            "missing_inputs": ["production_plant"],
        }

    try:
        with engine.connect() as conn:
            if not _table_exists(conn, "unit"):
                return {
                    "status": "not_available",
                    "source": "KPI_DB_Final.unit",
                    "message": "public.unit table does not exist",
                }

            cols = _columns(conn, "unit")
            rows = [_row_dict(row) for row in _execute_mappings(conn, "SELECT * FROM public.unit")]
            target_keys = {_normalize(production_plant)}
            if _normalize(production_plant) == "same":
                target_keys.add("elfahs")
            if _normalize(production_plant) == "elfahs":
                target_keys.add("same")

            matched = None
            for row in rows:
                names = [
                    row.get("unit_name"),
                    row.get("unit_short_name"),
                    row.get("name"),
                    row.get("plant"),
                ]
                if any(_normalize(name) in target_keys for name in names if name):
                    matched = row
                    break

            if not matched:
                return {
                    "status": "not_available",
                    "source": "KPI_DB_Final.unit",
                    "message": f"No unit row matched {production_plant}",
                    "available_columns": cols,
                }

            unit_name = _first_available(matched, ["unit_name", "name", "plant"])
            unit_short_name = matched.get("unit_short_name")
            alias_used = None
            if _normalize(production_plant) not in {
                _normalize(unit_name),
                _normalize(unit_short_name),
            }:
                alias_used = production_plant

            data = {
                "status": "found",
                "source": "KPI_DB_Final.unit",
                "plant": unit_name,
                "plant_alias": alias_used,
                "unit_short_name": unit_short_name,
                "selling_currency": matched.get("selling_currency"),
                "operating_currency": matched.get("operating_currency"),
                "dl_rate_operating_per_hour": matched.get("direct_labor_cost_per_hour"),
                "voh_rate_operating_per_hour": matched.get("base_variable_overhead_cost_per_hour"),
                "foh_percent_dc": matched.get("foh_dc_percent"),
                "fee_percent_dc": matched.get("fee_dc_percent"),
                "company_tax_rate": matched.get("company_tax_percent"),
                "number_of_shifts": matched.get("number_of_shifts"),
                "open_hours_per_year": matched.get("open_hours_per_year"),
            }
            required = [
                "selling_currency",
                "operating_currency",
                "dl_rate_operating_per_hour",
                "voh_rate_operating_per_hour",
                "foh_percent_dc",
                "fee_percent_dc",
                "company_tax_rate",
                "open_hours_per_year",
            ]
            data["missing_inputs"] = [
                field_name for field_name in required if data.get(field_name) in [None, ""]
            ]
            return data
    except Exception as exc:
        return {
            "status": "not_available",
            "source": "KPI_DB_Final.unit",
            "message": f"Unit DB lookup failed: {exc}",
        }


def get_manufacturing_strategy_from_db(product_line, product, delivery_zone):
    engine, error = _get_master_engine()
    if not engine:
        return {
            "status": "not_available",
            "source": "KPI_DB_Final.manufacturing_strategy",
            "message": error,
        }

    try:
        with engine.connect() as conn:
            if not _table_exists(conn, "manufacturing_strategy"):
                return {
                    "status": "not_available",
                    "source": "KPI_DB_Final.manufacturing_strategy",
                    "message": "public.manufacturing_strategy table does not exist",
                }

            count = _execute_scalar(conn, "SELECT COUNT(*) FROM public.manufacturing_strategy")
            if not count:
                return {
                    "status": "empty",
                    "source": "KPI_DB_Final.manufacturing_strategy",
                    "message": "manufacturing_strategy table exists but has no rows; using CSV fallback",
                }

            rows = _execute_mappings(
                conn,
                """
                SELECT
                    ms.*,
                    p.product_name,
                    pl.product_line_name,
                    z.zone_name,
                    u.unit_name,
                    u.unit_short_name
                FROM public.manufacturing_strategy ms
                LEFT JOIN public.product p ON p.product_id = ms.product_id
                LEFT JOIN public.product_line pl ON pl.product_line_id = p.product_line_id
                LEFT JOIN public.zone z ON z.zone_id = ms.zone_id
                LEFT JOIN public.unit u ON u.unit_id = ms.unit_id
                """,
            )
            product_line_key = _normalize_match(product_line)
            product_key = _normalize_match(product)
            zone_key = _normalize_match(delivery_zone)
            for row in rows:
                if _normalize_match(row.get("product_line_name")) != product_line_key:
                    continue
                if _normalize_match(row.get("product_name")) != product_key:
                    continue
                if _normalize_match(row.get("zone_name")) != zone_key:
                    continue
                return {
                    "status": "found",
                    "source": "KPI_DB_Final.manufacturing_strategy",
                    "product_line": row.get("product_line_name"),
                    "product": row.get("product_name"),
                    "customer_delivery_zone": row.get("zone_name"),
                    "delivery_zone": row.get("zone_name"),
                    "production_plant": row.get("unit_name") or row.get("unit_short_name"),
                    "target_van_percent": row.get("target_van_percent") or row.get("van_percent"),
                    "raw_row": _row_dict(row),
                }

            return {
                "status": "not_found",
                "source": "KPI_DB_Final.manufacturing_strategy",
                "message": "No DB manufacturing strategy matched product/product line/delivery zone",
            }
    except Exception as exc:
        return {
            "status": "not_available",
            "source": "KPI_DB_Final.manufacturing_strategy",
            "message": f"Manufacturing strategy DB lookup failed: {exc}",
        }


def get_master_manufacturing_strategy(product_line, product, delivery_zone):
    db_result = get_manufacturing_strategy_from_db(product_line, product, delivery_zone)
    if db_result.get("status") == "found":
        return db_result

    csv_result = select_manufacturing_strategy(product_line, product, delivery_zone)
    csv_result = {
        **csv_result,
        "source": "csv.product_matrix",
        "database_strategy_status": db_result,
    }
    return csv_result


def get_master_unit_data(production_plant):
    db_result = get_unit_data_from_db(production_plant)
    if db_result.get("status") == "found":
        return db_result

    csv_result = get_unit_data(production_plant)
    csv_result = {
        **csv_result,
        "source": "csv.unit_table",
        "source_detail": csv_result.get("source"),
        "database_unit_status": db_result,
    }
    return csv_result
