"""
Webhook Ingestion API.
Receives signals from TradingView and PineScript, normalizes, validates, and stores them.
"""

import hmac
import hashlib
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional

from app.database import get_supabase
from app.models.webhook_schemas import TradingViewWebhook, PineScriptEvent
from app.models.canonical_signal import ValidationResult, SignalStatus, EventType
from app.engine.normalizer import SignalNormalizer
from app.engine.validator import ValidationEngine
from app.engine.state_machine import SignalStateMachine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["webhooks"])

validator = ValidationEngine()
normalizer = SignalNormalizer()


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature from webhook."""
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_text_alert(text: str) -> dict:
    """
    Parse a raw TradingView alert text (with emojis) into structured fields.

    Handles formats like:
        🔴 SELL ALERT 🔴
        📊 Symbol: NQ1!
        📈 Entry: 20537
        🎯 Stop Loss: 20620.96 (+83.96 points)
        ✅ Take Profit 1: 20450.00
        ✅ Take Profit 2: 20350.00
        ✅ Take Profit 3: 20250.00
    """
    import re

    result = {}

    # Direction
    text_upper = text.upper()
    if "SELL" in text_upper or "SHORT" in text_upper:
        result["direction"] = "SHORT"
    elif "BUY" in text_upper or "LONG" in text_upper:
        result["direction"] = "LONG"

    # Symbol — look for "Symbol: NQ1!" or "Symbol: EURUSD"
    sym_match = re.search(r"Symbol[:\s]+([A-Za-z0-9!]+)", text, re.IGNORECASE)
    if sym_match:
        result["symbol"] = sym_match.group(1).strip().rstrip("!")

    # Entry price
    entry_match = re.search(r"Entry[:\s]+([\d.,]+)", text, re.IGNORECASE)
    if entry_match:
        result["entry"] = float(entry_match.group(1).replace(",", ""))

    # Stop Loss
    sl_match = re.search(r"Stop\s*Loss[:\s]+([\d.,]+)", text, re.IGNORECASE)
    if sl_match:
        result["sl"] = float(sl_match.group(1).replace(",", ""))

    # Take Profits (TP1, TP2, TP3)
    tp_matches = re.findall(r"Take\s*Profit\s*(\d)?[:\s]+([\d.,]+)", text, re.IGNORECASE)
    if tp_matches:
        for idx, (tp_num, price) in enumerate(tp_matches):
            key = f"tp{tp_num or idx + 1}"
            result[key] = float(price.replace(",", ""))
    else:
        # Try "TP1:", "TP2:", "TP3:" format
        for i in range(1, 4):
            tp_match = re.search(rf"TP{i}[:\s]+([\d.,]+)", text, re.IGNORECASE)
            if tp_match:
                result[f"tp{i}"] = float(tp_match.group(1).replace(",", ""))

    # Target/Profit Target fallback
    if "tp1" not in result:
        target_matches = re.findall(r"(?:Target|Profit)[:\s]+([\d.,]+)", text, re.IGNORECASE)
        for i, m in enumerate(target_matches):
            result[f"tp{i+1}"] = float(m.replace(",", ""))

    return result


async def _ensure_default_provider(sb) -> dict:
    """Get or create a default provider for webhook ingestion."""
    import hashlib
    import secrets

    # Try to find any active provider
    try:
        result = sb.table("providers").select("*").eq("is_active", True).order("created_at").limit(1).execute()
        if result.data:
            return result.data[0]
    except Exception:
        pass

    # Auto-create a default provider
    api_key = secrets.token_hex(32)
    webhook_secret = secrets.token_hex(32)
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    webhook_secret_hash = hashlib.sha256(webhook_secret.encode()).hexdigest()

    try:
        result = sb.table("providers").insert({
            "name": "AutoBridge",
            "description": "Auto-created provider for Signal Bridge",
            "api_key_hash": api_key_hash,
            "webhook_secret": webhook_secret_hash,
            "is_active": True,
        }).execute()

        if result.data:
            logger.info(f"Auto-created default provider: {result.data[0]['id']}")
            return result.data[0]
    except Exception as e:
        logger.error(f"Failed to auto-create provider: {e}")

    return None


@router.post("/webhook/tradingview")
async def ingest_tradingview(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
):
    """
    Receive a TradingView webhook alert.

    Supports two formats:
    1. Structured JSON: {"symbol": "NQ", "direction": "LONG", "entry": 20150.50, ...}
    2. Raw text (from TaskMagic): "🔴 SELL ALERT 🔴\n📊 Symbol: NQ1!\n📈 Entry: 20537\n..."

    Steps:
    1. Parse raw JSON body (or extract from text if TaskMagic format)
    2. Authenticate provider via API key
    3. Normalize to CanonicalSignal
    4. Validate (price sanity, RR, timing)
    5. Store in database
    6. Create ENTRY_REGISTERED event
    7. Return signal_id + validation result
    """
    # 1. Parse body
    raw_body = await request.body()
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse JSON body: {e}")
        raise HTTPException(400, "Invalid JSON body")

    # Check if this is a TaskMagic-style payload with raw text in "body" field
    if isinstance(body.get("body"), str) and "symbol" not in body:
        logger.info("Detected TaskMagic text alert format, parsing...")
        text_body = body["body"]
        parsed = _parse_text_alert(text_body)
        if parsed.get("symbol") and parsed.get("direction"):
            body = parsed
            body["raw_text"] = text_body
        else:
            logger.error(f"Could not parse text alert: {text_body[:200]}")
            raise HTTPException(422, "Could not parse text alert. Expected symbol and direction.")

    # Also handle if the entire body is just a string
    if isinstance(body, str):
        parsed = _parse_text_alert(body)
        if parsed.get("symbol") and parsed.get("direction"):
            body = parsed
        else:
            raise HTTPException(422, "Could not parse text alert body")

    # 2. Resolve provider
    sb = get_supabase()
    provider = None

    # Option A: API key in header
    if x_api_key:
        try:
            key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
            result = sb.table("providers").select("*").eq("api_key_hash", key_hash).eq("is_active", True).execute()
            if result.data:
                provider = result.data[0]
        except Exception as e:
            logger.warning(f"Provider lookup by API key failed: {e}")

    # Option B: Provider name in the webhook body
    if not provider and body.get("provider"):
        try:
            result = sb.table("providers").select("*").eq("name", body["provider"]).eq("is_active", True).execute()
            if result.data:
                provider = result.data[0]
        except Exception as e:
            logger.warning(f"Provider lookup by name failed: {e}")

    # Option C: Get or auto-create a default provider
    if not provider:
        provider = await _ensure_default_provider(sb)

    if not provider:
        logger.error("No provider could be resolved or created.")
        raise HTTPException(500, "Provider initialization failed. Check Supabase connection and SUPABASE_SERVICE_KEY.")

    provider_id = provider["id"]

    # 3. Normalize
    try:
        webhook = TradingViewWebhook(**body)
        signal = normalizer.normalize_tradingview(webhook, provider_id)
    except Exception as e:
        logger.error(f"Normalization failed: {e}")
        raise HTTPException(422, f"Signal normalization failed: {str(e)}")

    # 4. Validate
    validation = validator.validate(signal)

    if not validation.is_valid:
        logger.warning(f"Signal validation failed: {validation.errors}")
        # Store as INVALID for audit
        signal.status = SignalStatus.INVALID
        _store_signal(sb, signal, validation)
        raise HTTPException(422, {
            "message": "Signal failed validation",
            "errors": validation.errors,
            "warnings": validation.warnings,
        })

    # 5. Store signal
    signal_data = _store_signal(sb, signal, validation)

    # 6. Create ENTRY_REGISTERED event
    try:
        sb.table("signal_events").insert({
            "signal_id": signal.id,
            "event_type": "ENTRY_REGISTERED",
            "price": signal.entry_price,
            "source": "TRADINGVIEW",
            "event_time": datetime.now(timezone.utc).isoformat(),
            "metadata": {"raw_body": body},
        }).execute()
    except Exception as e:
        logger.error(f"Failed to create ENTRY_REGISTERED event: {e}")

    logger.info(f"Signal ingested: {signal.id} | {signal.symbol} {signal.direction} @ {signal.entry_price}")

    return {
        "status": "accepted",
        "signal_id": signal.id,
        "symbol": signal.symbol,
        "direction": signal.direction,
        "validation": {
            "is_valid": validation.is_valid,
            "warnings": validation.warnings,
            "confidence_score": validation.confidence_score,
            "rr_ratio": validation.rr_ratio,
        },
    }


@router.post("/webhook/pinescript")
async def ingest_pinescript_event(request: Request):
    """
    Receive a price-level event from the TradingView PineScript monitor.
    Used for real-time NQ futures TP/SL detection.

    Expected JSON:
    {
        "signal_id": "sig_...",
        "event_type": "ENTRY_HIT",
        "price": 20150.50,
        "timestamp": "2025-02-13T10:30:00Z"
    }
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse PineScript event JSON: {e}")
        raise HTTPException(400, "Invalid JSON body")

    try:
        event = PineScriptEvent(**body)
    except Exception as e:
        logger.error(f"Invalid PineScript event schema: {e}")
        raise HTTPException(422, f"Invalid PineScript event: {str(e)}")

    sb = get_supabase()

    # Find the signal
    try:
        result = sb.table("canonical_signals").select("*").eq("id", event.signal_id).execute()
        if not result.data:
            logger.warning(f"Signal not found: {event.signal_id}")
            raise HTTPException(404, f"Signal not found: {event.signal_id}")

        signal_data = result.data[0]
    except Exception as e:
        logger.error(f"Database query failed: {e}")
        raise HTTPException(500, "Failed to retrieve signal")

    # Process state transition
    current_status = SignalStatus(signal_data["status"])
    event_type = EventType(event.event_type)

    tr = SignalStateMachine.process_event(current_status, event_type)
    new_status, did_transition = tr.new_status, tr.did_transition

    if did_transition:
        try:
            # Create event
            sb.table("signal_events").insert({
                "signal_id": event.signal_id,
                "event_type": event.event_type,
                "price": event.price,
                "source": "PINESCRIPT",
                "event_time": event.timestamp or datetime.now(timezone.utc).isoformat(),
            }).execute()

            # Update signal status
            update_data = {"status": new_status.value}
            if new_status in (SignalStatus.SL_HIT, SignalStatus.CLOSED, SignalStatus.TP3_HIT):
                update_data["closed_at"] = datetime.now(timezone.utc).isoformat()
                update_data["close_reason"] = new_status.value
                update_data["exit_price"] = event.price
            if event_type == EventType.ENTRY_HIT:
                update_data["activated_at"] = datetime.now(timezone.utc).isoformat()

            sb.table("canonical_signals").update(update_data).eq("id", event.signal_id).execute()

            logger.info(f"PineScript event: {event.event_type} for signal {event.signal_id} @ {event.price}")
        except Exception as e:
            logger.error(f"Failed to process state transition: {e}")
            raise HTTPException(500, "Failed to process event")

    return {
        "status": "processed",
        "signal_id": event.signal_id,
        "event_type": event.event_type,
        "did_transition": did_transition,
        "new_status": new_status.value,
    }


def _store_signal(sb, signal, validation):
    """Store a canonical signal in the database."""
    try:
        data = {
            "id": signal.id,
            "provider_id": signal.provider_id,
            "external_signal_id": signal.external_signal_id,
            "strategy_name": signal.strategy_name,
            "symbol": signal.symbol,
            "asset_class": signal.asset_class.value if hasattr(signal.asset_class, 'value') else signal.asset_class,
            "direction": signal.direction.value if hasattr(signal.direction, 'value') else signal.direction,
            "entry_price": signal.entry_price,
            "sl": signal.sl,
            "tp1": signal.tp1,
            "tp2": signal.tp2,
            "tp3": signal.tp3,
            "risk_distance": signal.risk_distance,
            "rr_ratio": signal.rr_ratio,
            "status": signal.status.value if hasattr(signal.status, 'value') else signal.status,
            "entry_time": signal.entry_time.isoformat() if hasattr(signal.entry_time, 'isoformat') else signal.entry_time,
            "raw_payload": signal.raw_payload,
            "validation_errors": validation.errors if validation.errors else None,
            "validation_warnings": validation.warnings if validation.warnings else None,
            "next_poll_at": datetime.now(timezone.utc).isoformat(),
        }
        result = sb.table("canonical_signals").insert(data).execute()
        return result.data[0] if result.data else data
    except Exception as e:
        logger.error(f"Failed to store signal in database: {e}")
        raise
