import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))


def load_env() -> None:
    env_path = ROOT_DIR / ".env"
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


def route_paths():
    try:
        from app.main import app
    except ModuleNotFoundError as exc:
        venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if exc.name in {"anyio", "fastapi", "mcp", "dotenv", "psycopg2", "starlette"} and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
            print(f"{exc.name} is not installed for this Python; rerunning with .venv.")
            os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve())])
        raise

    paths = []
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        if path:
            paths.append(path)
    return sorted(set(paths))


def main() -> int:
    load_env()
    paths = route_paths()
    has_sse = "/sse" in paths
    has_mcp = "/mcp" in paths
    available = []
    if has_sse:
        available.append("/sse")
    if has_mcp:
        available.append("/mcp")

    host = os.getenv("MCP_HOST", "127.0.0.1")
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = os.getenv("MCP_PORT") or os.getenv("PORT") or "8000"
    local_base_url = f"http://{host}:{port}".rstrip("/")
    public_base_url = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")

    print("CHOKE COSTING MCP ENDPOINT CHECK")
    print("=" * 78)
    print(f"available MCP endpoint path: {', '.join(available) if available else 'none'}")
    if has_sse:
        print(f"preferred endpoint path for ChatGPT custom MCP: /sse")
        print(f"local URL: {local_base_url}/sse")
        if public_base_url:
            print(f"public URL: {public_base_url}/sse")
        else:
            print("public URL: not configured; set PUBLIC_BASE_URL to your ngrok/Azure App Service URL")
    elif has_mcp:
        print("preferred endpoint path for ChatGPT custom MCP: /mcp")
        print(f"local URL: {local_base_url}/mcp")
        if public_base_url:
            print(f"public URL: {public_base_url}/mcp")
        else:
            print("public URL: not configured; set PUBLIC_BASE_URL to your ngrok/Azure App Service URL")
    else:
        print("No MCP endpoint is mounted on app.main.")

    print()
    print("available related routes:")
    for path in paths:
        if path in {"/sse", "/messages", "/messages/", "/mcp", "/choke-costing"} or path.startswith("/api/choke-workflow"):
            print(f"- {path}")
    return 0 if available else 1


if __name__ == "__main__":
    raise SystemExit(main())
