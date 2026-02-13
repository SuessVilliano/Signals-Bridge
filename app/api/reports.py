"""
Performance Reports and Analytics Endpoints.
Provides trading performance metrics, equity curves, and leaderboards.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.database import get_supabase
from app.engine.outcome_resolver import OutcomeResolver

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["reports"])


# ============================================================================
# Response Models
# ============================================================================

class PerformanceMetrics(BaseModel):
    """Performance metrics for a provider."""
    total_trades: int
    closed_trades: int
    open_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    loss_rate: float
    avg_rr_ratio: float
    total_r_value: float
    largest_win_rr: Optional[float]
    largest_loss_rr: float = 1.0
    consecutive_wins: int
    consecutive_losses: int
    profit_factor: float
    expectancy_per_trade: float


class EquityCurvePoint(BaseModel):
    """Single point on equity curve."""
    timestamp: str
    cumulative_r_value: float
    trade_count: int
    win_count: int
    loss_count: int


class EquityCurveResponse(BaseModel):
    """Equity curve data."""
    provider_id: str
    provider_name: str
    points: List[EquityCurvePoint]
    start_date: Optional[str]
    end_date: Optional[str]


class ProviderLeaderboardEntry(BaseModel):
    """Single entry in provider leaderboard."""
    rank: int
    provider_id: str
    provider_name: str
    total_trades: int
    win_rate: float
    total_r_value: float
    expectancy: float
    sharpe_ratio: Optional[float]


class LeaderboardResponse(BaseModel):
    """Provider leaderboard."""
    period: str
    entries: List[ProviderLeaderboardEntry]


class PerformanceReportResponse(BaseModel):
    """Complete performance report."""
    provider_id: str
    provider_name: str
    period: str
    start_date: Optional[str]
    end_date: Optional[str]
    metrics: PerformanceMetrics


# ============================================================================
# Helper Functions
# ============================================================================

def get_provider_closed_signals(sb, provider_id: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> list:
    """Fetch closed signals for a provider."""
    try:
        query = sb.table("canonical_signals").select("*").eq("provider_id", provider_id)

        # Filter to closed signals
        query = query.in_("status", ["CLOSED", "TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT"])

        if start_date:
            query = query.gte("closed_at", start_date)
        if end_date:
            query = query.lte("closed_at", end_date)

        result = query.execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Failed to fetch closed signals for provider {provider_id}: {e}")
        return []


def calculate_consecutive_streaks(signals: list) -> tuple:
    """Calculate consecutive wins and losses from a list of signals."""
    if not signals:
        return 0, 0

    # Sort by closed_at
    sorted_signals = sorted(signals, key=lambda s: s.get("closed_at", ""))

    consecutive_wins = 0
    consecutive_losses = 0
    current_win_streak = 0
    current_loss_streak = 0

    for signal in sorted_signals:
        status = signal.get("status")
        if status == "TP3_HIT":
            current_win_streak += 1
            current_loss_streak = 0
            consecutive_wins = max(consecutive_wins, current_win_streak)
        elif status == "SL_HIT":
            current_loss_streak += 1
            current_win_streak = 0
            consecutive_losses = max(consecutive_losses, current_loss_streak)

    return consecutive_wins, consecutive_losses


def calculate_performance_metrics(signals: list) -> dict:
    """Calculate performance metrics from a list of closed signals."""
    if not signals:
        return {
            "total_trades": 0,
            "closed_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "loss_rate": 0.0,
            "avg_rr_ratio": 0.0,
            "total_r_value": 0.0,
            "largest_win_rr": 0.0,
            "largest_loss_rr": 1.0,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
            "profit_factor": 0.0,
            "expectancy_per_trade": 0.0,
        }

    closed_trades = len(signals)
    winning_trades = sum(1 for s in signals if s.get("status") == "TP3_HIT")
    losing_trades = sum(1 for s in signals if s.get("status") == "SL_HIT")

    win_rate = (winning_trades / closed_trades * 100) if closed_trades > 0 else 0.0
    loss_rate = 100.0 - win_rate

    # Calculate R-value
    total_r_value = 0.0
    for signal in signals:
        rr = signal.get("rr_ratio", 1.0)
        if signal.get("status") == "TP3_HIT":
            total_r_value += rr
        elif signal.get("status") == "SL_HIT":
            total_r_value -= 1.0

    avg_rr_ratio = sum(s.get("rr_ratio", 1.0) for s in signals) / closed_trades if closed_trades > 0 else 0.0

    largest_win = max((s.get("rr_ratio", 0.0) for s in signals if s.get("status") == "TP3_HIT"), default=0.0)

    consecutive_wins, consecutive_losses = calculate_consecutive_streaks(signals)

    # Profit factor: sum of wins / sum of losses
    sum_wins = sum(s.get("rr_ratio", 0.0) for s in signals if s.get("status") == "TP3_HIT")
    sum_losses = losing_trades * 1.0  # Each loss is -1R
    profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else 0.0

    # Expectancy per trade
    expectancy = (win_rate / 100 * avg_rr_ratio) - (loss_rate / 100 * 1.0) if closed_trades > 0 else 0.0

    return {
        "total_trades": closed_trades,
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(win_rate, 2),
        "loss_rate": round(loss_rate, 2),
        "avg_rr_ratio": round(avg_rr_ratio, 2),
        "total_r_value": round(total_r_value, 2),
        "largest_win_rr": round(largest_win, 2),
        "largest_loss_rr": 1.0,
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "profit_factor": round(profit_factor, 2),
        "expectancy_per_trade": round(expectancy, 4),
    }


def build_equity_curve(signals: list, provider_name: str) -> dict:
    """Build equity curve from closed signals."""
    if not signals:
        return {
            "provider_name": provider_name,
            "points": [],
            "start_date": None,
            "end_date": None,
        }

    # Sort by closed_at
    sorted_signals = sorted(signals, key=lambda s: s.get("closed_at", ""))

    points = []
    cumulative_r = 0.0
    win_count = 0
    loss_count = 0

    for signal in sorted_signals:
        status = signal.get("status")
        rr = signal.get("rr_ratio", 1.0)

        if status == "TP3_HIT":
            cumulative_r += rr
            win_count += 1
        elif status == "SL_HIT":
            cumulative_r -= 1.0
            loss_count += 1

        points.append(EquityCurvePoint(
            timestamp=signal.get("closed_at", ""),
            cumulative_r_value=round(cumulative_r, 2),
            trade_count=win_count + loss_count,
            win_count=win_count,
            loss_count=loss_count,
        ))

    return {
        "provider_name": provider_name,
        "points": points,
        "start_date": sorted_signals[0].get("closed_at") if sorted_signals else None,
        "end_date": sorted_signals[-1].get("closed_at") if sorted_signals else None,
    }


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/reports/performance", response_model=PerformanceReportResponse)
async def get_performance_report(
    provider_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """
    Get performance report for a provider.

    Query Parameters:
    - provider_id: Provider ID (required)
    - days: Number of days to look back (1-365, default 30)

    Returns comprehensive performance metrics.
    """
    if not provider_id:
        raise HTTPException(400, "provider_id is required")

    sb = get_supabase()

    try:
        # Fetch provider
        result = sb.table("providers").select("*").eq("id", provider_id).execute()
        if not result.data:
            raise HTTPException(404, f"Provider not found: {provider_id}")

        provider = result.data[0]

        # Calculate date range
        end_date = datetime.now(timezone.utc).isoformat()
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Fetch closed signals
        signals = get_provider_closed_signals(sb, provider_id, start_date, end_date)

        # Calculate metrics
        metrics_dict = calculate_performance_metrics(signals)

        return PerformanceReportResponse(
            provider_id=provider_id,
            provider_name=provider["name"],
            period=f"Last {days} days",
            start_date=start_date,
            end_date=end_date,
            metrics=PerformanceMetrics(**metrics_dict),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate performance report: {e}")
        raise HTTPException(500, "Failed to generate report")


@router.get("/reports/equity-curve", response_model=EquityCurveResponse)
async def get_equity_curve(
    provider_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """
    Get equity curve data for a provider.

    Query Parameters:
    - provider_id: Provider ID (required)
    - days: Number of days to look back (1-365, default 30)

    Returns equity curve points showing cumulative R-value progression.
    """
    if not provider_id:
        raise HTTPException(400, "provider_id is required")

    sb = get_supabase()

    try:
        # Fetch provider
        result = sb.table("providers").select("*").eq("id", provider_id).execute()
        if not result.data:
            raise HTTPException(404, f"Provider not found: {provider_id}")

        provider = result.data[0]

        # Calculate date range
        end_date = datetime.now(timezone.utc).isoformat()
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Fetch closed signals
        signals = get_provider_closed_signals(sb, provider_id, start_date, end_date)

        # Build equity curve
        curve_data = build_equity_curve(signals, provider["name"])

        return EquityCurveResponse(
            provider_id=provider_id,
            **curve_data,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate equity curve: {e}")
        raise HTTPException(500, "Failed to generate equity curve")


@router.get("/reports/leaderboard", response_model=LeaderboardResponse)
async def get_leaderboard(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Get provider performance leaderboard.

    Query Parameters:
    - days: Number of days to look back (1-365, default 30)
    - limit: Number of providers to return (1-100, default 20)

    Returns ranked list of providers by total R-value (expectancy).
    """
    sb = get_supabase()

    try:
        # Calculate date range
        end_date = datetime.now(timezone.utc).isoformat()
        start_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Fetch all providers
        providers_result = sb.table("providers").select("*").eq("is_active", True).execute()
        providers = providers_result.data or []

        # Calculate metrics for each provider
        leaderboard_data = []

        for provider in providers:
            signals = get_provider_closed_signals(sb, provider["id"], start_date, end_date)
            metrics = calculate_performance_metrics(signals)

            if metrics["closed_trades"] > 0:  # Only include providers with trades
                leaderboard_data.append({
                    "provider_id": provider["id"],
                    "provider_name": provider["name"],
                    "total_trades": metrics["closed_trades"],
                    "win_rate": metrics["win_rate"],
                    "total_r_value": metrics["total_r_value"],
                    "expectancy": metrics["expectancy_per_trade"],
                })

        # Sort by total_r_value descending
        leaderboard_data.sort(key=lambda x: x["total_r_value"], reverse=True)

        # Apply limit
        leaderboard_data = leaderboard_data[:limit]

        # Add ranks
        entries = [
            ProviderLeaderboardEntry(
                rank=i + 1,
                provider_id=entry["provider_id"],
                provider_name=entry["provider_name"],
                total_trades=entry["total_trades"],
                win_rate=entry["win_rate"],
                total_r_value=entry["total_r_value"],
                expectancy=entry["expectancy"],
                sharpe_ratio=None,  # TODO: calculate sharpe ratio
            )
            for i, entry in enumerate(leaderboard_data)
        ]

        return LeaderboardResponse(
            period=f"Last {days} days",
            entries=entries,
        )
    except Exception as e:
        logger.error(f"Failed to generate leaderboard: {e}")
        raise HTTPException(500, "Failed to generate leaderboard")
