import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


BASE_DIR = Path(__file__).resolve().parents[1]

DEFAULT_COMMERCIAL_PARAMETERS: Dict[str, Any] = {
    "tooling_margin_percent": 10,
    "specific_investment_margin_percent": 10,
    "lifetime_warranty_tooling_adder": 0,
    "productivity_y1_percent": 2,
    "productivity_y2_percent": 2,
    "productivity_y3_percent": 2,
    "incoterm": "FCA",
    "payment_terms": "60 days end of month the 10th",
}

COMMERCIAL_COSTING_PARAMETER_DDL = """
CREATE TABLE IF NOT EXISTS commercial_costing_parameter (
    id SERIAL PRIMARY KEY,
    project_code TEXT NULL,
    product_id TEXT NULL,
    quantity NUMERIC NULL,
    sop_date DATE NULL,
    delivery_zone TEXT NULL,
    tooling_payment_mode TEXT NULL,
    tooling_payment_at_order_percent NUMERIC NULL,
    tooling_payment_off_tool_percent NUMERIC NULL,
    tooling_payment_ppap_percent NUMERIC NULL,
    tooling_depreciation_pieces NUMERIC NULL,
    specific_capex_depreciation_pieces NUMERIC NULL,
    tooling_margin_percent NUMERIC DEFAULT 10,
    specific_investment_margin_percent NUMERIC DEFAULT 10,
    lifetime_warranty_tooling_adder NUMERIC DEFAULT 0,
    productivity_scope TEXT NULL,
    productivity_y1_percent NUMERIC DEFAULT 2,
    productivity_y2_percent NUMERIC DEFAULT 2,
    productivity_y3_percent NUMERIC DEFAULT 2,
    plant_indexation_enabled BOOLEAN NULL,
    business_link_percent NUMERIC NULL,
    delivery_frequency TEXT NULL,
    delivery_on_platform BOOLEAN NULL,
    incoterm TEXT DEFAULT 'FCA',
    payment_terms TEXT DEFAULT '60 days end of month the 10th',
    factoring_capability BOOLEAN NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
"""


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


def get_default_commercial_parameters(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = {
        **DEFAULT_COMMERCIAL_PARAMETERS,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in (overrides or {}).items():
        if value not in [None, ""]:
            payload[key] = value
    return payload


def ensure_commercial_costing_parameter_table(database_url: Optional[str] = None) -> Dict[str, Any]:
    _load_env()
    database_url = database_url or os.getenv("DATABASE_URL")
    if not database_url:
        return {
            "status": "blocked",
            "message": "DATABASE_URL is not configured.",
            "table": "commercial_costing_parameter",
        }

    try:
        from sqlalchemy import create_engine, text
    except Exception as exc:
        return {
            "status": "blocked",
            "message": f"SQLAlchemy unavailable: {exc}",
            "table": "commercial_costing_parameter",
        }

    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.begin() as connection:
            connection.execute(text(COMMERCIAL_COSTING_PARAMETER_DDL))
    except Exception as exc:
        return {
            "status": "failed",
            "message": str(exc),
            "table": "commercial_costing_parameter",
        }

    return {
        "status": "ready",
        "message": "commercial_costing_parameter table exists or was created.",
        "table": "commercial_costing_parameter",
    }
