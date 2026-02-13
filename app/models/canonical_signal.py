"""
Canonical data models for the Signal Bridge.
All trading signals are normalized to these schemas regardless of source.
"""

from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Any
from pydantic import BaseModel, Field, field_validator
import uuid


class SignalDirection(str, Enum):
    """Trade direction enumeration."""
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    """Signal lifecycle status enumeration."""
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    SL_HIT = "SL_HIT"
    CLOSED = "CLOSED"
    INVALID = "INVALID"


class AssetClass(str, Enum):
    """Asset class enumeration."""
    FUTURES = "FUTURES"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    STOCKS = "STOCKS"
    OTHER = "OTHER"


class EventType(str, Enum):
    """Signal event type enumeration."""
    ENTRY_REGISTERED = "ENTRY_REGISTERED"
    ENTRY_HIT = "ENTRY_HIT"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    SL_HIT = "SL_HIT"
    PRICE_UPDATE = "PRICE_UPDATE"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    EXPIRED = "EXPIRED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class EventSource(str, Enum):
    """Event source enumeration."""
    TRADINGVIEW = "TRADINGVIEW"
    PINESCRIPT = "PINESCRIPT"
    POLLING = "POLLING"
    WEBSOCKET = "WEBSOCKET"
    MANUAL = "MANUAL"
    HISTORICAL = "HISTORICAL"


# ---------- Core Models ----------


class CanonicalSignal(BaseModel):
    """
    The universal signal format. Every signal source converts to this.

    Represents a single trading signal with entry, stop-loss, and take-profit levels.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique signal identifier")
    provider_id: str = Field(description="ID of the provider that generated this signal")
    external_signal_id: Optional[str] = Field(default=None, description="Signal ID from the external source")
    strategy_name: Optional[str] = Field(default=None, description="Name of the strategy that generated this signal")

    # Instrument
    symbol: str = Field(description="Trading symbol (e.g., 'NQ', 'EURUSD', 'BTC')")
    asset_class: AssetClass = Field(default=AssetClass.OTHER, description="Asset class of the instrument")

    # Trade levels
    direction: SignalDirection = Field(description="Trade direction (LONG or SHORT)")
    entry_price: float = Field(description="Entry price for the trade")
    sl: float = Field(description="Stop-loss price")
    tp1: float = Field(description="First take-profit level")
    tp2: Optional[float] = Field(default=None, description="Second take-profit level")
    tp3: Optional[float] = Field(default=None, description="Third take-profit level")

    # Calculated
    risk_distance: Optional[float] = Field(default=None, description="Absolute distance from entry to stop-loss")
    rr_ratio: Optional[float] = Field(default=None, description="Risk-to-reward ratio (TP1 vs SL)")

    # State
    status: SignalStatus = Field(default=SignalStatus.PENDING, description="Current status of the signal")
    entry_time: datetime = Field(description="UTC timestamp when the signal was generated")

    # Audit
    raw_payload: Optional[dict] = Field(default=None, description="Original payload from the signal source")

    def calculate_risk_metrics(self) -> None:
        """
        Calculate risk_distance and rr_ratio from price levels.

        Sets:
            risk_distance: Absolute distance from entry to stop-loss
            rr_ratio: Ratio of TP1 distance to risk distance
        """
        self.risk_distance = abs(self.entry_price - self.sl)
        if self.risk_distance > 0:
            tp1_distance = abs(self.tp1 - self.entry_price)
            self.rr_ratio = round(tp1_distance / self.risk_distance, 4)


class SignalEvent(BaseModel):
    """
    A single lifecycle event for a signal.

    Tracks price-level hits, validation events, and other state changes.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique event identifier")
    signal_id: str = Field(description="ID of the signal this event belongs to")
    event_type: EventType = Field(description="Type of event that occurred")
    price: Optional[float] = Field(default=None, description="Price at which the event occurred")
    source: EventSource = Field(description="Source that detected or triggered this event")
    event_time: datetime = Field(description="UTC timestamp when the event occurred")
    metadata: dict = Field(default_factory=dict, description="Additional context for the event")


class SignalWithEvents(CanonicalSignal):
    """
    Signal with its full event history attached.

    Includes calculated performance metrics and closure information.
    """
    events: List[SignalEvent] = Field(default_factory=list, description="List of events for this signal")
    exit_price: Optional[float] = Field(default=None, description="Price at which the signal was closed")
    r_value: Optional[float] = Field(default=None, description="Number of risk units gained/lost")
    pnl_pct: Optional[float] = Field(default=None, description="Profit/loss as percentage")
    activated_at: Optional[datetime] = Field(default=None, description="UTC timestamp when signal was activated (entry_hit)")
    closed_at: Optional[datetime] = Field(default=None, description="UTC timestamp when signal was closed")
    close_reason: Optional[str] = Field(default=None, description="Reason for closure (e.g., 'TP1_HIT', 'SL_HIT', 'MANUAL')")


# ---------- Provider Models ----------


class Provider(BaseModel):
    """
    Represents a signal provider.

    Stores metadata about a provider including verification status and configuration.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique provider identifier")
    name: str = Field(description="Human-readable provider name")
    description: Optional[str] = Field(default=None, description="Description of the provider and their signals")
    is_active: bool = Field(default=True, description="Whether this provider is active")
    is_verified: bool = Field(default=False, description="Whether this provider has been verified")
    metadata: dict = Field(default_factory=dict, description="Additional configuration or metadata")
    created_at: Optional[datetime] = Field(default=None, description="UTC timestamp when provider was created")


class ProviderCreate(BaseModel):
    """
    Input model for creating a new provider.

    Used in API requests to register a new signal provider.
    """
    name: str = Field(description="Human-readable provider name")
    description: Optional[str] = Field(default=None, description="Description of the provider")


class ProviderWithKey(Provider):
    """
    Provider with authentication credentials.

    Returned once on creation — includes the raw API key and webhook secret.
    Should not be stored or returned in other contexts.
    """
    api_key: str = Field(description="API key for authentication")
    webhook_secret: str = Field(description="Secret for verifying webhook signatures")


class ProviderStats(BaseModel):
    """
    Aggregated performance metrics for a provider.

    Summarizes win rate, profit factor, average R-value, and other statistics.
    """
    provider_id: str = Field(description="ID of the provider")
    total_signals: int = Field(default=0, description="Total number of signals from this provider")
    open_signals: int = Field(default=0, description="Number of currently open signals")
    wins: int = Field(default=0, description="Number of winning signals")
    losses: int = Field(default=0, description="Number of losing signals")
    partials: int = Field(default=0, description="Number of partially closed signals")
    win_rate: float = Field(default=0.0, description="Win rate as percentage (0-100)")
    tp1_hit_rate: float = Field(default=0.0, description="Percentage of signals that hit TP1")
    tp2_hit_rate: float = Field(default=0.0, description="Percentage of signals that hit TP2")
    tp3_hit_rate: float = Field(default=0.0, description="Percentage of signals that hit TP3")
    avg_r: float = Field(default=0.0, description="Average R-value per signal")
    total_r: float = Field(default=0.0, description="Cumulative R-value across all signals")
    best_r: float = Field(default=0.0, description="Best single R-value achieved")
    worst_r: float = Field(default=0.0, description="Worst single R-value achieved")
    expectancy: float = Field(default=0.0, description="Mathematical expectancy per trade")
    avg_duration_hours: float = Field(default=0.0, description="Average duration of signals in hours")
    period_start: Optional[date] = Field(default=None, description="Start date of the analysis period")
    period_end: Optional[date] = Field(default=None, description="End date of the analysis period")
    calculated_at: Optional[datetime] = Field(default=None, description="UTC timestamp when stats were calculated")


# ---------- Webhook / Notification Models ----------


class WebhookConfig(BaseModel):
    """
    Configuration for an outbound webhook destination.

    Defines where and when to send signal event notifications.
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique webhook configuration ID")
    provider_id: str = Field(description="ID of the provider this webhook is for")
    name: str = Field(description="Human-readable name for this webhook")
    url: str = Field(description="HTTP(S) URL to send notifications to")
    event_types: List[str] = Field(description="List of event types to forward (e.g., ['ENTRY_HIT', 'TP1_HIT', 'SL_HIT'])")
    headers: dict = Field(default_factory=dict, description="Additional HTTP headers to include in requests")
    is_active: bool = Field(default=True, description="Whether this webhook is currently active")
    consecutive_failures: int = Field(default=0, description="Number of consecutive failed delivery attempts")


class WebhookConfigCreate(BaseModel):
    """
    Input model for creating an outbound webhook.

    Used in API requests to register a new notification destination.
    """
    name: str = Field(description="Human-readable name for this webhook")
    url: str = Field(description="HTTP(S) URL to send notifications to")
    event_types: List[str] = Field(description="List of event types to forward")
    headers: Optional[dict] = Field(default=None, description="Additional HTTP headers to include")


class NotificationPayload(BaseModel):
    """
    The JSON payload sent to outbound webhooks.

    Sent when a signal event occurs and the webhook is configured for that event type.
    """
    event_type: str = Field(description="Type of event that triggered this notification")
    signal_id: str = Field(description="ID of the signal")
    provider_id: str = Field(description="ID of the provider")
    symbol: str = Field(description="Trading symbol")
    direction: str = Field(description="Trade direction (LONG or SHORT)")
    entry_price: float = Field(description="Entry price")
    sl: float = Field(description="Stop-loss price")
    tp1: float = Field(description="First take-profit level")
    tp2: Optional[float] = Field(default=None, description="Second take-profit level")
    tp3: Optional[float] = Field(default=None, description="Third take-profit level")
    hit_price: Optional[float] = Field(default=None, description="Price at which the event occurred")
    r_value: Optional[float] = Field(default=None, description="R-value if the signal is closed")
    status: str = Field(description="Current status of the signal")
    event_time: str = Field(description="ISO 8601 formatted timestamp of the event")


# ---------- Validation Result ----------


class ValidationResult(BaseModel):
    """
    Output of the validation engine.

    Contains validation status, errors/warnings, and calculated risk metrics.
    """
    is_valid: bool = Field(description="Whether the signal passed validation")
    errors: List[str] = Field(default_factory=list, description="List of validation errors (if any)")
    warnings: List[str] = Field(default_factory=list, description="List of validation warnings (if any)")
    rr_ratio: Optional[float] = Field(default=None, description="Calculated risk-to-reward ratio")
    risk_distance: Optional[float] = Field(default=None, description="Calculated risk distance in pips/points")
    confidence_score: int = Field(default=0, description="Confidence score from 0-100")


# ---------- Price Models ----------


class PriceQuote(BaseModel):
    """
    A single price quote from any source.

    Used for price monitoring and level detection.
    """
    symbol: str = Field(description="Trading symbol")
    price: float = Field(description="Current price")
    bid: Optional[float] = Field(default=None, description="Bid price")
    ask: Optional[float] = Field(default=None, description="Ask price")
    source: str = Field(description="Source of the price quote")
    timestamp: datetime = Field(description="UTC timestamp of the quote")


class ProximityZone(str, Enum):
    """
    How close price is to the nearest TP/SL level.

    Used to determine if a level is imminent.
    """
    CLOSE = "CLOSE"    # within 20% of distance
    MID = "MID"        # within 50%
    FAR = "FAR"        # far away


# ---------- Report Models ----------


class PerformanceReport(BaseModel):
    """
    Comprehensive provider performance report.

    Aggregates statistics across multiple dimensions (symbol, asset class, time).
    """
    provider_id: str = Field(description="ID of the provider")
    provider_name: str = Field(description="Human-readable provider name")
    period_start: Optional[date] = Field(default=None, description="Start date of the reporting period")
    period_end: Optional[date] = Field(default=None, description="End date of the reporting period")
    summary: ProviderStats = Field(description="Overall statistics for the period")
    by_symbol: dict = Field(default_factory=dict, description="Statistics broken down by trading symbol")
    by_asset_class: dict = Field(default_factory=dict, description="Statistics broken down by asset class")
    equity_curve: List[dict] = Field(
        default_factory=list,
        description="Equity curve data points as list of {date, cumulative_r, equity}"
    )
    recent_signals: List[dict] = Field(
        default_factory=list,
        description="Summary of recent signals (last N closed trades)"
    )


class SignalOutcome(BaseModel):
    """
    Resolved outcome of a single signal.

    Captured when a signal is closed to enable performance analysis.
    """
    signal_id: str = Field(description="ID of the signal")
    result: str = Field(description="Result category (WIN, LOSS, PARTIAL, or OPEN)")
    entry_price: float = Field(description="Entry price")
    exit_price: Optional[float] = Field(default=None, description="Exit price (if closed)")
    r_value: Optional[float] = Field(default=None, description="Number of risk units gained/lost")
    tp_hits: List[int] = Field(
        default_factory=list,
        description="List of take-profit levels hit (e.g., [1], [1, 2], [1, 2, 3])"
    )
    max_favorable_excursion: Optional[float] = Field(
        default=None,
        description="Maximum favorable price movement from entry"
    )
    max_adverse_excursion: Optional[float] = Field(
        default=None,
        description="Maximum adverse price movement from entry"
    )
    duration_hours: Optional[float] = Field(
        default=None,
        description="Duration from entry to close in hours"
    )
    closed_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the signal was closed"
    )
