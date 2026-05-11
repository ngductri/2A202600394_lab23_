"""Routing functions for conditional edges."""

from __future__ import annotations

from .state import AgentState, Route


def route_after_classify(state: AgentState) -> str:
    """Map classified route to the next graph node.

    Handles unknown routes safely by sending them to clarification.
    """
    # Normalize route value so routing remains stable even if upstream adds casing/spacing noise.
    route = str(state.get("route", Route.SIMPLE.value)).strip().lower()

    mapping = {
        Route.SIMPLE.value: "answer",
        Route.TOOL.value: "tool",
        Route.MISSING_INFO.value: "clarify",
        Route.RISKY.value: "risky_action",
        Route.ERROR.value: "retry",
    }

    # Safe fallback for unknown routes: ask clarification instead of guessing an action.
    return mapping.get(route, "clarify")


def route_after_retry(state: AgentState) -> str:
    """Decide whether to retry, fallback, or dead-letter.

    Implements bounded retry and dead-letter routing.
    """
    # Defensive parsing in case state has malformed values.
    try:
        attempt = int(state.get("attempt", 0))
    except (TypeError, ValueError):
        attempt = 0

    try:
        max_attempts = int(state.get("max_attempts", 3))
    except (TypeError, ValueError):
        max_attempts = 3

    # Clamp to at least 1 so termination is always guaranteed.
    max_attempts = max(1, max_attempts)

    if attempt >= max_attempts:
        return "dead_letter"
    return "tool"


def route_after_evaluate(state: AgentState) -> str:
    """Decide whether tool result is satisfactory or needs retry.

    Uses structured state output from evaluate_node.
    """
    result = str(state.get("evaluation_result", "")).strip().lower()

    if result == "needs_retry":
        return "retry"
    if result == "success":
        return "answer"

    # Unknown evaluation outputs should go through retry path for safer recovery.
    # route_after_retry enforces the retry bound, so this cannot loop forever.
    if result:
        return "retry"
    return "answer"


def route_after_approval(state: AgentState) -> str:
    """Continue only if approved.

    Supports reject/edit outcomes by routing to clarification when approval is denied
    or reviewer requests edits.
    """
    approval = state.get("approval") or {}
    approved = bool(approval.get("approved", False))
    comment = str(approval.get("comment", "")).strip().lower()

    # Reviewer requested edits or more context -> clarification branch.
    if "edit" in comment or "revise" in comment or "missing" in comment:
        return "clarify"

    return "tool" if approved else "clarify"
