"""
Smart Adaptive Polling Scheduler.
Determines polling intervals based on price proximity to TP/SL levels.
Reduces API calls during low-volatility periods and increases during critical moments.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from collections import defaultdict

from app.models.canonical_signal import ProximityZone, CanonicalSignal
from app.config import settings

logger = logging.getLogger(__name__)

# Poll intervals in seconds based on proximity zone
POLL_INTERVALS = {
    ProximityZone.CLOSE: 5,      # Within 10% of TP/SL: poll frequently
    ProximityZone.MID: 15,       # Within 30% of TP/SL: moderate polling
    ProximityZone.FAR: 60,       # Far from levels: infrequent polling
}

# Minimum poll interval (don't exceed API limits)
MIN_POLL_INTERVAL = 1
# Maximum poll interval (even dormant trades need checks)
MAX_POLL_INTERVAL = 300


class SmartScheduler:
    """
    Intelligent polling scheduler that adapts intervals based on price action.

    Reduces unnecessary API calls by polling more frequently when prices are
    near take-profit or stop-loss levels, and less frequently when far away.

    This approach:
    - Reduces API costs during low-activity periods
    - Ensures quick detection of exits when price approaches targets
    - Scales to handle many concurrent signals
    - Respects rate limits across all data sources
    """

    def __init__(self, supabase_client=None):
        """
        Initialize the scheduler.

        Args:
            supabase_client: Optional Supabase client for persistence.
                If not provided, scheduling info is in-memory only.
        """
        self.supabase = supabase_client
        self._next_poll_cache: Dict[str, datetime] = {}
        self._last_zone_cache: Dict[str, ProximityZone] = {}
        self._signal_symbols: Dict[str, List[str]] = defaultdict(list)  # symbol -> [signal_ids]

    async def calculate_next_poll(self, proximity_zone: ProximityZone) -> int:
        """
        Calculate seconds until next poll based on proximity zone.

        Args:
            proximity_zone: ProximityZone (CLOSE, MID, or FAR)

        Returns:
            Seconds to wait before next poll
        """
        interval = POLL_INTERVALS.get(proximity_zone, MAX_POLL_INTERVAL)
        # Clamp between min and max
        interval = max(MIN_POLL_INTERVAL, min(interval, MAX_POLL_INTERVAL))
        return interval

    async def should_poll_signal(self, signal_id: str) -> bool:
        """
        Check if a signal is due for polling based on next_poll_at.

        Args:
            signal_id: The signal's unique identifier

        Returns:
            True if the signal should be polled now
        """
        if signal_id not in self._next_poll_cache:
            return True

        return datetime.now(timezone.utc) >= self._next_poll_cache[signal_id]

    def schedule_next_poll(
        self,
        signal_id: str,
        delay_seconds: int,
    ) -> datetime:
        """
        Schedule the next poll for a signal.

        Args:
            signal_id: The signal's unique identifier
            delay_seconds: Seconds until next poll

        Returns:
            The calculated next_poll_at datetime
        """
        next_poll_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        self._next_poll_cache[signal_id] = next_poll_at
        return next_poll_at

    async def get_signals_needing_poll(
        self,
        active_signals: List[Dict],
    ) -> Dict[str, List[Dict]]:
        """
        Filter active signals that need polling and group by symbol.

        This is more efficient than polling every signal individually.
        Groups signals by their trading symbol so we can fetch one price
        and apply it to multiple signals.

        Args:
            active_signals: List of signal dicts with keys:
                - id: signal ID
                - symbol: trading symbol (e.g., "BTCUSDT")
                - entry_price: entry price
                - stop_loss: SL price
                - tp1, tp2, tp3: Take-profit levels (if set)

        Returns:
            Dict mapping symbol -> list of signals needing poll
            Example:
                {
                    "BTCUSDT": [signal1, signal2, signal3],
                    "ETHUSDT": [signal4],
                }
        """
        signals_by_symbol: Dict[str, List[Dict]] = defaultdict(list)

        now = datetime.now(timezone.utc)

        for signal in active_signals:
            signal_id = signal.get("id")
            symbol = signal.get("symbol", "").upper()

            if not signal_id or not symbol:
                continue

            # Check if this signal is due for polling
            next_poll_at = self._next_poll_cache.get(signal_id)
            if next_poll_at and now < next_poll_at:
                continue  # Not due yet

            signals_by_symbol[symbol].append(signal)

        logger.debug(
            f"Signals needing poll: {sum(len(v) for v in signals_by_symbol.values())} "
            f"signals across {len(signals_by_symbol)} symbols"
        )

        return signals_by_symbol

    async def update_poll_schedule(
        self,
        signal_id: str,
        current_price: float,
        entry_price: float,
        stop_loss: float,
        tp_levels: List[Optional[float]],
        direction: str = "LONG",
    ) -> Dict:
        """
        Update polling schedule for a signal based on current proximity.

        Uses PriceManager.calculate_proximity internally to determine
        how close the price is to TP/SL, then adjusts polling frequency.

        Args:
            signal_id: The signal's unique identifier
            current_price: Current market price
            entry_price: Signal's entry price
            stop_loss: Stop-loss price
            tp_levels: List of take-profit prices (may contain None)
            direction: "LONG" or "SHORT" (for context)

        Returns:
            Dict with keys:
                - signal_id: str
                - proximity_zone: ProximityZone
                - next_poll_seconds: int
                - next_poll_at: datetime
                - nearest_level: str (e.g., "TP1", "SL")
        """
        from app.price.price_manager import PriceManager

        # Calculate proximity
        zone, distance_ratio, nearest_level = PriceManager.calculate_proximity(
            current_price=current_price,
            entry_price=entry_price,
            sl=stop_loss,
            tp_levels=tp_levels,
            direction=direction,
        )

        self._last_zone_cache[signal_id] = zone

        # Get polling interval for this zone
        next_poll_seconds = await self.calculate_next_poll(zone)

        # Schedule next poll
        next_poll_at = self.schedule_next_poll(signal_id, next_poll_seconds)

        result = {
            "signal_id": signal_id,
            "proximity_zone": zone,
            "next_poll_seconds": next_poll_seconds,
            "next_poll_at": next_poll_at,
            "nearest_level": nearest_level,
            "distance_ratio": distance_ratio,
        }

        logger.debug(f"Signal {signal_id}: {zone.value} zone, poll in {next_poll_seconds}s")

        # Persist to database if client available
        if self.supabase:
            await self._persist_schedule(signal_id, result)

        return result

    async def _persist_schedule(self, signal_id: str, schedule_data: Dict) -> None:
        """
        Persist polling schedule to database.

        This is optional - if Supabase is not configured, scheduling
        remains in-memory. This allows the system to survive restarts
        by recalculating on startup.

        Args:
            signal_id: Signal ID
            schedule_data: Dict with schedule info
        """
        try:
            # Update the signal's next_poll_at timestamp
            self.supabase.table("signals").update({
                "next_poll_at": schedule_data["next_poll_at"].isoformat(),
                "proximity_zone": schedule_data["proximity_zone"].value,
                "nearest_level": schedule_data["nearest_level"],
                "last_poll_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", signal_id).execute()

        except Exception as e:
            logger.error(f"Failed to persist schedule for signal {signal_id}: {e}")

    def group_by_symbol(self, signals: List[Dict]) -> Dict[str, List[Dict]]:
        """
        Group signals by their trading symbol.

        This is a utility method for batch processing - fetch one price,
        apply to multiple signals.

        Args:
            signals: List of signal dicts with 'symbol' key

        Returns:
            Dict mapping symbol -> list of signals
            Example:
                {
                    "BTCUSDT": [signal1, signal2],
                    "ETHUSDT": [signal3],
                }
        """
        grouped: Dict[str, List[Dict]] = defaultdict(list)

        for signal in signals:
            symbol = signal.get("symbol", "").upper()
            if symbol:
                grouped[symbol].append(signal)

        return grouped

    async def batch_update_schedules(
        self,
        price_quotes: Dict[str, "PriceQuote"],  # symbol -> PriceQuote
        signals_by_symbol: Dict[str, List[Dict]],  # symbol -> [signal_dicts]
    ) -> List[Dict]:
        """
        Efficiently update schedules for multiple signals using fetched prices.

        This is the main entry point for the polling loop:
        1. Get active signals
        2. Group by symbol
        3. Fetch prices for each symbol
        4. Call this method to update all schedules

        Args:
            price_quotes: Dict mapping symbol -> PriceQuote
            signals_by_symbol: Dict mapping symbol -> list of signals

        Returns:
            List of update results (one per signal)
        """
        results = []

        for symbol, signals in signals_by_symbol.items():
            quote = price_quotes.get(symbol)
            if not quote:
                logger.warning(f"No price quote for {symbol}, skipping {len(signals)} signals")
                continue

            for signal in signals:
                try:
                    result = await self.update_poll_schedule(
                        signal_id=signal.get("id"),
                        current_price=quote.price,
                        entry_price=signal.get("entry_price"),
                        stop_loss=signal.get("stop_loss"),
                        tp_levels=[
                            signal.get("tp1"),
                            signal.get("tp2"),
                            signal.get("tp3"),
                        ],
                        direction=signal.get("direction", "LONG"),
                    )
                    results.append(result)

                except Exception as e:
                    logger.error(f"Failed to update schedule for signal {signal.get('id')}: {e}")

        return results

    def get_scheduler_stats(self) -> Dict:
        """
        Get scheduler statistics for monitoring.

        Returns:
            Dict with keys:
                - total_scheduled_signals: int
                - signals_in_close_zone: int
                - signals_in_mid_zone: int
                - signals_in_far_zone: int
                - next_poll_times: list of (signal_id, next_poll_at)
        """
        close_count = sum(1 for z in self._last_zone_cache.values() if z == ProximityZone.CLOSE)
        mid_count = sum(1 for z in self._last_zone_cache.values() if z == ProximityZone.MID)
        far_count = sum(1 for z in self._last_zone_cache.values() if z == ProximityZone.FAR)

        next_polls = sorted(
            [(sig_id, next_time) for sig_id, next_time in self._next_poll_cache.items()],
            key=lambda x: x[1],
        )[:10]  # Top 10 upcoming

        return {
            "total_scheduled_signals": len(self._next_poll_cache),
            "signals_in_close_zone": close_count,
            "signals_in_mid_zone": mid_count,
            "signals_in_far_zone": far_count,
            "next_polls_upcoming": next_polls,
        }

    def reset_signal(self, signal_id: str) -> None:
        """
        Reset scheduling info for a signal (e.g., when signal is closed).

        Args:
            signal_id: Signal ID to reset
        """
        self._next_poll_cache.pop(signal_id, None)
        self._last_zone_cache.pop(signal_id, None)
        logger.debug(f"Reset schedule for signal {signal_id}")

    def reset_all(self) -> None:
        """Reset all scheduling info (e.g., for testing or restart)."""
        self._next_poll_cache.clear()
        self._last_zone_cache.clear()
        self._signal_symbols.clear()
        logger.info("Reset all scheduling info")
