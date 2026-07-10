import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

try:
    from app.routers import choke_orchestrator_router  # noqa: F401

    ROUTER_IMPORT_STATUS = "router import ok"
except Exception as exc:
    ROUTER_IMPORT_STATUS = f"router import skipped: {exc}"

from services.choke_orchestrator import run_choke_orchestration


BYD_DEMO_PAYLOAD = {
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
}


def main():
    result = run_choke_orchestration(
        BYD_DEMO_PAYLOAD,
        dry_run=True,
        trigger_agents=False,
        demo_override=True,
    )
    strategy = result.get("manufacturing_strategy") or {}
    unit_data = result.get("unit_data") or {}
    orchestration = result.get("agent_orchestration") or {}
    financial = result.get("financial_calculation") or {}

    print(ROUTER_IMPORT_STATUS)
    print(f"project_code: {result.get('project', {}).get('project_code')}")
    print(f"production plant: {strategy.get('production_plant')}")
    print(
        "currencies: "
        f"{unit_data.get('operating_currency')} -> {unit_data.get('selling_currency')}"
    )
    print(f"component calls: {len(orchestration.get('component_agent_calls') or [])}")
    print(f"MOST work packages: {len(result.get('most_work_packages') or [])}")
    print(f"DL: {financial.get('dl_cost_per_piece')}")
    print(f"VOH: {financial.get('voh_cost_per_piece')}")
    print(f"missing_inputs: {result.get('missing_inputs')}")


if __name__ == "__main__":
    main()
