import runpy
from pathlib import Path


runpy.run_path(
    str(Path(__file__).with_name("test_received_bom_clears_retryable_failure.py")),
    run_name="__main__",
)
