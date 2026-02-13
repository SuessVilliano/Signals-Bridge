"""
Inbound webhook schemas.
Defines the expected JSON formats from TradingView and other sources.
"""

from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field, model_validator


class TradingViewWebhook(BaseModel):
    """
    Expected JSON format from TradingView webhook alerts.

    Configure your TradingView alert message as:
    ```json
    {
        "alert": "Signal",
        "provider": "HybridAI",
        "strategy": "Auto Hybrid",
        "symbol": "NQ",
        "direction": "LONG",
        "entry": 20150.50,
        "tp1": 20250.00,
        "tp2": 20350.00,
        "tp3": 20450.00,
        "sl": 20050.00,
        "timestamp": "{{timenow}}"
    }
    ```

    Supports flexible field naming to accommodate various TradingView configurations.
    """
    # Required fields
    symbol: str = Field(description="Trading symbol (e.g., 'NQ', 'EURUSD', 'BTC')")
    direction: str = Field(
        description="Trade direction. Accepts 'LONG', 'SHORT', 'BUY', 'SELL' (normalized to LONG/SHORT)"
    )
    entry: Optional[float] = Field(
        default=None,
        alias="entry_price",
        description="Entry price for the trade"
    )
    sl: float = Field(description="Stop-loss price")
    tp1: float = Field(description="First take-profit level")

    # Optional fields
    alert: Optional[str] = Field(
        default=None,
        description="Alert name/description from TradingView"
    )
    provider: Optional[str] = Field(
        default=None,
        description="Provider/strategy name"
    )
    strategy: Optional[str] = Field(
        default=None,
        description="Strategy name or ID"
    )
    tp2: Optional[float] = Field(
        default=None,
        description="Second take-profit level"
    )
    tp3: Optional[float] = Field(
        default=None,
        description="Third take-profit level"
    )
    timestamp: Optional[str] = Field(
        default=None,
        description="ISO 8601 timestamp of the signal (from {{timenow}} in TradingView)"
    )

    # Allow extra fields from TradingView custom alerts
    model_config = {"extra": "allow", "populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        """
        Normalize field names and values from various TradingView configurations.

        Handles:
        - Multiple entry price field name variations
        - Direction normalization (LONG/SHORT, BUY/SELL)
        - Stop-loss and take-profit field name variations

        Args:
            data: Raw input data (typically from JSON webhook)

        Returns:
            Normalized data dictionary
        """
        if isinstance(data, dict):
            # Normalize entry price field names
            # Check for various common field name variations
            entry_field_names = ["entry", "entry_price", "price", "open", "entry_level"]
            for key in entry_field_names:
                if key in data and "entry" not in data:
                    data["entry"] = data[key]
                    break

            # Ensure entry_price alias is set
            if "entry_price" not in data and "entry" in data:
                data["entry_price"] = data["entry"]

            # Normalize direction to canonical LONG/SHORT
            direction = data.get("direction", data.get("side", data.get("action", ""))).upper()
            if direction in ("BUY", "LONG"):
                data["direction"] = "LONG"
            elif direction in ("SELL", "SHORT"):
                data["direction"] = "SHORT"

            # Normalize stop-loss field names
            sl_field_names = ["stop_loss", "stoploss", "stop", "stop_level", "sl_price"]
            for alt in sl_field_names:
                if alt in data and "sl" not in data:
                    data["sl"] = data[alt]
                    break

            # Normalize take-profit field names for TP1, TP2, TP3
            for i in [1, 2, 3]:
                tp_field_names = [
                    f"take_profit_{i}",
                    f"takeprofit{i}",
                    f"target{i}",
                    f"t{i}",
                    f"tp{i}_price",
                    f"profit_{i}",
                    f"tp_{i}"
                ]
                for alt in tp_field_names:
                    if alt in data and f"tp{i}" not in data:
                        data[f"tp{i}"] = data[alt]
                        break

        return data


class PineScriptEvent(BaseModel):
    """
    Event from the TradingView PineScript price monitor.

    Sent when price crosses a registered TP/SL level.
    Typically sent by a PineScript-based alert that monitors previously registered signals.
    """
    signal_id: str = Field(
        description="ID of the signal that this event is associated with"
    )
    event_type: str = Field(
        description="Type of event. Must be one of: ENTRY_HIT, TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT"
    )
    price: float = Field(
        description="The price at which the level was hit"
    )
    symbol: str = Field(
        description="Trading symbol confirming the asset"
    )
    timestamp: Optional[str] = Field(
        default=None,
        description="ISO 8601 timestamp when the event occurred"
    )

    # Allow extra fields from custom PineScript implementations
    model_config = {"extra": "allow"}


class ManualSignalInput(BaseModel):
    """
    For manually submitting signals.

    Used for:
    - CSV imports
    - UI form submissions
    - Manual signal registration
    - Backtesting data imports

    Provides flexibility in timestamp handling since manual signals
    may be historical or real-time.
    """
    symbol: str = Field(
        description="Trading symbol (e.g., 'NQ', 'EURUSD', 'BTC')"
    )
    direction: str = Field(
        description="Trade direction. Use 'LONG' or 'SHORT'"
    )
    entry_price: float = Field(
        description="Entry price for the trade"
    )
    sl: float = Field(
        description="Stop-loss price"
    )
    tp1: float = Field(
        description="First take-profit level"
    )
    tp2: Optional[float] = Field(
        default=None,
        description="Second take-profit level"
    )
    tp3: Optional[float] = Field(
        default=None,
        description="Third take-profit level"
    )
    entry_time: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp when the signal occurred (defaults to now if not provided)"
    )
    strategy_name: Optional[str] = Field(
        default=None,
        description="Name of the strategy that generated this signal"
    )
    provider_name: Optional[str] = Field(
        default=None,
        description="Name of the provider/account submitting this signal"
    )
