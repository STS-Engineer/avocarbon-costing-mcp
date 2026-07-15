from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, successful_most, workflow

try:
    project, product = create_most_ready_workflow()
    install_most_trigger_spy()
    triggered = workflow.trigger_most_operations(project, product, dry_run=True)
    for work_package_id in triggered["process_decomposition"]["required_work_package_ids"]:
        result = workflow.save_most_output(project, product, work_package_id, successful_most(work_package_id))
    assert result["state_status_after"] == "most_received", result
    assert result["remaining_work_packages"] == [], result
    assert result["state"]["current_step"] == "Step 4 Final Calculation", result
    assert result["state"]["missing_outputs"] == [], result
    print("PASS: all required MOST outputs advance to final calculation")
finally:
    cleanup()
