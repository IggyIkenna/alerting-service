"""
Coalesce-window + synthetic-event suppression helpers for the event router.

Split out of ``router.py`` 2026-06-11 (codex_violations_ratchet_to_five plan,
Phase 1.5 — file-size <900). ``router.py`` re-exports every public symbol here
under its original private name (``_is_synthetic``, ``_check_coalesce_window``,
…) so the test surface + any in-repo consumer resolve unchanged.

── Tick-staleness + connectivity-gap coalesce window ────────────────────
Per alerting_service_live_rules_2026_05_07 § "Tick-staleness +
connectivity-gap event taxonomy", when both TICK_STALENESS (MDPS) and
CONNECTIVITY_GAP_DETECTED (MTDS) fire on the same (venue, instrument)
within a 30-second window, operators see ONE merged alert rather than
two separate fires. The 30s window keys on ``(venue, instrument)``
extracted from the alert payload ``details`` dict.

Coalesce state: maps (venue, instrument) → (last_fire_monotonic, last_event_name,
merged_payload). The first event in the window fires normally + records
the timestamp; subsequent events within 30s short-circuit return after
logging a COALESCE_MERGED event with the merged payload (no second fire,
no second persistence record, no second channel dispatch).

Non-staleness / non-connectivity alerts pass through untouched — the
coalesce only fires for the closed set ``COALESCED_EVENT_NAMES``.

── Synthetic-event paging suppression (Phase 3.F of simulation_scenarios) ──
When a scenario harness drives the pipeline via AdversarialMatchingEngine /
arm_breaker(synthetic=True), downstream alerts carry ``synthetic=True`` in
their ``details`` payload. The router logs the synthetic alert + persists
the delivery record (so operator-visible dashboards still see it) but does
NOT dispatch to PagerDuty / Telegram — paging during synthetic scenario
drills is unnecessary noise and could confuse on-call.

Per CLAUDE.md "Live = batch" rule, real-fire alerts continue to page
normally — the ONLY distinction is the ``synthetic`` flag on the payload.
"""

COALESCE_WINDOW_SECONDS: float = 30.0
COALESCED_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "TICK_STALENESS",
        "CONNECTIVITY_GAP_DETECTED",
    }
)
_coalesce_window: dict[tuple[str, str], tuple[float, str, dict[str, object]]] = {}

SYNTHETIC_TRUTHY: frozenset[object] = frozenset({True, "True", "true", "TRUE", "1"})


def is_synthetic(details: dict[str, object]) -> bool:
    """Return True iff the details payload signals a synthetic scenario fire.

    Per Phase 3.F the producer stamps ``synthetic=True`` (bool) — we accept
    common string serialisations as well so handlers that re-serialise via
    JSON / Pub/Sub envelope conventions still match.
    """
    return details.get("synthetic") in SYNTHETIC_TRUTHY


def coalesce_key(event_name: str, details: dict[str, object]) -> tuple[str, str] | None:
    """Extract the (venue, instrument) coalesce key from an alert payload.

    Returns ``None`` if either field is missing — caller MUST pass through
    without coalescing in that case (operator-visible over-broad alert is
    preferred to a silent merge that drops the second fire).
    """
    venue_raw = details.get("venue")
    instrument_raw = details.get("instrument")
    if venue_raw is None or instrument_raw is None:
        return None
    return (str(venue_raw), str(instrument_raw))


def check_coalesce_window(
    event_name: str,
    details: dict[str, object],
    now_monotonic: float,
) -> bool:
    """Return True if this event should be COALESCED (suppressed); False if it should fire.

    Side-effect: when False (fires normally), records the (key, now, event_name, details)
    entry in ``_coalesce_window`` for future suppression. When True (suppressed),
    merges the new payload into the in-flight entry so the original fire's
    audit trail captures the union of both signals.

    Cleanup: stale entries (>30s old) are evicted on every call to keep
    the dict bounded under high-frequency emission. Eviction is O(N)
    over the window dict, which is bounded by the operator's distinct
    (venue, instrument) set per 30s — small.
    """
    if event_name not in COALESCED_EVENT_NAMES:
        return False
    key = coalesce_key(event_name, details)
    if key is None:
        return False
    # Evict stale entries
    for stale_key in [k for k, (ts, _, _) in _coalesce_window.items() if now_monotonic - ts >= COALESCE_WINDOW_SECONDS]:
        del _coalesce_window[stale_key]
    existing = _coalesce_window.get(key)
    if existing is not None and now_monotonic - existing[0] < COALESCE_WINDOW_SECONDS:
        # Merge new payload into in-flight entry; suppress this fire.
        merged = dict(existing[2])
        merged.update(details)
        merged["_coalesced_with"] = event_name
        _coalesce_window[key] = (existing[0], existing[1], merged)
        return True
    # Record this fire as the in-flight entry; do not suppress.
    _coalesce_window[key] = (now_monotonic, event_name, dict(details))
    return False


def reset_coalesce_window_for_tests() -> None:
    """Test-only helper: clear the coalesce-window dict.

    Used by ``tests/unit/test_router_coalesce.py`` between cases so each
    test starts from a clean window. Not part of the public API."""
    _coalesce_window.clear()
