import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.azure_blob_storage_service import is_azure_blob_configured


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


def exists_env(name):
    return "yes" if os.getenv(name) else "no"


def valid_agent(name):
    value = os.getenv(name, "")
    return "yes" if value.startswith("agtch_") else "no"


def is_local_url(value):
    text = str(value or "").strip().lower()
    return any(marker in text for marker in [
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
    ])


def writeback_endpoint_status(public_base_url):
    if not public_base_url:
        return "unknown"
    if is_local_url(public_base_url):
        return "local"
    return "public"


def resolve_path(env_name, default_relative, fallback_glob=None):
    configured = os.getenv(env_name)
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return configured, path.exists(), path
    path = ROOT_DIR / default_relative
    if not path.exists() and fallback_glob:
        matches = sorted((ROOT_DIR / "data").glob(fallback_glob))
        if matches:
            return "(discovered fallback)", True, matches[0]
    return "(default)", path.exists(), path


def main():
    load_env()
    product_cfg, product_exists, product_path = resolve_path(
        "PRODUCT_MATRIX_CSV_PATH",
        "data/Product matrix.csv",
    )
    unit_cfg, unit_exists, unit_path = resolve_path(
        "UNIT_TABLE_CSV_PATH",
        "data/AVOCarbon_Unit_Table_Update_Costing.csv",
        "AVOCarbon_Unit_Table_Update_Costing*.csv",
    )

    print("DATA SOURCE STATUS")
    print("=" * 78)
    print(f"DATABASE_URL configured? {exists_env('DATABASE_URL')}")
    print(f"KPI_DB_FINAL_URL configured? {exists_env('KPI_DB_FINAL_URL')}")
    public_base_url = os.getenv("PUBLIC_BASE_URL")
    public_base_url_local = is_local_url(public_base_url)
    writeback_status = writeback_endpoint_status(public_base_url)
    print(f"PUBLIC_BASE_URL configured? {exists_env('PUBLIC_BASE_URL')}")
    print(f"PUBLIC_BASE_URL is localhost? {'yes' if public_base_url_local else 'no'}")
    print(f"write-back endpoints public? {writeback_status}")
    if not public_base_url or public_base_url_local:
        print(
            "Agents cannot write back to local backend. Use ngrok or Azure App Service."
        )
    print(f"AZURE_STORAGE_CONNECTION_STRING configured? {exists_env('AZURE_STORAGE_CONNECTION_STRING')}")
    print(f"AZURE_STORAGE_CONTAINER_NAME: {os.getenv('AZURE_STORAGE_CONTAINER_NAME') or 'choke-rfq-documents'}")
    print(f"Azure Blob upload available? {'yes' if is_azure_blob_configured() else 'no'}")
    print(f"PRODUCT_MATRIX_CSV_PATH exists? {'yes' if product_exists else 'no'} ({product_cfg}: {product_path})")
    print(f"UNIT_TABLE_CSV_PATH exists? {'yes' if unit_exists else 'no'} ({unit_cfg}: {unit_path})")
    for env_name in [
        "CHATGPT_EXTERNAL_COMPONENT_AGENT_ID",
        "CHATGPT_CHOKE_BOM_AGENT_ID",
        "CHATGPT_MOST_AGENT_ID",
    ]:
        print(f"{env_name} configured? {exists_env(env_name)} | starts with agtch_? {valid_agent(env_name)}")
    print(f"Workspace Agent token configured? {exists_env('CHATGPT_WORKSPACE_AGENT_ACCESS_TOKEN')}")
    print()
    if os.getenv("KPI_DB_FINAL_URL"):
        print("Current manufacturing strategy source: KPI_DB_Final")
        print("Current unit data source: KPI_DB_Final")
    else:
        print("Current manufacturing strategy source: Product Matrix CSV fallback")
        print("Current unit data source: Unit Table CSV fallback")
    print()
    print(
        "MCP note: MCP 21 06 26 targets KPI_DB_Final, but Python backend reads it "
        "only when KPI_DB_FINAL_URL or a future MCP client bridge is configured."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
