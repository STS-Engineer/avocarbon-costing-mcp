from component_step2_test_support import cleanup, create_workflow, successful_raw, workflow

try:
    project, product = create_workflow()
    for component_id in ["ferrite_core", "magnet_wire", "lead_tinning"]:
        result = workflow.save_component_output(project, product, component_id, successful_raw(component_id))
    assert result["state_status_after"] == "components_received", result
    assert result["remaining_component_ids"] == [], result
    assert result["state"]["current_step"] == "Step 3 MOST Agent", result
    assert result["state"]["missing_outputs"] == [], result
    print("PASS: all external component outputs advance workflow to MOST")
finally:
    cleanup()
