"""Extract a reviewed JSON reference from an approved Choke XLSM quotation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.choke_excel_golden_reference import extract_golden_reference


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "tests" / "fixtures" / "choke_24018_excel_golden_reference.json",
    )
    args = parser.parse_args()
    result = extract_golden_reference(args.workbook)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"status: extracted")
    print(f"workbook: {result['workbook']}")
    print(f"source_hash: {result['source_hash']}")
    print(f"output: {args.output.resolve()}")
    print(f"worksheets: {len(result['workbook_inventory']['worksheets'])}")
    print(f"formula_without_cached_value_count: {result['workbook_inventory']['formula_without_cached_value_count']}")
    print(f"quoted_y0_selling_price: {result['solver']['quoted_y0_selling_price']}")
    print(f"npv: {result['solver']['npv']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
