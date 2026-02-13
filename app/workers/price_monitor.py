"""
Background Price Monitor Worker.

Continuously monitors prices for open signals and detects TP/SL hits.
Runs as an asyncio background task within the FastAPI application.

This is the heartbeat of the Signal Bridge — it checks prices,
detects level hits, triggers state transitions, and sends notifications.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.config import settings
from app.database import get_supabase
from app.models.canonical_signal import (
    SignalStatus, EventType, EventSource, ProximityZone,
    CanonicalSignal, SignalEvent, PriceQuote,
)
from app.engine.state_machine import SignalStateMachine
from app.engine.outcome_resolver import OutcomeResolver
from app.price.price_manager import PriceManager
from app.price.smart_scheduler import SmartScheduler

logger = logging.getLogger(__name__)


class PriceMonitorWorker:
    """
    Main background worker that:
    1. Fetches signals needing price checks (next_poll_at <= NOW)
    2. Groups them by symbol
    3. Fetches one price per symbol
    4. Checks each signal against TP/SL levels
    5. Creates events for any hits
    6. Updates signal states
    7. Triggers notifications
    8. Recalculates next poll times
    """

    def __init__(self, price_manager: PriceManager):
        self.price_manager = price_manager
        self.scheduler = SmartScheduler()
        self.outcome_resolver = OutcomeResolver()
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._stats = {
            "cycles": 0,
            "signals_checked": 0,
            "hits_detected": 0,
            "errors": 0,
            "last_cycle_at": None,
            "last_cycle_duration_ms": 0,
        }

    async def start(self) -> None:
        """Start the background monitoring loop."""
        if self.is_running:
            logger.warning("PriceMonitorWorker already running")
            return

        self.is_running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("PriceMonitorWorker started")

    async def stop(self) -> None:
        """Gracefully stop the monitoring loop."""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PriceMonitorWorker stopped")

    @property
    def stats(self) -> dict:
        return self._stats.copy()

    async def _run_loop(self) -> None:
        """Main monitoring loop. Runs until stopped."""
        logger.info("Price monitor loop started")

        while self.is_running:
            cycle_start = datetime.now(timezone.utc)
            try:
                await self._run_single_cycle()
                self._stats["cycles"] += 1
                self._stats["last_cycle_at"] = cycle_start.isoformat()
                self._stats["last_cycle_duration_ms"] = int(
                    (datetime.now(timezone.utc) - cycle_start).total_seconds() * 1000
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"Price monitor cycle error: {e}", exc_info=True)

            # Log heartbeat every 60 cycles (~5 minutes at 5s intervals)
            if self._stats["cycles"] % 60 == 0:
                logger.info(
                    f"Price monitor heartbeat: {self._stats['cycles']} cycles, "
                    f"{self._stats['signals_checked']} signals checked, "
                    f"{self._stats['hits_detected']} hits detected"
                )

            # Sleep before next cycle (adaptive based on how many signals are close)
            await asyncio.sleep(3)  # base 3-second loop

    async def _run_single_cycle(self) -> None:
        """Execute one monitoring cycle."""
        sb = get_supabase()

        # 1. Fetch signals needing poll
        signals_to_check = self._fetch_signals_due(sb)
        if not signals_to_check:
            return

        # 2. Group by symbol
        symbol_groups = self.scheduler.group_by_symbol(signals_to_check)

        # 3. Get unique symbols
        symbols = list(symbol_groups.keys())

        # 4. Batch fetch prices
        prices = await self.price_manager.get_prices_batch(symbols)

        # 5. Check each signal against current price
        for symbol, signal_list in symbol_groups.items():
            price_quote = prices.get(symbol)
            if not price_quote:
                logger.debug(f"No price available for {symbol}, skipping {len(signal_list)} signals")
                continue

            for signal_data in signal_list:
                self._stats["signals_checked"] += 1
                await self._check_signal(sb, signal_data, price_quote)

    def _fetch_signals_due(self, sb) -> list[dict]:
        """Query signals where next_poll_at <= NOW() and status is monitorable."""
        try:
            result = (
                sb.table("canonical_signals")
                .select("*")
                .in_("status", ["PENDING", "ACTIVE", "TP1_HIT", "TP2_HIT", "TP3_HIT"])
                .lte("next_poll_at", datetime.now(timezone.utc).isoformat())
                .order("next_poll_at")
                .limit(200)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Failed to fetch signals for polling: {e}")
            return []

    async def _check_signal(self, sb, signal_data: dict, price_quote: PriceQuote) -> None:
        """
        Check a single signal against the current price.
        Detect entry fills, TP hits, and SL hits.
        """
        current_price = price_quote.price
        signal_id = signal_data["id"]
        status = SignalStatus(signal_data["status"])
        direction = signal_data["direction"]
        entry_price = float(signal_data["entry_price"])
        sl = float(signal_data["sl"])
        tp1 = float(signal_data["tp1"])
        tp2 = float(signal_data["tp2"]) if signal_data.get("tp2") else None
        tp3 = float(signal_data["tp3"]) if signal_data.get("tp3") else None

        # Update last known price
        sb.table("canonical_signals").update({
            "last_price": current_price,
            "last_price_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", signal_id).execute()

        # Determine which event occurred (if any)
        event_type = self._detect_event(
            status, direction, current_price, entry_price, sl, tp1, tp2, tp3
        )

        if event_type:
            self._stats["hits_detected"] += 1
            await self._process_hit(sb, signal_id, signal_data, event_type, current_price)

        # Recalculate next poll time
        tp_levels = [tp1]
        if tp2:
            tp_levels.append(tp2)
        if tp3:
            tp_levels.append(tp3)

        zone, ratio, nearest = PriceManager.calculate_proximity(
            current_price, entry_price, sl, tp_levels, direction
        )
        next_poll_seconds = self.scheduler.calculate_next_poll(zone)
        next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=next_poll_seconds)

        sb.table("canonical_signals").update({
            "next_poll_at": next_poll_at.isoformat(),
        }).eq("id", signal_id).execute()

    def _detect_event(
        self,
        status: SignalStatus,
        direction: str,
        current_price: float,
        entry_price: float,
        sl: float,
        tp1: float,
        tp2: Optional[float],
        tp3: Optional[float],
    ) -> Optional[EventType]:
        """
        Detect which event occurred based on current price and signal state.

        Logic:
        - PENDING: check if entry price has been reached
        - ACTIVE: check SL and TP1
        - TP1_HIT: check SL and TP2
        - TP2_HIT: check SL and TP3
        """
        is_long = direction == "LONG"

        if status == SignalStatus.PENDING:
            # Check if entry price has been reached
            if is_long and current_price <= entry_price:
                return EventType.ENTRY_HIT
            elif not is_long and current_price >= entry_price:
                return EventType.ENTRY_HIT

        elif status == SignalStatus.ACTIVE:
            # Check SL first (priority over TP)
            if is_long and current_price <= sl:
                return EventType.SL_HIT
            elif not is_long and current_price >= sl:
                return EventType.SL_HIT
            # Check TP1
            if is_long and current_price >= tp1:
                return EventType.TP1_HIT
            elif not is_long and current_price <= tp1:
                return EventType.TP1_HIT

        elif status == SignalStatus.TP1_HIT:
            # Check SL
            if is_long and current_price <= sl:
                return EventType.SL_HIT
            elif not is_long and current_price >= sl:
                return EventType.SL_HIT
            # Check TP2
            if tp2:
                if is_long and current_price >= tp2:
                    return EventType.TP2_HIT
                elif not is_long and current_price <= tp2:
                    return EventType.TP2_HIT

        elif status == SignalStatus.TP2_HIT:
            # Check SL
            if is_long and current_price <= sl:
                return EventType.SL_HIT
            elif not is_long and current_price >= sl:
                return EventType.SL_HIT
            # Check TP3
            if tp3:
                if is_long and current_price >= tp3:
                    return EventType.TP3_HIT
                elif not is_long and current_price <= tp3:
                    return EventType.TP3_HIT

        return None

    async def _process_hit(
        self,
        sb,
        signal_id: str,
        signal_data: dict,
        event_type: EventType,
        hit_price: float,
    ) -> None:
        """Process a detected level hit: create event, transition state, notify."""
        now = datetime.now(timezone.utc)
        status = SignalStatus(signal_data["status"])

        # State transition
        tr = SignalStateMachine.process_event(status, event_type)
        new_status, did_transition = tr.new_status, tr.did_transition

        if not did_transition:
            logger.warning(
                f"Invalid transition: {status} + {event_type} for signal {signal_id}"
            )
            return

        logger.info(
            f"HIT DETECTED: {event_type.value} for {signal_data['symbol']} "
            f"signal {signal_id} @ {hit_price} | {status.value} → {new_status.value}"
        )

        # Create event record
        sb.table("signal_events").insert({
            "signal_id": signal_id,
            "event_type": event_type.value,
            "price": hit_price,
            "source": "POLLING",
            "event_time": now.isoformat(),
            "metadata": {
                "detected_by": "price_monitor_worker",
                "last_known_price": signal_data.get("last_price"),
            },
        }).execute()

        # Update signal status
        update_data = {"status": new_status.value}

        if event_type == EventType.ENTRY_HIT:
            update_data["activated_at"] = now.isoformat()

        # If signal is now closed (SL or TP3), resolve outcome
        if SignalStateMachine.is_terminal(new_status) or new_status == SignalStatus.SL_HIT:
            update_data["closed_at"] = now.isoformat()
            update_data["close_reason"] = new_status.value
            update_data["exit_price"] = hit_price

            # Calculate R-value
            entry_price = float(signal_data["entry_price"])
            risk_distance = float(signal_data.get("risk_distance") or abs(entry_price - float(signal_data["sl"])))
            if risk_distance > 0:
                if signal_data["direction"] == "LONG":
                    r_value = (hit_price - entry_price) / risk_distance
                else:
                    r_value = (entry_price - hit_price) / risk_distance
                update_data["r_value"] = round(r_value, 4)
                update_data["pnl_pct"] = round(
                    ((hit_price - entry_price) / entry_price) * 100
                    if signal_data["direction"] == "LONG"
                    else ((entry_price - hit_price) / entry_price) * 100,
                    4,
                )

        sb.table("canonical_signals").update(update_data).eq("id", signal_id).execute()

        # Track max favorable / adverse excursion
        if event_type != EventType.ENTRY_HIT:
            self._update_excursions(sb, signal_id, signal_data, hit_price)

        # Send notifications (fire and forget)
        try:
            from app.notifications.notification_engine import trigger_notification
            asyncio.create_task(
                trigger_notification(signal_id, event_type.value, hit_price)
            )
        except Exception as e:
            logger.error(f"Failed to trigger notification: {e}")

    def _update_excursions(self, sb, signal_id: str, signal_data: dict, price: float) -> None:
        """Track max favorable and adverse excursion."""
        current_mfe = signal_data.get("max_favorable")
        current_mae = signal_data.get("max_adverse")
        is_long = signal_data["direction"] == "LONG"

        update = {}

        if is_long:
            if current_mfe is None or price > float(current_mfe):
                update["max_favorable"] = price
            if current_mae is None or price < float(current_mae):
                update["max_adverse"] = price
        else:
            if current_mfe is None or price < float(current_mfe):
                update["max_favorable"] = price
            if current_mae is None or price > float(current_mae):
                update["max_adverse"] = price

        if update:
            sb.table("canonical_signals").update(update).eq("id", signal_id).execute()
