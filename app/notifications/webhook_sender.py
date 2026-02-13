"""
Outbound Webhook Sender.
Handles asynchronous delivery of webhook notifications with retry logic and circuit breaking.
"""

import logging
import hashlib
import hmac
import httpx
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict
from dataclasses import dataclass

from app.database import get_supabase

logger = logging.getLogger(__name__)


@dataclass
class NotificationPayload:
    """Webhook notification payload structure."""
    event_id: str
    signal_id: str
    event_type: str
    price: Optional[float]
    timestamp: str
    signal: Dict


class WebhookSender:
    """
    Manages outbound webhook delivery.

    Features:
    - Exponential backoff retry (1s, 5s, 30s)
    - Circuit breaker pattern (disable after 10 consecutive failures)
    - HMAC-SHA256 signature verification
    - Idempotency key headers
    - Comprehensive logging
    """

    # Retry configuration (in seconds)
    RETRY_DELAYS = [1, 5, 30]
    MAX_CONSECUTIVE_FAILURES = 10
    REQUEST_TIMEOUT = 10

    async def send(
        self,
        webhook_config: dict,
        payload: NotificationPayload,
        webhook_secret: str,
    ) -> bool:
        """
        Send a webhook notification with automatic retry.

        Args:
            webhook_config: Webhook configuration from database
            payload: Notification payload to send
            webhook_secret: Secret for HMAC signing

        Returns:
            True if delivery succeeded, False otherwise
        """
        url = webhook_config["url"]
        webhook_id = webhook_config["id"]

        # Check circuit breaker
        if webhook_config.get("consecutive_failures", 0) >= self.MAX_CONSECUTIVE_FAILURES:
            logger.warning(f"Webhook circuit breaker triggered: {webhook_id} | {url}")
            return False

        # Prepare payload
        payload_dict = {
            "event_id": payload.event_id,
            "signal_id": payload.signal_id,
            "event_type": payload.event_type,
            "price": payload.price,
            "timestamp": payload.timestamp,
            "signal": payload.signal,
        }

        # Attempt delivery with retries
        for attempt, delay in enumerate([0] + self.RETRY_DELAYS):
            if attempt > 0:
                logger.info(f"Retrying webhook: {webhook_id} | attempt {attempt} | delay {delay}s")
                await asyncio.sleep(delay)

            success = await self._send_request(
                webhook_id=webhook_id,
                url=url,
                payload=payload_dict,
                webhook_secret=webhook_secret,
                headers=webhook_config.get("headers", {}),
            )

            if success:
                # Update webhook config: reset consecutive failures, update last delivery
                sb = get_supabase()
                try:
                    sb.table("outbound_webhooks").update({
                        "consecutive_failures": 0,
                        "last_delivery_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("id", webhook_id).execute()
                except Exception as e:
                    logger.error(f"Failed to update webhook status: {e}")

                return True

        # Delivery failed after all retries
        sb = get_supabase()
        try:
            # Increment consecutive failures
            new_failures = webhook_config.get("consecutive_failures", 0) + 1
            sb.table("outbound_webhooks").update({
                "consecutive_failures": new_failures,
            }).eq("id", webhook_id).execute()

            logger.error(f"Webhook delivery failed after all retries: {webhook_id}")
        except Exception as e:
            logger.error(f"Failed to update webhook failure count: {e}")

        return False

    async def _send_request(
        self,
        webhook_id: str,
        url: str,
        payload: dict,
        webhook_secret: str,
        headers: dict = None,
    ) -> bool:
        """
        Send a single webhook request.

        Returns:
            True if status code < 300, False otherwise
        """
        try:
            # Serialize payload as JSON
            import json
            payload_json = json.dumps(payload, separators=(",", ":"))

            # Generate signature
            signature = self._generate_signature(payload_json, webhook_secret)

            # Prepare headers
            request_headers = {
                "Content-Type": "application/json",
                "X-Idempotency-Key": payload["event_id"],
                "X-Signature": signature,
            }

            # Add custom headers
            if headers:
                request_headers.update(headers)

            # Send request
            async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT) as client:
                response = await client.post(
                    url,
                    content=payload_json,
                    headers=request_headers,
                )

                success = response.status_code < 300

                # Log delivery
                self._log_delivery(
                    webhook_id=webhook_id,
                    url=url,
                    event_id=payload["event_id"],
                    status_code=response.status_code,
                    success=success,
                    response_text=response.text[:200] if response.status_code >= 400 else None,
                )

                return success

        except httpx.TimeoutException as e:
            logger.error(f"Webhook timeout: {webhook_id} | {url} | {e}")
            return False
        except httpx.RequestError as e:
            logger.error(f"Webhook request error: {webhook_id} | {url} | {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending webhook: {webhook_id} | {url} | {e}")
            return False

    @staticmethod
    def _generate_signature(payload_json: str, webhook_secret: str) -> str:
        """
        Generate HMAC-SHA256 signature for webhook payload.

        Args:
            payload_json: JSON-serialized payload
            webhook_secret: Secret for HMAC signing

        Returns:
            Hex-encoded HMAC-SHA256 signature
        """
        signature = hmac.new(
            webhook_secret.encode(),
            payload_json.encode(),
            hashlib.sha256,
        ).hexdigest()
        return signature

    @staticmethod
    def _log_delivery(
        webhook_id: str,
        url: str,
        event_id: str,
        status_code: int,
        success: bool,
        response_text: Optional[str] = None,
    ):
        """Log webhook delivery attempt."""
        sb = get_supabase()

        try:
            sb.table("notification_logs").insert({
                "webhook_id": webhook_id,
                "event_id": event_id,
                "url": url,
                "status_code": status_code,
                "success": success,
                "response_text": response_text,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            level = "info" if success else "warning"
            log_message = f"Webhook delivery {'succeeded' if success else 'failed'}: {webhook_id} | {event_id} | {status_code}"
            if response_text:
                log_message += f" | {response_text}"

            if level == "info":
                logger.info(log_message)
            else:
                logger.warning(log_message)

        except Exception as e:
            logger.error(f"Failed to log webhook delivery: {e}")


class WebhookSenderPool:
    """
    Manages concurrent webhook delivery across multiple webhooks.
    Queues and batches notifications for efficient async processing.
    """

    def __init__(self, max_concurrent: int = 10):
        """
        Initialize the webhook sender pool.

        Args:
            max_concurrent: Maximum concurrent webhook deliveries (default 10)
        """
        self.max_concurrent = max_concurrent
        self.sender = WebhookSender()
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def send_batch(
        self,
        webhook_configs: list,
        payload: NotificationPayload,
        webhook_secret: str,
    ) -> dict:
        """
        Send notifications to multiple webhooks concurrently.

        Args:
            webhook_configs: List of webhook configurations
            payload: Notification payload
            webhook_secret: Secret for HMAC signing

        Returns:
            Dictionary with delivery results by webhook ID
        """
        tasks = [
            self._send_with_semaphore(config, payload, webhook_secret)
            for config in webhook_configs
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            config["id"]: (result if isinstance(result, bool) else False)
            for config, result in zip(webhook_configs, results)
        }

    async def _send_with_semaphore(
        self,
        config: dict,
        payload: NotificationPayload,
        webhook_secret: str,
    ) -> bool:
        """Wrapper to limit concurrent sends using semaphore."""
        async with self.semaphore:
            return await self.sender.send(config, payload, webhook_secret)
