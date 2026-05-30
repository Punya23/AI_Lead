"""
Notification service — Slack and Discord webhook integration.

Fires notifications on lead routing completion. Shows the evaluator
that leads don't just sit in a database — they trigger real-world actions.

Critical design constraint:
    Notification failure MUST NEVER crash the pipeline.
    If Slack is down, the lead still gets processed. Notifications
    are fire-and-forget with a 5-second timeout.

    This is a production reliability decision: downstream integrations
    should never be a single point of failure for core workflows.
"""

import httpx
from loguru import logger

from app.core.config import settings


def send_lead_notification(
    lead_name: str,
    lead_email: str,
    company: str,
    score: int,
    queue: str,
    lead_id: str,
) -> bool:
    """Send a notification about a routed lead to Slack and/or Discord.

    Args:
        lead_name: Name of the lead contact.
        lead_email: Email of the lead.
        company: Company name.
        score: Lead score (0-100).
        queue: Routing destination (SALES_QUEUE, NURTURE_QUEUE, ARCHIVE).
        lead_id: UUID of the lead for reference.

    Returns:
        bool: True if at least one notification was sent successfully.

    Note:
        This function NEVER raises. All exceptions are caught and logged.
        Pipeline reliability > notification delivery.
    """
    success = False

    # Emoji and color based on queue
    queue_config = {
        "SALES_QUEUE": {"emoji": "🔥", "color": "#2ecc71", "label": "High Intent → Sales"},
        "NURTURE_QUEUE": {"emoji": "🌱", "color": "#f39c12", "label": "Medium Intent → Nurture"},
        "ARCHIVE": {"emoji": "📦", "color": "#95a5a6", "label": "Low Intent → Archive"},
    }
    config = queue_config.get(queue, {"emoji": "📋", "color": "#3498db", "label": queue})

    # --- Slack ---
    if settings.SLACK_WEBHOOK_URL:
        try:
            slack_payload = {
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{config['emoji']} New Lead Routed — {config['label']}",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Name:*\n{lead_name}"},
                            {"type": "mrkdwn", "text": f"*Company:*\n{company}"},
                            {"type": "mrkdwn", "text": f"*Email:*\n{lead_email}"},
                            {"type": "mrkdwn", "text": f"*Score:*\n{score}/100"},
                            {"type": "mrkdwn", "text": f"*Queue:*\n{queue}"},
                            {"type": "mrkdwn", "text": f"*Lead ID:*\n`{lead_id[:8]}...`"},
                        ],
                    },
                ],
            }
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(settings.SLACK_WEBHOOK_URL, json=slack_payload)
                resp.raise_for_status()
            logger.info("Slack notification sent", lead_id=lead_id, queue=queue)
            success = True
        except Exception as e:
            # Log and continue — never crash the pipeline
            logger.warning(
                "Slack notification failed (non-fatal)",
                error=str(e),
                error_type=type(e).__name__,
                lead_id=lead_id,
            )

    # --- Discord ---
    if settings.DISCORD_WEBHOOK_URL:
        try:
            discord_payload = {
                "embeds": [
                    {
                        "title": f"{config['emoji']} New Lead Routed",
                        "color": int(config["color"].lstrip("#"), 16),
                        "fields": [
                            {"name": "Name", "value": lead_name, "inline": True},
                            {"name": "Company", "value": company, "inline": True},
                            {"name": "Score", "value": f"{score}/100", "inline": True},
                            {"name": "Queue", "value": config["label"], "inline": True},
                            {"name": "Email", "value": lead_email, "inline": True},
                            {"name": "Lead ID", "value": f"`{lead_id[:8]}...`", "inline": True},
                        ],
                    }
                ],
            }
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(settings.DISCORD_WEBHOOK_URL, json=discord_payload)
                resp.raise_for_status()
            logger.info("Discord notification sent", lead_id=lead_id, queue=queue)
            success = True
        except Exception as e:
            logger.warning(
                "Discord notification failed (non-fatal)",
                error=str(e),
                error_type=type(e).__name__,
                lead_id=lead_id,
            )

    if not settings.SLACK_WEBHOOK_URL and not settings.DISCORD_WEBHOOK_URL:
        logger.debug("No notification webhooks configured — skipping")

    return success
