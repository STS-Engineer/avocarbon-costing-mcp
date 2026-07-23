import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONTAINER = "choke-rfq-documents"


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


def safe_filename(value: Any, fallback: str = "file") -> str:
    original = Path(str(value or "")).name
    suffix = Path(original).suffix.lower()
    stem = Path(original).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    safe_stem = re.sub(r"_+", "_", safe_stem).strip("._-")
    return f"{safe_stem or fallback}{suffix}"


def _safe_path_part(value: Any, fallback: str = "project") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or ""))
    text = re.sub(r"_+", "_", text).strip("._-")
    return text or fallback


def _connection_string() -> Optional[str]:
    _load_env()
    return os.getenv("AZURE_STORAGE_CONNECTION_STRING")


def _container_name() -> str:
    _load_env()
    return (
        os.getenv("AZURE_STORAGE_CONTAINER")
        or os.getenv("AZURE_STORAGE_CONTAINER_NAME")
        or DEFAULT_CONTAINER
    )


def _expiry_hours(expiry_hours: Optional[int] = None) -> int:
    _load_env()
    if expiry_hours:
        return int(expiry_hours)
    raw_value = os.getenv("AZURE_BLOB_SAS_EXPIRY_HOURS") or "24"
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 24


def _network_timeout_seconds() -> int:
    _load_env()
    raw_value = os.getenv("AZURE_BLOB_NETWORK_TIMEOUT_SECONDS") or "10"
    try:
        return max(3, int(raw_value))
    except ValueError:
        return 10


def _account_key_from_connection_string(connection_string: str) -> Optional[str]:
    for part in str(connection_string or "").split(";"):
        if part.startswith("AccountKey="):
            return part.split("=", 1)[1]
    return None


def is_azure_blob_configured() -> bool:
    return bool(_connection_string())


def generate_blob_sas_url(
    container: str,
    blob_name: str,
    expiry_hours: Optional[int] = None,
) -> str:
    connection_string = _connection_string()
    if not connection_string:
        raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING is not configured")

    try:
        from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas
    except ImportError as exc:
        raise RuntimeError("azure-storage-blob is not installed") from exc

    account_key = _account_key_from_connection_string(connection_string)
    if not account_key:
        raise RuntimeError("Connection string must include AccountKey to generate a read-only SAS URL")

    timeout = _network_timeout_seconds()
    service_client = BlobServiceClient.from_connection_string(
        connection_string,
        connection_timeout=timeout,
        read_timeout=timeout,
    )
    blob_client = service_client.get_blob_client(container=container, blob=blob_name)
    hours = _expiry_hours(expiry_hours)
    sas_token = generate_blob_sas(
        account_name=service_client.account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    return f"{blob_client.url}?{sas_token}"


def refresh_blob_sas_url(
    upload_metadata: Optional[Dict[str, Any]] = None,
    blob_url: Optional[str] = None,
) -> Dict[str, Any]:
    metadata = upload_metadata if isinstance(upload_metadata, dict) else {}
    container = str(metadata.get("container") or "").strip()
    blob_name = str(metadata.get("blob_name") or "").strip()
    source_blob_url = str(blob_url or metadata.get("blob_url") or "").strip()
    if (not container or not blob_name) and source_blob_url:
        path_parts = [
            unquote(part)
            for part in urlsplit(source_blob_url).path.split("/")
            if part
        ]
        if len(path_parts) >= 2:
            container = container or path_parts[0]
            blob_name = blob_name or "/".join(path_parts[1:])
    if not container or not blob_name:
        return {
            "status": "not_available",
            "reason": "blob_container_or_name_missing",
        }
    try:
        sas_url = generate_blob_sas_url(container, blob_name)
        return {
            "status": "generated",
            "container": container,
            "blob_name": blob_name,
            "sas_url": sas_url,
            "fresh": True,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "container": container,
            "blob_name": blob_name,
            "reason": str(exc),
        }


def upload_file_to_blob(
    local_file_path: Any,
    project_code: Any,
    original_filename: Optional[str] = None,
) -> Dict[str, Any]:
    connection_string = _connection_string()
    container_name = _container_name()
    hours = _expiry_hours()
    if not connection_string:
        return {
            "status": "not_configured",
            "fallback": "local_file_url",
            "message": "AZURE_STORAGE_CONNECTION_STRING is not configured",
        }

    path = Path(local_file_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        return {
            "status": "failed",
            "error": f"Local file not found: {path}",
            "fallback": "local_file_url",
        }

    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError as exc:
        return {
            "status": "failed",
            "error": "azure-storage-blob is not installed",
            "fallback": "local_file_url",
        }

    try:
        timeout = _network_timeout_seconds()
        service_client = BlobServiceClient.from_connection_string(
            connection_string,
            connection_timeout=timeout,
            read_timeout=timeout,
        )
        container_client = service_client.get_container_client(container_name)
        try:
            container_client.create_container(timeout=timeout)
        except ResourceExistsError:
            pass

        filename = safe_filename(original_filename or path.name, "drawing")
        blob_name = f"choke-rfq/{_safe_path_part(project_code)}/{filename}"
        blob_client = container_client.get_blob_client(blob_name)
        content_settings = None
        if path.suffix.lower() == ".pdf":
            content_settings = ContentSettings(content_type="application/pdf")
        with path.open("rb") as handle:
            blob_client.upload_blob(
                handle,
                overwrite=True,
                content_settings=content_settings,
                timeout=timeout,
            )
        sas_url = generate_blob_sas_url(container_name, blob_name, expiry_hours=hours)
        return {
            "status": "uploaded",
            "container": container_name,
            "blob_name": blob_name,
            "blob_url": blob_client.url,
            "sas_url": sas_url,
            "expires_hours": hours,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "fallback": "local_file_url",
        }
