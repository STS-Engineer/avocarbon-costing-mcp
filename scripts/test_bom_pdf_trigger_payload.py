import json
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.choke_sequential_agent_workflow import (  # noqa: E402
    build_bom_trigger_preview,
    start_real_choke_workflow,
)
from services.azure_blob_storage_service import (  # noqa: E402
    is_azure_blob_configured,
    upload_file_to_blob,
)


PROJECT_CODE = "RFQ-PDF-TRIGGER-TEST"
PRODUCT_ID = "UNKNOWN-PART-PDF-TRIGGER-TEST"
INPUT_PATH = ROOT_DIR / "data" / "customer_inputs" / "test_bom_pdf_trigger_payload.json"
PDF_PATH = ROOT_DIR / "data" / "customer_inputs" / "uploads" / PROJECT_CODE / "test-drawing.pdf"


def load_env():
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


def ensure_test_input():
    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PDF_PATH.exists():
        PDF_PATH.write_bytes(
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n"
        )

    local_path = "data/customer_inputs/uploads/RFQ-PDF-TRIGGER-TEST/test-drawing.pdf"
    azure_result = {}
    drawing_file_url = None
    drawing_access_mode = "local"
    drawing_blob_url = None
    drawing_sas_url = None
    if is_azure_blob_configured():
        azure_result = upload_file_to_blob(
            PDF_PATH,
            project_code=PROJECT_CODE,
            original_filename=PDF_PATH.name,
        )
        if azure_result.get("status") == "uploaded":
            drawing_blob_url = azure_result.get("blob_url")
            drawing_sas_url = azure_result.get("sas_url")
            drawing_file_url = drawing_sas_url
            drawing_access_mode = "azure_blob_sas"

    payload = {
        "project_code": PROJECT_CODE,
        "customer": "PDF trigger test customer",
        "product_line": "Chokes",
        "product": None,
        "product_id": PRODUCT_ID,
        "workflow_product_id": PRODUCT_ID,
        "part_number": None,
        "drawing_reference": "test-drawing.pdf",
        "drawing_file_path": local_path,
        "drawing_file_url": drawing_file_url,
        "drawing_access_mode": drawing_access_mode,
        "drawing_blob_url": drawing_blob_url,
        "drawing_sas_url": drawing_sas_url,
        "drawing_azure_upload": azure_result,
        "customer_delivery_zone": None,
        "annual_quantity": None,
        "currency": None,
        "target_price": None,
        "technical_fields_pending_bom": True,
    }
    INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return INPUT_PATH


def main():
    load_env()
    input_path = ensure_test_input()
    input_file = input_path.relative_to(ROOT_DIR).as_posix()
    public_base_url = os.getenv("PUBLIC_BASE_URL")
    preview = build_bom_trigger_preview(
        input_file,
        request_base_url="http://127.0.0.1:8000/",
    )

    print("BOM PDF TRIGGER PAYLOAD CHECK")
    print("=" * 78)
    print(f"PUBLIC_BASE_URL configured? {'yes' if public_base_url else 'no'}")
    print(f"drawing_file_path: {preview.get('drawing_file_path')}")
    print(f"drawing_access_mode: {preview.get('drawing_access_mode')}")
    drawing_file_url = preview.get("drawing_file_url") or ""
    print(f"drawing_file_url first 150 chars: {drawing_file_url[:150]}")
    print(f"drawing_url_is_local: {preview.get('drawing_url_is_local')}")
    print(f"input_text includes drawing_file_url? {'yes' if 'drawing_file_url' in (preview.get('input_text') or '') else 'no'}")
    print()
    print("input_text first 1000 chars:")
    print((preview.get("input_text") or "")[:1000])
    print()
    if preview.get("warnings"):
        print("warnings:")
        for warning in preview["warnings"]:
            print(f"- {warning}")
    else:
        print("warnings: none")

    started = start_real_choke_workflow(
        input_file,
        dry_run=True,
        request_base_url="http://127.0.0.1:8000/",
    )
    state = started["state"]
    components = state.get("components") or {}
    most = state.get("most") or {}
    assert state.get("bom", {}).get("status") == "triggered"
    assert components == {}
    assert most == {}
    assert "drawing_file_url" in (preview.get("input_text") or "")
    print()
    print("component calls triggered before BOM saved? no")
    print("MOST calls triggered before BOM saved? no")
    print("status: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
