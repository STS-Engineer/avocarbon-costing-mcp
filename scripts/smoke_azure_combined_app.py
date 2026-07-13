import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REQUIRED_REST_PATHS = {
    "/api/health",
    "/api/choke-costing/customer-inputs",
    "/api/choke-costing/customer-inputs/create",
    "/api/choke-costing/files/{project_code}/{filename}",
    "/api/choke-workflow/start",
    "/api/choke-workflow/status/{project_code}/{product_id}",
    "/api/choke-workflow/save-bom-output",
    "/api/choke-workflow/trigger-components",
    "/api/choke-workflow/save-component-output",
    "/api/choke-workflow/trigger-most",
    "/api/choke-workflow/save-most-output",
    "/api/choke-workflow/calculate-final",
    "/api/choke-workflow/final-result/{project_code}/{product_id}",
}

REQUIRED_MCP_TOOLS = {
    "save_bom_output",
    "save_component_output",
    "save_most_output",
    "get_choke_workflow_status",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Smoke test the combined AVOCarbon REST and MCP application."
    )
    parser.add_argument(
        "--base-url",
        help="Optional deployed URL, for example https://mcp-costing.azurewebsites.net",
    )
    return parser.parse_args()


def print_check(ok, label, detail=""):
    suffix = f" - {detail}" if detail else ""
    print(f"{'PASS' if ok else 'FAIL'} {label}{suffix}")
    return ok


def _http_get(base_url, path):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json,text/html"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, response.headers.get("Content-Type", ""), body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode(
            "utf-8", errors="replace"
        )


def run_remote(base_url):
    print(f"Testing deployed combined app: {base_url.rstrip('/')}")
    checks = []
    for path in ["/", "/health", "/api/health", "/openapi.json", "/mcp"]:
        status, content_type, body = _http_get(base_url, path)
        expected = {200} if path != "/mcp" else {200, 400, 405, 406}
        checks.append(print_check(status in expected, f"GET {path}", f"HTTP {status}"))
        if path == "/" and status == 200:
            try:
                payload = json.loads(body)
                checks.append(print_check(
                    payload.get("service") == "AVOCarbon Costing MCP",
                    "MCP root contract",
                ))
            except json.JSONDecodeError:
                checks.append(print_check(False, "MCP root contract", content_type))

    status, _, _ = _http_get(
        base_url,
        "/api/choke-workflow/status/COMBINED-SMOKE/COMBINED-PRODUCT",
    )
    checks.append(print_check(status == 200, "workflow status endpoint", f"HTTP {status}"))
    return all(checks)


def run_in_process():
    try:
        from fastapi.testclient import TestClient
        from app.main import app
        from server import mcp
        from services import choke_sequential_agent_workflow as workflow_service
    except ModuleNotFoundError as exc:
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
            print(f"{exc.name} is unavailable in {sys.executable}; retrying with {venv_python}.")
            return subprocess.call([str(venv_python), str(Path(__file__).resolve())], cwd=BASE_DIR) == 0
        raise

    client = TestClient(app, raise_server_exceptions=False)
    checks = []
    responses = {}
    for path in ["/", "/health", "/api/health", "/docs", "/openapi.json"]:
        response = client.get(path)
        responses[path] = response
        checks.append(print_check(response.status_code == 200, f"GET {path}", f"HTTP {response.status_code}"))

    root_payload = responses["/"].json() if responses["/"].status_code == 200 else {}
    checks.append(print_check(
        root_payload.get("status") == "ok"
        and root_payload.get("service") == "AVOCarbon Costing MCP"
        and root_payload.get("mcp_endpoint") == "/mcp",
        "MCP root response preserved",
    ))

    api_payload = responses["/api/health"].json() if responses["/api/health"].status_code == 200 else {}
    checks.append(print_check(
        api_payload == {"status": "ok", "service": "avocarbon-costing-backend"},
        "REST health response",
    ))

    openapi_paths = set(
        (responses["/openapi.json"].json().get("paths") or {})
        if responses["/openapi.json"].status_code == 200
        else {}
    )
    missing_paths = sorted(REQUIRED_REST_PATHS - openapi_paths)
    checks.append(print_check(
        not missing_paths,
        "React REST endpoints registered",
        f"missing={missing_paths}" if missing_paths else f"count={len(REQUIRED_REST_PATHS)}",
    ))

    route_paths = {getattr(route, "path", None) for route in app.routes}
    checks.append(print_check("/mcp" in route_paths, "MCP /mcp route registered"))

    registered_tools = set(mcp._tool_manager._tools)
    missing_tools = sorted(REQUIRED_MCP_TOOLS - registered_tools)
    checks.append(print_check(
        not missing_tools,
        "MCP write-back tools registered",
        f"missing={missing_tools}" if missing_tools else f"tools={len(registered_tools)}",
    ))

    status_response = client.get(
        "/api/choke-workflow/status/COMBINED-SMOKE/COMBINED-PRODUCT"
    )
    checks.append(print_check(
        status_response.status_code == 200,
        "workflow status endpoint",
        f"HTTP {status_response.status_code}",
    ))

    storage_path = Path(workflow_service.RUNS_DIR).resolve()
    checks.append(print_check(
        "services.choke_sequential_agent_workflow" in sys.modules,
        "REST and MCP share workflow service",
        str(storage_path),
    ))

    return all(checks)


def main():
    args = parse_args()
    print("AVOCARBON AZURE COMBINED APP SMOKE TEST")
    print("=" * 78)
    ok = run_remote(args.base_url) if args.base_url else run_in_process()
    print()
    print(f"Combined app smoke test: {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
