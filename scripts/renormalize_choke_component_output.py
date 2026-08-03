"""Rebuild one saved glue or magnet-wire normalized artifact.

Dry-run is the default. This script never invokes a Workspace Agent and never
modifies the raw component output.
"""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.choke_sequential_agent_workflow import (  # noqa: E402
    calculate_final_choke_costing_from_saved_outputs,
    renormalize_component_output,
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-code", required=True)
    parser.add_argument("--product-id", required=True)
    parser.add_argument(
        "--component-id",
        required=True,
        choices=("glue", "magnet_wire"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Archive and replace the normalized artifact atomically.",
    )
    parser.add_argument(
        "--recalculate",
        action="store_true",
        help="Recalculate the preliminary result after an applied rebuild.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.recalculate and not args.apply:
        raise SystemExit("--recalculate requires --apply")
    result = renormalize_component_output(
        args.project_code,
        args.product_id,
        args.component_id,
        dry_run=not args.apply,
    )
    if args.recalculate:
        calculation = calculate_final_choke_costing_from_saved_outputs(
            args.project_code,
            args.product_id,
            result_mode="preliminary",
        )
        result["preliminary_calculation"] = {
            "status": calculation.get("status"),
            "missing_inputs": calculation.get("missing_inputs") or [],
            "component_breakdown": calculation.get("component_breakdown") or [],
            "calculated_delivered_material_cost_for_resolved_components": (
                calculation.get(
                    "calculated_delivered_material_cost_for_resolved_components"
                )
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
