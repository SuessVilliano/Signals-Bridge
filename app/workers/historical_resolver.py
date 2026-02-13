"""
Historical Signal Resolver.

Backtests past signals against historical price data to verify
performance claims. This is how you prove 3 years of signals are real.

Usage:
    resolver = HistoricalResolver()
    report = await resolver.resolve_batch(signals, start_date, end_date)
"""

import asyncio
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Optional

from app.database import get_supabase
from app.models.canonical_signal import (
    SignalStatus, EventType, EventSource, AssetClass,
    SignalOutcome, ProviderStats,
)
from app.engine.state_machine import SignalStateMachine
from app.engine.outcome_resolver import OutcomeResolver
from app.price.rest_poller import RESTPoller

logger = logging.getLogger(__name__)


class HistoricalCandle:
    """A single OHLCV candle."""
    def __init__(self, timestamp: datetime, open: float, high: float, low: float, close: float, volume: float = 0):
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class HistoricalResolver:
    """
    Resolves historical signals by replaying price action against
    signal levels (entry, TP1-3, SL).

    For each historical signal:
    1. Fetch OHLCV candles covering the signal's lifetime
    2. Walk through candles chronologically
    3. Detect entry, TP, and SL hits using high/low prices
    4. Record events and calculate outcomes
    """

    def __init__(self):
        self.outcome_resolver = OutcomeResolver()
        self.poller = RESTPoller()
        self._stats = {
            "signals_processed": 0,
            "signals_resolved": 0,
            "signals_failed": 0,
            "candles_fetched": 0,
        }

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    async def resolve_signal(
        self,
        signal_data: dict,
        candles: list[HistoricalCandle],
    ) -> Optional[dict]:
        """
        Resolve a single historical signal against candle data.

        Returns dict with:
        - result: WIN / LOSS / PARTIAL / NO_FILL
        - events: list of detected events
        - r_value: realized R
        - exit_price: price at close
        - duration_candles: how many candles the trade lasted
        """
        direction = signal_data["direction"]
        entry_price = float(signal_data["entry_price"])
        sl = float(signal_data["sl"])
        tp1 = float(signal_data["tp1"])
        tp2 = float(signal_data.get("tp2") or 0) or None
        tp3 = float(signal_data.get("tp3") or 0) or None
        is_long = direction == "LONG"

        risk_distance = abs(entry_price - sl)
        if risk_distance == 0:
            return {"result": "INVALID", "reason": "Zero risk distance"}

        status = SignalStatus.PENDING
        events = []
        entry_candle_idx = None
        exit_price = None
        max_favorable = None
        max_adverse = None

        for i, candle in enumerate(candles):
            # --- PENDING: Look for entry fill ---
            if status == SignalStatus.PENDING:
                entry_filled = False
                if is_long:
                    # For a LONG entry: price needs to come down to entry level
                    if candle.low <= entry_price:
                        entry_filled = True
                else:
                    # For a SHORT entry: price needs to come up to entry level
                    if candle.high >= entry_price:
                        entry_filled = True

                if entry_filled:
                    status = SignalStatus.ACTIVE
                    entry_candle_idx = i
                    events.append({
                        "event_type": "ENTRY_HIT",
                        "price": entry_price,
                        "candle_time": candle.timestamp.isoformat(),
                        "candle_idx": i,
                    })
                    max_favorable = entry_price
                    max_adverse = entry_price
                continue

            # --- ACTIVE or TP_HIT: Check SL and TP levels ---
            if status in (SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT):
                # Track excursions
                if is_long:
                    max_favorable = max(max_favorable, candle.high) if max_favorable else candle.high
                    max_adverse = min(max_adverse, candle.low) if max_adverse else candle.low
                else:
                    max_favorable = min(max_favorable, candle.low) if max_favorable else candle.low
                    max_adverse = max(max_adverse, candle.high) if max_adverse else candle.high

                # Check SL hit (priority — on the same candle, SL wins)
                sl_hit = False
                if is_long and candle.low <= sl:
                    sl_hit = True
                elif not is_long and candle.high >= sl:
                    sl_hit = True

                # Check TP levels
                tp_hit = None
                if status == SignalStatus.ACTIVE:
                    if is_long and candle.high >= tp1:
                        tp_hit = ("TP1_HIT", tp1)
                    elif not is_long and candle.low <= tp1:
                        tp_hit = ("TP1_HIT", tp1)
                elif status == SignalStatus.TP1_HIT and tp2:
                    if is_long and candle.high >= tp2:
                        tp_hit = ("TP2_HIT", tp2)
                    elif not is_long and candle.low <= tp2:
                        tp_hit = ("TP2_HIT", tp2)
                elif status == SignalStatus.TP2_HIT and tp3:
                    if is_long and candle.high >= tp3:
                        tp_hit = ("TP3_HIT", tp3)
                    elif not is_long and candle.low <= tp3:
                        tp_hit = ("TP3_HIT", tp3)

                # Resolve conflicts: if both SL and TP hit on same candle
                if sl_hit and tp_hit:
                    # Assume SL hit if price went through it (conservative)
                    # In reality, order depends on candle internals we can't see
                    # Conservative: SL wins unless TP was closer to open
                    if is_long:
                        sl_distance = abs(candle.open - sl)
                        tp_distance = abs(tp_hit[1] - candle.open)
                    else:
                        sl_distance = abs(sl - candle.open)
                        tp_distance = abs(candle.open - tp_hit[1])

                    if tp_distance < sl_distance:
                        # TP was likely hit first
                        sl_hit = False
                    else:
                        tp_hit = None

                if sl_hit:
                    events.append({
                        "event_type": "SL_HIT",
                        "price": sl,
                        "candle_time": candle.timestamp.isoformat(),
                        "candle_idx": i,
                    })
                    exit_price = sl
                    # Determine result based on whether any TP was hit before
                    if status in (SignalStatus.TP1_HIT, SignalStatus.TP2_HIT):
                        result = "PARTIAL"
                    else:
                        result = "LOSS"
                    break

                if tp_hit:
                    event_name, tp_price = tp_hit
                    events.append({
                        "event_type": event_name,
                        "price": tp_price,
                        "candle_time": candle.timestamp.isoformat(),
                        "candle_idx": i,
                    })

                    new_status = SignalStateMachine.EVENT_TO_STATUS.get(
                        EventType(event_name), status
                    )
                    status = new_status

                    # If TP3 hit (or highest TP hit with no more TPs), trade is done
                    if event_name == "TP3_HIT":
                        exit_price = tp_price
                        result = "WIN"
                        break
                    elif event_name == "TP2_HIT" and tp3 is None:
                        exit_price = tp_price
                        result = "WIN"
                        break
                    elif event_name == "TP1_HIT" and tp2 is None:
                        exit_price = tp_price
                        result = "WIN"
                        break

        else:
            # Ran out of candles — signal didn't fully resolve
            if status == SignalStatus.PENDING:
                return {
                    "result": "NO_FILL",
                    "events": events,
                    "reason": "Entry price never reached within candle range",
                }
            else:
                # Still open — use last candle close as current state
                exit_price = candles[-1].close if candles else None
                result = "OPEN"

        # Calculate R-value
        r_value = None
        if exit_price and risk_distance > 0:
            if is_long:
                r_value = round((exit_price - entry_price) / risk_distance, 4)
            else:
                r_value = round((entry_price - exit_price) / risk_distance, 4)

        # Calculate duration
        duration_candles = None
        if entry_candle_idx is not None and candles:
            last_event_idx = events[-1].get("candle_idx", len(candles) - 1) if events else len(candles) - 1
            duration_candles = last_event_idx - entry_candle_idx

        tp_hits = [int(e["event_type"].replace("TP", "").replace("_HIT", ""))
                    for e in events if "TP" in e["event_type"]]

        return {
            "result": result,
            "events": events,
            "r_value": r_value,
            "exit_price": exit_price,
            "entry_price": entry_price,
            "risk_distance": risk_distance,
            "tp_hits": tp_hits,
            "duration_candles": duration_candles,
            "max_favorable": max_favorable,
            "max_adverse": max_adverse,
        }

    async def resolve_batch(
        self,
        provider_id: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        symbol: Optional[str] = None,
    ) -> dict:
        """
        Resolve multiple historical signals from the database.

        Steps:
        1. Fetch unresolved signals from DB
        2. For each symbol, fetch historical candles
        3. Resolve each signal
        4. Write results back to DB
        5. Return summary report
        """
        sb = get_supabase()

        # Build query
        query = sb.table("canonical_signals").select("*")
        if provider_id:
            query = query.eq("provider_id", provider_id)
        if start_date:
            query = query.gte("entry_time", start_date.isoformat())
        if end_date:
            query = query.lte("entry_time", end_date.isoformat())
        if symbol:
            query = query.eq("symbol", symbol)
        # Only resolve signals that haven't been resolved yet
        query = query.in_("status", ["PENDING", "ACTIVE"])

        result = query.order("entry_time").execute()
        signals = result.data or []

        if not signals:
            return {
                "status": "no_signals",
                "message": "No unresolved signals found matching criteria",
            }

        logger.info(f"Resolving {len(signals)} historical signals")

        # Group by symbol
        symbol_groups: dict[str, list[dict]] = {}
        for sig in signals:
            sym = sig["symbol"]
            if sym not in symbol_groups:
                symbol_groups[sym] = []
            symbol_groups[sym].append(sig)

        # Fetch historical candles per symbol
        results = []
        total_resolved = 0
        total_failed = 0

        for sym, sig_list in symbol_groups.items():
            # Determine date range for candles
            earliest = min(s["entry_time"] for s in sig_list)
            latest = max(s["entry_time"] for s in sig_list)
            # Add buffer: fetch 30 days after latest signal for resolution
            candle_end = datetime.fromisoformat(latest.replace("Z", "+00:00")) + timedelta(days=30)
            candle_start = datetime.fromisoformat(earliest.replace("Z", "+00:00")) - timedelta(hours=1)

            # Fetch candles
            try:
                asset_class = sig_list[0].get("asset_class", "OTHER")
                raw_candles = await self.poller.get_historical_candles(
                    symbol=sym,
                    interval="1h",  # hourly candles for backtesting
                    start_date=candle_start.strftime("%Y-%m-%d"),
                    end_date=candle_end.strftime("%Y-%m-%d"),
                )
                candles = [
                    HistoricalCandle(
                        timestamp=c["timestamp"] if isinstance(c["timestamp"], datetime)
                                  else datetime.fromisoformat(str(c["timestamp"])),
                        open=float(c["open"]),
                        high=float(c["high"]),
                        low=float(c["low"]),
                        close=float(c["close"]),
                        volume=float(c.get("volume", 0)),
                    )
                    for c in (raw_candles or [])
                ]
                self._stats["candles_fetched"] += len(candles)
            except Exception as e:
                logger.error(f"Failed to fetch candles for {sym}: {e}")
                total_failed += len(sig_list)
                continue

            if not candles:
                logger.warning(f"No candle data available for {sym}")
                total_failed += len(sig_list)
                continue

            # Resolve each signal
            for sig in sig_list:
                self._stats["signals_processed"] += 1
                try:
                    # Filter candles to start from signal entry time
                    sig_time = datetime.fromisoformat(
                        sig["entry_time"].replace("Z", "+00:00")
                    )
                    relevant_candles = [c for c in candles if c.timestamp >= sig_time]

                    if not relevant_candles:
                        total_failed += 1
                        continue

                    outcome = await self.resolve_signal(sig, relevant_candles)

                    if outcome and outcome["result"] != "NO_FILL":
                        # Write results back to DB
                        await self._write_outcome(sb, sig["id"], outcome)
                        total_resolved += 1
                        self._stats["signals_resolved"] += 1
                        results.append({
                            "signal_id": sig["id"],
                            "symbol": sym,
                            **outcome,
                        })
                    else:
                        total_failed += 1
                        self._stats["signals_failed"] += 1

                except Exception as e:
                    logger.error(f"Failed to resolve signal {sig['id']}: {e}")
                    total_failed += 1
                    self._stats["signals_failed"] += 1

                # Rate limiting — don't overwhelm the DB
                await asyncio.sleep(0.1)

        # Aggregate stats
        wins = sum(1 for r in results if r["result"] == "WIN")
        losses = sum(1 for r in results if r["result"] == "LOSS")
        partials = sum(1 for r in results if r["result"] == "PARTIAL")
        r_values = [r["r_value"] for r in results if r.get("r_value") is not None]

        return {
            "status": "completed",
            "total_signals": len(signals),
            "resolved": total_resolved,
            "failed": total_failed,
            "wins": wins,
            "losses": losses,
            "partials": partials,
            "win_rate": round(wins / max(wins + losses, 1), 4),
            "avg_r": round(sum(r_values) / max(len(r_values), 1), 4) if r_values else 0,
            "total_r": round(sum(r_values), 4) if r_values else 0,
            "best_r": round(max(r_values), 4) if r_values else 0,
            "worst_r": round(min(r_values), 4) if r_values else 0,
            "results": results[:100],  # cap for response size
        }

    async def _write_outcome(self, sb, signal_id: str, outcome: dict) -> None:
        """Write resolved outcome back to the database."""
        status_map = {
            "WIN": "CLOSED",
            "LOSS": "SL_HIT",
            "PARTIAL": "CLOSED",
            "OPEN": None,  # don't change
        }
        new_status = status_map.get(outcome["result"])

        update_data = {
            "exit_price": outcome.get("exit_price"),
            "r_value": outcome.get("r_value"),
            "max_favorable": outcome.get("max_favorable"),
            "max_adverse": outcome.get("max_adverse"),
        }
        if new_status:
            update_data["status"] = new_status
            update_data["close_reason"] = f"HISTORICAL_{outcome['result']}"
            update_data["closed_at"] = datetime.now(timezone.utc).isoformat()

        sb.table("canonical_signals").update(update_data).eq("id", signal_id).execute()

        # Write events
        for event in outcome.get("events", []):
            sb.table("signal_events").insert({
                "signal_id": signal_id,
                "event_type": event["event_type"],
                "price": event["price"],
                "source": "HISTORICAL",
                "event_time": event.get("candle_time", datetime.now(timezone.utc).isoformat()),
                "metadata": {"candle_idx": event.get("candle_idx")},
            }).execute()
