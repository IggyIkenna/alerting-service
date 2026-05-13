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
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from unified_api_contracts.alerting import AlertCode

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


@dataclass
class GovernanceProposal:
    """A governance proposal detected by the forum watcher."""

    forum: str  # 'Snapshot' or 'Tally'
    proposal_id: str
    title: str
    tags: list[str]
    posted_at: datetime
    url: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_alert_payload(self) -> dict[str, object]:
        """Serialize to alert event payload dict."""
        return {
            "event_name": AlertCode.GOVERNANCE_INCIDENT_DETECTED.value,
            "forum": self.forum,
            "proposal_id": self.proposal_id,
            "title": self.title,
            "tags": self.tags,
            "posted_at": self.posted_at.isoformat(),
            "url": self.url,
            "correlation_id": self.correlation_id,
        }


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

    async def fetch_proposals(self, client: httpx.AsyncClient) -> list[dict[str, object]]:
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
            data: dict[str, object] = response.json()
            proposals = data.get("data", {})
            if isinstance(proposals, dict):
                raw_proposals = proposals.get("proposals", [])
                if isinstance(raw_proposals, list):
                    return raw_proposals  # type: ignore[return-value]
            return []
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("SnapshotForumPoller: rate limited (429), skipping cycle")
            else:
                logger.warning(
                    "SnapshotForumPoller HTTP error %s: %s", exc.response.status_code, exc
                )
            return []
        except Exception as exc:
            logger.warning("SnapshotForumPoller fetch error: %s", exc)
            return []

    async def check(self, client: httpx.AsyncClient) -> list[GovernanceProposal]:
        """Return proposals matching risk keywords from Snapshot."""
        raw_proposals = await self.fetch_proposals(client)
        results: list[GovernanceProposal] = []

        for p in raw_proposals:
            if not isinstance(p, dict):
                continue
            proposal_id = str(p.get("id", ""))
            title = str(p.get("title", ""))
            body = str(p.get("body", ""))
            space_name = ""
            space = p.get("space")
            if isinstance(space, dict):
                space_name = str(space.get("name", ""))

            matched_keywords = _matches_risk_keywords(title, body, space_name)
            if not matched_keywords:
                continue

            created_ts = p.get("created")
            posted_at: datetime
            if isinstance(created_ts, int):
                posted_at = datetime.fromtimestamp(created_ts, tz=UTC)
            else:
                posted_at = datetime.now(UTC)

            space_id = ""
            if isinstance(space, dict):
                space_id = str(space.get("id", ""))
            url = f"https://snapshot.org/#/{space_id}/proposal/{proposal_id}"

            results.append(
                GovernanceProposal(
                    forum="Snapshot",
                    proposal_id=proposal_id,
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

    async def fetch_proposals(self, client: httpx.AsyncClient) -> list[dict[str, object]]:
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
            data: dict[str, object] = response.json()
            proposals_data = data.get("data", {})
            if isinstance(proposals_data, dict):
                proposals_wrapper = proposals_data.get("proposals", {})
                if isinstance(proposals_wrapper, dict):
                    nodes = proposals_wrapper.get("nodes", [])
                    if isinstance(nodes, list):
                        return nodes  # type: ignore[return-value]
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

    def _parse_proposal(self, p: dict[str, object]) -> GovernanceProposal | None:
        """Parse a raw Tally proposal dict; return None if keywords don't match."""
        proposal_id = str(p.get("id", ""))
        title = str(p.get("title", ""))
        description = str(p.get("description", ""))
        governor = p.get("governor")
        governor_name = str(governor.get("name", "")) if isinstance(governor, dict) else ""

        matched_keywords = _matches_risk_keywords(title, description, governor_name)
        if not matched_keywords:
            return None

        created_at_raw = p.get("createdAt")
        if isinstance(created_at_raw, str):
            try:
                posted_at: datetime = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except ValueError:
                posted_at = datetime.now(UTC)
        else:
            posted_at = datetime.now(UTC)

        governor_id = str(governor.get("id", "")) if isinstance(governor, dict) else ""
        url = f"https://www.tally.xyz/gov/{governor_id}/proposal/{proposal_id}"
        return GovernanceProposal(
            forum="Tally",
            proposal_id=proposal_id,
            title=title,
            tags=matched_keywords,
            posted_at=posted_at,
            url=url,
        )

    async def check(self, client: httpx.AsyncClient) -> list[GovernanceProposal]:
        """Return proposals matching risk keywords from Tally."""
        raw_proposals = await self.fetch_proposals(client)
        results: list[GovernanceProposal] = []
        for p in raw_proposals:
            if not isinstance(p, dict):
                continue
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
                route_event(str(payload.get("event_name", "")), payload)

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

    async def poll_once(self) -> list[GovernanceProposal]:
        """Run one full poll cycle; return NEW proposals not seen before."""
        all_proposals: list[GovernanceProposal] = []

        async with httpx.AsyncClient() as client:
            snapshot_proposals = await self._snapshot.check(client)
            all_proposals.extend(snapshot_proposals)

            tally_proposals = await self._tally.check(client)
            all_proposals.extend(tally_proposals)

        # Dedup on proposal_id
        new_proposals: list[GovernanceProposal] = []
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
    "GovernanceForumWatcher",
    "GovernanceProposal",
    "SnapshotForumPoller",
    "TallyForumPoller",
]
