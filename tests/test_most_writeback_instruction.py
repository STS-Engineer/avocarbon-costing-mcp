import inspect
import json
from pathlib import Path

import server
from services import choke_sequential_agent_workflow as workflow
from services.choke_writeback_mcp_diagnostic import WRITEBACK_TOOL_SCHEMAS


OLD_MOST_INSTRUCTION = "Analyze only this work package and call save_most_output."
BOM_INSTRUCTION = (
    "Analyze the drawing according to your permanent agent instructions. "
    "After producing the complete BOM JSON, call save_bom_output exactly once "
    "with the exact project_code, product_id, trigger_run_id, and raw_json. "
    "The backend accepts completion only from this correlated write-back."
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
    assert "mcp__" not in instruction
    assert "generated runtime callable name" in instruction
    for forbidden_fallback in (
        "create_or_update_component",
        "create_or_update_bom_line",
        "save_component_output",
        "save_component_costing_result",
        "store_agent_json",
        "import_agent_costing_package",
    ):
        assert forbidden_fallback in instruction
    assert "MOST_WRITEBACK_ACTION_NOT_BOUND" in instruction
    for required_name in (
        "project_code",
        "product_id",
        "work_package_id",
        "most_scope_id",
        "trigger_run_id",
        "raw_json",
    ):
        assert required_name in instruction
    assert "Copy trigger_run_id exactly from this input" in instruction
    assert "never invent or omit it" in instruction
    assert "MOST_WRITEBACK_BLOCKED" in instruction
    assert "Confirm the save_most_output success response" in instruction
    assert instruction != OLD_MOST_INSTRUCTION
    assert "call save_most_output" not in instruction


def test_most_payload_fields_and_identity_are_unchanged():
    payload = workflow._most_trigger_payload(_state(), _work_package())

    assert payload["project_code"] == "TEST-PROJECT"
    assert payload["product_id"] == "TEST-PRODUCT"
    assert payload["work_package_id"] == "wp_20_wire_winding"
    assert payload["most_scope_id"] == "wp_20_wire_winding"
    assert isinstance(payload["trigger_run_id"], str)
    assert payload["operation_id"] is None
    assert payload["operation_name"] == "Wire winding"
    assert payload["component_ids"] == ["magnet_wire"]
    assert payload["technical_inputs"] == {"turns": 13}
    assert payload["annual_quantity"] == 600000
    assert payload["production_plant"] == "Kunshan"
    assert payload["unit_data"] == {"status": "found", "plant": "Kunshan"}
    assert payload["save_address"].endswith("agent_outputs/most/wp_20_wire_winding.json")


def test_bom_instruction_unchanged_and_component_instruction_requires_pricing_basis():
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
    # The component instruction was hardened (Phase 6) to require an explicit
    # pricing basis/currency for every priced value, closing the unit-mismatch
    # gap that let a wire developed-length get costed as if it were a kg price.
    assert component["instruction"].startswith(workflow.COMPONENT_COSTING_INSTRUCTION)
    assert "raw enameled wire material only" in component["instruction"]
    assert "unit_price_basis" in component["instruction"]
    assert "unit_price_currency" in component["instruction"]
    assert "transportation_cost_basis" in component["instruction"]


def test_connector_diagnostic_and_openapi_require_trigger_run_id():
    assert "trigger_run_id" in WRITEBACK_TOOL_SCHEMAS["save_most_output"]["required"]

    schema = json.loads(
        Path("docs/choke_agent_writeback_openapi.json").read_text(encoding="utf-8")
    )
    request_schema = schema["components"]["schemas"]["SaveMostOutputRequest"]
    assert "trigger_run_id" in request_schema["required"]
    assert request_schema["properties"]["trigger_run_id"]["type"] == "string"
    assert {item["type"] for item in request_schema["properties"]["raw_json"]["oneOf"]} == {
        "object",
        "string",
    }

    runtime_signature = inspect.signature(server.save_most_output)
    parameter = runtime_signature.parameters["trigger_run_id"]
    assert parameter.default is inspect.Parameter.empty
