import json
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.choke_sequential_agent_workflow import (
    calculate_final_choke_costing_from_saved_outputs,
    calculate_from_real_outputs,
    get_workflow_state,
    save_bom_output,
    save_component_output,
    save_most_output,
    start_real_choke_workflow,
    trigger_most_operations,
    trigger_next_component_costing,
)


router = APIRouter(prefix="/api/choke-workflow", tags=["Choke Sequential Workflow"])
BASE_DIR = Path(__file__).resolve().parents[2]


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


def _handle(callback):
    try:
        return callback()
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


@router.get("/status/{project_code}/{product_id}")
def workflow_status(project_code: str, product_id: str):
    return _handle(lambda: get_workflow_state(project_code, product_id))


@router.post("/save-bom-output")
def save_bom(request: SaveBomOutputRequest):
    return _handle(lambda: save_bom_output(
        project_code=request.project_code,
        product_id=request.product_id,
        raw_json=request.raw_json,
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
        BASE_DIR
        / "data"
        / "costing_runs"
        / project_code
        / product_id
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
