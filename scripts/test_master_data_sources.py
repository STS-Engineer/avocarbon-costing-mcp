import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.costing_master_data_service import (
    get_master_manufacturing_strategy,
    get_master_unit_data,
)


CASES = [
    ("Chokes", "Fuse choke", "China South Pacific"),
    ("Chokes", "Rod choke", "Europe"),
]


def print_case(product_line, product, delivery_zone):
    strategy = get_master_manufacturing_strategy(product_line, product, delivery_zone)
    unit_data = get_master_unit_data(strategy.get("production_plant"))
    print(f"{product_line} / {product} / {delivery_zone}")
    print(f"manufacturing strategy source used: {strategy.get('source')}")
    print(f"production plant: {strategy.get('production_plant')}")
    print(f"unit data source used: {unit_data.get('source')}")
    print(f"operating currency: {unit_data.get('operating_currency')}")
    print(f"selling currency: {unit_data.get('selling_currency')}")
    print(f"DL rate: {unit_data.get('dl_rate_operating_per_hour')}")
    print(f"VOH rate: {unit_data.get('voh_rate_operating_per_hour')}")
    print(f"open hours per year: {unit_data.get('open_hours_per_year')}")
    print(f"unit status: {unit_data.get('status')}")
    print("-" * 78)


def main():
    for case in CASES:
        print_case(*case)


if __name__ == "__main__":
    main()
