import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.choke_orchestrator import run_choke_orchestration


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def value(data, key, default=""):
    return data.get(key) if data.get(key) is not None else default


def money(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.6f}"
    return str(value)


def load_customer_input(path):
    input_path = Path(path)
    if not input_path.is_absolute():
        input_path = ROOT_DIR / input_path
    with input_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def mode_from_args(args):
    if args.full_demo:
        return {
            "dry_run": True,
            "trigger_agents": False,
            "full_demo_mode": True,
            "demo_override": True,
            "mode_label": "full_demo",
        }
    if args.trigger_agents:
        return {
            "dry_run": False,
            "trigger_agents": True,
            "full_demo_mode": False,
            "demo_override": True,
            "mode_label": "trigger_agents",
        }
    return {
        "dry_run": True,
        "trigger_agents": False,
        "full_demo_mode": False,
        "demo_override": True,
        "mode_label": "dry_run",
    }


def print_customer_input(project):
    section("CUSTOMER INPUT")
    print(f"project_code: {value(project, 'project_code')}")
    print(f"customer: {value(project, 'customer')}")
    print(f"product: {value(project, 'product')}")
    print(f"product_id: {value(project, 'product_id')}")
    print(f"customer_delivery_zone: {value(project, 'customer_delivery_zone')}")
    print(f"annual_quantity: {value(project, 'annual_quantity')}")


def print_manufacturing_decision(project, strategy):
    section("MANUFACTURING DECISION")
    print(
        f"Product {project.get('product')} delivered to "
        f"{project.get('customer_delivery_zone')} will be manufactured in "
        f"{strategy.get('production_plant')}."
    )


def print_unit_data(unit_data):
    section("UNIT / FACTORY DATA")
    print(f"operating_currency: {value(unit_data, 'operating_currency')}")
    print(f"selling_currency: {value(unit_data, 'selling_currency')}")
    print(f"DL rate: {value(unit_data, 'dl_rate_operating_per_hour')}")
    print(f"VOH rate: {value(unit_data, 'voh_rate_operating_per_hour')}")
    print(f"open hours per year: {value(unit_data, 'open_hours_per_year')}")
    print(f"source: {value(unit_data, 'source')}")


def print_bom(envelope, full_demo):
    section("BOM AGENT / BOM OUTPUT")
    bom = envelope.get("bom") or {}
    if full_demo:
        print(f"BOM status: {bom.get('status')}")
        for component in bom.get("normalized_components") or []:
            print(
                f"- {component.get('component_id')} | "
                f"{component.get('component_type')} | "
                f"route={component.get('costing_route')} | "
                f"qty={component.get('quantity_per_product')}"
            )
    else:
        bom_agent = (envelope.get("agent_orchestration") or {}).get("bom_agent") or {}
        print(f"BOM agent status: {bom_agent.get('status')}")
        print(f"BOM save_address: {bom_agent.get('save_address')}")
        if bom_agent.get("agent_id"):
            print(f"BOM agent_id: {bom_agent.get('agent_id')}")


def print_component_costing(envelope):
    section("COMPONENT COSTING")
    components = envelope.get("components") or []
    if not components:
        print("No component costing rows available yet.")
        return
    for component in components:
        cost = component.get("normalized_cost") or {}
        raw = component.get("agent_raw_output") or {}
        print(
            f"- {component.get('component_id')} | "
            f"{component.get('component_type')} | "
            f"status={raw.get('status') or component.get('costing_status')} | "
            f"save_address={component.get('costing_save_address')} | "
            f"delivered_cost_per_piece={money(cost.get('delivered_cost_per_piece'))} | "
            f"commercially_usable={cost.get('commercially_usable')}"
        )


def print_most_work_packages(envelope):
    section("MOST COMPONENT-OPERATION WORK PACKAGES")
    work_packages = envelope.get("most_work_packages") or []
    if not work_packages:
        print("No MOST work packages available yet.")
        return
    for work_package in work_packages:
        operation = work_package.get("normalized_operation") or {}
        print(
            f"- {work_package.get('work_package_id')} | "
            f"component_id={work_package.get('component_id')} | "
            f"operation_id={work_package.get('operation_id')} | "
            f"operation_name={work_package.get('operation_name')} | "
            f"p_h={operation.get('p_h')} | "
            f"oee={operation.get('oee')} | "
            f"operator_percent={operation.get('operator_percent')} | "
            f"save_address={work_package.get('most_save_address')}"
        )


def print_financial(envelope):
    section("FINANCIAL CALCULATION")
    financial = envelope.get("financial_calculation") or {}
    print(f"material_cost_per_piece: {money(financial.get('material_cost_per_piece'))}")
    print(f"dl_cost_per_piece: {money(financial.get('dl_cost_per_piece'))}")
    print(f"voh_cost_per_piece: {money(financial.get('voh_cost_per_piece'))}")
    print(f"tooling_adder_per_piece: {money(financial.get('tooling_adder_per_piece'))}")
    print(f"preliminary_direct_cost_per_piece: {money(financial.get('preliminary_direct_cost_per_piece'))}")
    print(f"currency: {value(financial, 'currency')}")


def print_missing(envelope):
    section("MISSING INPUTS / CONFIRMATIONS")
    missing = envelope.get("missing_inputs") or []
    if not missing:
        print("None")
        return
    for item in missing:
        print(f"- {item}")


def print_saved_path(envelope):
    section("SAVED STANDARD JSON")
    print(f"path: {envelope.get('orchestration_result_absolute_path')}")


def print_trigger_statuses(envelope):
    section("WORKSPACE AGENT TRIGGER STATUS")
    orchestration = envelope.get("agent_orchestration") or {}
    bom_agent = orchestration.get("bom_agent") or {}
    print(f"BOM: {bom_agent.get('status')} | save_address={bom_agent.get('save_address')}")
    for call in orchestration.get("component_agent_calls") or []:
        print(
            f"External Component: {call.get('component_id')} | "
            f"status={call.get('status')} | save_address={call.get('save_address')}"
        )
    for call in orchestration.get("most_agent_calls") or []:
        print(
            f"MOST: {call.get('work_package_id')} | "
            f"status={call.get('status')} | save_address={call.get('save_address')}"
        )
    print("Agent output pending MCP/write-back or manual JSON loading.")


def parse_args():
    parser = argparse.ArgumentParser(description="Run Choke orchestrator from a customer input JSON file.")
    parser.add_argument("--input", required=True, help="Customer input JSON path.")
    parser.add_argument("--dry-run", action="store_true", help="Plan calls without full demo outputs.")
    parser.add_argument("--full-demo", action="store_true", help="Use controlled demo outputs for the full visible workflow.")
    parser.add_argument("--trigger-agents", action="store_true", help="Trigger Workspace Agents.")
    parser.add_argument("--show-json", action="store_true", help="Print the final standardized JSON.")
    return parser.parse_args()


def main():
    args = parse_args()
    selected_modes = [args.dry_run, args.full_demo, args.trigger_agents]
    if sum(1 for item in selected_modes if item) > 1:
        raise SystemExit("Choose only one mode: --dry-run, --full-demo, or --trigger-agents.")

    mode = mode_from_args(args)
    customer_input = load_customer_input(args.input)
    envelope = run_choke_orchestration(
        customer_input,
        dry_run=mode["dry_run"],
        trigger_agents=mode["trigger_agents"],
        full_demo_mode=mode["full_demo_mode"],
        demo_override=mode["demo_override"],
    )

    print(f"Choke customer input runner mode: {mode['mode_label']}")
    project = envelope.get("project") or {}
    print_customer_input(project)
    print_manufacturing_decision(project, envelope.get("manufacturing_strategy") or {})
    print_unit_data(envelope.get("unit_data") or {})
    print_bom(envelope, mode["full_demo_mode"])
    print_component_costing(envelope)
    print_most_work_packages(envelope)
    print_financial(envelope)
    print_missing(envelope)
    print_saved_path(envelope)
    if mode["trigger_agents"]:
        print_trigger_statuses(envelope)
    if args.show_json:
        section("UNIFIED JSON")
        print(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
