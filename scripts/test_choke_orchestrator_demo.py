import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_workspace_orchestrator import build_choke_workspace_orchestration
from services.manufacturing_strategy import select_manufacturing_strategy


PAYLOADS = [
    {
        "project_code": "24003-CHO-00",
        "product_line": "Chokes",
        "product": "Fuse chokes",
        "product_id": "316-5001",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "drawing_reference": "316-5001-1-customer-confirmed.pdf",
    },
    {
        "project_code": "demo-rod-europe",
        "product_line": "Chokes",
        "product": "Rod choke",
        "product_id": "demo-rod-choke",
        "customer_delivery_zone": "Europe",
        "annual_quantity": 1000000,
        "drawing_reference": "demo-rod-choke-customer-confirmed.pdf",
    },
]


def planned_call_count(result):
    outputs = result.get("agent_outputs") or {}
    return len(outputs.get("planned_calls") or [])


def main():
    assert select_manufacturing_strategy("Chokes", "Rod choke", "Europe")["production_plant"] == "SAME"
    assert select_manufacturing_strategy("Chokes", "Fuse choke", "China South Pacific")["production_plant"] == "Kunshan"
    assert select_manufacturing_strategy("Chokes", "Rod choke", "North America")["production_plant"] == "Monterrey"

    for payload in PAYLOADS:
        result = build_choke_workspace_orchestration(payload, dry_run=True)
        strategy = result.get("manufacturing_strategy") or {}
        plant_data = result.get("plant_data") or {}
        financial = result.get("financial_calculation") or {}

        print(f"Project: {payload['project_code']}")
        print(f"production plant: {strategy.get('production_plant')}")
        print(
            "currencies: "
            f"{plant_data.get('operating_currency')} -> {plant_data.get('selling_currency')}"
        )
        print(f"dl_cost_per_piece: {financial.get('dl_cost_per_piece')}")
        print(f"voh_cost_per_piece: {financial.get('voh_cost_per_piece')}")
        print(f"number of planned calls: {planned_call_count(result)}")
        print(f"missing_inputs: {result.get('missing_inputs')}")
        print("-" * 80)


if __name__ == "__main__":
    main()
