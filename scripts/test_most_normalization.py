from most_step3_test_support import cleanup, create_most_ready_workflow, install_most_trigger_spy, successful_most, workflow

try:
    project, product = create_most_ready_workflow()
    install_most_trigger_spy()
    triggered = workflow.trigger_most_operations(project, product, dry_run=True)
    work_package = triggered["process_decomposition"]["work_packages"][1]
    work_package_id = work_package["work_package_id"]
    workflow.save_most_output(project, product, work_package_id, successful_most(work_package_id, "Wire winding"))
    normalized = workflow.get_most_output(project, product, work_package_id)["normalized_most"]
    assert normalized["schema_version"] == "1.0", normalized
    assert normalized["method"] == "BasicMOST", normalized
    assert normalized["component_ids"] == ["magnet_wire", "ferrite_core"], normalized
    assert normalized["normal_time_seconds"] == 0.72, normalized
    assert normalized["pieces_per_hour"] == 4545.45, normalized
    print("PASS: MOST Agent output is normalized to schema 1.0")
finally:
    cleanup()
