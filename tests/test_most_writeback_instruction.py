from services import choke_sequential_agent_workflow as workflow


OLD_MOST_INSTRUCTION = "Analyze only this work package and call save_most_output."
BOM_INSTRUCTION = (
    "Analyze the drawing according to your permanent agent instructions and call "
    "save_bom_output with the complete BOM JSON."
)
COMPONENT_INSTRUCTION = (
    "Cost only this component. Return one complete JSON and call save_component_output."
)


def _state():
    return {
        "project_code": "TEST-PROJECT",
        "product_id": "TEST-PRODUCT",
        "customer_input": {
            "product": "Fuse choke",
            "product_line": "Chokes",
            "annual_quantity": 600000,
            "customer_delivery_zone": "China South Pacific",
            "currency": "RMB",
        },
        "production_plant": "Kunshan",
        "manufacturing_strategy": {"status": "found"},
        "unit_data": {"status": "found", "plant": "Kunshan"},
        "bom": {"normalized_path": "data/test/bom_normalized.json"},
    }


def _work_package():
    return {
        "work_package_id": "wp_20_wire_winding",
        "operation_name": "Wire winding",
        "component_ids": ["magnet_wire"],
        "technical_inputs": {"turns": 13},
        "annual_quantity": 600000,
        "production_plant": "Kunshan",
    }


def test_most_trigger_payload_uses_strong_writeback_instruction():
    payload = workflow._most_trigger_payload(_state(), _work_package())
    instruction = payload["instruction"]

    assert instruction == workflow.MOST_WRITEBACK_INSTRUCTION
    assert "Save one final MOST operation JSON to the backend workflow" in instruction
    assert "mcp__hoopa.mcp_kpi_costing_choke_writeback_link_as_020ff6fc1557" in instruction
    for required_name in (
        "project_code",
        "product_id",
        "work_package_id",
        "most_scope_id",
        "raw_json",
    ):
        assert required_name in instruction
    assert instruction != OLD_MOST_INSTRUCTION
    assert "call save_most_output" not in instruction


def test_most_payload_fields_and_identity_are_unchanged():
    payload = workflow._most_trigger_payload(_state(), _work_package())

    assert payload["project_code"] == "TEST-PROJECT"
    assert payload["product_id"] == "TEST-PRODUCT"
    assert payload["work_package_id"] == "wp_20_wire_winding"
    assert payload["most_scope_id"] == "wp_20_wire_winding"
    assert payload["operation_name"] == "Wire winding"
    assert payload["component_ids"] == ["magnet_wire"]
    assert payload["technical_inputs"] == {"turns": 13}
    assert payload["annual_quantity"] == 600000
    assert payload["production_plant"] == "Kunshan"
    assert payload["unit_data"] == {"status": "found", "plant": "Kunshan"}
    assert payload["save_address"].endswith("agent_outputs/most/wp_20_wire_winding.json")


def test_bom_and_component_instructions_remain_unchanged():
    bom = workflow._build_bom_trigger_payload(
        "TEST-PROJECT",
        "TEST-PRODUCT",
        {
            "drawing_file_url": "https://example.test/drawing.pdf",
            "drawing_reference": "drawing.pdf",
        },
    )
    component = workflow._component_trigger_payload(
        _state(),
        {
            "component_id": "magnet_wire",
            "component": "Enameled wire",
            "external_component_type": "enameled_wire",
            "category": "wire",
            "quantity_per_product": 1,
            "component_definition": {},
        },
    )

    assert bom["payload"]["instruction"] == BOM_INSTRUCTION
    assert component["instruction"] == COMPONENT_INSTRUCTION
