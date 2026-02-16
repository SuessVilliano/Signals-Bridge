"""
Outbound Webhook Configuration Endpoints.
Manages destination webhooks that receive notifications from the Signal Bridge.
"""

import logging
import httpx
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

from app.database import get_supabase
from app.notifications.webhook_sender import WebhookSender
from app.models.canonical_signal import EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["webhooks"])


# ============================================================================
# Request/Response Models
# ============================================================================

class CreateWebhookRequest(BaseModel):
    """Request to create an outbound webhook configuration."""
    provider_id: str
    name: str = Field(..., min_length=1, max_length=255)
    url: HttpUrl
    event_types: List[str] = Field(default=["ENTRY_REGISTERED", "ENTRY_HIT", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"])
    headers: Optional[dict] = None
    is_active: bool = True


class WebhookResponse(BaseModel):
    """Outbound webhook configuration response."""
    id: str
    provider_id: str
    name: str
    url: str
    event_types: List[str]
    headers: Optional[dict]
    is_active: bool
    created_at: str
    updated_at: Optional[str]
    last_delivery_at: Optional[str]
    consecutive_failures: int


class WebhookListResult(BaseModel):
    """Paginated webhook list."""
    total: int
    limit: int
    offset: int
    items: List[WebhookResponse]


class TestWebhookRequest(BaseModel):
    """Request to test a webhook."""
    event_type: str = "TEST_EVENT"
    signal_id: Optional[str] = None
    price: Optional[float] = None


class TestWebhookResponse(BaseModel):
    """Response from webhook test."""
    status: str
    code: int
    latency_ms: float
    message: Optional[str]


# ============================================================================
# Valid Event Types
# ============================================================================

VALID_EVENT_TYPES = [
    "ENTRY_REGISTERED",
    "ENTRY_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "SL_HIT",
    "CLOSED",
    "INVALID",
    "MANUAL_CLOSE",
    "TEST_EVENT",
]


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/webhooks/outbound", response_model=WebhookResponse)
async def create_webhook(req: CreateWebhookRequest):
    """
    Configure a new outbound webhook.

    The webhook will receive POST requests with signal events matching the
    subscribed event_types.

    Payload format:
    {
        "event_id": "evt_...",
        "signal_id": "sig_...",
        "event_type": "ENTRY_HIT",
        "price": 20150.50,
        "timestamp": "2025-02-13T10:30:00Z",
        "signal": {
            "symbol": "NQ",
            "direction": "LONG",
            "entry_price": 20150.50,
            ...
        }
    }

    All requests include:
    - X-Idempotency-Key: event_id for idempotent processing
    - X-Signature: HMAC-SHA256 of body using webhook_secret
    """
    sb = get_supabase()

    try:
        # Verify provider exists
        result = sb.table("providers").select("*").eq("id", req.provider_id).execute()
        if not result.data:
            raise HTTPException(404, f"Provider not found: {req.provider_id}")

        # Validate event types
        for et in req.event_types:
            if et not in VALID_EVENT_TYPES:
                raise HTTPException(400, f"Invalid event type: {et}")

        now = datetime.now(timezone.utc).isoformat()

        # Insert webhook config
        result = sb.table("webhook_configs").insert({
            "provider_id": req.provider_id,
            "name": req.name,
            "url": str(req.url),
            "event_types": req.event_types,
            "headers": req.headers or {},
            "is_active": req.is_active,
            "consecutive_failures": 0,
            "created_at": now,
        }).execute()

        if not result.data:
            raise Exception("Failed to insert webhook config")

        webhook = result.data[0]

        logger.info(f"Outbound webhook created: {webhook['id']} | {req.name}")

        return WebhookResponse(
            id=webhook["id"],
            provider_id=webhook["provider_id"],
            name=webhook["name"],
            url=webhook["url"],
            event_types=webhook["event_types"],
            headers=webhook.get("headers"),
            is_active=webhook["is_active"],
            created_at=webhook["created_at"],
            updated_at=webhook.get("updated_at"),
            last_delivery_at=webhook.get("last_delivery_at"),
            consecutive_failures=webhook.get("consecutive_failures", 0),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create outbound webhook: {e}")
        raise HTTPException(500, "Failed to create webhook")


@router.get("/webhooks/outbound", response_model=WebhookListResult)
async def list_webhooks(
    provider_id: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    List outbound webhook configurations.

    Query Parameters:
    - provider_id: Filter by provider ID
    - is_active: Filter by active status
    - limit: Number of results (1-500, default 50)
    - offset: Pagination offset (default 0)
    """
    sb = get_supabase()

    try:
        query = sb.table("webhook_configs").select("*")

        if provider_id:
            query = query.eq("provider_id", provider_id)
        if is_active is not None:
            query = query.eq("is_active", is_active)

        # Get total count
        count_result = query.execute()
        total = len(count_result.data) if count_result.data else 0

        # Apply pagination
        query = sb.table("webhook_configs").select("*")
        if provider_id:
            query = query.eq("provider_id", provider_id)
        if is_active is not None:
            query = query.eq("is_active", is_active)

        result = query.order("created_at", desc=True).range(offset, offset + limit - 1).execute()

        items = [
            WebhookResponse(
                id=webhook["id"],
                provider_id=webhook["provider_id"],
                name=webhook["name"],
                url=webhook["url"],
                event_types=webhook["event_types"],
                headers=webhook.get("headers"),
                is_active=webhook["is_active"],
                created_at=webhook["created_at"],
                updated_at=webhook.get("updated_at"),
                last_delivery_at=webhook.get("last_delivery_at"),
                consecutive_failures=webhook.get("consecutive_failures", 0),
            )
            for webhook in (result.data or [])
        ]

        return WebhookListResult(
            total=total,
            limit=limit,
            offset=offset,
            items=items,
        )
    except Exception as e:
        logger.error(f"Failed to list outbound webhooks: {e}")
        raise HTTPException(500, "Failed to list webhooks")


@router.delete("/webhooks/outbound/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """
    Delete an outbound webhook configuration.

    Path Parameters:
    - webhook_id: The webhook configuration ID
    """
    sb = get_supabase()

    try:
        # Verify webhook exists
        result = sb.table("webhook_configs").select("*").eq("id", webhook_id).execute()
        if not result.data:
            raise HTTPException(404, f"Webhook not found: {webhook_id}")

        # Delete webhook
        sb.table("webhook_configs").delete().eq("id", webhook_id).execute()

        logger.info(f"Outbound webhook deleted: {webhook_id}")

        return {
            "status": "deleted",
            "webhook_id": webhook_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete webhook {webhook_id}: {e}")
        raise HTTPException(500, "Failed to delete webhook")


@router.post("/webhooks/outbound/{webhook_id}/test", response_model=TestWebhookResponse)
async def test_webhook(webhook_id: str, req: Optional[TestWebhookRequest] = None):
    """
    Send a test notification to verify webhook configuration.

    Path Parameters:
    - webhook_id: The webhook configuration ID

    Request Body (optional):
    - event_type: Type of test event (default: TEST_EVENT)
    - signal_id: Optional signal ID to reference
    - price: Optional price for the test event

    Returns delivery status and latency.
    """
    sb = get_supabase()
    sender = WebhookSender()

    try:
        # Fetch webhook config
        result = sb.table("webhook_configs").select("*").eq("id", webhook_id).execute()
        if not result.data:
            raise HTTPException(404, f"Webhook not found: {webhook_id}")

        webhook = result.data[0]

        if not webhook["is_active"]:
            raise HTTPException(400, "Webhook is not active")

        req = req or TestWebhookRequest()

        # Build test payload
        test_payload = {
            "event_id": f"test_{datetime.now(timezone.utc).timestamp()}",
            "signal_id": req.signal_id or "test_signal",
            "event_type": req.event_type,
            "price": req.price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": {
                "symbol": "TEST",
                "direction": "LONG",
                "entry_price": 100.0,
                "sl": 95.0,
                "tp1": 105.0,
                "tp2": 110.0,
                "tp3": 115.0,
                "rr_ratio": 5.0,
            },
        }

        # Send test request
        import time
        start = time.time()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    webhook["url"],
                    json=test_payload,
                    headers={
                        "X-Idempotency-Key": test_payload["event_id"],
                        "Content-Type": "application/json",
                    },
                )
                latency_ms = (time.time() - start) * 1000

                return TestWebhookResponse(
                    status="success" if response.status_code < 300 else "failed",
                    code=response.status_code,
                    latency_ms=round(latency_ms, 2),
                    message=response.text[:500] if response.status_code >= 400 else None,
                )
        except httpx.RequestError as e:
            latency_ms = (time.time() - start) * 1000
            logger.error(f"Webhook test failed: {e}")
            return TestWebhookResponse(
                status="failed",
                code=0,
                latency_ms=round(latency_ms, 2),
                message=str(e),
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to test webhook {webhook_id}: {e}")
        raise HTTPException(500, "Failed to test webhook")
