import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from services.choke_orchestrator import run_choke_orchestration


BASE_DIR = Path(__file__).resolve().parents[2]
router = APIRouter(prefix="/api/choke-orchestrator", tags=["Choke Orchestrator"])


class RunRequest(BaseModel):
    customer_input: Dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True
    trigger_agents: bool = False
    demo_override: bool = True
    full_demo_mode: bool = False


class TriggerAgentsRequest(BaseModel):
    customer_input: Dict[str, Any] = Field(default_factory=dict)
    trigger_bom: bool = True
    trigger_components: bool = True
    trigger_most: bool = True
    demo_override: bool = True


class CalculateRequest(BaseModel):
    customer_input: Dict[str, Any] = Field(default_factory=dict)
    bom_json: Optional[Any] = None
    component_cost_outputs: Optional[List[Any]] = None
    most_outputs: Optional[List[Any]] = None
    demo_override: bool = False


def _run_path(project_code: str, product_id: str) -> Path:
    return BASE_DIR / "data" / "costing_runs" / project_code / product_id / "orchestration_result.json"


def _trigger_statuses(envelope: Dict[str, Any]) -> Dict[str, Any]:
    orchestration = envelope.get("agent_orchestration") or {}
    bom_agent = orchestration.get("bom_agent") or {}
    component_calls = orchestration.get("component_agent_calls") or []
    most_calls = orchestration.get("most_agent_calls") or []
    return {
        "bom": {
            "agent_id": bom_agent.get("agent_id"),
            "status": bom_agent.get("status"),
            "save_address": bom_agent.get("save_address"),
            "trigger_result": bom_agent.get("trigger_result"),
        },
        "components": [
            {
                "component_id": call.get("component_id"),
                "agent_id": call.get("agent_id"),
                "status": call.get("status"),
                "save_address": call.get("save_address"),
                "trigger_result": call.get("trigger_result"),
            }
            for call in component_calls
        ],
        "most": [
            {
                "work_package_id": call.get("work_package_id"),
                "component_id": call.get("component_id"),
                "operation_id": call.get("operation_id"),
                "operation_name": call.get("operation_name"),
                "agent_id": call.get("agent_id"),
                "status": call.get("status"),
                "save_address": call.get("save_address"),
                "trigger_result": call.get("trigger_result"),
            }
            for call in most_calls
        ],
    }


def _byd_fuse_choke_input() -> Dict[str, Any]:
    return {
        "project_code": "24003-CHO-00",
        "customer": "Zhejiang NBT",
        "final_customer": "BYD",
        "product_line": "Chokes",
        "product": "Fuse choke",
        "product_id": "316-5001",
        "part_number": "316-5001",
        "drawing_reference": "316-5001-1-熔断电感-QS198102-0051 customer confirmed.pdf",
        "customer_delivery_zone": "China South Pacific",
        "annual_quantity": 600000,
        "currency": "RMB",
        "target_price": 1.5,
        "sop_date": None,
    }


def _rod_choke_europe_input() -> Dict[str, Any]:
    return {
        "project_code": "DEMO-ROD-EU",
        "product_line": "Chokes",
        "product": "Rod choke",
        "product_id": "DEMO-ROD",
        "part_number": "DEMO-ROD",
        "customer_delivery_zone": "Europe",
        "annual_quantity": 1000000,
        "drawing_reference": "demo.pdf",
    }


@router.post("/run")
def run(request: RunRequest):
    return run_choke_orchestration(
        request.customer_input,
        dry_run=request.dry_run,
        trigger_agents=request.trigger_agents,
        demo_override=request.demo_override,
        full_demo_mode=request.full_demo_mode,
    )


@router.get("/runs/{project_code}/{product_id}")
def get_run(project_code: str, product_id: str):
    path = _run_path(project_code, product_id)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Choke orchestration run not found at "
                f"data/costing_runs/{project_code}/{product_id}/orchestration_result.json"
            ),
        )

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Saved orchestration result is not valid JSON: {exc}",
        ) from exc


@router.post("/trigger-agents")
def trigger_agents(request: TriggerAgentsRequest):
    envelope = run_choke_orchestration(
        request.customer_input,
        dry_run=False,
        trigger_agents=True,
        trigger_bom=request.trigger_bom,
        trigger_components=request.trigger_components,
        trigger_most=request.trigger_most,
        demo_override=request.demo_override,
    )
    statuses = _trigger_statuses(envelope)

    envelope["trigger_statuses"] = statuses
    return {
        "trigger_statuses": statuses,
        "envelope": envelope,
    }


@router.post("/calculate")
def calculate(request: CalculateRequest):
    return run_choke_orchestration(
        request.customer_input,
        dry_run=True,
        trigger_agents=False,
        bom_json=request.bom_json,
        component_cost_outputs=request.component_cost_outputs,
        most_outputs=request.most_outputs,
        demo_override=request.demo_override,
    )


@router.get("/demo/byd-fuse-choke")
def demo_byd_fuse_choke():
    return run_choke_orchestration(
        _byd_fuse_choke_input(),
        dry_run=True,
        trigger_agents=False,
        demo_override=True,
    )


@router.get("/demo/full-byd")
def demo_full_byd():
    return run_choke_orchestration(
        _byd_fuse_choke_input(),
        dry_run=True,
        trigger_agents=False,
        demo_override=True,
        full_demo_mode=True,
    )


@router.get("/demo/rod-choke-europe")
def demo_rod_choke_europe():
    return run_choke_orchestration(
        _rod_choke_europe_input(),
        dry_run=True,
        trigger_agents=False,
        demo_override=True,
    )


@router.get("/demo-page", response_class=HTMLResponse)
def demo_page():
    path = BASE_DIR / "app" / "static" / "choke_demo.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Demo page not found.")
    return HTMLResponse(path.read_text(encoding="utf-8"))
