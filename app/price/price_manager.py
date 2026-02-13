"""
Unified Price Manager.
Routes price requests to the appropriate source based on asset class.
Maintains a price cache and handles fallbacks.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
from app.models.canonical_signal import PriceQuote, ProximityZone, AssetClass

logger = logging.getLogger(__name__)


class PriceCache:
    """In-memory price cache with TTL."""

    def __init__(self, ttl_seconds: int = 10):
        self._cache: Dict[str, PriceQuote] = {}
        self.ttl = timedelta(seconds=ttl_seconds)

    def get(self, symbol: str) -> Optional[PriceQuote]:
        quote = self._cache.get(symbol)
        if quote and (datetime.now(timezone.utc) - quote.timestamp) < self.ttl:
            return quote
        return None

    def set(self, symbol: str, quote: PriceQuote) -> None:
        self._cache[symbol] = quote

    def get_all(self) -> Dict[str, PriceQuote]:
        """Return all cached prices (for batch operations)."""
        now = datetime.now(timezone.utc)
        return {k: v for k, v in self._cache.items() if (now - v.timestamp) < self.ttl}


class PriceManager:
    """
    Central price manager. Routes to correct source, caches results, handles fallbacks.

    Usage:
        pm = PriceManager()
        await pm.initialize()
        quote = await pm.get_price("BTCUSDT")
    """

    def __init__(self):
        self.cache = PriceCache(ttl_seconds=10)
        self._binance_ws = None  # Set after initialization
        self._rest_poller = None
        self._initialized = False

    async def initialize(self):
        """Initialize price feed connections."""
        from app.price.binance_ws import BinanceWebSocketManager
        from app.price.rest_poller import RESTPoller

        self._binance_ws = BinanceWebSocketManager()
        self._rest_poller = RESTPoller()
        self._initialized = True
        logger.info("PriceManager initialized")

    async def shutdown(self):
        """Clean up connections."""
        if self._binance_ws:
            await self._binance_ws.close_all()
        self._initialized = False
        logger.info("PriceManager shutdown")

    async def get_price(self, symbol: str, asset_class: AssetClass = None) -> Optional[PriceQuote]:
        """
        Get current price for a symbol.

        Priority:
        1. Cache (if fresh)
        2. WebSocket stream (crypto)
        3. REST API poll
        """
        # Check cache first
        cached = self.cache.get(symbol)
        if cached:
            return cached

        quote = None

        # Route to appropriate source
        if asset_class == AssetClass.CRYPTO or symbol.endswith(("USDT", "USD", "BTC")):
            # Try Binance WebSocket cache first
            if self._binance_ws:
                quote = self._binance_ws.get_latest_price(symbol)
            # Fallback to REST
            if not quote and self._rest_poller:
                quote = await self._rest_poller.get_crypto_price(symbol)

        elif asset_class == AssetClass.FOREX:
            if self._rest_poller:
                quote = await self._rest_poller.get_forex_price(symbol)

        elif asset_class == AssetClass.FUTURES:
            if self._rest_poller:
                quote = await self._rest_poller.get_futures_price(symbol)

        else:
            # Try all sources
            if self._rest_poller:
                quote = await self._rest_poller.get_price_any(symbol)

        if quote:
            self.cache.set(symbol, quote)

        return quote

    async def get_prices_batch(self, symbols: list[str]) -> Dict[str, PriceQuote]:
        """Get prices for multiple symbols efficiently (group by source)."""
        results = {}
        crypto_symbols = []
        forex_symbols = []
        futures_symbols = []

        for sym in symbols:
            cached = self.cache.get(sym)
            if cached:
                results[sym] = cached
            elif sym.endswith(("USDT", "USD", "BTC", "ETH")):
                crypto_symbols.append(sym)
            elif len(sym) == 6 and sym.isalpha():
                forex_symbols.append(sym)
            else:
                futures_symbols.append(sym)

        # Fetch uncached prices in parallel
        tasks = []
        if crypto_symbols:
            tasks.append(self._fetch_crypto_batch(crypto_symbols))
        if forex_symbols:
            tasks.append(self._fetch_forex_batch(forex_symbols))
        if futures_symbols:
            tasks.append(self._fetch_futures_batch(futures_symbols))

        if tasks:
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for batch in batch_results:
                if isinstance(batch, dict):
                    results.update(batch)
                    for sym, quote in batch.items():
                        self.cache.set(sym, quote)

        return results

    async def _fetch_crypto_batch(self, symbols: list[str]) -> Dict[str, PriceQuote]:
        results = {}
        if self._rest_poller:
            for sym in symbols:
                try:
                    quote = await self._rest_poller.get_crypto_price(sym)
                    if quote:
                        results[sym] = quote
                except Exception as e:
                    logger.warning(f"Failed to fetch crypto price for {sym}: {e}")
        return results

    async def _fetch_forex_batch(self, symbols: list[str]) -> Dict[str, PriceQuote]:
        results = {}
        if self._rest_poller:
            for sym in symbols:
                try:
                    quote = await self._rest_poller.get_forex_price(sym)
                    if quote:
                        results[sym] = quote
                except Exception as e:
                    logger.warning(f"Failed to fetch forex price for {sym}: {e}")
        return results

    async def _fetch_futures_batch(self, symbols: list[str]) -> Dict[str, PriceQuote]:
        results = {}
        if self._rest_poller:
            for sym in symbols:
                try:
                    quote = await self._rest_poller.get_futures_price(sym)
                    if quote:
                        results[sym] = quote
                except Exception as e:
                    logger.warning(f"Failed to fetch futures price for {sym}: {e}")
        return results

    def subscribe_crypto(self, symbol: str):
        """Subscribe to real-time crypto WebSocket stream."""
        if self._binance_ws:
            asyncio.create_task(self._binance_ws.subscribe(symbol))

    @staticmethod
    def calculate_proximity(
        current_price: float,
        entry_price: float,
        sl: float,
        tp_levels: list[float],
        direction: str
    ) -> tuple[ProximityZone, float, str]:
        """
        Calculate how close price is to the nearest TP/SL level.

        Returns: (zone, distance_ratio, nearest_level_name)
        """
        all_levels = {"SL": sl}
        for i, tp in enumerate(tp_levels, 1):
            if tp is not None:
                all_levels[f"TP{i}"] = tp

        # Find nearest level
        min_distance = float('inf')
        nearest_name = ""
        for name, level in all_levels.items():
            distance = abs(current_price - level)
            if distance < min_distance:
                min_distance = distance
                nearest_name = name

        # Calculate as ratio of total range
        total_range = abs(max(tp_levels[0], sl) - min(tp_levels[0], sl))
        if total_range == 0:
            return ProximityZone.FAR, 1.0, nearest_name

        ratio = min_distance / total_range

        if ratio <= 0.10:  # within 10% of nearest level
            zone = ProximityZone.CLOSE
        elif ratio <= 0.30:  # within 30%
            zone = ProximityZone.MID
        else:
            zone = ProximityZone.FAR

        return zone, ratio, nearest_name
