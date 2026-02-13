"""
Outcome Resolver - calculates win/loss, R values, and performance metrics.

This module handles post-trade analysis and performance calculation. It resolves
the outcome of closed signals, calculates R-values, maximum favorable/adverse
excursions, and aggregates statistics across multiple signals.

Key concepts:
- R-value: Measure of profit/loss in units of risk. Defined as:
  * For LONG: (exit_price - entry_price) / risk_distance
  * For SHORT: (entry_price - exit_price) / risk_distance
  * SL hits = -1.0 R by definition
  * Win = positive R, Loss = negative R
  * Partial = partial R based on which TP was hit

- Partial wins: Signal hit TP1 or TP2 before hitting SL
- Full wins: All TPs hit (or signal closed at TP3)
- Losses: SL hit without hitting any TP

- MFE (Max Favorable Excursion): Best price seen during trade
- MAE (Max Adverse Excursion): Worst price seen during trade
- Duration: Time from entry to close
"""

from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from statistics import mean, stdev, median
from dataclasses import dataclass, field

from app.models.canonical_signal import (
    CanonicalSignal,
    SignalEvent,
    SignalOutcome,
    SignalDirection,
    EventType,
    ProviderStats,
)


@dataclass
class TradeExcursion:
    """Track price excursions during a signal's lifetime."""

    max_favorable: float = 0.0  # Best price reached
    max_adverse: float = 0.0  # Worst price reached
    favorable_pips: float = 0.0  # MFE as distance from entry
    adverse_pips: float = 0.0  # MAE as distance from entry


class OutcomeResolver:
    """
    Calculates outcomes and performance metrics for closed signals.

    All methods are pure functions with no side effects. Calculations are
    based solely on signal data and price history.
    """

    @staticmethod
    def resolve_signal(
        signal: CanonicalSignal,
        events: List[SignalEvent],
    ) -> SignalOutcome:
        """
        Calculate the outcome of a closed signal.

        Determines the result (WIN, LOSS, PARTIAL, OPEN), exit price,
        R-value, and other performance metrics.

        Args:
            signal: The CanonicalSignal to resolve
            events: List of all SignalEvent objects for this signal

        Returns:
            SignalOutcome with complete analysis

        Raises:
            ValueError: If signal data is incomplete or invalid
        """
        if signal.entry_price is None or signal.sl is None:
            raise ValueError("Signal missing required entry_price or sl")

        # Find relevant events
        entry_hit_event = None
        tp_hits = []
        sl_hit_event = None
        close_event = None
        price_events = []

        for event in events:
            if event.event_type == EventType.ENTRY_HIT:
                entry_hit_event = event
            elif event.event_type == EventType.TP1_HIT:
                tp_hits.append((1, event))
            elif event.event_type == EventType.TP2_HIT:
                tp_hits.append((2, event))
            elif event.event_type == EventType.TP3_HIT:
                tp_hits.append((3, event))
            elif event.event_type == EventType.SL_HIT:
                sl_hit_event = event
            elif event.event_type == EventType.MANUAL_CLOSE:
                close_event = event
            elif event.event_type == EventType.PRICE_UPDATE:
                price_events.append(event)

        # Determine exit price and result
        exit_price: Optional[float] = None
        result: str = "OPEN"
        tp_hit_levels: List[int] = []

        # Check if SL was hit
        if sl_hit_event:
            exit_price = sl_hit_event.price or signal.sl
            # Check if any TP was hit before SL (partial win)
            if tp_hits:
                result = "PARTIAL"
                tp_hit_levels = [tp_level for tp_level, _ in tp_hits]
            else:
                result = "LOSS"

        # Check if TPs were hit
        elif tp_hits:
            result = "WIN"
            tp_hit_levels = [tp_level for tp_level, _ in tp_hits]
            # Exit at the highest TP hit
            last_tp_level, last_tp_event = tp_hits[-1]
            exit_price = last_tp_event.price or [signal.tp1, signal.tp2, signal.tp3][last_tp_level - 1]

        # Check if manually closed
        elif close_event:
            exit_price = close_event.price
            result = "CLOSED"

        # If still open, try to get last known price
        if exit_price is None and price_events:
            exit_price = price_events[-1].price

        # Calculate R-value
        r_value = OutcomeResolver.calculate_r_value(signal, exit_price) if exit_price else None

        # Calculate excursions
        excursion = OutcomeResolver._calculate_excursions(
            signal, price_events
        )

        # Calculate duration
        duration_hours = None
        if entry_hit_event and close_event:
            duration = close_event.event_time - entry_hit_event.event_time
            duration_hours = duration.total_seconds() / 3600
        elif entry_hit_event and (sl_hit_event or tp_hits):
            # Use earliest TP or SL hit for duration
            close_time = None
            if sl_hit_event:
                close_time = sl_hit_event.event_time
            if tp_hits:
                tp_close_time = tp_hits[0][1].event_time
                if close_time is None or tp_close_time < close_time:
                    close_time = tp_close_time
            if close_time:
                duration = close_time - entry_hit_event.event_time
                duration_hours = duration.total_seconds() / 3600

        return SignalOutcome(
            signal_id=signal.id,
            result=result,
            entry_price=signal.entry_price,
            exit_price=exit_price,
            r_value=r_value,
            tp_hits=tp_hit_levels,
            max_favorable_excursion=excursion.favorable_pips,
            max_adverse_excursion=excursion.adverse_pips,
            duration_hours=duration_hours,
            closed_at=close_event.event_time if close_event else (sl_hit_event.event_time if sl_hit_event else None),
        )

    @staticmethod
    def calculate_r_value(signal: CanonicalSignal, exit_price: Optional[float]) -> Optional[float]:
        """
        Calculate R-value for a signal.

        R-value represents profit/loss in units of risk:
        - R = (profit/loss) / risk_distance
        - For LONG: (exit - entry) / |entry - sl|
        - For SHORT: (entry - exit) / |entry - sl|

        By definition:
        - SL hit = -1.0 R
        - Exit at TP1 = TP1_distance / risk_distance
        - Profitable closes above entry = positive R
        - Loss before TP1 = between 0 and -1.0 R

        Args:
            signal: The CanonicalSignal
            exit_price: Price at which position was closed/exited

        Returns:
            R-value or None if unable to calculate
        """
        if exit_price is None or signal.risk_distance is None or signal.risk_distance == 0:
            return None

        if signal.direction == SignalDirection.LONG:
            profit_loss = exit_price - signal.entry_price
        else:  # SHORT
            profit_loss = signal.entry_price - exit_price

        r_value = profit_loss / signal.risk_distance
        return round(r_value, 4)

    @staticmethod
    def _calculate_excursions(
        signal: CanonicalSignal,
        price_events: List[SignalEvent],
    ) -> TradeExcursion:
        """
        Calculate maximum favorable and adverse excursions.

        MFE: Best price movement in the profitable direction
        MAE: Worst price movement in the adverse direction

        Args:
            signal: The CanonicalSignal
            price_events: List of price update events

        Returns:
            TradeExcursion with calculated values
        """
        excursion = TradeExcursion()

        if not price_events:
            return excursion

        prices = [event.price for event in price_events if event.price is not None]
        if not prices:
            return excursion

        if signal.direction == SignalDirection.LONG:
            # Favorable = highest price above entry
            highest = max(prices)
            excursion.max_favorable = highest
            excursion.favorable_pips = highest - signal.entry_price

            # Adverse = lowest price below entry
            lowest = min(prices)
            excursion.max_adverse = lowest
            excursion.adverse_pips = signal.entry_price - lowest

        else:  # SHORT
            # Favorable = lowest price below entry
            lowest = min(prices)
            excursion.max_favorable = lowest
            excursion.favorable_pips = signal.entry_price - lowest

            # Adverse = highest price above entry
            highest = max(prices)
            excursion.max_adverse = highest
            excursion.adverse_pips = highest - signal.entry_price

        return excursion

    @staticmethod
    def aggregate_provider_stats(
        outcomes: List[SignalOutcome],
        provider_id: str,
    ) -> ProviderStats:
        """
        Aggregate multiple signal outcomes into provider-level statistics.

        Calculates win rate, profit factor, average R-value, and other
        summary statistics from closed signals.

        Args:
            outcomes: List of SignalOutcome objects (closed signals)
            provider_id: ID of the provider

        Returns:
            ProviderStats with aggregated metrics
        """
        if not outcomes:
            return ProviderStats(provider_id=provider_id)

        # Categorize outcomes
        wins = [o for o in outcomes if o.result == "WIN"]
        losses = [o for o in outcomes if o.result == "LOSS"]
        partials = [o for o in outcomes if o.result == "PARTIAL"]
        closed = [o for o in outcomes if o.result == "CLOSED"]

        # R-values
        r_values = [o.r_value for o in outcomes if o.r_value is not None]
        win_r_values = [o.r_value for o in wins if o.r_value is not None]
        loss_r_values = [o.r_value for o in losses if o.r_value is not None]

        # Calculate statistics
        total_signals = len(outcomes)
        win_count = len(wins)
        loss_count = len(losses)
        partial_count = len(partials)

        win_rate = (win_count / total_signals * 100) if total_signals > 0 else 0.0
        total_r = sum(r_values) if r_values else 0.0
        avg_r = (total_r / len(r_values)) if r_values else 0.0

        best_r = max(r_values) if r_values else 0.0
        worst_r = min(r_values) if r_values else 0.0

        # TP hit rates
        tp1_hits = len([o for o in outcomes if 1 in o.tp_hits])
        tp2_hits = len([o for o in outcomes if 2 in o.tp_hits])
        tp3_hits = len([o for o in outcomes if 3 in o.tp_hits])

        tp1_rate = (tp1_hits / total_signals * 100) if total_signals > 0 else 0.0
        tp2_rate = (tp2_hits / total_signals * 100) if total_signals > 0 else 0.0
        tp3_rate = (tp3_hits / total_signals * 100) if total_signals > 0 else 0.0

        # Profit factor: sum of wins / abs(sum of losses)
        win_r_sum = sum(win_r_values) if win_r_values else 0.0
        loss_r_sum = abs(sum(loss_r_values)) if loss_r_values else 0.01
        profit_factor = win_r_sum / loss_r_sum if loss_r_sum > 0 else (1.0 if win_r_sum > 0 else 0.0)

        # Expectancy: average R-value (mathematical edge per trade)
        expectancy = avg_r

        # Average duration
        durations = [o.duration_hours for o in outcomes if o.duration_hours is not None]
        avg_duration_hours = mean(durations) if durations else 0.0

        return ProviderStats(
            provider_id=provider_id,
            total_signals=total_signals,
            open_signals=0,  # Not included in this batch
            wins=win_count,
            losses=loss_count,
            partials=partial_count,
            win_rate=round(win_rate, 2),
            tp1_hit_rate=round(tp1_rate, 2),
            tp2_hit_rate=round(tp2_rate, 2),
            tp3_hit_rate=round(tp3_rate, 2),
            avg_r=round(avg_r, 4),
            total_r=round(total_r, 4),
            best_r=round(best_r, 4),
            worst_r=round(worst_r, 4),
            expectancy=round(expectancy, 4),
            avg_duration_hours=round(avg_duration_hours, 2),
            calculated_at=datetime.utcnow(),
        )

    @staticmethod
    def build_equity_curve(
        outcomes: List[SignalOutcome],
        starting_equity: float = 10000.0,
    ) -> List[Dict]:
        """
        Build cumulative equity curve from signal outcomes.

        Simulates account growth assuming each signal represents one position
        sized at a fixed risk amount. Uses R-values to calculate equity at
        each point.

        Args:
            outcomes: List of SignalOutcome objects (must be sorted by date)
            starting_equity: Starting account size in currency

        Returns:
            List of dictionaries with {date, cumulative_r, equity}

        Example:
            Starting equity: $10,000
            Position size: $100 per trade (1% risk)
            Signal 1: +2.5R → Equity = $10,250
            Signal 2: -1.0R → Equity = $10,150
            etc.
        """
        if not outcomes:
            return []

        curve = []
        cumulative_r = 0.0
        risk_per_trade = starting_equity * 0.01  # 1% risk per trade

        for outcome in outcomes:
            if outcome.r_value is not None:
                cumulative_r += outcome.r_value

            equity = starting_equity + (cumulative_r * risk_per_trade)

            curve.append({
                "date": outcome.closed_at.isoformat() if outcome.closed_at else None,
                "cumulative_r": round(cumulative_r, 4),
                "equity": round(equity, 2),
                "r_value": outcome.r_value,
                "signal_result": outcome.result,
            })

        return curve

    @staticmethod
    def calculate_drawdown_metrics(
        outcomes: List[SignalOutcome],
        starting_equity: float = 10000.0,
    ) -> Dict[str, float]:
        """
        Calculate maximum drawdown and other drawdown metrics.

        Args:
            outcomes: List of SignalOutcome objects
            starting_equity: Starting account size

        Returns:
            Dictionary with drawdown metrics
        """
        curve = OutcomeResolver.build_equity_curve(outcomes, starting_equity)

        if not curve:
            return {
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
                "recovery_trades": 0,
                "current_drawdown": 0.0,
            }

        equities = [point["equity"] for point in curve]
        peak = starting_equity
        max_drawdown = 0.0
        max_drawdown_pct = 0.0

        for equity in equities:
            if equity > peak:
                peak = equity
            drawdown = peak - equity
            drawdown_pct = (drawdown / peak * 100) if peak > 0 else 0.0

            if drawdown > max_drawdown:
                max_drawdown = drawdown
                max_drawdown_pct = drawdown_pct

        # Current drawdown
        current_equity = equities[-1] if equities else starting_equity
        current_drawdown = peak - current_equity

        return {
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "current_drawdown": round(current_drawdown, 2),
            "peak_equity": round(peak, 2),
        }

    @staticmethod
    def calculate_monthly_breakdown(outcomes: List[SignalOutcome]) -> Dict[str, ProviderStats]:
        """
        Break down signal outcomes by month.

        Args:
            outcomes: List of SignalOutcome objects

        Returns:
            Dictionary mapping month (YYYY-MM) to ProviderStats
        """
        monthly_groups: Dict[str, List[SignalOutcome]] = {}

        for outcome in outcomes:
            if outcome.closed_at:
                month_key = outcome.closed_at.strftime("%Y-%m")
                if month_key not in monthly_groups:
                    monthly_groups[month_key] = []
                monthly_groups[month_key].append(outcome)

        result = {}
        for month, month_outcomes in sorted(monthly_groups.items()):
            stats = OutcomeResolver.aggregate_provider_stats(month_outcomes, "")
            result[month] = stats

        return result

    @staticmethod
    def get_consistency_score(r_values: List[float]) -> float:
        """
        Calculate a consistency score (0-100) based on R-value distribution.

        Higher score = more consistent performance with less variance.
        Uses coefficient of variation: lower CV = more consistent.

        Args:
            r_values: List of R-values from closed signals

        Returns:
            Consistency score from 0-100
        """
        if not r_values or len(r_values) < 2:
            return 0.0

        avg = mean(r_values)
        if avg == 0:
            return 0.0

        std_dev = stdev(r_values)
        cv = std_dev / abs(avg)  # Coefficient of variation

        # Convert CV to 0-100 score
        # CV < 0.5 = high consistency (>75), CV > 2.0 = low consistency (<25)
        score = max(0, min(100, 100 - (cv * 25)))
        return round(score, 1)

    @staticmethod
    def compare_periods(
        outcomes_period1: List[SignalOutcome],
        outcomes_period2: List[SignalOutcome],
    ) -> Dict[str, any]:
        """
        Compare performance between two periods.

        Args:
            outcomes_period1: Outcomes from first period
            outcomes_period2: Outcomes from second period

        Returns:
            Dictionary with comparative metrics
        """
        stats1 = OutcomeResolver.aggregate_provider_stats(outcomes_period1, "P1")
        stats2 = OutcomeResolver.aggregate_provider_stats(outcomes_period2, "P2")

        return {
            "period1": {
                "total_signals": stats1.total_signals,
                "win_rate": stats1.win_rate,
                "avg_r": stats1.avg_r,
                "total_r": stats1.total_r,
            },
            "period2": {
                "total_signals": stats2.total_signals,
                "win_rate": stats2.win_rate,
                "avg_r": stats2.avg_r,
                "total_r": stats2.total_r,
            },
            "changes": {
                "signal_count_delta": stats2.total_signals - stats1.total_signals,
                "win_rate_delta": round(stats2.win_rate - stats1.win_rate, 2),
                "avg_r_delta": round(stats2.avg_r - stats1.avg_r, 4),
            },
        }
