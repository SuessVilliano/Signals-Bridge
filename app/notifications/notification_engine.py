"""
Notification Engine.
Routes signal events to configured webhooks and manages notification delivery.
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List

from app.database import get_supabase
from app.notifications.webhook_sender import WebhookSender, WebhookSenderPool, NotificationPayload

logger = logging.getLogger(__name__)


class NotificationEngine:
    """
    Event routing engine for signal notifications.

    On every signal event:
    1. Fetch the signal details from database
    2. Find all webhooks subscribed to this event type
    3. Filter by provider (signal provider_id must match webhook provider_id)
    4. Send notifications to matching webhooks asynchronously
    5. Log all delivery results
    """

    def __init__(self, max_concurrent: int = 10):
        """
        Initialize notification engine.

        Args:
            max_concurrent: Maximum concurrent webhook deliveries (default 10)
        """
        self.sender = WebhookSender()
        self.sender_pool = WebhookSenderPool(max_concurrent=max_concurrent)

    async def on_signal_event(
        self,
        signal_id: str,
        event_type: str,
        price: Optional[float] = None,
    ):
        """
        Called whenever a signal event occurs.
        Routes event to all matching webhook configurations.

        Args:
            signal_id: The signal ID
            event_type: The event type (e.g., "ENTRY_HIT", "TP1_HIT", "SL_HIT")
            price: The price at which the event occurred (optional)
        """
        logger.debug(f"Processing signal event: {signal_id} | {event_type}")

        sb = get_supabase()

        try:
            # 1. Fetch signal details
            signal = await self._fetch_signal(sb, signal_id)
            if not signal:
                logger.warning(f"Signal not found: {signal_id}")
                return

            # 2. Find matching webhooks
            webhooks = await self._find_matching_webhooks(sb, signal, event_type)
            if not webhooks:
                logger.debug(f"No matching webhooks for event: {signal_id} | {event_type}")
                return

            logger.info(f"Found {len(webhooks)} matching webhooks for event: {signal_id} | {event_type}")

            # 3. Build notification payload
            payload = self._build_payload(signal, event_type, price)

            # 4. Send notifications asynchronously
            await self._send_notifications(sb, webhooks, payload)

        except Exception as e:
            logger.error(f"Error processing signal event: {signal_id} | {event_type} | {e}")

    async def _fetch_signal(self, sb, signal_id: str) -> Optional[dict]:
        """Fetch signal details from database."""
        try:
            result = sb.table("canonical_signals").select("*").eq("id", signal_id).execute()
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Failed to fetch signal {signal_id}: {e}")
            return None

    async def _find_matching_webhooks(self, sb, signal: dict, event_type: str) -> List[dict]:
        """
        Find all webhook configurations that match the signal event.

        Matching criteria:
        - Webhook provider_id matches signal provider_id
        - Webhook is active
        - event_type is in webhook's subscribed event_types
        - Circuit breaker is not triggered (< 10 consecutive failures)
        """
        try:
            provider_id = signal.get("provider_id")

            # Fetch all webhooks for this provider
            result = sb.table("outbound_webhooks").select("*").eq("provider_id", provider_id).eq("is_active", True).execute()
            webhooks = result.data or []

            # Filter by event_type subscription and circuit breaker
            matching = [
                webhook for webhook in webhooks
                if event_type in webhook.get("event_types", [])
                and webhook.get("consecutive_failures", 0) < 10
            ]

            return matching

        except Exception as e:
            logger.error(f"Failed to find matching webhooks: {e}")
            return []

    def _build_payload(self, signal: dict, event_type: str, price: Optional[float] = None) -> NotificationPayload:
        """Build notification payload from signal and event data."""
        import uuid
        event_id = f"evt_{uuid.uuid4().hex[:12]}"

        # Extract signal data
        signal_data = {
            "id": signal.get("id"),
            "symbol": signal.get("symbol"),
            "direction": signal.get("direction"),
            "entry_price": signal.get("entry_price"),
            "sl": signal.get("sl"),
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "tp3": signal.get("tp3"),
            "rr_ratio": signal.get("rr_ratio"),
            "risk_distance": signal.get("risk_distance"),
            "status": signal.get("status"),
            "strategy_name": signal.get("strategy_name"),
        }

        return NotificationPayload(
            event_id=event_id,
            signal_id=signal.get("id"),
            event_type=event_type,
            price=price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            signal=signal_data,
        )

    async def _send_notifications(self, sb, webhooks: list, payload: NotificationPayload):
        """
        Send notifications to all matching webhooks.

        Uses concurrent delivery for efficiency.
        """
        if not webhooks:
            return

        # Get webhook secrets from providers
        provider_ids = set(w.get("provider_id") for w in webhooks)
        provider_map = {}

        try:
            for provider_id in provider_ids:
                result = sb.table("providers").select("webhook_secret").eq("id", provider_id).execute()
                if result.data:
                    provider_map[provider_id] = result.data[0].get("webhook_secret", "")
        except Exception as e:
            logger.error(f"Failed to fetch webhook secrets: {e}")
            return

        # Send notifications in parallel
        try:
            # Group webhooks by provider for efficient delivery
            for provider_id, webhook_configs in self._group_by_provider(webhooks):
                secret = provider_map.get(provider_id, "")
                if not secret:
                    logger.warning(f"No webhook secret for provider: {provider_id}")
                    continue

                # Send to all webhooks for this provider concurrently
                results = await self.sender_pool.send_batch(webhook_configs, payload, secret)

                # Log delivery results
                for webhook_id, success in results.items():
                    if success:
                        logger.info(f"Webhook delivered: {webhook_id} | {payload.event_id}")
                    else:
                        logger.warning(f"Webhook delivery failed: {webhook_id} | {payload.event_id}")

        except Exception as e:
            logger.error(f"Error sending notifications: {e}")

    @staticmethod
    def _group_by_provider(webhooks: list) -> List[tuple]:
        """Group webhooks by provider_id."""
        grouped = {}
        for webhook in webhooks:
            provider_id = webhook.get("provider_id")
            if provider_id not in grouped:
                grouped[provider_id] = []
            grouped[provider_id].append(webhook)

        return list(grouped.items())


class NotificationEventHandler:
    """
    High-level event handler that can be called from anywhere in the application.
    Manages async task creation for notification delivery.
    """

    def __init__(self):
        """Initialize the event handler."""
        self.engine = NotificationEngine()

    def trigger_event(
        self,
        signal_id: str,
        event_type: str,
        price: Optional[float] = None,
    ):
        """
        Trigger an event notification (fire and forget).

        Safe to call from sync code. Uses asyncio.create_task for background processing.

        Args:
            signal_id: The signal ID
            event_type: The event type
            price: Optional price at event time
        """
        try:
            # Create background task
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in async context
                asyncio.create_task(self.engine.on_signal_event(signal_id, event_type, price))
            else:
                # In sync context, run as background task
                asyncio.run(self.engine.on_signal_event(signal_id, event_type, price))
        except RuntimeError:
            # No event loop, log and skip
            logger.warning(f"No event loop for notification: {signal_id} | {event_type}")
        except Exception as e:
            logger.error(f"Failed to trigger notification event: {e}")

    async def trigger_event_async(
        self,
        signal_id: str,
        event_type: str,
        price: Optional[float] = None,
    ):
        """
        Trigger an event notification (async).

        Use this when already in async context.

        Args:
            signal_id: The signal ID
            event_type: The event type
            price: Optional price at event time
        """
        await self.engine.on_signal_event(signal_id, event_type, price)


# Global instance for convenience
notification_handler = NotificationEventHandler()


def trigger_notification(signal_id: str, event_type: str, price: Optional[float] = None):
    """
    Global convenience function to trigger a notification event.

    Safe to call from sync or async code.

    Args:
        signal_id: The signal ID
        event_type: The event type (e.g., "ENTRY_HIT", "TP1_HIT")
        price: Optional price at event time
    """
    notification_handler.trigger_event(signal_id, event_type, price)
