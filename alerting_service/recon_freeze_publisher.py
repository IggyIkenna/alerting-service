"""Recon-freeze coordination-event PUBLISHER (observability_master G12 P0).

When alerting-service detects a CRITICAL reconciliation-age breach OR any
immediate-SEV0 override, it MUST publish a ``RECON_FREEZE_ARMED`` coordination
event so the (separately-wired) execution-service ``ReconFreezeChecker.arm()``
blocks new orders for the affected scope. Today nothing publishes it — the
safety chain is dormant. This module is the alerting-side publisher that closes
that gap.

Per-incident granularity (operator decision 2026-06-01):
  - **symbol-scoped** freeze for symbol/venue-level breaks (carry venue+instrument)
  - **account-wide** freeze for account-level SEV0s (instrument marker ``"*"``)

The symmetric ``RECON_FREEZE_LIFTED`` event is published on operator-triggered
unfreeze.

SCOPE = the alerting-side publisher ONLY. The execution-side subscriber +
per-incident emit are SEPARATE todos in execution_master (this module does NOT
import or touch execution-service).

Subscriber contract (execution-service ``ReconFreezeChecker.arm/lift`` take
``strategy_id`` / ``venue`` / ``instrument``): the published payload carries
those three fields plus ``scope`` ("symbol" | "account"), a typed ``reason``,
and — for account-wide freezes — an ``instrument="*"`` marker with optional
``account_id`` / ``client_id`` context for downstream account-level arming.

Reference: ``plans/archive/recon_freeze_armed_never_published_2026_05_27`` +
``plans/epics/observability_master.md`` G12.
"""

from __future__ import annotations

from typing import Final

from unified_api_contracts.incident import ImmediateSev0Override
from unified_trading_library import JSONDict, log_event, publish_coordination_event

# ── Coordination event-type string constants ──────────────────────────────
RECON_FREEZE_ARMED: Final[str] = "RECON_FREEZE_ARMED"
RECON_FREEZE_LIFTED: Final[str] = "RECON_FREEZE_LIFTED"

# ── Freeze-scope tokens (matched by the execution-side subscriber) ─────────
SCOPE_SYMBOL: Final[str] = "symbol"
SCOPE_ACCOUNT: Final[str] = "account"

# Account-wide marker for the ``instrument`` field on an account-level freeze.
# The execution-side subscriber arms an account-wide block when it sees this.
ACCOUNT_WIDE_INSTRUMENT: Final[str] = "*"

# ── Per-incident-type granularity classification ──────────────────────────
# Operator decision (2026-06-01): freeze granularity is keyed to the BLAST
# RADIUS of the safety failure, not a one-size-fits-all halt.
#
# ACCOUNT-WIDE overrides — the uncertainty is about the WHOLE account's state
# (net exposure / balance movement / margin safety / kill-switch confirmation),
# so a single-symbol freeze would leave the rest of the account trading on an
# unknown footing. Freeze the entire account.
#   - UNKNOWN_NET_EXPOSURE                  — account-level net exposure unknown
#   - MATERIAL_BALANCE_MOVEMENT_UNEXPLAINED — account-level balance moved unexpectedly
#   - MARGIN_COLLATERAL_SAFETY_UNCERTAIN    — account-level margin/collateral unsafe
#   - KILL_SWITCH_CANNOT_CONFIRM_CANCEL     — cannot confirm cancels account-wide
#
# SYMBOL/VENUE-SCOPED overrides — the uncertainty is localised to a specific
# venue+instrument, so freezing just that symbol contains the risk without
# halting unrelated positions.
#   - OPEN_ORDERS_UNCONFIRMABLE                      — per venue+instrument order set
#   - VENUE_INTERNAL_BALANCE_MISMATCH                — per-venue balance mismatch
#   - POSITION_EXISTS_EXTERNALLY_UNKNOWN_INTERNALLY  — per venue+instrument position
_ACCOUNT_WIDE_OVERRIDES: Final[frozenset[ImmediateSev0Override]] = frozenset(
    {
        ImmediateSev0Override.UNKNOWN_NET_EXPOSURE,
        ImmediateSev0Override.MATERIAL_BALANCE_MOVEMENT_UNEXPLAINED,
        ImmediateSev0Override.MARGIN_COLLATERAL_SAFETY_UNCERTAIN,
        ImmediateSev0Override.KILL_SWITCH_CANNOT_CONFIRM_CANCEL,
    }
)

# CRITICAL recon-age breaches are always symbol-scoped — the age alert carries
# a concrete (venue, instrument) and the break is localised to that cell.
_RECON_AGE_CRITICAL_RULE_ID: Final[str] = "RECONCILIATION_AGE_CRITICAL"
_SEV0_RULE_ID_PREFIX: Final[str] = "IMMEDIATE_SEV0_"


def _scope_for_override(override: ImmediateSev0Override) -> str:
    """Return the freeze scope token for an ImmediateSev0Override member."""
    return SCOPE_ACCOUNT if override in _ACCOUNT_WIDE_OVERRIDES else SCOPE_SYMBOL


def publish_recon_freeze_armed(
    *,
    strategy_id: str,
    venue: str,
    instrument: str,
    scope: str,
    reason: str,
    account_id: str = "",
    client_id: str = "",
) -> None:
    """Publish a ``RECON_FREEZE_ARMED`` coordination event.

    The execution-service ``ReconFreezeChecker`` subscriber consumes this and
    arms a new-order block for the affected scope.

    Args:
        strategy_id: Strategy whose orders are frozen (subscriber ``arm`` arg).
        venue: Venue of the affected scope (subscriber ``arm`` arg).
        instrument: Instrument of the affected scope. For ``scope="account"``
            this is the account-wide marker ``"*"``.
        scope: ``"symbol"`` or ``"account"`` — the freeze blast radius.
        reason: Human-readable + machine-parseable reason (typically the
            alert's ``rule_id`` plus context).
        account_id: Optional account identifier for account-wide freezes.
        client_id: Optional client identifier for account-wide freezes.
    """
    payload: JSONDict = {
        "strategy_id": strategy_id,
        "venue": venue,
        "instrument": instrument,
        "scope": scope,
        "reason": reason,
        "account_id": account_id,
        "client_id": client_id,
    }
    publish_coordination_event(RECON_FREEZE_ARMED, payload=payload)
    log_event(
        "RECON_FREEZE_ARMED_PUBLISHED",
        details={
            "strategy_id": strategy_id,
            "venue": venue,
            "instrument": instrument,
            "scope": scope,
            "reason": reason,
        },
    )


def publish_recon_freeze_lifted(
    *,
    strategy_id: str,
    venue: str,
    instrument: str,
    scope: str,
    reason: str,
    account_id: str = "",
    client_id: str = "",
) -> None:
    """Publish a ``RECON_FREEZE_LIFTED`` coordination event (operator unfreeze).

    Symmetric to :func:`publish_recon_freeze_armed`. The execution-service
    subscriber consumes this and lifts the corresponding new-order block.

    Args:
        strategy_id: Strategy whose freeze is lifted (subscriber ``lift`` arg).
        venue: Venue of the affected scope (subscriber ``lift`` arg).
        instrument: Instrument of the affected scope (``"*"`` for account-wide).
        scope: ``"symbol"`` or ``"account"`` — must match the armed scope.
        reason: Human-readable reason for the lift (e.g. operator + ticket).
        account_id: Optional account identifier for account-wide freezes.
        client_id: Optional client identifier for account-wide freezes.
    """
    payload: JSONDict = {
        "strategy_id": strategy_id,
        "venue": venue,
        "instrument": instrument,
        "scope": scope,
        "reason": reason,
        "account_id": account_id,
        "client_id": client_id,
    }
    publish_coordination_event(RECON_FREEZE_LIFTED, payload=payload)
    log_event(
        "RECON_FREEZE_LIFTED_PUBLISHED",
        details={
            "strategy_id": strategy_id,
            "venue": venue,
            "instrument": instrument,
            "scope": scope,
            "reason": reason,
        },
    )


def _override_from_rule_id(rule_id: str) -> ImmediateSev0Override | None:
    """Map a sev0 alert ``rule_id`` back to its ImmediateSev0Override member.

    Alert dicts carry ``rule_id=f"IMMEDIATE_SEV0_{override.value}"`` (set by
    ``evaluate_immediate_sev0``). Strip the prefix and look up the enum; an
    unrecognised value returns ``None`` (caller logs + skips arming).
    """
    if not rule_id.startswith(_SEV0_RULE_ID_PREFIX):
        return None
    value = rule_id[len(_SEV0_RULE_ID_PREFIX) :]
    try:
        return ImmediateSev0Override(value)
    except ValueError:
        return None


def arm_recon_freeze_for_alerts(
    recon_age_alerts: list[dict[str, object]],
    sev0_alerts: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Publish ``RECON_FREEZE_ARMED`` for every CRITICAL recon-age + sev0 alert.

    For each CRITICAL recon-age alert (``rule_id == RECONCILIATION_AGE_CRITICAL``)
    publishes a **symbol-scoped** freeze (the alert carries venue+instrument).
    For each sev0 alert, looks up its override from ``rule_id`` (strips the
    ``IMMEDIATE_SEV0_`` prefix), picks ``account`` vs ``symbol`` scope from
    :data:`_ACCOUNT_WIDE_OVERRIDES`, and publishes.

    Non-CRITICAL recon-age alerts (WARN / INVESTIGATE bands) are skipped — only
    a CRITICAL breach arms the freeze.

    Args:
        recon_age_alerts: Alert dicts from ``evaluate_recon_age``.
        sev0_alerts: Alert dicts from ``evaluate_immediate_sev0``.

    Returns:
        The list of published freeze descriptors (one per armed event), for
        observability + testing. Each descriptor mirrors the published payload.
    """
    published: list[dict[str, object]] = []

    for alert in recon_age_alerts:
        if str(alert.get("rule_id", "")) != _RECON_AGE_CRITICAL_RULE_ID:  # noqa: qg-empty-fallback
            continue
        strategy_id = str(alert.get("strategy_id", ""))  # noqa: qg-empty-fallback
        venue = str(alert.get("venue", ""))  # noqa: qg-empty-fallback
        instrument = str(alert.get("instrument", ""))  # noqa: qg-empty-fallback
        reason = f"{_RECON_AGE_CRITICAL_RULE_ID}: {alert.get('message', '')}"  # noqa: qg-empty-fallback
        publish_recon_freeze_armed(
            strategy_id=strategy_id,
            venue=venue,
            instrument=instrument,
            scope=SCOPE_SYMBOL,
            reason=reason,
        )
        published.append(
            {
                "strategy_id": strategy_id,
                "venue": venue,
                "instrument": instrument,
                "scope": SCOPE_SYMBOL,
                "reason": reason,
            }
        )

    for alert in sev0_alerts:
        rule_id = str(alert.get("rule_id", ""))  # noqa: qg-empty-fallback
        override = _override_from_rule_id(rule_id)
        if override is None:
            log_event(
                "RECON_FREEZE_SKIPPED_UNKNOWN_SEV0",
                details={"rule_id": rule_id},
            )
            continue
        scope = _scope_for_override(override)
        strategy_id = str(alert.get("strategy_id", ""))  # noqa: qg-empty-fallback
        venue = str(alert.get("venue", ""))  # noqa: qg-empty-fallback
        account_id = str(alert.get("account_id", ""))  # noqa: qg-empty-fallback
        client_id = str(alert.get("client_id", ""))  # noqa: qg-empty-fallback
        # Account-wide freezes mark the instrument as ``"*"``; symbol-scoped
        # freezes carry the concrete instrument from the alert.
        instrument = ACCOUNT_WIDE_INSTRUMENT if scope == SCOPE_ACCOUNT else str(alert.get("instrument", ""))  # noqa: qg-empty-fallback
        reason = f"{rule_id}: {alert.get('message', '')}"  # noqa: qg-empty-fallback
        publish_recon_freeze_armed(
            strategy_id=strategy_id,
            venue=venue,
            instrument=instrument,
            scope=scope,
            reason=reason,
            account_id=account_id,
            client_id=client_id,
        )
        published.append(
            {
                "strategy_id": strategy_id,
                "venue": venue,
                "instrument": instrument,
                "scope": scope,
                "reason": reason,
                "account_id": account_id,
                "client_id": client_id,
            }
        )

    return published
