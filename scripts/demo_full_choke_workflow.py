import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_orchestrator import run_choke_orchestration


CUSTOMER_INPUT = {
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


def money(value):
    if value is None:
        return "missing"
    return f"{value:.6f}"


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main():
    result = run_choke_orchestration(
        CUSTOMER_INPUT,
        full_demo_mode=True,
        dry_run=True,
        trigger_agents=False,
    )
    strategy = result.get("manufacturing_strategy") or {}
    unit_data = result.get("unit_data") or {}
    bom = result.get("bom") or {}
    components = result.get("components") or []
    work_packages = result.get("most_work_packages") or []
    financial = result.get("financial_calculation") or {}

    section("CUSTOMER INPUT")
    project = result.get("project") or {}
    print(f"Project: {project.get('project_code')}")
    print(f"Customer: {project.get('customer')} / final customer {project.get('final_customer')}")
    print(f"Product: {project.get('product_line')} - {project.get('product')} - {project.get('product_id')}")
    print(f"Annual quantity: {project.get('annual_quantity')}")
    print(f"Delivery zone: {project.get('customer_delivery_zone')}")

    section("MANUFACTURING STRATEGY")
    print(f"Production plant: {strategy.get('production_plant')}")
    print(f"Target VAN percent: {strategy.get('target_van_percent')}")
    print(f"Currency: {unit_data.get('operating_currency')} -> {unit_data.get('selling_currency')}")
    print(f"Unit table status: {unit_data.get('status')}")

    section("BOM COMPONENTS")
    for component in bom.get("normalized_components") or []:
        definition = component.get("bom_definition") or {}
        print(
            f"- {component.get('component_id')} | {component.get('component_type')} | "
            f"route={component.get('costing_route')} | qty={component.get('quantity_per_product')}"
        )
        if definition.get("missing_data"):
            print(f"  missing: {', '.join(definition.get('missing_data'))}")

    section("EXTERNAL COMPONENT COSTING OUTPUTS")
    for component in components:
        raw = component.get("agent_raw_output") or {}
        cost = component.get("normalized_cost") or {}
        print(
            f"- {component.get('component_id')} | status={raw.get('status') or component.get('costing_status')} | "
            f"delivered={money(cost.get('delivered_cost_per_piece'))} {cost.get('currency')}"
        )
        if raw.get("recommended_offer", {}).get("confirmation_gaps"):
            print(f"  gaps: {', '.join(raw['recommended_offer']['confirmation_gaps'])}")

    section("MOST COMPONENT-OPERATION OUTPUTS")
    for work_package in work_packages:
        operation = work_package.get("normalized_operation") or {}
        print(
            f"- {work_package.get('work_package_id')} | component={work_package.get('component_id')} | "
            f"operation={work_package.get('operation_name')} | p_h={operation.get('p_h')} | "
            f"oee={operation.get('oee')} | operator={operation.get('operator_percent')}"
        )

    section("DL/VOH MATHEMATICAL RESULT")
    print(f"DL cost per piece: {money(financial.get('dl_cost_per_piece'))} {financial.get('currency')}")
    print(f"VOH cost per piece: {money(financial.get('voh_cost_per_piece'))} {financial.get('currency')}")
    print(f"Tooling adder per piece: {money(financial.get('tooling_adder_per_piece'))} {financial.get('currency')}")
    print("Material cost breakdown:")
    for line in financial.get("material_cost_breakdown") or []:
        print(
            f"  - {line.get('component_id')}: {money(line.get('delivered_cost_per_piece'))} "
            f"{line.get('currency')} ({line.get('status')})"
        )

    section("FINAL PRELIMINARY DEMO COST")
    print(f"Material cost per piece: {money(financial.get('material_cost_per_piece'))} {financial.get('currency')}")
    print(f"Preliminary direct cost per piece: {money(financial.get('preliminary_direct_cost_per_piece'))} {financial.get('currency')}")
    print(f"Financial status: {financial.get('status')}")
    print("Commercially usable: false")

    section("MISSING CONFIRMATIONS")
    for item in result.get("missing_inputs") or []:
        print(f"- {item}")

    section("SAVED JSON PATH")
    print(result.get("orchestration_result_absolute_path"))


if __name__ == "__main__":
    main()
