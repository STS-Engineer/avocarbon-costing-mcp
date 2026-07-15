from component_step2_test_support import cleanup, create_workflow, install_trigger_spy, successful_raw, workflow

try:
    project, product = create_workflow()
    workflow.save_component_output(project, product, "ferrite_core", successful_raw("ferrite_core"))
    calls = install_trigger_spy()
    result = workflow.trigger_next_component_costing(project, product, dry_run=True)
    assert [call["payload"]["component_id"] for call in calls] == ["magnet_wire", "lead_tinning"], calls
    assert result["skipped_components"] == [{"component_id": "ferrite_core", "status": "received", "reason": "already_processed"}], result
    print("PASS: received component outputs are not retriggered")
finally:
    cleanup()
