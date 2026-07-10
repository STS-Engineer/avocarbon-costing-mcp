import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

BACKEND_TABLES = [
    "manufacturing_strategy",
    "components",
    "component_offers",
    "bill_of_materials",
    "router_operations",
    "plants",
    "currencies",
    "customers",
    "calculation_runs",
    "documents",
    "costing_agent_outputs",
]


def load_env():
    env_path = ROOT_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except Exception:
        if not env_path.exists():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def try_sqlalchemy(database_url):
    try:
        from sqlalchemy import create_engine, text
    except Exception as exc:
        return None, None, f"SQLAlchemy unavailable: {exc}"
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        return engine, text, None
    except Exception as exc:
        return None, None, f"SQLAlchemy engine failed: {exc}"


def inspect_with_sqlalchemy(engine, text):
    with engine.connect() as conn:
        for table_name in BACKEND_TABLES:
            exists = bool(conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = :table_name
                    )
                    """
                ),
                {"table_name": table_name},
            ).scalar())
            if not exists:
                print(f"MISSING {table_name}")
                continue
            count = conn.execute(text(f"SELECT COUNT(*) FROM public.{table_name}")).scalar()
            rows = conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                    ORDER BY ordinal_position
                    """
                ),
                {"table_name": table_name},
            ).mappings().all()
            columns = [row["column_name"] for row in rows]
            print(f"EXISTS  {table_name} | rows={count}")
            print(f"  columns: {', '.join(columns)}")


def main():
    load_env()
    section("BACKEND DATABASE TABLE CHECK")
    print("Checking backend database from DATABASE_URL: avocarbon_costing or configured backend DB.")
    print("Note: this is NOT KPI_DB_Final / MCP 21 06 26.")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not configured. Backend table check skipped.")
        return 0

    engine, text, error = try_sqlalchemy(database_url)
    if error:
        print(error)
        print("Backend table check skipped. Install requirements to enable database inspection.")
        return 0

    try:
        inspect_with_sqlalchemy(engine, text)
    except Exception as exc:
        print(f"Backend table check failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
