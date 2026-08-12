"""Evidence collector — populates the 14-field IncidentEvidence bundle at
AUDIT_REPORT_GENERATED state transition.

Triggers per-service evidence-capture callbacks; each service exposes
``/evidence/{incident_key}`` HTTP endpoint that writes a snapshot to GCS at
``incidents/{date}/{incident_key}/evidence/<type>.json``. The collector
gathers the URLs + stamps ``config_hash`` / ``code_version`` /
``runbook_version`` (the 3 mandatory reproducibility fields).

Codex SSOT: ``codex/04-architecture/incident-gateway-state-machine.md``
§ "Evidence Store" + implementation plan
``plans/active/incident_runbooks_and_evidence_store_2026_05_23.md``.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import httpx
from unified_api_contracts.incident import IncidentEnvelope, IncidentEvidence

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_logger = logging.getLogger(__name__)


EvidenceCaptureCallback = Callable[[str], "tuple[str, str] | None"]
"""Signature: ``(incident_key) -> (evidence_type, gcs_url) | None``.

Each per-service registrant returns ``(type, url)`` if it has evidence for
this incident, or None to opt out. ``evidence_type`` is one of the 14
IncidentEvidence field names (``raw_venue_api_snapshot_url`` etc) or any
key for ``additional_evidence``.
"""


@dataclass
class EvidenceCollectorConfig:  # CORRECT-LOCAL: internal collector config type
    """Per-service HTTP endpoints to call for evidence collection.

    Each entry: ``service_name -> base_url``. Collector POSTs to
    ``{base_url}/evidence/{incident_key}``; expects 200 with JSON
    ``{"evidence_type": str, "gcs_url": str}`` OR 204 No Content (no evidence).
    """

    service_endpoints: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    workspace_root: str = "."


class EvidenceCollector:
    """Drives per-service evidence collection at AUDIT_REPORT_GENERATED."""

    def __init__(self, config: EvidenceCollectorConfig) -> None:
        self._cfg = config

    async def collect(self, envelope: IncidentEnvelope) -> IncidentEvidence:
        """Fan-out per-service evidence collection + stamp reproducibility fields.

        Returns an IncidentEvidence with as many fields populated as
        services responded with. Failures of individual services log
        warnings but never raise — defence-in-depth.
        """
        # Reproducibility fields (mandatory)
        config_hash = self._git_sha(self._cfg.workspace_root)
        code_version = envelope.code_version or "unknown"
        runbook_version = self._git_sha(self._cfg.workspace_root)  # PM repo SHA

        # Per-service fan-out
        per_service_results = await self._fanout_evidence_capture(envelope.incident_key)

        # Map service responses → IncidentEvidence fields
        fields: dict[str, str] = {}
        additional: dict[str, str] = {}
        for evidence_type, gcs_url in per_service_results:
            if evidence_type in {
                "raw_venue_api_snapshot_url",
                "internal_ledger_snapshot_url",
                "order_fill_records_url",
                "position_records_url",
                "balance_records_url",
                "logs_url",
                "metrics_url",
                "traces_url",
                "agent_action_logs_url",
                "human_acknowledgement_trail_url",
            }:
                fields[evidence_type] = gcs_url
            else:
                additional[evidence_type] = gcs_url

        return IncidentEvidence(
            config_hash=config_hash,
            code_version=code_version,
            runbook_version=runbook_version,
            additional_evidence=additional,
            **fields,
        )

    async def _fanout_evidence_capture(self, incident_key: str) -> list[tuple[str, str]]:
        async def _capture_one(service: str, base_url: str) -> tuple[str, str] | None:
            url = f"{base_url.rstrip('/')}/evidence/{incident_key}"
            try:
                async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as client:
                    resp = await client.post(url)
                if resp.status_code == 204:
                    return None
                if resp.status_code == 200:
                    data = cast("dict[str, object]", resp.json())
                    et = data.get("evidence_type")
                    url_v = data.get("gcs_url")
                    if et and url_v:
                        return (str(et), str(url_v))
                _logger.warning(
                    "Evidence capture from %s returned %d: %s",
                    service,
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                _logger.warning("Evidence capture from %s failed: %s", service, repr(exc)[:200])
                return None

        tasks = [
            asyncio.create_task(_capture_one(service, base_url))
            for service, base_url in self._cfg.service_endpoints.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return [r for r in results if r is not None]

    @staticmethod
    def _git_sha(repo_path: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return r.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return "unknown"
