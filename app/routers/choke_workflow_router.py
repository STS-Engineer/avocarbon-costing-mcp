import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.choke_sequential_agent_workflow import (
    calculate_final_choke_costing_from_saved_outputs,
    calculate_from_real_outputs,
    get_bom_output,
    get_writeback_debug,
    get_workflow_debug,
    get_workflow_state,
    save_bom_output,
    save_component_output,
    save_most_output,
    retry_bom_agent,
    run_storage_self_test,
    test_bom_agent_trigger,
    start_real_choke_workflow,
    trigger_most_operations,
    trigger_next_component_costing,
    update_commercial_fields,
)
from services.project_data_paths import (
    CustomerInputFileNotFound,
    get_workflow_run_paths,
)


router = APIRouter(prefix="/api/choke-workflow", tags=["Choke Sequential Workflow"])


class StartWorkflowRequest(BaseModel):
    input_file: str
    dry_run: bool = False


class SaveBomOutputRequest(BaseModel):
    project_code: str
    product_id: str
    raw_json: Dict[str, Any] = Field(default_factory=dict)


class TriggerStageRequest(BaseModel):
    project_code: str
    product_id: str
    dry_run: bool = False


class RetryBomRequest(BaseModel):
    project_code: str
    product_id: str


class TestBomAgentTriggerRequest(BaseModel):
    project_code: str
    product_id: str
    drawing_file_url: str
    drawing_reference: Optional[str] = None


class SaveComponentOutputRequest(BaseModel):
    project_code: str
    product_id: str
    component_id: str
    raw_json: Dict[str, Any] = Field(default_factory=dict)


class SaveMostOutputRequest(BaseModel):
    project_code: str
    product_id: str
    work_package_id: str
    raw_json: Dict[str, Any] = Field(default_factory=dict)


class CalculateRealOutputsRequest(BaseModel):
    project_code: str
    product_id: str
    unit_data: Dict[str, Any] | None = None


class UpdateCommercialFieldsRequest(BaseModel):
    project_code: str
    product_id: str
    customer: str | None = None
    final_customer: str | None = None
    customer_delivery_zone: str | None = None
    annual_quantity: float | None = None
    currency: str | None = None
    target_price: float | None = None
    sop_date: str | None = None


def _handle(callback):
    try:
        return callback()
    except CustomerInputFileNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.details) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/start")
def start_workflow(request: Request, payload: StartWorkflowRequest):
    return _handle(lambda: start_real_choke_workflow(
        input_file=payload.input_file,
        dry_run=payload.dry_run,
        request_base_url=str(request.base_url),
    ))


@router.post("/storage-self-test")
def storage_self_test():
    return _handle(run_storage_self_test)


@router.get("/status/{project_code}/{product_id}")
def workflow_status(project_code: str, product_id: str):
    return _handle(lambda: get_workflow_state(project_code, product_id))


@router.get("/debug/{project_code}/{product_id}")
def workflow_debug(project_code: str, product_id: str):
    return _handle(lambda: get_workflow_debug(project_code, product_id))


@router.get("/writeback-debug/{project_code}/{product_id}")
def workflow_writeback_debug(project_code: str, product_id: str):
    return _handle(lambda: get_writeback_debug(project_code, product_id))


@router.get("/bom-output/{project_code}/{product_id}")
def bom_output(project_code: str, product_id: str):
    return _handle(lambda: get_bom_output(project_code, product_id))


@router.post("/update-commercial-fields")
def update_workflow_commercial_fields(request: UpdateCommercialFieldsRequest):
    dump = getattr(request, "model_dump", request.dict)
    fields = dump(exclude={"project_code", "product_id"}, exclude_unset=True)
    return _handle(lambda: update_commercial_fields(
        project_code=request.project_code,
        product_id=request.product_id,
        fields=fields,
    ))


@router.post("/save-bom-output")
def save_bom(request: SaveBomOutputRequest):
    return _handle(lambda: save_bom_output(
        project_code=request.project_code,
        product_id=request.product_id,
        raw_json=request.raw_json,
    ))


@router.post("/retry-bom")
def retry_bom(request: RetryBomRequest):
    return _handle(lambda: retry_bom_agent(
        project_code=request.project_code,
        product_id=request.product_id,
    ))


@router.post("/test-bom-agent-trigger")
def test_bom_trigger(request: TestBomAgentTriggerRequest):
    return _handle(lambda: test_bom_agent_trigger(
        project_code=request.project_code,
        product_id=request.product_id,
        drawing_file_url=request.drawing_file_url,
        drawing_reference=request.drawing_reference,
    ))


@router.post("/trigger-components")
def trigger_components(request: TriggerStageRequest):
    return _handle(lambda: trigger_next_component_costing(
        project_code=request.project_code,
        product_id=request.product_id,
        dry_run=request.dry_run,
    ))


@router.post("/save-component-output")
def save_component(request: SaveComponentOutputRequest):
    return _handle(lambda: save_component_output(
        project_code=request.project_code,
        product_id=request.product_id,
        component_id=request.component_id,
        raw_json=request.raw_json,
    ))


@router.post("/trigger-most")
def trigger_most(request: TriggerStageRequest):
    return _handle(lambda: trigger_most_operations(
        project_code=request.project_code,
        product_id=request.product_id,
        dry_run=request.dry_run,
    ))


@router.post("/save-most-output")
def save_most(request: SaveMostOutputRequest):
    return _handle(lambda: save_most_output(
        project_code=request.project_code,
        product_id=request.product_id,
        work_package_id=request.work_package_id,
        raw_json=request.raw_json,
    ))


@router.post("/calculate-from-real-outputs")
def calculate_real_outputs(request: CalculateRealOutputsRequest):
    return _handle(lambda: calculate_from_real_outputs(
        project_code=request.project_code,
        product_id=request.product_id,
    ))


@router.post("/calculate-final")
def calculate_final_outputs(request: CalculateRealOutputsRequest):
    return _handle(lambda: calculate_final_choke_costing_from_saved_outputs(
        project_code=request.project_code,
        product_id=request.product_id,
        unit_data_override=request.unit_data,
    ))


@router.get("/final-result/{project_code}/{product_id}")
def get_final_result(project_code: str, product_id: str):
    if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in [project_code, product_id]):
        raise HTTPException(status_code=400, detail="Invalid project_code or product_id")
    path = (
        get_workflow_run_paths(project_code, product_id)["run_dir"]
        / "final_choke_costing_result.json"
    )
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Final Choke costing result not found. "
                "Call POST /api/choke-workflow/calculate-final after all agent outputs are saved."
            ),
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Saved final result is not valid JSON") from exc
