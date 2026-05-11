"""CLI for the lab."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer
import yaml

from .graph import build_graph
from .metrics import MetricsReport, metric_from_state, summarize_metrics, write_metrics
from .persistence import build_checkpointer
from .report import write_report
from .scenarios import load_scenarios
from .state import Route, initial_state

app = typer.Typer(no_args_is_help=True)


@app.command("run-scenarios")
def run_scenarios(
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Print per-scenario progress logs.")] = False,
) -> None:
    """Run all grading scenarios and write metrics JSON."""
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    scenarios = load_scenarios(cfg["scenarios_path"])
    if verbose:
        typer.echo(f"Loaded {len(scenarios)} scenarios from {cfg['scenarios_path']}")

    checkpointer = build_checkpointer(cfg.get("checkpointer", "memory"), cfg.get("database_url"))
    graph = build_graph(checkpointer=checkpointer)
    metrics = []
    for idx, scenario in enumerate(scenarios, start=1):
        if verbose:
            typer.echo(f"[{idx}/{len(scenarios)}] START scenario={scenario.id}")

        state = initial_state(scenario)
        run_config = {"configurable": {"thread_id": state["thread_id"]}}
        final_state = graph.invoke(state, config=run_config)
        metric = metric_from_state(final_state, scenario.expected_route.value, scenario.requires_approval)
        metrics.append(metric)

        if verbose:
            typer.echo(
                f"[{idx}/{len(scenarios)}] DONE scenario={scenario.id} "
                f"route={metric.actual_route} success={metric.success} retries={metric.retry_count}"
            )

    report = summarize_metrics(metrics)
    write_metrics(report, output)
    if cfg.get("report_path"):
        write_report(report, cfg["report_path"])

    if verbose:
        typer.echo(
            "Summary: "
            f"success_rate={report.success_rate:.2%}, "
            f"total_retries={report.total_retries}, "
            f"total_interrupts={report.total_interrupts}"
        )
    typer.echo(f"Wrote metrics to {output}")


@app.command("validate-metrics")
def validate_metrics(metrics: Annotated[Path, typer.Option("--metrics")]) -> None:
    """Validate metrics JSON schema for grading."""
    payload = json.loads(metrics.read_text(encoding="utf-8"))
    report = MetricsReport.model_validate(payload)
    if report.total_scenarios < 6:
        raise typer.BadParameter("Expected at least 6 scenarios")
    typer.echo(f"Metrics valid. success_rate={report.success_rate:.2%}")


@app.command("export-diagram")
def export_diagram(
    output: Annotated[Path, typer.Option("--output")] = Path("outputs/graph.mmd"),
) -> None:
    """Export graph architecture as Mermaid text for report/demo evidence."""
    graph = build_graph(checkpointer=None)
    mermaid = graph.get_graph().draw_mermaid()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    typer.echo(f"Wrote Mermaid diagram to {output}")


def _load_cfg_and_graph(config: Path):
    """Load config and return a compiled graph with configured checkpointer."""
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    checkpointer = build_checkpointer(cfg.get("checkpointer", "memory"), cfg.get("database_url"))
    return cfg, build_graph(checkpointer=checkpointer)


def _pick_risky_scenario(config_payload: dict, scenario_id: str | None):
    """Pick a risky scenario for HITL demonstrations."""
    scenarios = load_scenarios(config_payload["scenarios_path"])
    if scenario_id:
        for scenario in scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise typer.BadParameter(f"Scenario id '{scenario_id}' not found")

    for scenario in scenarios:
        if scenario.expected_route == Route.RISKY:
            return scenario
    raise typer.BadParameter("No risky scenario found in scenarios file")


@app.command("run-hitl-once")
def run_hitl_once(
    config: Annotated[Path, typer.Option("--config")] = Path("configs/lab.yaml"),
    scenario_id: Annotated[str | None, typer.Option("--scenario-id")] = None,
    thread_id: Annotated[str | None, typer.Option("--thread-id")] = None,
) -> None:
    """Run until approval interrupt and persist checkpoint for later resume."""
    cfg, graph = _load_cfg_and_graph(config)
    scenario = _pick_risky_scenario(cfg, scenario_id)
    state = initial_state(scenario)

    # Reuse thread_id if provided, otherwise generate a stable demo id.
    if thread_id:
        state["thread_id"] = thread_id
    else:
        state["thread_id"] = f"hitl-{scenario.id}-{uuid.uuid4().hex[:8]}"

    run_config = {"configurable": {"thread_id": state["thread_id"]}}

    # Enable real interrupt behavior for this run.
    previous_interrupt_flag = os.getenv("LANGGRAPH_INTERRUPT")
    os.environ["LANGGRAPH_INTERRUPT"] = "true"
    try:
        result = graph.invoke(state, config=run_config)
    finally:
        if previous_interrupt_flag is None:
            os.environ.pop("LANGGRAPH_INTERRUPT", None)
        else:
            os.environ["LANGGRAPH_INTERRUPT"] = previous_interrupt_flag

    interrupts = result.get("__interrupt__", [])
    typer.echo(f"thread_id={state['thread_id']}")
    typer.echo(f"scenario_id={scenario.id}")
    if interrupts:
        typer.echo(f"interrupt_count={len(interrupts)}")
        for idx, item in enumerate(interrupts, start=1):
            value = getattr(item, "value", item)
            typer.echo(f"interrupt[{idx}]={value}")
        typer.echo("Graph paused at approval. Resume with command: resume-hitl")
    else:
        typer.echo("No interrupt returned. Check LANGGRAPH_INTERRUPT and checkpointer configuration.")


@app.command("resume-hitl")
def resume_hitl(
    config: Annotated[Path, typer.Option("--config")] = Path("configs/lab.yaml"),
    thread_id: Annotated[str, typer.Option("--thread-id")] = ...,
    approved: Annotated[bool, typer.Option("--approved/--rejected")] = True,
    reviewer: Annotated[str, typer.Option("--reviewer")] = "human-reviewer",
    comment: Annotated[str, typer.Option("--comment")] = "approved via resume-hitl",
) -> None:
    """Resume a paused HITL thread from checkpoint using Command(resume=...)."""
    cfg, graph = _load_cfg_and_graph(config)
    _ = cfg  # keep linter satisfied while preserving config loading semantics
    run_config = {"configurable": {"thread_id": thread_id}}

    from langgraph.types import Command

    # Optional visibility: warn if thread is not currently paused at an interrupt.
    current_state = graph.get_state(run_config)
    pending_next = list(getattr(current_state, "next", ()) or [])
    if not pending_next:
        typer.echo(
            "Warning: thread has no pending next node (it may already be completed). "
            "Resume payload may not have any effect."
        )

    decision_payload = {
        "approved": approved,
        "reviewer": reviewer,
        "comment": comment,
    }

    # Ensure approval_node executes interrupt branch so resume payload is consumed.
    previous_interrupt_flag = os.getenv("LANGGRAPH_INTERRUPT")
    os.environ["LANGGRAPH_INTERRUPT"] = "true"
    try:
        result = graph.invoke(Command(resume=decision_payload), config=run_config)
    finally:
        if previous_interrupt_flag is None:
            os.environ.pop("LANGGRAPH_INTERRUPT", None)
        else:
            os.environ["LANGGRAPH_INTERRUPT"] = previous_interrupt_flag

    typer.echo(f"Resumed thread_id={thread_id}")
    typer.echo(f"route={result.get('route')}")
    typer.echo(f"attempt={result.get('attempt')}")
    typer.echo(f"final_answer={result.get('final_answer')}")


@app.command("show-history")
def show_history(
    config: Annotated[Path, typer.Option("--config")] = Path("configs/lab.yaml"),
    thread_id: Annotated[str, typer.Option("--thread-id")] = ...,
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """Show checkpoint history for a thread (useful for crash-recovery evidence)."""
    _cfg, graph = _load_cfg_and_graph(config)
    run_config = {"configurable": {"thread_id": thread_id}}
    snapshots = list(graph.get_state_history(run_config, limit=limit))

    typer.echo(f"thread_id={thread_id} checkpoints={len(snapshots)}")
    for idx, snap in enumerate(snapshots, start=1):
        values = getattr(snap, "values", {}) or {}
        next_nodes = getattr(snap, "next", ())
        snap_cfg = getattr(snap, "config", {}) or {}
        cfg_obj = snap_cfg.get("configurable", {}) if isinstance(snap_cfg, dict) else {}
        checkpoint_id = cfg_obj.get("checkpoint_id", "unknown")
        route = values.get("route")
        attempt = values.get("attempt")
        typer.echo(
            f"[{idx}] checkpoint_id={checkpoint_id} route={route} attempt={attempt} next={list(next_nodes)}"
        )


if __name__ == "__main__":
    app()
