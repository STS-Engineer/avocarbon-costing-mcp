import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def check(client, method, path, expected_statuses, **kwargs):
    response = client.request(method, path, **kwargs)
    ok = response.status_code in expected_statuses
    expected = ", ".join(str(status) for status in sorted(expected_statuses))
    print(f"{'PASS' if ok else 'FAIL'} {method} {path}: HTTP {response.status_code} (expected {expected})")
    if not ok:
        print(response.text[:500])
    return ok, response


def main():
    try:
        from fastapi.testclient import TestClient
        from app.main import app
    except ModuleNotFoundError as exc:
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if exc.name == "fastapi" and venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
            print(f"FastAPI is not installed in {sys.executable}; retrying with {venv_python}.")
            return subprocess.call([str(venv_python), str(Path(__file__).resolve())], cwd=BASE_DIR)
        print(f"FAIL Could not import FastAPI app: {exc}")
        return 1
    except Exception as exc:
        print(f"FAIL Could not import FastAPI app: {exc}")
        return 1

    client = TestClient(app, raise_server_exceptions=False)
    checks = []

    # The FastAPI root is intentionally not claimed; the standalone MCP server owns
    # its own root behavior. A 404 here is acceptable and confirms no server error.
    checks.append(check(client, "GET", "/", {200, 404})[0])

    health_ok, health_response = check(client, "GET", "/api/health", {200})
    checks.append(health_ok)
    if health_ok:
        expected_health = {
            "status": "ok",
            "service": "avocarbon-costing-backend",
        }
        payload = health_response.json()
        shape_ok = payload == expected_health
        print(f"{'PASS' if shape_ok else 'FAIL'} /api/health response contract")
        checks.append(shape_ok)

    openapi_ok, openapi_response = check(client, "GET", "/openapi.json", {200})
    checks.append(openapi_ok)
    if openapi_ok:
        paths = openapi_response.json().get("paths", {})
        required_paths = {
            "/api/health",
            "/api/choke-workflow/start",
            "/api/choke-workflow/status/{project_code}/{product_id}",
            "/api/choke-workflow/calculate-final",
            "/api/choke-workflow/final-result/{project_code}/{product_id}",
        }
        paths_ok = required_paths <= set(paths)
        print(f"{'PASS' if paths_ok else 'FAIL'} OpenAPI contains required backend paths")
        if not paths_ok:
            print(f"Missing OpenAPI paths: {sorted(required_paths - set(paths))}")
        checks.append(paths_ok)

    docs_ok, _ = check(client, "GET", "/docs", {200})
    checks.append(docs_ok)

    status_ok, _ = check(
        client,
        "GET",
        "/api/choke-workflow/status/SMOKE-NOT-FOUND/SMOKE-PRODUCT",
        {200, 404},
    )
    checks.append(status_ok)

    cors_ok, cors_response = check(
        client,
        "OPTIONS",
        "/api/health",
        {200},
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    checks.append(cors_ok)
    if cors_ok:
        header_ok = cors_response.headers.get("access-control-allow-origin") == "http://localhost:5173"
        print(f"{'PASS' if header_ok else 'FAIL'} local React origin is allowed by CORS")
        checks.append(header_ok)

    print()
    print(f"Backend smoke test: {'PASSED' if all(checks) else 'FAILED'}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
