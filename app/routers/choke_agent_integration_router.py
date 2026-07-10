from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services.agent_writeback_service import (
    calculate_choke_from_saved_agent_outputs,
    get_costing_run_status,
    save_choke_bom_result,
    save_component_costing_result,
    save_most_operation_result,
)


router = APIRouter(prefix="/api/agent-writeback", tags=["Agent Write-Back"])


class SaveBomRequest(BaseModel):
    project_code: str
    product_id: str
    agent_name: str = "Choke BOM Analyzer"
    raw_json: Dict[str, Any] = Field(default_factory=dict)
    save_to_database: bool = False


class SaveComponentRequest(BaseModel):
    project_code: str
    product_id: str
    component_id: str
    component_type: str = ""
    agent_name: str = "External Component Costing Agent"
    raw_json: Dict[str, Any] = Field(default_factory=dict)
    save_to_database: bool = False


class SaveMostRequest(BaseModel):
    project_code: str
    product_id: str
    work_package_id: str
    component_id: str = ""
    operation_id: str = ""
    operation_name: str = ""
    agent_name: str = "Estimateur MOST Assemblage"
    raw_json: Dict[str, Any] = Field(default_factory=dict)
    save_to_database: bool = False


class CalculateSavedOutputsRequest(BaseModel):
    project_code: str
    product_id: str
    input_file: str = "data/customer_inputs/byd_3165001.json"


def _handle_errors(callback):
    try:
        return callback()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/save-bom")
def save_bom(request: SaveBomRequest):
    return _handle_errors(lambda: save_choke_bom_result(
        project_code=request.project_code,
        product_id=request.product_id,
        agent_name=request.agent_name,
        raw_json=request.raw_json,
        save_to_database=request.save_to_database,
    ))


@router.post("/save-component")
def save_component(request: SaveComponentRequest):
    return _handle_errors(lambda: save_component_costing_result(
        project_code=request.project_code,
        product_id=request.product_id,
        component_id=request.component_id,
        component_type=request.component_type,
        agent_name=request.agent_name,
        raw_json=request.raw_json,
        save_to_database=request.save_to_database,
    ))


@router.post("/save-most")
def save_most(request: SaveMostRequest):
    return _handle_errors(lambda: save_most_operation_result(
        project_code=request.project_code,
        product_id=request.product_id,
        work_package_id=request.work_package_id,
        component_id=request.component_id,
        operation_id=request.operation_id,
        operation_name=request.operation_name,
        agent_name=request.agent_name,
        raw_json=request.raw_json,
        save_to_database=request.save_to_database,
    ))


@router.get("/status/{project_code}/{product_id}")
def run_status(project_code: str, product_id: str):
    return _handle_errors(lambda: get_costing_run_status(project_code, product_id))


@router.post("/calculate")
def calculate_from_saved_outputs(request: CalculateSavedOutputsRequest):
    return _handle_errors(lambda: calculate_choke_from_saved_agent_outputs(
        project_code=request.project_code,
        product_id=request.product_id,
        input_file=request.input_file,
    ))
