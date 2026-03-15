"""
Cloud-agnostic persistence for alert history, config snapshots, and cooldown state.

Uses ``get_storage_client()`` from unified_cloud_interface — works on GCS, S3, or
local filesystem depending on ``CLOUD_PROVIDER`` env var. No direct cloud SDK imports.

Path conventions (see ``docs/GCS_PATHS.md``):
  - ``alerting/history/date={YYYY-MM-DD}/`` — JSONL alert events
  - ``alerting/configs/`` — YAML routing rule snapshots
  - ``alerting/state/`` — cooldown state JSON
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import cast

import yaml
from unified_cloud_interface import StorageClient, get_storage_client
from unified_config_interface import UnifiedCloudConfig
from unified_events_interface import log_event

logger = logging.getLogger(__name__)

_HISTORY_PREFIX = "alerting/history"
_CONFIGS_PREFIX = "alerting/configs"
_STATE_PREFIX = "alerting/state"
_COOLDOWN_BLOB = f"{_STATE_PREFIX}/cooldowns.json"


@lru_cache(maxsize=1)
def _get_cloud_config() -> UnifiedCloudConfig:
    """Return singleton UnifiedCloudConfig instance."""
    return UnifiedCloudConfig()


def _bucket_name(project_id: str) -> str:
    """Derive the storage bucket name from the project ID.

    Convention: ``alerting-service-{project_id}``.
    """
    return f"alerting-service-{project_id}"


class AlertStorageStore:
    """Cloud-agnostic persistence for alerting-service.

    Wraps the UCI StorageClient to read/write alert history (JSONL),
    routing config snapshots (YAML), and cooldown state (JSON).
    """

    def __init__(
        self,
        storage_client: StorageClient | None = None,
        bucket: str | None = None,
    ) -> None:
        config = _get_cloud_config()
        project_id = config.gcp_project_id
        self._client: StorageClient = storage_client or get_storage_client(
            project_id=project_id,
        )
        self._bucket = bucket or _bucket_name(project_id)

    # ------------------------------------------------------------------
    # Alert history (JSONL)
    # ------------------------------------------------------------------

    def write_alert_history(self, alert_event: dict[str, object]) -> None:
        """Append a single alert event as a JSONL line to the history partition.

        Each call writes one JSONL object to a uniquely-named blob under
        ``alerting/history/date={YYYY-MM-DD}/``.  The blob name includes a
        UUID to avoid overwrites when multiple instances write concurrently.
        """
        now = datetime.now(UTC)
        date_partition = now.strftime("%Y-%m-%d")
        blob_id = uuid.uuid4().hex[:12]
        blob_path = f"{_HISTORY_PREFIX}/date={date_partition}/{blob_id}.jsonl"

        line = json.dumps(alert_event, default=str)
        data = (line + "\n").encode("utf-8")

        try:
            self._client.upload_bytes(
                bucket=self._bucket,
                blob_path=blob_path,
                data=data,
                content_type="application/x-ndjson",
            )
            log_event(
                "PERSISTENCE_COMPLETED",
                details={"target": "alert_history", "blob_path": blob_path},
            )
        except Exception:
            logger.exception("Failed to write alert history to GCS: %s", blob_path)

    # ------------------------------------------------------------------
    # Config snapshots (YAML)
    # ------------------------------------------------------------------

    def write_config_snapshot(self, config: dict[str, object], name: str = "default_rules") -> None:
        """Write a YAML snapshot of the routing config to GCS.

        Args:
            config: Routing rule configuration dictionary.
            name: Filename stem (default ``default_rules``).
        """
        blob_path = f"{_CONFIGS_PREFIX}/{name}.yaml"
        data = yaml.dump(config, default_flow_style=False).encode("utf-8")

        try:
            self._client.upload_bytes(
                bucket=self._bucket,
                blob_path=blob_path,
                data=data,
                content_type="application/x-yaml",
            )
            log_event(
                "PERSISTENCE_COMPLETED",
                details={"target": "config_snapshot", "blob_path": blob_path},
            )
        except Exception:
            logger.exception("Failed to write config snapshot to GCS: %s", blob_path)

    # ------------------------------------------------------------------
    # Cooldown state (JSON)
    # ------------------------------------------------------------------

    def read_cooldown_state(self) -> dict[str, object]:
        """Read cooldown state from GCS.

        Returns an empty dict if the blob does not exist or cannot be read.
        """
        try:
            if not self._client.blob_exists(bucket=self._bucket, blob_path=_COOLDOWN_BLOB):
                return {}
            raw = self._client.download_bytes(bucket=self._bucket, blob_path=_COOLDOWN_BLOB)
            result: dict[str, object] = cast("dict[str, object]", json.loads(raw.decode("utf-8")))
            return result
        except Exception:
            logger.exception("Failed to read cooldown state from GCS")
            return {}

    def read_delivery_records(self, alert_id: str) -> list[dict[str, object]]:
        """Read delivery records for a specific alert_id from GCS history.

        Scans recent date partitions (last 7 days) for JSONL blobs
        containing matching alert_id values.

        Args:
            alert_id: The alert ID to search for.

        Returns:
            List of delivery record dicts matching the alert_id.
        """
        records: list[dict[str, object]] = []
        now = datetime.now(UTC)

        for day_offset in range(7):
            date_partition = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            prefix = f"{_HISTORY_PREFIX}/date={date_partition}/"

            try:
                blobs = self._client.list_blobs(bucket=self._bucket, prefix=prefix)
                for blob_meta in blobs:
                    raw = self._client.download_bytes(bucket=self._bucket, blob_path=blob_meta.name)
                    for line in raw.decode("utf-8").strip().splitlines():
                        if not line:
                            continue
                        record: dict[str, object] = cast("dict[str, object]", json.loads(line))
                        if record.get("alert_id") == alert_id:
                            records.append(record)
            except Exception:
                logger.exception("Failed to read delivery records for date=%s", date_partition)
                continue

        return records

    def write_cooldown_state(self, state: dict[str, object]) -> None:
        """Write cooldown state JSON to GCS.

        Args:
            state: Cooldown state dictionary to persist.
        """
        data = json.dumps(state, default=str).encode("utf-8")

        try:
            self._client.upload_bytes(
                bucket=self._bucket,
                blob_path=_COOLDOWN_BLOB,
                data=data,
                content_type="application/json",
            )
            log_event(
                "PERSISTENCE_COMPLETED",
                details={"target": "cooldown_state", "blob_path": _COOLDOWN_BLOB},
            )
        except Exception:
            logger.exception("Failed to write cooldown state to GCS")
