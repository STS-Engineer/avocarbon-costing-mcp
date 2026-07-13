import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _run_test():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import choke_costing_ui_router
    from services.project_data_paths import DATA_ROOT, resolve_customer_input_path

    client = TestClient(app, raise_server_exceptions=True)
    pdf_content = b"%PDF-1.4\n% AVOCarbon test drawing\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"

    with patch.object(choke_costing_ui_router, "is_azure_blob_configured", return_value=False):
        save_response = client.post(
            "/api/choke-costing/customer-inputs/create",
            data={"product_line": "Chokes"},
            files={
                "drawing_pdf": (
                    "drawing-only-test.pdf",
                    pdf_content,
                    "application/pdf",
                )
            },
        )

    assert save_response.status_code == 200, save_response.text
    saved = save_response.json()
    input_file = saved.get("input_file")
    customer_input = saved.get("customer_input") or {}
    project_code = customer_input.get("project_code")
    product_id = customer_input.get("workflow_product_id") or customer_input.get("product_id")
    drawing_url = customer_input.get("drawing_file_url")

    assert input_file, saved
    assert project_code, saved
    assert product_id, saved
    assert input_file.startswith("data/customer_inputs/"), input_file
    assert resolve_customer_input_path(input_file).exists(), input_file
    assert drawing_url, customer_input

    original_cwd = Path.cwd()
    alternate_cwd = BASE_DIR / "data" / "_test_alternate_cwd"
    alternate_cwd.mkdir(parents=True, exist_ok=True)
    try:
        os.chdir(alternate_cwd)
        start_response = client.post(
            "/api/choke-workflow/start",
            json={"input_file": input_file, "dry_run": True},
        )
    finally:
        os.chdir(original_cwd)

    assert start_response.status_code == 200, start_response.text
    started = start_response.json()
    state = started.get("state") or {}
    assert state.get("project_code") == project_code, state
    assert state.get("product_id") == product_id, state
    assert state.get("status") == "bom_triggered", state
    assert state.get("drawing_file_url") == drawing_url, state
    assert ((state.get("bom") or {}).get("trigger_result") or {}).get("status") == "dry_run", state

    missing_response = client.post(
        "/api/choke-workflow/start",
        json={
            "input_file": f"data/customer_inputs/{project_code}_DOES-NOT-EXIST.json",
            "dry_run": True,
        },
    )
    assert missing_response.status_code == 404, missing_response.text
    details = missing_response.json().get("detail") or {}
    for key in [
        "input_file_received",
        "resolved_path",
        "cwd",
        "data_root",
        "existing_customer_input_files_matching_project_code",
    ]:
        assert key in details, details

    print("CUSTOMER INPUT SAVE THEN START TEST")
    print("=" * 78)
    print(f"data_root: {DATA_ROOT}")
    print(f"input_file: {input_file}")
    print(f"project_code: {project_code}")
    print(f"product_id: {product_id}")
    print(f"workflow_status: {state.get('status')}")
    print(f"drawing_url_preserved: {state.get('drawing_file_url') == drawing_url}")
    print("alternate_cwd_resolution: passed")
    print("structured_missing_file_diagnostics: passed")
    return 0


def main():
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError as exc:
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
            print(f"{exc.name} is unavailable in {sys.executable}; retrying with {venv_python}.")
            return subprocess.call(
                [str(venv_python), str(Path(__file__).resolve())],
                cwd=BASE_DIR,
                env=os.environ.copy(),
            )
        raise

    data_root = BASE_DIR / "data" / "_test_customer_input_data_root"
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_ROOT"] = str(data_root)
    return _run_test()


if __name__ == "__main__":
    raise SystemExit(main())
