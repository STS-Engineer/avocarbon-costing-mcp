from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Query

from services.choke_simulation_service import (
    SimulationError,
    calculate_simulation,
    create_from_workflow,
    create_simulation,
    get_result,
    get_simulation,
    get_simulation_master_data,
    normalize_output,
    save_output,
    update_context,
    validate_envelope,
    validate_simulation,
)


router = APIRouter(prefix="/api/choke-simulation", tags=["Choke Simulation"])


def _call(action):
    try:
        return action()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SimulationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/master-data")
def master_data():
    return get_simulation_master_data()


@router.post("/normalize")
def normalize(payload: Dict[str, Any] = Body(...)):
    return _call(lambda: {
        "normalized_output": normalize_output(
            payload.get("raw_json") or {},
            payload.get("output_type"),
            payload.get("context") or {},
            payload.get("identifier"),
        )
    })


@router.post("")
def create(payload: Dict[str, Any] = Body(...)):
    return _call(lambda: create_simulation(payload))


@router.post("/from-workflow")
def from_workflow(payload: Dict[str, Any] = Body(...)):
    return _call(lambda: create_from_workflow(payload.get("project_code"), payload.get("product_id")))


@router.get("/{simulation_id}")
def read(simulation_id: str):
    return _call(lambda: get_simulation(simulation_id))


@router.put("/{simulation_id}/context")
def put_context(simulation_id: str, payload: Dict[str, Any] = Body(...)):
    return _call(lambda: update_context(simulation_id, payload))


@router.post("/{simulation_id}/bom")
def save_bom(simulation_id: str, payload: Dict[str, Any] = Body(...), replace: bool = Query(False)):
    raw = payload.get("raw_json", payload)
    return _call(lambda: save_output(simulation_id, "bom", raw, replace=replace))


@router.post("/{simulation_id}/components/{component_id}")
def save_component(simulation_id: str, component_id: str, payload: Dict[str, Any] = Body(...), replace: bool = Query(False)):
    raw = payload.get("raw_json", payload)
    return _call(lambda: save_output(simulation_id, "component_costing", raw, component_id, replace))


@router.post("/{simulation_id}/most/{component_id}")
def save_most(simulation_id: str, component_id: str, payload: Dict[str, Any] = Body(...), replace: bool = Query(False)):
    raw = payload.get("raw_json", payload)
    output_type = "most_final_assembly" if component_id == "final_assembly" else "most_component"
    return _call(lambda: save_output(simulation_id, output_type, raw, component_id, replace))


@router.post("/{simulation_id}/validate")
def validate(simulation_id: str, payload: Dict[str, Any] | None = Body(None)):
    if payload and payload.get("envelope"):
        return validate_envelope(payload["envelope"], payload.get("output_type"))
    return _call(lambda: validate_simulation(simulation_id))


@router.post("/{simulation_id}/calculate")
def calculate(simulation_id: str):
    return _call(lambda: calculate_simulation(simulation_id))


@router.get("/{simulation_id}/result")
def result(simulation_id: str):
    return _call(lambda: get_result(simulation_id))
