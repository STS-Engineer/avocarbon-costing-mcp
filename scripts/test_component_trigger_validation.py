from component_step2_test_support import cleanup, create_workflow, workflow

try:
    project, product = create_workflow(customer_overrides={"annual_quantity": None})
    blocked = workflow.trigger_next_component_costing(project, product, dry_run=True)
    assert blocked["status"] == "blocked", blocked
    assert blocked["missing_inputs"] == ["annual_quantity"], blocked

    state = workflow._load_state(project, product)
    state["customer_input"]["annual_quantity"] = 600000
    state["manufacturing_strategy"] = {"status": "not_found", "available_product_candidates": ["Fuse chokes"]}
    workflow.get_master_manufacturing_strategy = lambda *args: state["manufacturing_strategy"]
    workflow.get_master_unit_data = lambda plant: {"status": "missing_unit_data", "plant": None}
    workflow._save_state(state)
    missing_strategy = workflow.trigger_next_component_costing(project, product, dry_run=True)
    assert missing_strategy["status"] == "strategy_not_found", missing_strategy
    assert missing_strategy["missing_inputs"] == [], missing_strategy
    assert "manufacturing_strategy" in missing_strategy["dependent_missing_inputs"], missing_strategy
    assert missing_strategy["product"] == "Fuse choke", missing_strategy
    print("PASS: direct and dependent Step 2 inputs are reported separately")
finally:
    cleanup()
