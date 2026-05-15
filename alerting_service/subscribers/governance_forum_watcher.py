"""Governance forum watcher (D.7).

Polls governance forums for proposals tagged with depeg-risk keywords:
- Snapshot.org GraphQL API: proposals tagged ``incident``, ``freeze``,
  stablecoin names, ``aave``, ``spark``.
- Tally REST API: same tag filtering.

Discord ingestion is deferred (cutover MVP — no Discord ingestion).
**DEFERRED**: Discord webhook ingestion — successor plan: D.7 follow-up
``plans/active/risk_simulations_limits_alerting_2026_05_10.md`` Phase D.7 Discord item.

Emits ``AlertCode.GOVERNANCE_INCIDENT_DETECTED`` (operator-page only, NOT auto-action).

Event payload schema::

    {
        "event_name": "GOVERNANCE_INCIDENT_DETECTED",
        "forum": "Snapshot",
        "proposal_id": "QmXxxx...",
        "title": "Emergency: Suspend USDC PSM",
        "tags": ["usdc", "emergency", "freeze"],
        "posted_at": "2026-05-13T08:00:00Z",
        "url": "https://snapshot.org/#/...",
        "correlation_id": "<uuid>",
    }

SSOT: ``plans/active/risk_simulations_limits_alerting_2026_05_10.md`` Phase D.7.
Reference: ``scratch_scenarios_day1/17_lrt_lending_meltdown_composite.md``
governance_forum_watcher trigger_d spec.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from pydantic import BaseModel
from unified_api_contracts.internal.alerting import GovernanceForumProposal

# Re-export under the historic name so existing consumers import unchanged.
GovernanceProposal = GovernanceForumProposal

logger = logging.getLogger(__name__)

# Poll interval
_DEFAULT_POLL_INTERVAL_SECONDS: float = 300.0  # 5-minute cadence

# Snapshot.org GraphQL endpoint
_SNAPSHOT_GRAPHQL_URL: str = "https://hub.snapshot.org/graphql"

# Tally REST API endpoint
_TALLY_API_URL: str = "https://api.tally.xyz/query"

# Keywords that trigger an alert — matched against proposal title + body (case-insensitive)
RISK_KEYWORDS: frozenset[str] = frozenset(
    [
        "incident",
        "freeze",
        "pause",
        "usdc",
        "usdt",
        "dai",
        "usde",
        "frax",
        "gho",
        "crvusd",
        "pyusd",
        "aave",
        "spark",
        "depeg",
        "emergency",
        "suspension",
        "halt",
    ]
)

# Lookback window — only consider proposals created within last N hours
_PROPOSAL_LOOKBACK_HOURS: int = 24


class AlertEmitter(Protocol):
    """Protocol for emitting alert payloads."""

    def emit(self, payload: dict[str, object]) -> None:
        """Emit alert payload to the alerting bus."""
        ...


def _matches_risk_keywords(title: str, body: str = "", space: str = "") -> list[str]:
    """Return list of matched risk keywords from title + body + space.

    Returns empty list if no keywords match — proposal should be skipped.
    """
    combined = f"{title} {body} {space}".lower()
    return [kw for kw in RISK_KEYWORDS if kw in combined]


# ─────────────────────────────────────────────────────────────────────────────
# Private Pydantic models for raw API response shapes (parser-internal).
# Underscore prefix: excluded from schema-provenance check.
# Using Optional[X] fields with None defaults for API fields that may be absent.
# ─────────────────────────────────────────────────────────────────────────────


class _SnapshotSpace(BaseModel):  # CORRECT-LOCAL
    """Snapshot proposal space sub-object."""

    id: str | None = None
    name: str | None = None


class _SnapshotRawProposal(BaseModel):  # CORRECT-LOCAL
    """Raw Snapshot.org proposal record as returned by GraphQL."""

    id: str
    title: str
    body: str | None = None
    created: int | None = None
    space: _SnapshotSpace | None = None
    state: str | None = None


class _SnapshotProposalsData(BaseModel):  # CORRECT-LOCAL
    """``data.proposals`` wrapper from Snapshot GraphQL response."""

    proposals: list[_SnapshotRawProposal] = []


class _SnapshotApiResponse(BaseModel):  # CORRECT-LOCAL
    """Top-level Snapshot.org GraphQL response envelope."""

    data: _SnapshotProposalsData | None = None


class _TallyGovernor(BaseModel):  # CORRECT-LOCAL
    """Tally proposal governor sub-object."""

    id: str | None = None
    name: str | None = None


class _TallyRawProposal(BaseModel):  # CORRECT-LOCAL
    """Raw Tally proposal record as returned by GraphQL."""

    id: str
    title: str
    description: str | None = None
    createdAt: str | None = None  # noqa: N815 — mirrors API field name
    governor: _TallyGovernor | None = None


class _TallyProposalsNodes(BaseModel):  # CORRECT-LOCAL
    """``data.proposals`` wrapper from Tally GraphQL response."""

    nodes: list[_TallyRawProposal] = []


class _TallyProposalsData(BaseModel):  # CORRECT-LOCAL
    """``data`` wrapper from Tally GraphQL response."""

    proposals: _TallyProposalsNodes | None = None


class _TallyApiResponse(BaseModel):  # CORRECT-LOCAL
    """Top-level Tally GraphQL response envelope."""

    data: _TallyProposalsData | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot.org poller
# ─────────────────────────────────────────────────────────────────────────────

_SNAPSHOT_QUERY = """
query RecentProposals($created_gt: Int!) {
  proposals(
    first: 50,
    skip: 0,
    where: { created_gt: $created_gt },
    orderBy: "created",
    orderDirection: desc
  ) {
    id
    title
    body
    start
    end
    created
    space { id name }
    state
  }
}
"""


class SnapshotForumPoller:
    """Polls Snapshot.org for recent governance proposals tagged with risk keywords."""

    def __init__(
        self,
        graphql_url: str = _SNAPSHOT_GRAPHQL_URL,
        lookback_hours: int = _PROPOSAL_LOOKBACK_HOURS,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._url = graphql_url
        self._lookback_hours = lookback_hours
        self._timeout = timeout_seconds

    async def fetch_proposals(self, client: httpx.AsyncClient) -> list[_SnapshotRawProposal]:
        """Fetch recent proposals from Snapshot.org GraphQL API."""
        cutoff_ts = int((datetime.now(UTC) - timedelta(hours=self._lookback_hours)).timestamp())
        payload = {
            "query": _SNAPSHOT_QUERY,
            "variables": {"created_gt": cutoff_ts},
        }
        try:
            response = await client.post(
                self._url,
                json=payload,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            api_response = _SnapshotApiResponse.model_validate(response.json())
            if api_response.data is not None:
                return api_response.data.proposals
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("SnapshotForumPoller: rate limited (429), skipping cycle")
            else:
                logger.warning("SnapshotForumPoller HTTP error %s: %s", exc.response.status_code, exc)
            return []
        except Exception as exc:
            logger.warning("SnapshotForumPoller fetch error: %s", exc)
            return []

    async def check(self, client: httpx.AsyncClient) -> list[GovernanceForumProposal]:
        """Return proposals matching risk keywords from Snapshot."""
        raw_proposals = await self.fetch_proposals(client)
        results: list[GovernanceForumProposal] = []

        for p in raw_proposals:
            title = p.title
            body = p.body or ""
            space_name = p.space.name if p.space and p.space.name else ""

            matched_keywords = _matches_risk_keywords(title, body, space_name)
            if not matched_keywords:
                continue

            posted_at = datetime.fromtimestamp(p.created, tz=UTC) if p.created is not None else datetime.now(UTC)

            space_id = p.space.id if p.space and p.space.id else ""
            url = f"https://snapshot.org/#/{space_id}/proposal/{p.id}"

            results.append(
                GovernanceForumProposal(
                    forum="Snapshot",
                    proposal_id=p.id,
                    title=title,
                    tags=matched_keywords,
                    posted_at=posted_at,
                    url=url,
                )
            )

        return results


# ─────────────────────────────────────────────────────────────────────────────
# Tally poller
# ─────────────────────────────────────────────────────────────────────────────

_TALLY_QUERY = """
query RecentProposals($afterTime: Timestamp!) {
  proposals(
    sort: { sortBy: id, isDescending: true },
    pagination: { limit: 50 },
    where: { proposerIds: [], afterTime: $afterTime }
  ) {
    nodes {
      id
      title
      description
      createdAt
      governor { id name }
    }
  }
}
"""


class TallyForumPoller:
    """Polls Tally REST/GraphQL API for recent governance proposals."""

    def __init__(
        self,
        api_url: str = _TALLY_API_URL,
        lookback_hours: int = _PROPOSAL_LOOKBACK_HOURS,
        timeout_seconds: float = 15.0,
        api_key: str | None = None,
    ) -> None:
        self._url = api_url
        self._lookback_hours = lookback_hours
        self._timeout = timeout_seconds
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Api-Key"] = self._api_key
        return headers

    async def fetch_proposals(self, client: httpx.AsyncClient) -> list[_TallyRawProposal]:
        """Fetch recent proposals from Tally GraphQL API."""
        after_time = (datetime.now(UTC) - timedelta(hours=self._lookback_hours)).isoformat()
        payload = {
            "query": _TALLY_QUERY,
            "variables": {"afterTime": after_time},
        }
        try:
            response = await client.post(
                self._url,
                json=payload,
                timeout=self._timeout,
                headers=self._headers(),
            )
            response.raise_for_status()
            api_response = _TallyApiResponse.model_validate(response.json())
            if api_response.data is not None and api_response.data.proposals is not None:
                return api_response.data.proposals.nodes
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("TallyForumPoller: rate limited (429), skipping cycle")
            else:
                logger.warning("TallyForumPoller HTTP error %s: %s", exc.response.status_code, exc)
            return []
        except Exception as exc:
            logger.warning("TallyForumPoller fetch error: %s", exc)
            return []

    def _parse_proposal(self, p: _TallyRawProposal) -> GovernanceForumProposal | None:
        """Parse a Tally proposal; return None if keywords don't match."""
        title = p.title
        description = p.description or ""
        governor_name = p.governor.name if p.governor and p.governor.name else ""

        matched_keywords = _matches_risk_keywords(title, description, governor_name)
        if not matched_keywords:
            return None

        posted_at: datetime
        if p.createdAt is not None:
            try:
                posted_at = datetime.fromisoformat(p.createdAt.replace("Z", "+00:00"))
            except ValueError:
                posted_at = datetime.now(UTC)
        else:
            posted_at = datetime.now(UTC)

        governor_id = p.governor.id if p.governor and p.governor.id else ""
        url = f"https://www.tally.xyz/gov/{governor_id}/proposal/{p.id}"
        return GovernanceForumProposal(
            forum="Tally",
            proposal_id=p.id,
            title=title,
            tags=matched_keywords,
            posted_at=posted_at,
            url=url,
        )

    async def check(self, client: httpx.AsyncClient) -> list[GovernanceForumProposal]:
        """Return proposals matching risk keywords from Tally."""
        raw_proposals = await self.fetch_proposals(client)
        results: list[GovernanceForumProposal] = []
        for p in raw_proposals:
            proposal = self._parse_proposal(p)
            if proposal is not None:
                results.append(proposal)
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Governance Forum Watcher orchestrator
# ─────────────────────────────────────────────────────────────────────────────


class GovernanceForumWatcher:
    """Orchestrates Snapshot + Tally polling and emits GOVERNANCE_INCIDENT_DETECTED.

    Discord ingestion is deferred (no-Discord MVP per D.7 spec).

    Usage::

        class _RouterEmitter:
            def emit(self, payload: dict[str, object]) -> None:
                route_event(str(payload["event_name"]), payload)

        watcher = GovernanceForumWatcher(emitter=_RouterEmitter())
        await watcher.run_until_stopped()
    """

    def __init__(
        self,
        emitter: AlertEmitter,
        snapshot_poller: SnapshotForumPoller | None = None,
        tally_poller: TallyForumPoller | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._emitter = emitter
        self._snapshot = snapshot_poller or SnapshotForumPoller()
        self._tally = tally_poller or TallyForumPoller()
        self._poll_interval = poll_interval_seconds
        self._running = False
        # Dedup: track seen proposal IDs to avoid re-alerting
        self._seen_ids: set[str] = set()

    def stop(self) -> None:
        """Signal the polling loop to stop."""
        self._running = False

    async def poll_once(self) -> list[GovernanceForumProposal]:
        """Run one full poll cycle; return NEW proposals not seen before."""
        all_proposals: list[GovernanceForumProposal] = []

        async with httpx.AsyncClient() as client:
            snapshot_proposals = await self._snapshot.check(client)
            all_proposals.extend(snapshot_proposals)

            tally_proposals = await self._tally.check(client)
            all_proposals.extend(tally_proposals)

        # Dedup on proposal_id
        new_proposals: list[GovernanceForumProposal] = []
        for proposal in all_proposals:
            dedup_key = f"{proposal.forum}:{proposal.proposal_id}"
            if dedup_key not in self._seen_ids:
                self._seen_ids.add(dedup_key)
                new_proposals.append(proposal)

        return new_proposals

    async def run_until_stopped(self) -> None:
        """Poll governance forums in a loop until ``stop()`` is called."""
        self._running = True
        logger.info(
            "GovernanceForumWatcher starting (interval=%.0fs)",
            self._poll_interval,
        )
        while self._running:
            proposals = await self.poll_once()
            for proposal in proposals:
                payload = proposal.to_alert_payload()
                logger.warning(
                    "Governance incident detected: forum=%s id=%s title='%s' tags=%s",
                    proposal.forum,
                    proposal.proposal_id,
                    proposal.title,
                    proposal.tags,
                )
                self._emitter.emit(payload)

            if self._running:
                await asyncio.sleep(self._poll_interval)

        logger.info("GovernanceForumWatcher stopped")


__all__ = [
    "RISK_KEYWORDS",
    "GovernanceForumProposal",
    "GovernanceForumWatcher",
    "GovernanceProposal",
    "SnapshotForumPoller",
    "TallyForumPoller",
]
