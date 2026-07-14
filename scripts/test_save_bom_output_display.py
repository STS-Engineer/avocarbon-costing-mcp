import runpy
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
runpy.run_path(
    str(SCRIPT_DIR / "test_bom_writeback_preserves_state_and_bom_output.py"),
    run_name="__main__",
)
