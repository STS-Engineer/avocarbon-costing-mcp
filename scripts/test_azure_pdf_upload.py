import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.azure_blob_storage_service import (  # noqa: E402
    is_azure_blob_configured,
    upload_file_to_blob,
)


def _ensure_sample_pdf() -> Path:
    uploads_root = ROOT_DIR / "data" / "customer_inputs" / "uploads"
    existing = sorted(uploads_root.glob("**/*.pdf")) if uploads_root.exists() else []
    if existing:
        return existing[0]

    sample_path = uploads_root / "AZURE-UPLOAD-TEST" / "test-drawing.pdf"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    return sample_path


def main() -> int:
    pdf_path = _ensure_sample_pdf()
    print("AZURE PDF UPLOAD TEST")
    print("=" * 78)
    print(f"azure_configured: {'yes' if is_azure_blob_configured() else 'no'}")
    print(f"local_pdf: {pdf_path}")

    result = upload_file_to_blob(
        pdf_path,
        project_code=pdf_path.parent.name,
        original_filename=pdf_path.name,
    )
    print(f"status: {result.get('status')}")
    print(f"container: {result.get('container')}")
    print(f"blob_name: {result.get('blob_name')}")
    sas_url = result.get("sas_url") or ""
    print(f"sas_url_prefix: {sas_url.split('?', 1)[0] if sas_url else ''}")
    print(f"expires_hours: {result.get('expires_hours')}")

    if result.get("status") == "not_configured":
        print("Azure Blob is not configured; upload test skipped without printing secrets.")
        return 0
    if result.get("status") != "uploaded":
        print(f"error: {result.get('error')}")
        return 1
    if not sas_url.startswith("https://"):
        print("error: SAS URL does not start with https://")
        return 1
    print("status: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
