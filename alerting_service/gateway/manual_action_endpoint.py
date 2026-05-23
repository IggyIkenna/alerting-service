"""Manual-action endpoint — DART Safety Ops tab → Incident Gateway entry point.

Every operator-initiated Layer-0 action (Cancel Open Orders / Pause Strategy
/ etc) MUST flow through this endpoint so it emits an IncidentEnvelope with
``provenance=MANUAL_OPERATOR`` + spawns the underlying Layer-0 script via the
same closed-set wrapper the LLM agent uses (defence-in-depth: manual + LLM +
automatic actions all share one audit trail).

Codex SSOT: ``codex/04-architecture/recovery-defence-in-depth-layers.md``
§ "Layer M — Operator manual override".
Implementation plan: ``plans/active/deployment_ui_safety_ops_tab_2026_05_23.md``
Phase 1.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from unified_api_contracts.incident import (
    ActionProvenance,
    ActionType,
)

_logger = logging.getLogger(__name__)

# ── Confirm-string registry — closed set per (action_type, scope) ─────────
#
# Operators must type the exact expected string (case-sensitive) to confirm
# the action. Protects against accidental fat-finger destruction.
#
# Format: f-string-style template; available variables depend on action.
#
# This registry is the SSOT for confirm-string contracts. The Safety Ops UI
# generates the expected string client-side from the same template + shows
# it to the operator BEFORE they type.

_CONFIRM_STRING_TEMPLATES: dict[ActionType, str] = {
    ActionType.RESTART_SERVICE: "RESTART_{service}",
    ActionType.RESTART_CONTAINER: "RESTART_CONTAINER_{container}",
    ActionType.REDEPLOY_KNOWN_GOOD: "REDEPLOY_{service}_to_{to_revision}",
    ActionType.RESIZE_MACHINE_AFTER_OOM: "RESIZE_{vm}",
    ActionType.FAILOVER_FEED: "FAILOVER_{venue}_{primary}_to_{backup}",
    ActionType.PAUSE_STRATEGY: "PAUSE_{strategy}",
    ActionType.CANCEL_OPEN_ORDERS: "CANCEL_ALL_{venue}",
    ActionType.DISABLE_VENUE: "DISABLE_{venue}",
    ActionType.ENTER_SAFE_MODE: "SAFE_MODE_{strategy}",
    ActionType.ENTER_READONLY_RECON_MODE: "READONLY_{service}",
}


# ── Operator authz allowlist (closed set; SSOT here for Phase 1) ──────────
_OPERATOR_ALLOWLIST: frozenset[str] = frozenset({
    "ikenna@odum-research.com",
    "harsh@odum-research.com",
})


# ── Rate-limit: 1 action per 10s per operator ────────────────────────────
_RATE_LIMIT_WINDOW_S = 10.0
_LAST_ACTION_AT: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(operator_id: str) -> bool:
    """Returns True if the operator is allowed to act now."""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_S
    history = _LAST_ACTION_AT[operator_id]
    while history and history[0] < cutoff:
        history.popleft()
    if len(history) >= 1:
        return False
    history.append(now)
    return True


def _expected_confirm_string(action_type: ActionType, scope: dict[str, str]) -> str:
    """Render the expected confirm-string from the registry template."""
    template = _CONFIRM_STRING_TEMPLATES[action_type]
    try:
        return template.format(**scope)
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Scope missing required key for {action_type.value}: {exc.args[0]}",
        )


# ── Request / Response schemas ────────────────────────────────────────────


class ManualActionRequest(BaseModel):
    """Operator-initiated Layer-0 action request from DART Safety Ops tab."""

    action_type: ActionType
    scope: dict[str, str] = Field(default_factory=dict)
    """Scope args for the underlying Layer-0 script — must include all keys
    required by the action_type's CLI args (e.g. ``service`` for restart_service,
    ``venue`` for cancel_open_orders)."""
    reason: str = Field(min_length=1, max_length=500)
    operator_id: str = Field(min_length=1)
    """Operator email — must be on the allowlist."""
    confirm_string: str
    """Operator-typed exact-match of the expected confirm-string. UI shows
    the expected value; mismatch returns 400."""
    incident_key: str | None = None
    """Parent IncidentEnvelope.incident_key if this manual action is part of
    an existing incident. If None, a fresh adhoc key is minted."""
    dry_run: bool = False


class ManualActionResponse(BaseModel):
    ok: bool
    incident_key: str
    action_event_id: str
    expected_confirm_string: str
    """Echoed back so the UI can highlight if the operator's input mismatched."""
    layer0_exit_code: int | None
    layer0_stderr_tail: str | None


# ── Endpoint ──────────────────────────────────────────────────────────────

router = APIRouter(prefix="/admin/manual-action", tags=["safety-ops"])


@router.post("", response_model=ManualActionResponse)
async def post_manual_action(request: ManualActionRequest) -> ManualActionResponse:
    """Operator-initiated Layer-0 action.

    Flow:
      1. Validate operator on allowlist.
      2. Rate-limit (1 action / 10s / operator).
      3. Validate confirm_string exact-match against registry template.
      4. Invoke Layer-0 script via subprocess (NOT shell) with
         ``provenance=MANUAL_OPERATOR``; underlying script emits AgentActionEvent.
      5. Return result; the incident gateway state machine + LLM
         recovery-audit-signoff agent + DART audit-ack queue all see the
         action equivalently to automated actions.
    """
    # 1. Authz
    if request.operator_id not in _OPERATOR_ALLOWLIST:
        raise HTTPException(status_code=403, detail=f"Operator {request.operator_id} not on Safety Ops allowlist")

    # 2. Rate-limit
    if not _check_rate_limit(request.operator_id):
        raise HTTPException(
            status_code=429,
            detail=f"Rate-limited (1 action per {int(_RATE_LIMIT_WINDOW_S)}s per operator)",
        )

    # 3. Confirm-string exact match
    expected = _expected_confirm_string(request.action_type, request.scope)
    if request.confirm_string != expected:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "confirm_string mismatch",
                "expected": expected,
                "got": request.confirm_string,
            },
        )

    # 4. Build Layer-0 invocation (uses the SAME wrapper the LLM uses, but
    #    with provenance=MANUAL_OPERATOR + agent-id=operator_email)
    incident_key = request.incident_key or f"manual-{uuid.uuid4()}"
    event_id = f"evt-{uuid.uuid4()}"

    # Build inner argv: python -m scripts.recovery.<action_type> --reason ...
    inner_argv: list[str] = [
        "python",  # interpreter on PATH in the deployed environment
        "-m",
        f"scripts.recovery.{request.action_type.value}",
        "--reason",
        request.reason,
        "--incident-key",
        incident_key,
        "--provenance",
        ActionProvenance.MANUAL_OPERATOR.value,
        "--agent-id",
        f"operator:{request.operator_id}",
    ]
    if request.dry_run:
        inner_argv.append("--dry-run")
    # Append scope args (--service / --venue / etc).
    for k, v in request.scope.items():
        inner_argv.append(f"--{k.replace('_', '-')}")
        inner_argv.append(str(v))

    _logger.info(
        "Manual action: operator=%s action=%s scope=%s incident_key=%s dry_run=%s",
        request.operator_id, request.action_type.value, request.scope,
        incident_key, request.dry_run,
    )

    # 5. Invoke in subprocess (DOES NOT use shell — argv list).
    #    Run async to not block the FastAPI event loop.
    proc = await asyncio.create_subprocess_exec(
        *inner_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="Layer-0 script exceeded 120s timeout")

    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return ManualActionResponse(
        ok=proc.returncode == 0,
        incident_key=incident_key,
        action_event_id=event_id,
        expected_confirm_string=expected,
        layer0_exit_code=proc.returncode,
        layer0_stderr_tail=stderr[-500:] if stderr else None,
    )


def expected_confirm_string(action_type: ActionType, scope: dict[str, str]) -> str:
    """Public helper for the Safety Ops UI client to compute the expected
    confirm-string in JS (template is the same — keep this in sync with
    ``_CONFIRM_STRING_TEMPLATES`` above)."""
    return _expected_confirm_string(action_type, scope)
