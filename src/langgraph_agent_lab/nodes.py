"""Node implementations for the LangGraph workflow.

Each function returns a partial state update and should not mutate input state in place.
"""

from __future__ import annotations

import re

from .state import AgentState, ApprovalDecision, Route, make_event


def intake_node(state: AgentState) -> dict:
    """Normalize raw query into state fields.

    Responsibilities implemented here:
    - Normalization of raw text
    - Lightweight PII checks + masking for logs
    - Metadata extraction for observability
    """
    # Normalize input text for stable downstream processing.
    raw_query = str(state.get("query", ""))
    query = " ".join(raw_query.strip().split())

    # Basic PII regex patterns (kept simple for lab use).
    email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    phone_pattern = r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?){2}\d{4}\b"
    card_pattern = r"\b(?:\d[ -]?){13,19}\b"

    # Mask detected PII before writing human-readable logs/messages.
    masked_query = query
    pii_types: list[str] = []

    if re.search(email_pattern, masked_query):
        pii_types.append("email")
        masked_query = re.sub(email_pattern, "[REDACTED_EMAIL]", masked_query)

    if re.search(phone_pattern, masked_query):
        pii_types.append("phone")
        masked_query = re.sub(phone_pattern, "[REDACTED_PHONE]", masked_query)

    if re.search(card_pattern, masked_query):
        pii_types.append("card_like_number")
        masked_query = re.sub(card_pattern, "[REDACTED_CARD]", masked_query)

    # Extract metadata for debugging and metrics.
    words = re.findall(r"\b[a-zA-Z0-9']+\b", query)
    metadata = {
        "query_len_chars": len(query),
        "query_len_words": len(words),
        "pii_detected": bool(pii_types),
        "pii_types": pii_types,
    }

    return {
        # Keep normalized original query in state for routing and tool logic.
        "query": query,
        # Keep only sanitized snippet in append-only message logs.
        "messages": [f"intake:{masked_query[:80]}"],
        "events": [make_event("intake", "completed", "query normalized", **metadata)],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route.

    Policy is deterministic keyword routing with priority:
    risky > tool > missing_info > error > simple
    """
    query = str(state.get("query", ""))
    normalized = query.lower()

    # Tokenization by word boundary avoids punctuation-related mismatches.
    clean_words = re.findall(r"\b[a-z0-9']+\b", normalized)
    word_set = set(clean_words)

    # Keyword groups can be tuned without changing flow architecture.
    risky_keywords = {"refund", "delete", "send", "cancel", "remove", "revoke"}
    tool_keywords = {"status", "order", "lookup", "check", "track", "find", "search"}
    error_keywords = {"timeout", "fail", "failure", "error", "crash", "unavailable"}
    vague_pronouns = {"it", "this", "that"}

    route = Route.SIMPLE
    risk_level = "low"

    # Priority is important to avoid ambiguity when multiple keywords appear.
    if word_set & risky_keywords:
        route = Route.RISKY
        risk_level = "high"
    elif word_set & tool_keywords:
        route = Route.TOOL
    elif len(clean_words) < 5 and bool(word_set & vague_pronouns):
        route = Route.MISSING_INFO
    elif word_set & error_keywords:
        route = Route.ERROR

    return {
        "route": route.value,
        "risk_level": risk_level,
        "events": [
            make_event(
                "classify",
                "completed",
                f"route={route.value}",
                token_count=len(clean_words),
            )
        ],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating.

    Generates a specific follow-up question based on available context.
    """
    query = str(state.get("query", ""))
    lowered = query.lower()

    # Keep clarification actionable and specific to common support intents.
    if "order" in lowered:
        question = "Please provide your order ID and what status or change you need."
    elif "account" in lowered:
        question = "Please provide your account ID and the exact issue you want us to handle."
    else:
        question = "Please provide missing details (for example order ID, account ID, and exact issue)."

    return {
        "pending_question": question,
        # Clarify branch can terminate in this lab graph, so we set final_answer as well.
        "final_answer": question,
        "events": [make_event("clarify", "completed", "missing information requested")],
    }


def tool_node(state: AgentState) -> dict:
    """Call a mock tool.

    Simulates transient failures for error-route scenarios to demonstrate retry loops.
    Behavior is deterministic for a given scenario/attempt to keep tests reproducible.
    """
    scenario_id = str(state.get("scenario_id", "unknown"))
    route = str(state.get("route", ""))
    attempt = int(state.get("attempt", 0))

    # Simulate transient tool failures only for ERROR route on early attempts.
    if route == Route.ERROR.value and attempt < 2:
        result = (
            "ERROR: transient failure "
            f"scenario={scenario_id} attempt={attempt} code=TRANSIENT_TIMEOUT"
        )
    else:
        result = (
            "OK: tool execution success "
            f"scenario={scenario_id} attempt={attempt} route={route or 'unknown'}"
        )

    return {
        # Keep append-only plain-text result list to match state schema.
        "tool_results": [result],
        "events": [
            make_event(
                "tool",
                "completed",
                "mock tool executed",
                scenario_id=scenario_id,
                route=route or "unknown",
                attempt=attempt,
            )
        ],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for approval.

    Builds a proposed action with explicit risk rationale for HITL review.
    """
    query = str(state.get("query", ""))
    scenario_id = str(state.get("scenario_id", "unknown"))

    proposed_action = (
        "Risky action request detected. "
        f"Scenario={scenario_id}. Proposed action: '{query}'. "
        "Risk reason: operation may change customer data/funds or trigger external side effects."
    )

    return {
        "proposed_action": proposed_action,
        "events": [
            make_event(
                "risky_action",
                "pending_approval",
                "approval required before risky action",
                scenario_id=scenario_id,
            )
        ],
    }


def approval_node(state: AgentState) -> dict:
    """Human approval step with optional LangGraph interrupt().

    Set LANGGRAPH_INTERRUPT=true to use real interrupt() for HITL demos.
    Default uses mock decision so tests and CI run offline.

    Supports:
    - approve/reject decisions
    - malformed payload fallback escalation
    - rejection-without-reason escalation note
    """
    import os

    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt

        value = interrupt(
            {
                "proposed_action": state.get("proposed_action"),
                "risk_level": state.get("risk_level"),
                "instructions": (
                    "Return {approved: bool, reviewer: str, comment: str}. "
                    "Set approved=false if rejected or edits are required."
                ),
            }
        )

        # Accept either structured dict or plain bool from human response.
        if isinstance(value, dict):
            try:
                decision = ApprovalDecision(**value)
            except Exception:
                # Escalation fallback when payload is malformed.
                decision = ApprovalDecision(
                    approved=False,
                    reviewer="system-escalation",
                    comment="Malformed approval payload; escalated for manual follow-up.",
                )
        else:
            decision = ApprovalDecision(
                approved=bool(value),
                reviewer="hitl-reviewer",
                comment="Boolean decision received from interrupt payload.",
            )
    else:
        # Offline deterministic default.
        decision = ApprovalDecision(approved=True, comment="mock approval for lab")

    # Escalate when rejection is missing reason for traceability.
    if not decision.approved and not decision.comment.strip():
        decision = ApprovalDecision(
            approved=False,
            reviewer=decision.reviewer or "system-escalation",
            comment="Rejected without reason; escalated for manual clarification.",
        )

    return {
        "approval": decision.model_dump(),
        "events": [
            make_event(
                "approval",
                "completed",
                f"approved={decision.approved}",
                reviewer=decision.reviewer,
            )
        ],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt or fallback decision.

    This node updates retry counters and diagnostics only.
    Route choice (retry vs dead_letter) is handled in routing.py.
    """
    attempt = int(state.get("attempt", 0)) + 1
    max_attempts = int(state.get("max_attempts", 3))

    # Exponential backoff metadata (capped) for observability.
    backoff_ms = min(8000, 500 * (2 ** max(0, attempt - 1)))
    exhausted = attempt >= max_attempts

    errors = [
        (
            f"transient failure attempt={attempt}/{max_attempts}; "
            f"backoff_ms={backoff_ms}; exhausted={exhausted}"
        )
    ]

    return {
        "attempt": attempt,
        "errors": errors,
        "events": [
            make_event(
                "retry",
                "completed",
                "retry attempt recorded",
                attempt=attempt,
                max_attempts=max_attempts,
                backoff_ms=backoff_ms,
                exhausted=exhausted,
            )
        ],
    }


def answer_node(state: AgentState) -> dict:
    """Produce a final response.

    Grounds output in tool results and approval context when available.
    """
    route = str(state.get("route", ""))
    tool_results = state.get("tool_results", []) or []
    approval = state.get("approval") or {}
    pending_question = state.get("pending_question")

    # Clarification route should return the targeted follow-up question.
    if route == Route.MISSING_INFO.value and pending_question:
        answer = str(pending_question)
    elif tool_results:
        # Ground answer in latest tool output for traceability.
        answer = f"I found: {tool_results[-1]}"

        # Add approval context for risky operations.
        if route == Route.RISKY.value:
            answer += (
                " | approval="
                f"{approval.get('approved', False)} reviewer={approval.get('reviewer', 'unknown')}"
            )
    elif route == Route.RISKY.value and not approval.get("approved", False):
        answer = "Action not executed because approval was denied. Please provide updated instructions."
    else:
        answer = "This is a safe mock answer. Replace with your agent response."

    return {
        "final_answer": answer,
        "events": [make_event("answer", "completed", "answer generated")],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results - the done-check that enables retry loops.

    Uses structured rule checks instead of an LLM judge in this lab implementation.
    """
    tool_results = state.get("tool_results", [])
    latest = tool_results[-1] if tool_results else ""

    # Retry when no output is returned.
    if not latest:
        return {
            "evaluation_result": "needs_retry",
            "events": [make_event("evaluate", "completed", "no tool output, retry needed")],
        }

    # Retry on explicit error marker from tool.
    if "ERROR:" in latest.upper():
        return {
            "evaluation_result": "needs_retry",
            "events": [make_event("evaluate", "completed", "tool result indicates failure, retry needed")],
        }

    # Otherwise treat as successful tool execution.
    return {
        "evaluation_result": "success",
        "events": [make_event("evaluate", "completed", "tool result satisfactory")],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Log unresolvable failures for manual review.

    Third layer of error strategy: retry -> fallback -> dead letter.
    """
    scenario_id = str(state.get("scenario_id", "unknown"))
    attempt = int(state.get("attempt", 0))
    max_attempts = int(state.get("max_attempts", 3))
    latest_error = (state.get("errors") or ["unknown error"])[-1]

    return {
        "final_answer": (
            "Request could not be completed after maximum retry attempts. "
            "It has been recorded for manual review."
        ),
        "events": [
            make_event(
                "dead_letter",
                "completed",
                f"max retries exceeded ({attempt}/{max_attempts})",
                scenario_id=scenario_id,
                latest_error=latest_error,
            )
        ],
    }


def finalize_node(state: AgentState) -> dict:
    """Finalize the run and emit a final audit event."""
    return {"events": [make_event("finalize", "completed", "workflow finished")]}
