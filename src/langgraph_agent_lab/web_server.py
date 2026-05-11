"""FastAPI web UI for LangGraph flow visualization and HITL control.

Run with:
    uvicorn langgraph_agent_lab.web_server:app --reload
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field

from .graph import build_graph
from .metrics import MetricsReport
from .persistence import build_checkpointer
from .scenarios import load_scenarios
from .state import Route, Scenario, initial_state

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DEFAULT_CONFIG_PATH = "configs/lab.yaml"


class RunFlowRequest(BaseModel):
    config_path: str = DEFAULT_CONFIG_PATH
    input_mode: Literal["scenario", "free"] = "scenario"
    scenario_id: str | None = None
    query: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=10)
    thread_id: str | None = None


class ResumeFlowRequest(BaseModel):
    config_path: str = DEFAULT_CONFIG_PATH
    thread_id: str
    approved: bool
    reviewer: str = "web-reviewer"
    comment: str = "decision from web ui"
    known_event_count: int = Field(default=0, ge=0)


class HistoryResponseItem(BaseModel):
    checkpoint_id: str
    route: str | None
    attempt: int | None
    next_nodes: list[str]


app = FastAPI(title="LangGraph Agent Lab Web UI")
app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


def _load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


def _build_graph_from_cfg(cfg: dict[str, Any]):
    checkpointer = build_checkpointer(cfg.get("checkpointer", "memory"), cfg.get("database_url"))
    return build_graph(checkpointer=checkpointer)


def _extract_state(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict) and chunk.get("type") == "values":
        payload = chunk.get("data")
        return payload if isinstance(payload, dict) else {}
    if isinstance(chunk, dict):
        return chunk
    return {}


def _extract_interrupts(chunk: Any) -> list[Any]:
    if isinstance(chunk, dict) and "__interrupt__" in chunk:
        return list(chunk.get("__interrupt__") or [])
    if isinstance(chunk, dict) and chunk.get("type") == "values":
        return list(chunk.get("interrupts") or [])
    return []


def _create_free_state(query: str, max_attempts: int, thread_id: str) -> dict[str, Any]:
    scenario = Scenario(
        id=f"FREE_{uuid.uuid4().hex[:8]}",
        query=query,
        expected_route=Route.SIMPLE,
        max_attempts=max_attempts,
    )
    state = initial_state(scenario)
    state["thread_id"] = thread_id
    return state


def _find_scenario(scenarios: list[Scenario], scenario_id: str | None) -> Scenario:
    if scenario_id:
        for scenario in scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise ValueError(f"Scenario '{scenario_id}' not found")
    return scenarios[0]


def _build_run_payload(req: RunFlowRequest, cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    thread_id = req.thread_id or f"web-{uuid.uuid4().hex[:8]}"

    if req.input_mode == "free":
        query = (req.query or "").strip()
        if not query:
            raise ValueError("Free query input must not be empty")
        return _create_free_state(query, req.max_attempts, thread_id), thread_id

    scenarios = load_scenarios(cfg["scenarios_path"])
    scenario = _find_scenario(scenarios, req.scenario_id)
    state = initial_state(scenario)
    state["thread_id"] = thread_id
    return state, thread_id


def _build_timeline(
    state: dict[str, Any],
    from_event_index: int,
    next_nodes: list[str],
    *,
    status: str = "done",
) -> tuple[list[dict[str, Any]], int]:
    events = state.get("events", []) or []
    new_events = events[from_event_index:]

    timeline: list[dict[str, Any]] = []
    for event in new_events:
        timeline.append(
            {
                "node": event.get("node", "unknown"),
                "status": status,
                "event_type": event.get("event_type", ""),
                "message": event.get("message", ""),
                "route": state.get("route"),
                "attempt": state.get("attempt"),
                "evaluation_result": state.get("evaluation_result"),
                "next": next_nodes,
                "thread_id": state.get("thread_id"),
            }
        )

    return timeline, len(events)


def _execute_stream(
    graph: Any,
    input_payload: Any,
    run_config: dict[str, Any],
    *,
    from_event_index: int = 0,
) -> dict[str, Any]:
    timeline: list[dict[str, Any]] = []
    event_cursor = from_event_index
    final_state: dict[str, Any] = {}
    interrupt_payload: Any | None = None

    previous_interrupt_flag = os.getenv("LANGGRAPH_INTERRUPT")
    os.environ["LANGGRAPH_INTERRUPT"] = "true"
    try:
        for chunk in graph.stream(input_payload, config=run_config, stream_mode="values"):
            state = _extract_state(chunk)
            if state:
                final_state = state
                snapshot = graph.get_state(run_config)
                next_nodes = list(getattr(snapshot, "next", ()) or [])
                steps, event_cursor = _build_timeline(state, event_cursor, next_nodes)
                timeline.extend(steps)

            interrupts = _extract_interrupts(chunk)
            if interrupts:
                first = interrupts[0]
                interrupt_payload = getattr(first, "value", first)
                timeline.append(
                    {
                        "node": "approval",
                        "status": "interrupted",
                        "event_type": "interrupt",
                        "message": "waiting for human approval",
                        "route": final_state.get("route"),
                        "attempt": final_state.get("attempt"),
                        "evaluation_result": final_state.get("evaluation_result"),
                        "next": ["approval"],
                        "thread_id": final_state.get("thread_id"),
                    }
                )
                break
    finally:
        if previous_interrupt_flag is None:
            os.environ.pop("LANGGRAPH_INTERRUPT", None)
        else:
            os.environ["LANGGRAPH_INTERRUPT"] = previous_interrupt_flag

    return {
        "thread_id": run_config["configurable"]["thread_id"],
        "timeline": timeline,
        "event_count": event_cursor,
        "interrupted": interrupt_payload is not None,
        "interrupt_payload": interrupt_payload,
        "final_state": {
            "route": final_state.get("route"),
            "attempt": final_state.get("attempt"),
            "evaluation_result": final_state.get("evaluation_result"),
            "final_answer": final_state.get("final_answer"),
            "scenario_id": final_state.get("scenario_id"),
            "events_count": len(final_state.get("events", []) or []),
        },
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/scenarios")
def api_scenarios(config_path: str = Query(DEFAULT_CONFIG_PATH)) -> dict[str, Any]:
    try:
        cfg = _load_config(config_path)
        scenarios = load_scenarios(cfg["scenarios_path"])
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "scenarios": [
            {
                "id": item.id,
                "query": item.query,
                "expected_route": item.expected_route.value,
                "max_attempts": item.max_attempts,
            }
            for item in scenarios
        ]
    }


@app.post("/api/flow/run")
def api_flow_run(req: RunFlowRequest) -> JSONResponse:
    try:
        cfg = _load_config(req.config_path)
        graph = _build_graph_from_cfg(cfg)
        payload, thread_id = _build_run_payload(req, cfg)
        run_config = {"configurable": {"thread_id": thread_id}}
        result = _execute_stream(graph, payload, run_config)
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/flow/resume")
def api_flow_resume(req: ResumeFlowRequest) -> JSONResponse:
    try:
        cfg = _load_config(req.config_path)
        graph = _build_graph_from_cfg(cfg)
        run_config = {"configurable": {"thread_id": req.thread_id}}
        decision = {
            "approved": req.approved,
            "reviewer": req.reviewer,
            "comment": req.comment,
        }
        result = _execute_stream(
            graph,
            Command(resume=decision),
            run_config,
            from_event_index=req.known_event_count,
        )
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/history")
def api_history(
    thread_id: str,
    limit: int = Query(20, ge=1, le=200),
    config_path: str = Query(DEFAULT_CONFIG_PATH),
) -> dict[str, Any]:
    try:
        cfg = _load_config(config_path)
        graph = _build_graph_from_cfg(cfg)
        snapshots = list(graph.get_state_history({"configurable": {"thread_id": thread_id}}, limit=limit))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items: list[HistoryResponseItem] = []
    for snap in snapshots:
        values = getattr(snap, "values", {}) or {}
        next_nodes = list(getattr(snap, "next", ()) or [])
        snap_cfg = getattr(snap, "config", {}) or {}
        cobj = snap_cfg.get("configurable", {}) if isinstance(snap_cfg, dict) else {}
        items.append(
            HistoryResponseItem(
                checkpoint_id=str(cobj.get("checkpoint_id", "unknown")),
                route=values.get("route"),
                attempt=values.get("attempt"),
                next_nodes=next_nodes,
            )
        )

    return {"thread_id": thread_id, "count": len(items), "items": [item.model_dump() for item in items]}


@app.get("/api/metrics")
def api_metrics(metrics_path: str = Query("outputs/metrics.json")) -> dict[str, Any]:
    path = Path(metrics_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Metrics file not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = MetricsReport.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return report.model_dump()


@app.get("/api/diagram")
def api_diagram(config_path: str = Query(DEFAULT_CONFIG_PATH)) -> dict[str, str]:
    try:
        _ = _load_config(config_path)
        graph = build_graph(checkpointer=None)
        mermaid = graph.get_graph().draw_mermaid()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"mermaid": mermaid}
