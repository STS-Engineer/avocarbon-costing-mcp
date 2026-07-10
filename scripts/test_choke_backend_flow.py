import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_orchestrator import run_choke_orchestration


BYD_INPUT = {
    "project_code": "24003-CHO-00",
    "customer": "Zhejiang NBT",
    "final_customer": "BYD",
    "product_line": "Chokes",
    "product": "Fuse choke",
    "product_id": "316-5001",
    "part_number": "316-5001",
    "drawing_reference": "316-5001-1-customer-confirmed.pdf",
    "customer_delivery_zone": "China South Pacific",
    "annual_quantity": 600000,
    "currency": "RMB",
    "target_price": 1.5,
    "sop_date": None,
}

ROD_EU_INPUT = {
    "project_code": "DEMO-ROD-EU",
    "product_line": "Chokes",
    "product": "Rod choke",
    "product_id": "DEMO-ROD",
    "part_number": "DEMO-ROD",
    "drawing_reference": "demo.pdf",
    "customer_delivery_zone": "Europe",
    "annual_quantity": 1000000,
}


def print_summary(result):
    strategy = result.get("manufacturing_strategy") or {}
    unit_data = result.get("unit_data") or {}
    orchestration = result.get("agent_orchestration") or {}
    financial = result.get("financial_calculation") or {}
    print(f"project_code: {result.get('project', {}).get('project_code')}")
    print(f"production plant: {strategy.get('production_plant')}")
    print(f"operating currency: {unit_data.get('operating_currency')}")
    print(f"selling currency: {unit_data.get('selling_currency')}")
    print(f"number of component calls: {len(orchestration.get('component_agent_calls') or [])}")
    print(f"number of MOST work packages: {len(result.get('most_work_packages') or [])}")
    print(f"dl_cost_per_piece: {financial.get('dl_cost_per_piece')}")
    print(f"voh_cost_per_piece: {financial.get('voh_cost_per_piece')}")
    print(f"missing_inputs: {result.get('missing_inputs')}")
    print(f"saved: {result.get('orchestration_result_save_address')}")
    print("-" * 80)


def main():
    byd = run_choke_orchestration(BYD_INPUT, dry_run=True, trigger_agents=False)
    assert byd["project"]["project_code"] == "24003-CHO-00"
    assert byd["manufacturing_strategy"]["status"] == "found"
    assert byd["manufacturing_strategy"]["production_plant"] == "Kunshan"
    assert byd["unit_data"]["status"] in ["found", "found_with_missing_values"]
    assert byd["bom"]["status"] in ["planned", "available"]
    assert len(byd["agent_orchestration"]["component_agent_calls"]) >= 2
    assert len(byd["most_work_packages"]) >= 3
    assert byd["financial_calculation"]["dl_cost_per_piece"] is not None
    assert byd["financial_calculation"]["voh_cost_per_piece"] is not None
    assert Path(ROOT_DIR / byd["orchestration_result_save_address"]).exists()
    print_summary(byd)

    rod = run_choke_orchestration(ROD_EU_INPUT, dry_run=True, trigger_agents=False)
    assert rod["manufacturing_strategy"]["status"] == "found"
    assert rod["manufacturing_strategy"]["production_plant"] == "SAME"
    assert rod["unit_data"]["operating_currency"] == "TND"
    assert rod["unit_data"]["selling_currency"] == "EUR"
    print_summary(rod)


if __name__ == "__main__":
    main()
