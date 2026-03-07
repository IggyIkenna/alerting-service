import logging

import anthropic
from anthropic.types import TextBlock
from unified_internal_contracts import AlertEvent

logger = logging.getLogger(__name__)

TRIAGE_SYSTEM_PROMPT = (
    "You are an institutional trading system on-call engineer. "
    "Analyze the alert and provide: "
    "1. Root cause hypothesis (2-3 sentences). "
    "2. Immediate action steps (numbered list, max 5). "
    "3. Which service/log to check first. "
    "Be concise. This goes to Slack. Use bullet points."
)


async def post_ai_triage(
    event: AlertEvent,
    slack_ts: str,
    webhook_url: str,
    anthropic_api_key: str,
) -> None:
    context = (
        f"Alert: {event.message}\n"
        f"Severity: {event.severity}\n"
        f"Metric: {event.rule_id} = {event.metric_value} (threshold: {event.threshold})\n"
        f"Strategy: {event.strategy_id}\n"
        f"Venue: {event.venue}\n"
        f"Time: {event.triggered_at}\n"
    )
    client = anthropic.Anthropic(api_key=anthropic_api_key)
    response = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=400,
        system=TRIAGE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    first_block = response.content[0] if response.content else None
    triage_text = (
        first_block.text if isinstance(first_block, TextBlock) else "No analysis available."
    )
    logger.info("AI triage generated for alert %s (slack_ts=%s)", event.alert_id, slack_ts)
    _ = triage_text
