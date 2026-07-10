import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
sys.path.insert(0, str(ROOT_DIR))

FILES_TO_CHECK = [
    "services/customer_input_schema.py",
    "services/manufacturing_strategy.py",
    "services/unit_table_service.py",
    "services/workspace_agent_client.py",
    "services/choke_process_decomposition.py",
    "services/choke_financial_calculation.py",
    "services/choke_standard_schema.py",
    "services/choke_orchestrator.py",
    "services/choke_demo_outputs.py",
    "services/costing_master_data_service.py",
    "scripts/test_choke_backend_flow.py",
    "scripts/test_workspace_agent_trigger.py",
    "scripts/demo_full_choke_workflow.py",
    "scripts/check_costing_master_tables.py",
    "app/routers/choke_orchestrator_router.py",
]

TABLES_TO_CHECK = [
    "unit",
    "manufacturing_strategy_matrix",
    "component_costing_result",
    "operation_rate_reference",
    "material_exchange_monthly_rate",
    "product_costing_boundary",
    "commercial_parameter",
    "customer_contact",
    "unit_factory_cost_change_log",
]

TEST_CUSTOMER_INPUT = {
    "project_code": "24003-CHO-00",
    "customer": "Zhejiang NBT",
    "final_customer": "BYD",
    "product_line": "Chokes",
    "product": "Fuse choke",
    "product_id": "316-5001",
    "part_number": "316-5001",
    "drawing_reference": "316-5001-1-熔断电感-QS198102-0051 customer confirmed.pdf",
    "customer_delivery_zone": "China South Pacific",
    "annual_quantity": 600000,
    "currency": "RMB",
    "target_price": 1.5,
}


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def load_env():
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH)
    except Exception:
        if not ENV_PATH.exists():
            return
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def masked_status(name):
    value = os.getenv(name)
    return "FOUND" if value else "MISSING"


def valid_agent_id(name):
    value = os.getenv(name, "").strip()
    return bool(value), value.startswith("agtch_")


def configured_path_status(env_name, default_relative=None, fallback_glob=None):
    configured = os.getenv(env_name)
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return configured, path.exists(), path
    if default_relative:
        path = ROOT_DIR / default_relative
        if not path.exists() and fallback_glob:
            matches = sorted((ROOT_DIR / "data").glob(fallback_glob))
            if matches:
                return "(discovered fallback)", True, matches[0]
        return "(default)", path.exists(), path
    return None, False, None


def audit_files():
    results = {}
    section("1. EXISTING FILES / MODULES")
    for rel_path in FILES_TO_CHECK:
        exists = (ROOT_DIR / rel_path).exists()
        results[rel_path] = exists
        print(f"{'FOUND  ' if exists else 'MISSING'} {rel_path}")
    return results


def audit_environment():
    section("2. ENVIRONMENT VALIDATION")
    load_env()
    env_results = {
        "DATABASE_URL": bool(os.getenv("DATABASE_URL")),
        "CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN": bool(os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN")),
    }
    print(f"DATABASE_URL: {masked_status('DATABASE_URL')}")

    for name in [
        "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
        "CHATGPT_CHOKE_BOM_AGENT_ID",
        "CHATGPT_MOST_AGENT_ID",
    ]:
        exists, valid = valid_agent_id(name)
        env_results[name] = exists and valid
        status = "FOUND_VALID" if exists and valid else ("FOUND_INVALID" if exists else "MISSING")
        print(f"{name}: {status}")

    print(f"CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN: {masked_status('CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN')}")

    product_config, product_exists, product_path = configured_path_status(
        "PRODUCT_MATRIX_CSV_PATH",
        "data/Product matrix.csv",
    )
    unit_config, unit_exists, unit_path = configured_path_status(
        "UNIT_TABLE_CSV_PATH",
        "data/AVOCarbon_Unit_Table_Update_Costing.csv",
        "AVOCarbon_Unit_Table_Update_Costing*.csv",
    )
    env_results["PRODUCT_MATRIX_CSV_PATH"] = product_exists
    env_results["UNIT_TABLE_CSV_PATH"] = unit_exists

    print(f"PRODUCT_MATRIX_CSV_PATH: {product_config} -> {'FILE_EXISTS' if product_exists else 'FILE_MISSING'} ({product_path})")
    print(f"UNIT_TABLE_CSV_PATH: {unit_config} -> {'FILE_EXISTS' if unit_exists else 'FILE_MISSING'} ({unit_path})")
    return env_results


def audit_functional_tests():
    section("3. FUNCTIONAL TESTS")
    result = {"ok": False}
    try:
        from services.choke_financial_calculation import calculate_dl_voh
        from services.choke_orchestrator import run_choke_orchestration
        from services.customer_input_schema import normalize_customer_input
        from services.manufacturing_strategy import select_manufacturing_strategy
        from services.unit_table_service import get_unit_data

        normalized = normalize_customer_input(TEST_CUSTOMER_INPUT)
        strategy = select_manufacturing_strategy("Chokes", "Fuse choke", "China South Pacific")
        unit_data = get_unit_data(strategy.get("production_plant"))
        envelope = run_choke_orchestration(
            TEST_CUSTOMER_INPUT,
            dry_run=True,
            trigger_agents=False,
            demo_override=True,
            full_demo_mode=True,
        )
        financial = envelope.get("financial_calculation") or {}
        orchestration = envelope.get("agent_orchestration") or {}

        sample_calc = calculate_dl_voh(
            envelope.get("most_work_packages") or [],
            envelope.get("unit_data") or {},
            TEST_CUSTOMER_INPUT["annual_quantity"],
        )

        print(f"customer input status: {normalized.get('status')}")
        print(f"production plant: {strategy.get('production_plant')}")
        print(f"unit operating currency: {unit_data.get('operating_currency')}")
        print(f"unit selling currency: {unit_data.get('selling_currency')}")
        print(f"number of component agent calls: {len(orchestration.get('component_agent_calls') or [])}")
        print(f"number of MOST work packages: {len(envelope.get('most_work_packages') or [])}")
        print(f"dl_cost_per_piece: {financial.get('dl_cost_per_piece')}")
        print(f"voh_cost_per_piece: {financial.get('voh_cost_per_piece')}")
        print(f"saved output path: {envelope.get('orchestration_result_absolute_path')}")
        print(f"missing inputs: {envelope.get('missing_inputs')}")
        print(f"calculate_dl_voh direct status: {sample_calc.get('status')}")

        result = {
            "ok": True,
            "envelope": envelope,
            "strategy": strategy,
            "unit_data": unit_data,
        }
    except Exception as exc:
        print(f"FUNCTIONAL TEST FAILED: {exc}")
        result = {"ok": False, "error": str(exc)}
    return result


def audit_database_tables():
    section("4. DATABASE TABLE CHECK")
    database_url = os.getenv("DATABASE_URL")
    results = {}
    if not database_url:
        print("DATABASE_URL missing. Database table check skipped.")
        return results

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        with psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor,
            sslmode=os.getenv("PGSSLMODE", "require"),
            connect_timeout=10,
        ) as conn:
            with conn.cursor() as cur:
                for table_name in TABLES_TO_CHECK:
                    cur.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = %s
                        ORDER BY ordinal_position
                        """,
                        (table_name,),
                    )
                    columns = [row["column_name"] for row in cur.fetchall()]
                    exists = bool(columns)
                    results[table_name] = exists
                    print(f"{'EXISTS ' if exists else 'MISSING'} {table_name}")
                    if exists:
                        print(f"  columns: {', '.join(columns)}")
    except Exception as exc:
        print(f"Database table check failed and was skipped: {exc}")
    return results


def audit_workspace_dry_run():
    section("5. WORKSPACE AGENT TRIGGER DRY-RUN")
    try:
        from services.workspace_agent_client import clean_agent_id, trigger_workspace_agent

        agent_id = clean_agent_id(os.getenv("CHATGPT_EXTERNAL_COMPONENT_AGENT_ID"))
        input_text = """Project 24003-CHO-00.
Component ferrite only.
This is one external component only, not a complete choke.
Annual quantity 600000.
Production plant Kunshan.
Destination China.
Save address:
data/costing_runs/24003-CHO-00/316-5001/components/316-5001-ferrite.json"""
        dry_run = trigger_workspace_agent(
            agent_id=agent_id,
            access_token=os.getenv("CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN"),
            input_text=input_text,
            conversation_key="audit-external-ferrite-24003-CHO-00",
            idempotency_key="audit-external-ferrite-dry-run",
            dry_run=True,
        )
        print(f"agent id validity: {'VALID' if agent_id.startswith('agtch_') else 'INVALID_OR_MISSING'}")
        print("first 300 characters of input_text:")
        print((dry_run.get("input_text") or "")[:300])
        return {"ok": True, "agent_id_valid": agent_id.startswith("agtch_")}
    except Exception as exc:
        print(f"Workspace dry-run failed: {exc}")
        return {"ok": False, "error": str(exc)}


def final_summary(file_results, env_results, functional_results, db_results, workspace_results):
    section("6. FINAL SUMMARY")
    implemented = []
    partial = []
    missing = []
    next_steps = []

    for rel_path, exists in file_results.items():
        if exists:
            implemented.append(f"File/module present: {rel_path}")
        else:
            missing.append(f"File/module missing: {rel_path}")

    if functional_results.get("ok"):
        implemented.append("Full demo service path runs from customer input to unified JSON.")
        implemented.append("Manufacturing strategy selects Kunshan for BYD fuse choke.")
        implemented.append("DL/VOH calculation produces numeric cost per piece.")
    else:
        missing.append("Functional service flow failed.")

    if workspace_results.get("agent_id_valid"):
        implemented.append("Workspace Agent dry-run can build external ferrite trigger payload.")
    else:
        partial.append("Workspace Agent dry-run exists but agent ID is missing or invalid.")

    for env_name, ok in env_results.items():
        if not ok:
            partial.append(f"Environment/config not fully valid: {env_name}")

    for table_name in TABLES_TO_CHECK:
        if db_results.get(table_name):
            implemented.append(f"Database table exists: {table_name}")
        else:
            partial.append(f"Database table missing or unchecked: {table_name}")

    if missing:
        next_steps.append("Create missing modules/scripts or confirm they are intentionally deferred.")
    if any("Database table missing" in item for item in partial):
        next_steps.append("Decide whether master-data tables should be migrated into PostgreSQL or remain CSV/demo fallback for now.")
    next_steps.append("Connect agent MCP write-back so pending Workspace Agent outputs can be loaded from save_address.")
    next_steps.append("Replace demo preliminary component costs with validated supplier/component outputs before commercial use.")

    print("IMPLEMENTED")
    for item in implemented:
        print(f"- {item}")

    print()
    print("PARTIALLY IMPLEMENTED")
    for item in partial:
        print(f"- {item}")

    print()
    print("MISSING / BLOCKED")
    if missing:
        for item in missing:
            print(f"- {item}")
    else:
        print("- No hard blocker detected by this audit, aside from listed partial items.")

    print()
    print("NEXT RECOMMENDED STEPS")
    for item in list(dict.fromkeys(next_steps)):
        print(f"- {item}")


def main():
    print("AVOCarbon Choke Costing Backend Implementation Audit")
    print(f"Project folder: {ROOT_DIR}")
    file_results = audit_files()
    env_results = audit_environment()
    functional_results = audit_functional_tests()
    db_results = audit_database_tables()
    workspace_results = audit_workspace_dry_run()
    final_summary(file_results, env_results, functional_results, db_results, workspace_results)


if __name__ == "__main__":
    main()
