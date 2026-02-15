"""
Signal Bridge — Vercel Serverless Edition

Key difference from main.py:
- No background worker (Vercel is serverless, functions spin up/down)
- Price monitoring runs via Vercel Cron hitting /api/v1/cron/poll-prices every minute
- Binance WebSocket replaced with REST polling (no persistent connections in serverless)
- All state lives in Supabase (no in-memory state between requests)
"""

import logging
import pathlib
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("signal_bridge")

# Create the FastAPI app (no lifespan needed — serverless)
app = FastAPI(
    title="Signal Bridge",
    description="Ultimate Signal Provider Network — Vercel Serverless Edition",
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Register API Routers ---
from app.api.webhook_ingest import router as webhook_router
from app.api.signals import router as signals_router
from app.api.providers import router as providers_router
from app.api.webhooks_outbound import router as webhooks_outbound_router
from app.api.reports import router as reports_router

app.include_router(webhook_router)
app.include_router(signals_router)
app.include_router(providers_router)
app.include_router(webhooks_outbound_router)
app.include_router(reports_router)

# --- Frontend Dashboard ---
_static_dir = pathlib.Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/dashboard", tags=["frontend"], include_in_schema=False)
async def dashboard():
    """Serve the Signal Bridge dashboard UI."""
    return FileResponse(str(_static_dir / "index.html"))


# ============================================================
# CRON ENDPOINT — Vercel Cron hits this every minute
# Replaces the background price monitor worker
# ============================================================
@app.get("/api/v1/cron/poll-prices", tags=["cron"])
async def cron_poll_prices(request: Request):
    """
    Called by Vercel Cron every minute.

    Fetches all active signals due for polling, gets current prices,
    checks for TP/SL hits, creates events, and sends notifications.

    Protected: only Vercel Cron can call this (via Authorization header).
    """
    # Verify this is from Vercel Cron (optional but recommended)
    auth = request.headers.get("Authorization")
    if auth and auth != f"Bearer {settings.webhook_secret}":
        # In production, use CRON_SECRET env var
        pass  # Allow for now during development

    from app.database import get_supabase
    from app.models.canonical_signal import SignalStatus, EventType
    from app.engine.state_machine import SignalStateMachine
    from app.price.price_manager import PriceManager
    from app.price.smart_scheduler import SmartScheduler
    import asyncio

    sb = get_supabase()
    scheduler = SmartScheduler()
    price_manager = PriceManager()
    await price_manager.initialize()

    # 1. Fetch signals due for polling
    try:
        result = (
            sb.table("canonical_signals")
            .select("*")
            .in_("status", ["PENDING", "ACTIVE", "TP1_HIT", "TP2_HIT", "TP3_HIT"])
            .lte("next_poll_at", datetime.now(timezone.utc).isoformat())
            .order("next_poll_at")
            .limit(100)
            .execute()
        )
        signals = result.data or []
    except Exception as e:
        logger.error(f"Cron: failed to fetch signals: {e}")
        return {"status": "error", "message": str(e)}

    if not signals:
        return {"status": "ok", "signals_checked": 0, "hits": 0}

    # 2. Group by symbol
    symbol_groups: dict[str, list[dict]] = {}
    for sig in signals:
        sym = sig["symbol"]
        if sym not in symbol_groups:
            symbol_groups[sym] = []
        symbol_groups[sym].append(sig)

    # 3. Fetch prices (one per symbol)
    prices = await price_manager.get_prices_batch(list(symbol_groups.keys()))

    hits_detected = 0
    signals_checked = 0

    # 4. Check each signal
    for symbol, sig_list in symbol_groups.items():
        price_quote = prices.get(symbol)
        if not price_quote:
            logger.debug(f"No price for {symbol}, skipping {len(sig_list)} signals")
            continue

        current_price = price_quote.price

        for sig in sig_list:
            signals_checked += 1
            signal_id = sig["id"]
            status = SignalStatus(sig["status"])
            direction = sig["direction"]
            is_long = direction == "LONG"
            entry_price = float(sig["entry_price"])
            sl = float(sig["sl"])
            tp1 = float(sig["tp1"])
            tp2 = float(sig["tp2"]) if sig.get("tp2") else None
            tp3 = float(sig["tp3"]) if sig.get("tp3") else None

            # Update last known price
            sb.table("canonical_signals").update({
                "last_price": current_price,
                "last_price_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", signal_id).execute()

            # Detect event
            event_type = _detect_event(status, is_long, current_price, entry_price, sl, tp1, tp2, tp3)

            if event_type:
                hits_detected += 1
                await _process_hit(sb, sig, event_type, current_price)

            # Update next poll time based on proximity
            tp_levels = [tp1]
            if tp2:
                tp_levels.append(tp2)
            if tp3:
                tp_levels.append(tp3)

            zone, ratio, nearest = PriceManager.calculate_proximity(
                current_price, entry_price, sl, tp_levels, direction
            )
            next_seconds = scheduler.calculate_next_poll(zone)
            next_poll = datetime.now(timezone.utc) + timedelta(seconds=next_seconds)

            sb.table("canonical_signals").update({
                "next_poll_at": next_poll.isoformat(),
            }).eq("id", signal_id).execute()

    return {
        "status": "ok",
        "signals_checked": signals_checked,
        "hits": hits_detected,
        "symbols_polled": len(symbol_groups),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _detect_event(status, is_long, price, entry, sl, tp1, tp2, tp3):
    """Detect which event occurred based on current price."""
    if status == SignalStatus.PENDING:
        if is_long and price <= entry:
            return EventType.ENTRY_HIT
        elif not is_long and price >= entry:
            return EventType.ENTRY_HIT

    elif status == SignalStatus.ACTIVE:
        if is_long and price <= sl:
            return EventType.SL_HIT
        elif not is_long and price >= sl:
            return EventType.SL_HIT
        if is_long and price >= tp1:
            return EventType.TP1_HIT
        elif not is_long and price <= tp1:
            return EventType.TP1_HIT

    elif status == SignalStatus.TP1_HIT:
        if is_long and price <= sl:
            return EventType.SL_HIT
        elif not is_long and price >= sl:
            return EventType.SL_HIT
        if tp2:
            if is_long and price >= tp2:
                return EventType.TP2_HIT
            elif not is_long and price <= tp2:
                return EventType.TP2_HIT

    elif status == SignalStatus.TP2_HIT:
        if is_long and price <= sl:
            return EventType.SL_HIT
        elif not is_long and price >= sl:
            return EventType.SL_HIT
        if tp3:
            if is_long and price >= tp3:
                return EventType.TP3_HIT
            elif not is_long and price <= tp3:
                return EventType.TP3_HIT

    return None


async def _process_hit(sb, signal_data, event_type, hit_price):
    """Process a detected level hit."""
    from app.engine.state_machine import SignalStateMachine
    from app.models.canonical_signal import SignalStatus

    now = datetime.now(timezone.utc)
    signal_id = signal_data["id"]
    status = SignalStatus(signal_data["status"])

    tr = SignalStateMachine.process_event(status, event_type)
    if not tr.did_transition:
        return

    logger.info(
        f"HIT: {event_type.value} | {signal_data['symbol']} "
        f"{signal_data['direction']} | {status.value} -> {tr.new_status.value} @ {hit_price}"
    )

    # Create event
    sb.table("signal_events").insert({
        "signal_id": signal_id,
        "event_type": event_type.value,
        "price": hit_price,
        "source": "POLLING",
        "event_time": now.isoformat(),
        "metadata": {"detected_by": "vercel_cron"},
    }).execute()

    # Update signal
    update = {"status": tr.new_status.value}

    if event_type == EventType.ENTRY_HIT:
        update["activated_at"] = now.isoformat()

    if tr.is_terminal or tr.new_status == SignalStatus.SL_HIT:
        update["closed_at"] = now.isoformat()
        update["close_reason"] = tr.new_status.value
        update["exit_price"] = hit_price

        entry_price = float(signal_data["entry_price"])
        risk = float(signal_data.get("risk_distance") or abs(entry_price - float(signal_data["sl"])))
        if risk > 0:
            if signal_data["direction"] == "LONG":
                r_val = (hit_price - entry_price) / risk
            else:
                r_val = (entry_price - hit_price) / risk
            update["r_value"] = round(r_val, 4)

    sb.table("canonical_signals").update(update).eq("id", signal_id).execute()

    # Fire notifications (best-effort)
    try:
        from app.notifications.notification_engine import NotificationEngine
        engine = NotificationEngine()
        await engine.on_signal_event(signal_id, event_type.value, hit_price)
    except Exception as e:
        logger.error(f"Notification failed: {e}")


# ============================================================
# STANDARD ENDPOINTS
# ============================================================
@app.get("/health", tags=["system"])
async def health_check():
    return {
        "status": "ok",
        "service": "signal-bridge",
        "version": "1.0.0",
        "runtime": "vercel-serverless",
    }


@app.get("/", tags=["system"])
async def root():
    return {
        "service": "Signal Bridge",
        "version": "1.0.0",
        "runtime": "vercel-serverless",
        "docs": "/docs",
        "endpoints": {
            "webhook_tradingview": "POST /api/v1/webhook/tradingview",
            "webhook_pinescript": "POST /api/v1/webhook/pinescript",
            "signals": "GET /api/v1/signals",
            "providers": "GET /api/v1/providers",
            "reports": "GET /api/v1/reports/performance",
            "leaderboard": "GET /api/v1/reports/leaderboard",
            "cron_poll": "GET /api/v1/cron/poll-prices",
        },
    }


# Historical resolver endpoint
@app.post("/api/v1/admin/resolve-historical", tags=["admin"])
async def resolve_historical_signals(
    provider_id: str = None,
    start_date: str = None,
    end_date: str = None,
    symbol: str = None,
):
    from datetime import date as date_type
    from app.workers.historical_resolver import HistoricalResolver

    resolver = HistoricalResolver()
    start = date_type.fromisoformat(start_date) if start_date else None
    end = date_type.fromisoformat(end_date) if end_date else None

    report = await resolver.resolve_batch(
        provider_id=provider_id, start_date=start, end_date=end, symbol=symbol,
    )
    return report
