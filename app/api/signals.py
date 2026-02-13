"""
Signal Query and Management Endpoints.
Provides REST API for listing, filtering, and managing canonical signals.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import get_supabase
from app.models.canonical_signal import SignalStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["signals"])


# ============================================================================
# Response Models
# ============================================================================

class SignalEventResponse(BaseModel):
    """Response model for a single signal event."""
    signal_id: str
    event_type: str
    price: Optional[float]
    source: str
    event_time: str
    metadata: Optional[dict] = None


class SignalDetailResponse(BaseModel):
    """Detailed signal response with full event history."""
    id: str
    provider_id: str
    symbol: str
    asset_class: str
    direction: str
    entry_price: float
    sl: float
    tp1: float
    tp2: Optional[float]
    tp3: Optional[float]
    status: str
    entry_time: str
    activated_at: Optional[str]
    closed_at: Optional[str]
    close_reason: Optional[str]
    exit_price: Optional[float]
    rr_ratio: Optional[float]
    risk_distance: Optional[float]
    strategy_name: Optional[str]
    external_signal_id: Optional[str]
    validation_warnings: Optional[List[str]]
    validation_errors: Optional[List[str]]
    events: List[SignalEventResponse]


class SignalListResponse(BaseModel):
    """Compact signal response for list views."""
    id: str
    symbol: str
    direction: str
    entry_price: float
    sl: float
    tp1: float
    status: str
    entry_time: str
    rr_ratio: Optional[float]


class SignalListResult(BaseModel):
    """Paginated list result."""
    total: int
    limit: int
    offset: int
    items: List[SignalListResponse]


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/signals", response_model=SignalListResult)
async def list_signals(
    provider_id: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    List all signals with optional filters.

    Query Parameters:
    - provider_id: Filter by provider ID
    - symbol: Filter by trading symbol (e.g., "NQ", "ES")
    - status: Filter by status (PENDING, ACTIVE, TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, CLOSED, INVALID)
    - limit: Number of results (1-500, default 50)
    - offset: Pagination offset (default 0)

    Returns paginated list of signals.
    """
    sb = get_supabase()

    try:
        # Build query
        query = sb.table("canonical_signals").select("*")

        if provider_id:
            query = query.eq("provider_id", provider_id)
        if symbol:
            query = query.eq("symbol", symbol)
        if status:
            query = query.eq("status", status)

        # Get total count
        count_result = query.execute()
        total = len(count_result.data) if count_result.data else 0

        # Apply pagination
        query = sb.table("canonical_signals").select("*")
        if provider_id:
            query = query.eq("provider_id", provider_id)
        if symbol:
            query = query.eq("symbol", symbol)
        if status:
            query = query.eq("status", status)

        result = query.order("entry_time", desc=True).range(offset, offset + limit - 1).execute()

        items = [
            SignalListResponse(
                id=sig["id"],
                symbol=sig["symbol"],
                direction=sig["direction"],
                entry_price=sig["entry_price"],
                sl=sig["sl"],
                tp1=sig["tp1"],
                status=sig["status"],
                entry_time=sig["entry_time"],
                rr_ratio=sig.get("rr_ratio"),
            )
            for sig in result.data
        ]

        return SignalListResult(
            total=total,
            limit=limit,
            offset=offset,
            items=items,
        )
    except Exception as e:
        logger.error(f"Failed to list signals: {e}")
        raise HTTPException(500, "Failed to list signals")


@router.get("/signals/{signal_id}", response_model=SignalDetailResponse)
async def get_signal(signal_id: str):
    """
    Get detailed signal information including full event history.

    Path Parameters:
    - signal_id: The signal ID

    Returns complete signal details with all associated events.
    """
    sb = get_supabase()

    try:
        # Fetch signal
        result = sb.table("canonical_signals").select("*").eq("id", signal_id).execute()
        if not result.data:
            raise HTTPException(404, f"Signal not found: {signal_id}")

        signal = result.data[0]

        # Fetch events
        events_result = sb.table("signal_events").select("*").eq("signal_id", signal_id).order("event_time").execute()

        events = [
            SignalEventResponse(
                signal_id=event["signal_id"],
                event_type=event["event_type"],
                price=event.get("price"),
                source=event.get("source", "UNKNOWN"),
                event_time=event["event_time"],
                metadata=event.get("metadata"),
            )
            for event in (events_result.data or [])
        ]

        return SignalDetailResponse(
            id=signal["id"],
            provider_id=signal["provider_id"],
            symbol=signal["symbol"],
            asset_class=signal.get("asset_class", "UNKNOWN"),
            direction=signal["direction"],
            entry_price=signal["entry_price"],
            sl=signal["sl"],
            tp1=signal["tp1"],
            tp2=signal.get("tp2"),
            tp3=signal.get("tp3"),
            status=signal["status"],
            entry_time=signal["entry_time"],
            activated_at=signal.get("activated_at"),
            closed_at=signal.get("closed_at"),
            close_reason=signal.get("close_reason"),
            exit_price=signal.get("exit_price"),
            rr_ratio=signal.get("rr_ratio"),
            risk_distance=signal.get("risk_distance"),
            strategy_name=signal.get("strategy_name"),
            external_signal_id=signal.get("external_signal_id"),
            validation_warnings=signal.get("validation_warnings"),
            validation_errors=signal.get("validation_errors"),
            events=events,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get signal {signal_id}: {e}")
        raise HTTPException(500, "Failed to retrieve signal")


@router.get("/signals/active/list", response_model=SignalListResult)
async def list_active_signals(
    provider_id: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    List all currently active/open signals.

    Active signals are those with status in: PENDING, ACTIVE, TP1_HIT, TP2_HIT, TP3_HIT

    Query Parameters:
    - provider_id: Filter by provider ID
    - symbol: Filter by trading symbol
    - limit: Number of results (1-500, default 50)
    - offset: Pagination offset (default 0)

    Returns paginated list of active signals.
    """
    sb = get_supabase()

    try:
        active_statuses = ["PENDING", "ACTIVE", "TP1_HIT", "TP2_HIT", "TP3_HIT"]

        # Build query
        query = sb.table("canonical_signals").select("*")

        if provider_id:
            query = query.eq("provider_id", provider_id)
        if symbol:
            query = query.eq("symbol", symbol)

        # Filter by active statuses
        for status in active_statuses:
            query = query.or_(f"status.eq.{status}")

        result = query.execute()
        all_data = result.data or []

        # Filter to active statuses in memory (since Supabase OR might be complex)
        filtered = [s for s in all_data if s["status"] in active_statuses]
        total = len(filtered)

        # Apply pagination
        paginated = filtered[offset : offset + limit]

        items = [
            SignalListResponse(
                id=sig["id"],
                symbol=sig["symbol"],
                direction=sig["direction"],
                entry_price=sig["entry_price"],
                sl=sig["sl"],
                tp1=sig["tp1"],
                status=sig["status"],
                entry_time=sig["entry_time"],
                rr_ratio=sig.get("rr_ratio"),
            )
            for sig in paginated
        ]

        return SignalListResult(
            total=total,
            limit=limit,
            offset=offset,
            items=items,
        )
    except Exception as e:
        logger.error(f"Failed to list active signals: {e}")
        raise HTTPException(500, "Failed to list active signals")


@router.delete("/signals/{signal_id}")
async def close_signal(signal_id: str):
    """
    Manually close a signal (mark as CLOSED).

    Path Parameters:
    - signal_id: The signal ID to close

    Returns updated signal status.
    """
    sb = get_supabase()

    try:
        # Verify signal exists
        result = sb.table("canonical_signals").select("*").eq("id", signal_id).execute()
        if not result.data:
            raise HTTPException(404, f"Signal not found: {signal_id}")

        signal = result.data[0]

        # Already closed?
        if signal["status"] == "CLOSED":
            raise HTTPException(400, "Signal is already closed")

        # Update signal
        closed_at = datetime.now(timezone.utc).isoformat()
        sb.table("canonical_signals").update({
            "status": "CLOSED",
            "closed_at": closed_at,
            "close_reason": "MANUAL_CLOSE",
        }).eq("id", signal_id).execute()

        # Create event
        sb.table("signal_events").insert({
            "signal_id": signal_id,
            "event_type": "MANUAL_CLOSE",
            "source": "API",
            "event_time": closed_at,
        }).execute()

        logger.info(f"Signal manually closed: {signal_id}")

        return {
            "status": "closed",
            "signal_id": signal_id,
            "closed_at": closed_at,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to close signal {signal_id}: {e}")
        raise HTTPException(500, "Failed to close signal")
