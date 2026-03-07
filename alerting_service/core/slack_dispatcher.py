from typing import cast

import aiohttp
from unified_internal_contracts import AlertEvent

SEVERITY_COLORS: dict[str, str] = {
    "DEBUG": "#808080",
    "INFO": "#0EA5E9",
    "WARNING": "#F59E0B",
    "CRITICAL": "#EF4444",
    "FATAL": "#7C3AED",
}


def build_slack_blocks(event: AlertEvent, dashboard_url: str) -> dict[str, object]:
    return {
        "attachments": [
            {
                "color": SEVERITY_COLORS.get(event.severity, "#808080"),
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"[{event.severity}] {event.message}",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Rule:* {event.rule_id}"},
                            {"type": "mrkdwn", "text": f"*Value:* {event.metric_value:.4f}"},
                            {"type": "mrkdwn", "text": f"*Threshold:* {event.threshold}"},
                            {"type": "mrkdwn", "text": f"*Strategy:* {event.strategy_id or 'N/A'}"},
                            {"type": "mrkdwn", "text": f"*Venue:* {event.venue or 'N/A'}"},
                            {"type": "mrkdwn", "text": f"*Time:* {event.triggered_at.isoformat()}"},
                        ],
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "View Dashboard"},
                                "url": f"{dashboard_url}/system-health",
                                "style": "primary",
                            }
                        ],
                    },
                ],
            }
        ]
    }


async def send_slack_alert(webhook_url: str, event: AlertEvent, dashboard_url: str) -> str | None:
    payload = build_slack_blocks(event, dashboard_url)
    async with aiohttp.ClientSession() as session, session.post(webhook_url, json=payload) as resp:
        if resp.status == 200:
            data = cast(dict[str, object], await resp.json(content_type=None))
            ts = data.get("ts")
            return str(ts) if ts is not None else None
    return None
