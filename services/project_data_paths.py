import logging
import os
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


def _absolute_root(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        configured = BACKEND_ROOT / configured
    return configured.resolve()


DATA_ROOT = _absolute_root(os.getenv("DATA_ROOT"), BACKEND_ROOT / "data")
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
            (Path.cwd() / raw).resolve(),
        ]
    else:
        candidates = [
            (BACKEND_ROOT / raw).resolve(),
            (DATA_ROOT / raw).resolve(),
            (PROJECT_ROOT / raw).resolve(),
            (Path.cwd() / raw).resolve(),
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
