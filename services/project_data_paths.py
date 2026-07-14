import logging
import os
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List


logger = logging.getLogger(__name__)
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent

try:
    from dotenv import load_dotenv

    load_dotenv(BACKEND_ROOT / ".env", override=False)
except Exception:
    pass


DATA_ROOT_RAW = os.getenv("DATA_ROOT")
WORKFLOW_PATH_VERSION = "canonical-v1"


def _absolute_root(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    normalized_value = str(value).strip().replace("\\", "/")
    if os.name == "nt" and normalized_value.startswith("/") and not is_azure_environment():
        logger.info("Ignoring Azure/POSIX DATA_ROOT on local Windows: %s", value)
        return default.resolve()
    configured = Path(os.path.expandvars(value)).expanduser()
    if not configured.is_absolute():
        configured = BACKEND_ROOT / configured
    return configured.resolve()


def _default_data_root() -> Path:
    if os.getenv("WEBSITE_SITE_NAME") or os.getenv("WEBSITE_INSTANCE_ID"):
        azure_home = Path(os.getenv("HOME") or "/home")
        return azure_home / "data" / "avocarbon-costing"
    return BACKEND_ROOT / "data"


def get_data_root(create: bool = True) -> Path:
    root = _absolute_root(DATA_ROOT_RAW, _default_data_root())
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def is_azure_environment() -> bool:
    return bool(os.getenv("WEBSITE_SITE_NAME") or os.getenv("WEBSITE_INSTANCE_ID"))


def persistent_storage_enabled() -> bool:
    value = str(os.getenv("WEBSITES_ENABLE_APP_SERVICE_STORAGE") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def get_git_commit() -> str | None:
    for name in ("GIT_COMMIT_SHA", "BUILD_SOURCEVERSION", "WEBSITE_DEPLOYMENT_ID"):
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def validate_data_root_configuration() -> Dict[str, Any]:
    root = get_data_root(create=False)
    errors: List[str] = []
    if is_azure_environment():
        if not str(DATA_ROOT_RAW or "").strip():
            errors.append("DATA_ROOT must be configured explicitly on Azure.")
        raw_normalized = str(DATA_ROOT_RAW or "").strip().replace("\\", "/").lower()
        if raw_normalized == "/tmp" or raw_normalized.startswith("/tmp/"):
            errors.append(f"Azure DATA_ROOT is not persistent: {DATA_ROOT_RAW}")
        if raw_normalized == "/root/data" or raw_normalized.startswith("/root/data/"):
            errors.append(f"Azure DATA_ROOT is not persistent: {DATA_ROOT_RAW}")
        disallowed_roots = [Path("/tmp"), Path("/root/data")]
        if any(root == item or item in root.parents for item in disallowed_roots):
            errors.append(f"Azure DATA_ROOT is not persistent: {root}")
        if root == BACKEND_ROOT or BACKEND_ROOT in root.parents:
            errors.append("Azure DATA_ROOT must not be inside the application deployment directory.")
        if not persistent_storage_enabled():
            errors.append("WEBSITES_ENABLE_APP_SERVICE_STORAGE must be true on Azure.")
    return {
        "healthy": not errors,
        "errors": errors,
        "data_root_raw": DATA_ROOT_RAW,
        "data_root_resolved": str(root),
        "persistent_storage_enabled": persistent_storage_enabled(),
        "workflow_path_version": WORKFLOW_PATH_VERSION,
        "process_id": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "startup_module": "app.main:app",
        "git_commit": get_git_commit(),
    }


def ensure_workflow_storage_ready() -> None:
    status = validate_data_root_configuration()
    if not status["healthy"]:
        raise RuntimeError(" ".join(status["errors"]))
    get_data_root(create=True)


def atomic_write_json(path: Path, payload: Any) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        return path
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _safe_workflow_part(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    if text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"{field_name} must not contain path separators.")
    return text


def get_workflow_run_paths(project_code: str, product_id: str) -> Dict[str, Path]:
    project = _safe_workflow_part(project_code, "project_code")
    product = _safe_workflow_part(product_id, "product_id")
    run_dir = (get_data_root() / "costing_runs" / project / product).resolve()
    components_dir = (run_dir / "agent_outputs" / "components").resolve()
    most_dir = (run_dir / "agent_outputs" / "most").resolve()
    return {
        "run_dir": run_dir,
        "workflow_state_path": (run_dir / "workflow_state.json").resolve(),
        "raw_bom_path": (
            run_dir / "agent_outputs" / "bom" / "raw_bom_agent_output.json"
        ).resolve(),
        "normalized_bom_path": (run_dir / "bom_normalized.json").resolve(),
        "components_dir": components_dir,
        "most_dir": most_dir,
        "workflow_events_path": (run_dir / "workflow_events.jsonl").resolve(),
    }


def get_legacy_workflow_state_paths(project_code: str, product_id: str) -> List[Path]:
    project = _safe_workflow_part(project_code, "project_code")
    product = _safe_workflow_part(product_id, "product_id")
    canonical = get_workflow_run_paths(project, product)["workflow_state_path"]
    roots = [
        (BACKEND_ROOT / "data").resolve(),
        (PROJECT_ROOT / "data").resolve(),
    ]
    configured_legacy = os.getenv("LEGACY_DATA_ROOTS", "")
    roots.extend(
        _absolute_root(item.strip(), BACKEND_ROOT / "data")
        for item in configured_legacy.split(",")
        if item.strip()
    )
    if os.name != "nt":
        roots.extend(path.resolve() for path in Path("/tmp").glob("*/data"))
        roots.append(Path("/root/data").resolve())
    candidates = []
    for root in roots:
        candidate = (root / "costing_runs" / project / product / "workflow_state.json").resolve()
        if candidate != canonical and candidate.exists() and candidate.is_file():
            candidates.append(candidate)
    return list(dict.fromkeys(candidates))


def workflow_path_diagnostics(project_code: str, product_id: str) -> Dict[str, Any]:
    paths = get_workflow_run_paths(project_code, product_id)
    return {
        "process_id": os.getpid(),
        "cwd": str(Path.cwd().resolve()),
        "configured_data_root_raw": DATA_ROOT_RAW,
        "resolved_data_root": str(get_data_root()),
        "resolved_run_directory": str(paths["run_dir"]),
        "resolved_workflow_state_path": str(paths["workflow_state_path"]),
        "workflow_state_path_exists": paths["workflow_state_path"].exists(),
        "project_code": project_code,
        "product_id": product_id,
        "git_commit": get_git_commit(),
        "workflow_path_version": WORKFLOW_PATH_VERSION,
        "persistent_storage_enabled": persistent_storage_enabled(),
        "startup_module": "app.main:app",
    }


DATA_ROOT = get_data_root()
CUSTOMER_INPUT_DIR = (DATA_ROOT / "customer_inputs").resolve()
COSTING_RUNS_DIR = (DATA_ROOT / "costing_runs").resolve()
LEGACY_CUSTOMER_INPUT_DIRS = tuple(dict.fromkeys([
    (BACKEND_ROOT / "data" / "customer_inputs").resolve(),
    (PROJECT_ROOT / "data" / "customer_inputs").resolve(),
]))


class CustomerInputFileNotFound(FileNotFoundError):
    def __init__(self, details: Dict[str, Any]):
        self.details = details
        super().__init__(details.get("message") or "Customer input file not found")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def portable_data_reference(path: Path) -> str:
    resolved = path.resolve()
    if _is_within(resolved, DATA_ROOT):
        return (Path("data") / resolved.relative_to(DATA_ROOT)).as_posix()
    if _is_within(resolved, BACKEND_ROOT):
        return resolved.relative_to(BACKEND_ROOT).as_posix()
    return resolved.as_posix()


def resolve_data_reference(reference: str | Path) -> Path:
    return data_reference_candidates(reference)[0]


def data_reference_candidates(reference: str | Path) -> List[Path]:
    raw = Path(str(reference).replace("\\", "/"))
    if raw.is_absolute():
        candidates = [raw.resolve()]
    elif raw.parts and raw.parts[0].lower() == "data":
        candidates = [
            DATA_ROOT.joinpath(*raw.parts[1:]).resolve(),
            (BACKEND_ROOT / raw).resolve(),
            (PROJECT_ROOT / raw).resolve(),
        ]
    else:
        candidates = [
            (BACKEND_ROOT / raw).resolve(),
            (DATA_ROOT / raw).resolve(),
            (PROJECT_ROOT / raw).resolve(),
        ]
    return list(dict.fromkeys(candidates))


def resolve_existing_data_reference(reference: str | Path) -> Path | None:
    for candidate in data_reference_candidates(reference):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _allowed_customer_input_roots() -> List[Path]:
    return list(dict.fromkeys([CUSTOMER_INPUT_DIR, *LEGACY_CUSTOMER_INPUT_DIRS]))


def _candidate_paths(input_file: str) -> List[Path]:
    raw = Path(str(input_file).replace("\\", "/"))
    candidates: List[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend([
            Path.cwd() / raw,
            PROJECT_ROOT / raw,
            BACKEND_ROOT / raw,
        ])
        parts = raw.parts
        if parts and parts[0].lower() == "data":
            candidates.append(DATA_ROOT.joinpath(*parts[1:]))
        else:
            candidates.append(DATA_ROOT / raw)
        candidates.extend(root / raw.name for root in _allowed_customer_input_roots())

    unique: List[Path] = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _project_code_from_reference(input_file: str) -> str:
    stem = Path(str(input_file).replace("\\", "/")).stem
    return stem.split("_", 1)[0] if stem else ""


def customer_input_missing_details(input_file: str, candidates: List[Path]) -> Dict[str, Any]:
    project_code = _project_code_from_reference(input_file)
    matches = []
    if project_code:
        for root in _allowed_customer_input_roots():
            if root.exists():
                matches.extend(str(path.resolve()) for path in root.glob(f"{project_code}*.json"))
    canonical = (CUSTOMER_INPUT_DIR / Path(str(input_file).replace("\\", "/")).name).resolve()
    return {
        "message": f"Customer input file not found: {input_file}",
        "input_file_received": input_file,
        "resolved_path": str(canonical),
        "cwd": str(Path.cwd().resolve()),
        "data_root": str(DATA_ROOT),
        "existing_customer_input_files_matching_project_code": sorted(set(matches))[:25],
        "attempted_paths": [str(path) for path in candidates],
    }


def resolve_customer_input_path(input_file: str) -> Path:
    if not input_file or Path(str(input_file)).suffix.lower() != ".json":
        raise ValueError("input_file must reference a JSON file")

    allowed_roots = _allowed_customer_input_roots()
    candidates = _candidate_paths(input_file)
    for candidate in candidates:
        if not any(_is_within(candidate, root) for root in allowed_roots):
            continue
        if candidate.exists() and candidate.is_file():
            return candidate

    details = customer_input_missing_details(input_file, candidates)
    logger.error("Customer input resolution failed: %s", details)
    raise CustomerInputFileNotFound(details)
