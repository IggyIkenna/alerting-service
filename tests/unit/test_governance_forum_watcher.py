"""Tests for GovernanceForumWatcher (D.7).

Coverage: happy-path detect tagged proposal, untagged proposal ignored,
multi-tag match, API rate limit, no proposals, AlertCode emission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from unified_api_contracts.alerting import AlertCode

from alerting_service.subscribers.governance_forum_watcher import (
    GovernanceForumWatcher,
    GovernanceProposal,
    SnapshotForumPoller,
    TallyForumPoller,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


class _CapturingEmitter:
    def __init__(self) -> None:
        self.emitted: list[dict[str, object]] = []

    def emit(self, payload: dict[str, object]) -> None:
        self.emitted.append(payload)


_MOCK_SNAPSHOT_PROPOSAL = {
    "id": "QmAbc123",
    "title": "Emergency: Suspend USDC PSM on Aave",
    "body": "Proposal to freeze usdc operations due to incident",
    "created": int(datetime(2026, 5, 13, 10, 0, tzinfo=UTC).timestamp()),
    "space": {"id": "aave.eth", "name": "Aave"},
    "state": "active",
}

_MOCK_SNAPSHOT_UNTAGGED = {
    "id": "QmNoMatch",
    "title": "Increase treasury allocation for marketing",
    "body": "Standard marketing budget proposal.",
    "created": int(datetime(2026, 5, 13, 9, 0, tzinfo=UTC).timestamp()),
    "space": {"id": "compound.eth", "name": "Compound"},
    "state": "active",
}

_MOCK_TALLY_PROPOSAL = {
    "id": "prop-456",
    "title": "Spark: Freeze DAI lending market",
    "description": "Emergency response to depeg risk — suspend borrowing",
    "createdAt": "2026-05-13T10:00:00Z",
    "governor": {"id": "aave", "name": "Aave Governor"},
}

_MOCK_TALLY_UNTAGGED = {
    "id": "prop-999",
    "title": "Update validator configuration",
    "description": "Routine update to validator parameters",
    "createdAt": "2026-05-13T09:00:00Z",
    "governor": {"id": "uniswap", "name": "Uniswap Governor"},
}


# ── Test 1: Happy-path Snapshot proposal detected ────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_tagged_proposal_detected() -> None:
    """SnapshotForumPoller returns proposal matching risk keywords."""
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"proposals": [_MOCK_SNAPSHOT_PROPOSAL, _MOCK_SNAPSHOT_UNTAGGED]}},
        )
    )
    poller = SnapshotForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert len(proposals) == 1
    assert proposals[0].forum == "Snapshot"
    assert proposals[0].proposal_id == "QmAbc123"
    assert any("usdc" in t or "aave" in t or "incident" in t for t in proposals[0].tags)


# ── Test 2: Untagged proposal ignored ────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_untagged_proposal_ignored() -> None:
    """SnapshotForumPoller ignores proposals with no risk keywords."""
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"proposals": [_MOCK_SNAPSHOT_UNTAGGED]}},
        )
    )
    poller = SnapshotForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert len(proposals) == 0


# ── Test 3: Multi-tag match ───────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_multi_tag_match_all_captured() -> None:
    """Multiple risk keywords in one proposal result in all tags captured."""
    multi_tag_proposal = {
        **_MOCK_SNAPSHOT_PROPOSAL,
        "title": "Emergency freeze of USDC USDT DAI on Aave — incident response",
        "body": "Halt aave spark depeg exposure immediately",
    }
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"proposals": [multi_tag_proposal]}},
        )
    )
    poller = SnapshotForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert len(proposals) == 1
    tags = proposals[0].tags
    assert len(tags) >= 3  # At minimum: usdc, usdt, dai, aave, incident, freeze, etc.


# ── Test 4: Snapshot API rate limit ──────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_rate_limit_returns_empty() -> None:
    """SnapshotForumPoller returns empty list on 429 rate limit."""
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(429, text="rate limit exceeded")
    )
    poller = SnapshotForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert proposals == []


# ── Test 5: No proposals returned by Snapshot ────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_no_proposals_empty() -> None:
    """SnapshotForumPoller returns empty list when API returns no proposals."""
    respx.post("https://hub.snapshot.org/graphql").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"proposals": []}},
        )
    )
    poller = SnapshotForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert proposals == []


# ── Test 6: AlertCode emission ────────────────────────────────────────────


def test_governance_proposal_alert_code() -> None:
    """GovernanceProposal.to_alert_payload() sets correct AlertCode."""
    proposal = GovernanceProposal(
        forum="Snapshot",
        proposal_id="QmTest",
        title="Emergency DAI freeze",
        tags=["dai", "freeze", "emergency"],
        posted_at=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        url="https://snapshot.org/#/aave.eth/proposal/QmTest",
    )
    payload = proposal.to_alert_payload()

    assert payload["event_name"] == AlertCode.GOVERNANCE_INCIDENT_DETECTED.value
    assert payload["forum"] == "Snapshot"
    assert payload["proposal_id"] == "QmTest"
    assert "dai" in payload["tags"]


# ── Test 7: Tally proposal detected ──────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_tally_tagged_proposal_detected() -> None:
    """TallyForumPoller returns proposal matching risk keywords."""
    respx.post("https://api.tally.xyz/query").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"proposals": {"nodes": [_MOCK_TALLY_PROPOSAL, _MOCK_TALLY_UNTAGGED]}}},
        )
    )
    poller = TallyForumPoller()
    async with httpx.AsyncClient() as client:
        proposals = await poller.check(client)

    assert len(proposals) == 1
    assert proposals[0].forum == "Tally"
    assert proposals[0].proposal_id == "prop-456"


# ── Test 8: GovernanceForumWatcher deduplication ──────────────────────────


@pytest.mark.asyncio
async def test_watcher_deduplicates_proposals() -> None:
    """GovernanceForumWatcher does not re-emit proposals already seen."""
    proposal = GovernanceProposal(
        forum="Snapshot",
        proposal_id="QmDedup",
        title="Emergency USDC freeze",
        tags=["usdc", "freeze"],
        posted_at=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        url="https://snapshot.org/...",
    )

    emitter = _CapturingEmitter()

    # Inject pollers that always return the same proposal
    mock_snapshot = AsyncMock()
    mock_snapshot.check.return_value = [proposal]
    mock_tally = AsyncMock()
    mock_tally.check.return_value = []

    watcher = GovernanceForumWatcher(
        emitter=emitter,
        snapshot_poller=mock_snapshot,
        tally_poller=mock_tally,
    )

    # First poll — should detect
    new_proposals_1 = await watcher.poll_once()
    assert len(new_proposals_1) == 1

    # Second poll — same proposal, should be deduped
    new_proposals_2 = await watcher.poll_once()
    assert len(new_proposals_2) == 0
