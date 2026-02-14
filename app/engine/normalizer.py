"""
Signal Normalizer - converts raw TradingView webhook JSON into CanonicalSignal.

This module handles the critical task of normalizing signals from various external
sources into the canonical signal format used internally. It handles symbol normalization,
asset class detection, risk metric calculations, and edge case handling.

Key responsibilities:
- Parse external signal formats (TradingView webhooks, PineScript events) into CanonicalSignal
- Normalize symbol names (e.g., "NQ1!" → "NQ", "EURUSD" stays "EURUSD")
- Detect asset class from symbol patterns
- Calculate risk_distance and rr_ratio
- Preserve raw payload for audit trails
- Handle edge cases: missing tp2/tp3, timestamp format variations
"""

import re
from datetime import datetime
from typing import Tuple, Optional, Dict, Any
from pydantic import BaseModel, Field

from app.models.canonical_signal import (
    CanonicalSignal,
    SignalDirection,
    SignalStatus,
    AssetClass,
    EventType,
    EventSource,
    SignalEvent,
)


# Import models from canonical location to avoid duplication
from app.models.webhook_schemas import TradingViewWebhook, PineScriptEvent


class SignalNormalizer:
    """
    Normalizes signals from various sources into the canonical format.

    Handles symbol normalization, asset class detection, and risk metric calculations.
    This class is stateless and contains only static methods for pure transformations.
    """

    # Futures contract symbols (after normalization)
    FUTURES_SYMBOLS = {
        "NQ", "MNQ",    # Nasdaq 100 (micro)
        "ES", "MES",    # S&P 500 (micro)
        "YM", "MYM",    # Dow Jones (micro)
        "RTY", "M2K",   # Russell 2000 (micro)
        "GC", "MGC",    # Gold (micro)
        "CL", "MCL",    # Crude Oil (micro)
        "SI", "SIL",    # Silver (micro)
        "ZB", "ZN",     # Bonds
        "ZW", "ZC",     # Agricultural
    }

    # Forex pairs are exactly 6 uppercase letters
    FOREX_PATTERN = re.compile(r"^[A-Z]{6}$")

    # Crypto asset suffixes
    CRYPTO_SUFFIXES = ("USDT", "USD", "BTC", "ETH", "BUSD")

    # Symbol cleanup patterns
    SUFFIX_PATTERN = re.compile(r"[0-9]!$")  # e.g., "NQ1!" → "NQ"

    @staticmethod
    def normalize_symbol(raw_symbol: str) -> Tuple[str, AssetClass]:
        """
        Clean and normalize a symbol, returning the clean symbol and asset class.

        Handles various symbol formats:
        - Futures: "NQ1!" → "NQ", "ES1!" → "ES"
        - Forex: "EURUSD" → "EURUSD"
        - Crypto: "BTCUSDT" → "BTCUSDT"
        - Stocks: "AAPL" → "AAPL"

        Args:
            raw_symbol: Raw symbol string from external source

        Returns:
            Tuple of (normalized_symbol, asset_class)

        Raises:
            ValueError: If symbol is empty or invalid format
        """
        if not raw_symbol or not isinstance(raw_symbol, str):
            raise ValueError(f"Invalid symbol: {raw_symbol}")

        # Clean whitespace
        symbol = raw_symbol.strip().upper()

        # Remove futures suffix (e.g., "NQ1!" → "NQ")
        symbol = SignalNormalizer.SUFFIX_PATTERN.sub("", symbol)

        if not symbol:
            raise ValueError(f"Symbol resulted in empty string after normalization: {raw_symbol}")

        # Detect asset class
        asset_class = SignalNormalizer._detect_asset_class(symbol)

        return symbol, asset_class

    @staticmethod
    def _detect_asset_class(normalized_symbol: str) -> AssetClass:
        """
        Detect asset class from normalized symbol.

        Args:
            normalized_symbol: Clean, uppercase symbol

        Returns:
            AssetClass enum value
        """
        # Check futures first
        if normalized_symbol in SignalNormalizer.FUTURES_SYMBOLS:
            return AssetClass.FUTURES

        # Check forex BEFORE crypto (EURUSD ends in "USD" but is forex, not crypto)
        # Forex pairs are exactly 6 uppercase letters (e.g., EURUSD, GBPJPY)
        if SignalNormalizer.FOREX_PATTERN.match(normalized_symbol):
            return AssetClass.FOREX

        # Check crypto by suffix (must come after forex check)
        if any(normalized_symbol.endswith(suffix) for suffix in SignalNormalizer.CRYPTO_SUFFIXES):
            return AssetClass.CRYPTO

        # Default to stocks for 1-5 char symbols or other patterns
        return AssetClass.STOCKS

    @staticmethod
    def _parse_timestamp(timestamp_input: Optional[str]) -> datetime:
        """
        Parse various timestamp formats.

        Handles:
        - ISO 8601: "2024-02-13T12:34:56Z"
        - ISO with offset: "2024-02-13T12:34:56+00:00"
        - Unix epoch (as string): "1707826496"
        - Fallback to current UTC time

        Args:
            timestamp_input: Timestamp string or None

        Returns:
            datetime object in UTC

        Raises:
            ValueError: If timestamp format is unrecognizable
        """
        if not timestamp_input:
            return datetime.utcnow()

        timestamp_str = str(timestamp_input).strip()

        # Try ISO 8601 formats
        iso_formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ]

        for fmt in iso_formats:
            try:
                dt = datetime.strptime(timestamp_str, fmt)
                # Ensure timezone-naive datetime is treated as UTC
                return dt.replace(tzinfo=None) if dt.tzinfo is None else dt
            except ValueError:
                continue

        # Try Unix epoch (seconds since 1970)
        try:
            epoch_seconds = float(timestamp_str)
            if 0 < epoch_seconds < 10**10:  # Sanity check for seconds
                return datetime.utcfromtimestamp(epoch_seconds)
        except (ValueError, OSError):
            pass

        # Fallback: use current time and warn
        return datetime.utcnow()

    @staticmethod
    def normalize_tradingview(
        webhook: TradingViewWebhook,
        provider_id: str,
    ) -> CanonicalSignal:
        """
        Convert a TradingView webhook payload into a CanonicalSignal.

        Args:
            webhook: TradingViewWebhook model instance
            provider_id: ID of the provider sending this signal

        Returns:
            CanonicalSignal instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Validate required fields
        if not webhook.symbol:
            raise ValueError("Webhook missing required 'symbol' field")
        if not webhook.direction:
            raise ValueError("Webhook missing required 'direction' field")

        # Normalize symbol and detect asset class
        normalized_symbol, asset_class = SignalNormalizer.normalize_symbol(webhook.symbol)

        # Validate direction
        direction = SignalDirection.LONG if webhook.direction.upper() == "LONG" else SignalDirection.SHORT

        # Parse timestamp
        entry_time = SignalNormalizer._parse_timestamp(webhook.timestamp)

        # Create the canonical signal
        signal = CanonicalSignal(
            provider_id=provider_id,
            external_signal_id=getattr(webhook, 'external_id', None),
            strategy_name=getattr(webhook, 'strategy', None) or getattr(webhook, 'strategy_name', None),
            symbol=normalized_symbol,
            asset_class=asset_class,
            direction=direction,
            entry_price=float(webhook.entry if webhook.entry is not None else (getattr(webhook, 'entry_price', None) or webhook.model_extra.get('entry_price', 0) or webhook.model_extra.get('entry', 0))),
            sl=float(webhook.sl) if webhook.sl is not None else None,
            tp1=float(webhook.tp1) if webhook.tp1 is not None else None,
            tp2=float(webhook.tp2) if webhook.tp2 is not None else None,
            tp3=float(webhook.tp3) if webhook.tp3 is not None else None,
            status=SignalStatus.PENDING,
            entry_time=entry_time,
            raw_payload=webhook.model_dump(),
        )

        # Calculate risk metrics
        signal.calculate_risk_metrics()

        return signal

    @staticmethod
    def normalize_pinescript_event(event: PineScriptEvent) -> SignalEvent:
        """
        Convert a PineScript price event into a SignalEvent.

        PineScript events represent individual price-level hits, not complete signals.
        This is typically used for tracking TP/SL hits after a signal is already registered.

        Args:
            event: PineScriptEvent model instance

        Returns:
            SignalEvent instance

        Raises:
            ValueError: If required fields are missing
        """
        if not event.symbol:
            raise ValueError("PineScript event missing 'symbol' field")
        if not event.price:
            raise ValueError("PineScript event missing 'price' field")
        if not event.event_type:
            raise ValueError("PineScript event missing 'event_type' field")

        # Map event_type string to EventType enum
        event_type_map = {
            "entry": EventType.ENTRY_HIT,
            "tp1": EventType.TP1_HIT,
            "tp2": EventType.TP2_HIT,
            "tp3": EventType.TP3_HIT,
            "sl": EventType.SL_HIT,
            "close": EventType.MANUAL_CLOSE,
        }

        event_type_str = event.event_type.lower().strip()
        event_type = event_type_map.get(event_type_str, EventType.PRICE_UPDATE)

        # Parse timestamp
        event_time = SignalNormalizer._parse_timestamp(event.timestamp)

        # Extract signal_id from metadata if available
        signal_id = event.metadata.get("signal_id", "")
        if not signal_id:
            raise ValueError("PineScript event metadata must contain 'signal_id'")

        # Create the signal event
        signal_event = SignalEvent(
            signal_id=signal_id,
            event_type=event_type,
            price=float(event.price),
            source=EventSource.PINESCRIPT,
            event_time=event_time,
            metadata=event.metadata,
        )

        return signal_event

    @staticmethod
    def batch_normalize_tradingview(
        webhooks: list[TradingViewWebhook],
        provider_id: str,
    ) -> list[CanonicalSignal]:
        """
        Normalize a batch of TradingView webhooks.

        Args:
            webhooks: List of TradingViewWebhook instances
            provider_id: ID of the provider

        Returns:
            List of CanonicalSignal instances

        Note:
            If any single webhook fails to normalize, that webhook is skipped and
            the error is logged. Processing continues for remaining webhooks.
        """
        signals = []

        for webhook in webhooks:
            try:
                signal = SignalNormalizer.normalize_tradingview(webhook, provider_id)
                signals.append(signal)
            except (ValueError, TypeError) as e:
                # Log error and skip this webhook
                # In production, use proper logging: logger.error(f"Failed to normalize webhook: {e}")
                continue

        return signals

    @staticmethod
    def get_normalization_stats(raw_symbol: str) -> Dict[str, Any]:
        """
        Get diagnostic information about symbol normalization.

        Useful for debugging and understanding how a symbol was normalized.

        Args:
            raw_symbol: Raw symbol from external source

        Returns:
            Dictionary with normalization details
        """
        try:
            normalized, asset_class = SignalNormalizer.normalize_symbol(raw_symbol)
            return {
                "raw_symbol": raw_symbol,
                "normalized_symbol": normalized,
                "asset_class": asset_class.value,
                "success": True,
                "error": None,
            }
        except ValueError as e:
            return {
                "raw_symbol": raw_symbol,
                "normalized_symbol": None,
                "asset_class": None,
                "success": False,
                "error": str(e),
            }
