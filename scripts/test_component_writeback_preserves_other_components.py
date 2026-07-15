from component_step2_test_support import cleanup, create_workflow, install_trigger_spy, successful_raw, workflow

try:
    project, product = create_workflow()
    install_trigger_spy()
    workflow.trigger_next_component_costing(project, product, dry_run=True)
    result = workflow.save_component_output(project, product, "ferrite_core", successful_raw("ferrite_core"))
    state = result["state"]
    assert state["components"]["ferrite_core"]["status"] == "received", state
    assert state["components"]["magnet_wire"]["status"] == "triggered", state
    assert state["components"]["lead_tinning"]["status"] == "triggered", state
    assert result["remaining_component_ids"] == ["magnet_wire", "lead_tinning"], result
    print("PASS: one component write-back preserves all other component states")
finally:
    cleanup()
