"""
Binance WebSocket Manager for real-time crypto prices.
Handles connection, reconnection with exponential backoff, and dynamic subscription.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Set
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from app.models.canonical_signal import PriceQuote, AssetClass

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
RECONNECT_DELAYS = [1, 2, 4, 8, 16, 32, 60]  # exponential backoff, max 60s


class BinanceWebSocketManager:
    """
    Manages WebSocket connections to Binance for real-time crypto prices.

    Handles:
    - Dynamic subscription/unsubscription
    - Automatic reconnection with exponential backoff
    - Price caching
    - 24-hour disconnection handling (Binance default)
    """

    def __init__(self):
        self._prices: Dict[str, PriceQuote] = {}
        self._subscribed_symbols: Set[str] = set()
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._connections: Dict[str, object] = {}
        self._reconnect_delays: Dict[str, int] = {}  # track reconnect delay per symbol
        self._lock = asyncio.Lock()
        self._shutdown = False

    async def subscribe(self, symbol: str) -> None:
        """
        Subscribe to a symbol's ticker stream.
        Symbol format: "BTCUSDT", "ETHUSDT", etc. (uppercase)
        """
        symbol = symbol.upper()

        async with self._lock:
            if symbol in self._subscribed_symbols:
                logger.debug(f"Already subscribed to {symbol}")
                return

            self._subscribed_symbols.add(symbol)

        logger.info(f"Subscribing to {symbol}")

        # Start the worker task for this symbol
        if symbol not in self._active_tasks:
            task = asyncio.create_task(self._stream_worker(symbol))
            self._active_tasks[symbol] = task

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from a symbol's stream."""
        symbol = symbol.upper()

        async with self._lock:
            if symbol not in self._subscribed_symbols:
                return
            self._subscribed_symbols.discard(symbol)

        logger.info(f"Unsubscribing from {symbol}")

        # Cancel the worker task
        if symbol in self._active_tasks:
            task = self._active_tasks.pop(symbol)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Close connection
        if symbol in self._connections:
            try:
                await self._connections[symbol].close()
            except Exception as e:
                logger.warning(f"Error closing connection for {symbol}: {e}")
            del self._connections[symbol]

    def get_latest_price(self, symbol: str) -> Optional[PriceQuote]:
        """Get the latest cached price for a symbol."""
        symbol = symbol.upper()
        return self._prices.get(symbol)

    async def close_all(self) -> None:
        """Clean shutdown of all connections."""
        logger.info("Closing all WebSocket connections")
        self._shutdown = True

        async with self._lock:
            symbols = list(self._subscribed_symbols)

        for symbol in symbols:
            await self.unsubscribe(symbol)

        # Wait for all tasks to finish
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)

        logger.info("All WebSocket connections closed")

    async def _stream_worker(self, symbol: str) -> None:
        """
        Main worker loop for a symbol's WebSocket stream.
        Handles connection, data parsing, and reconnection.
        """
        symbol = symbol.upper()
        reconnect_attempt = 0

        while not self._shutdown and symbol in self._subscribed_symbols:
            try:
                await self._connect_and_stream(symbol)
                # Reset reconnect delay on successful connection
                reconnect_attempt = 0
                self._reconnect_delays[symbol] = 0

            except asyncio.CancelledError:
                logger.debug(f"Stream worker for {symbol} cancelled")
                break

            except Exception as e:
                logger.error(f"Error in stream worker for {symbol}: {e}")

                # Calculate reconnect delay with exponential backoff
                delay = RECONNECT_DELAYS[min(reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
                reconnect_attempt += 1

                logger.info(f"Reconnecting {symbol} in {delay}s (attempt {reconnect_attempt})")
                self._reconnect_delays[symbol] = delay

                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break

    async def _connect_and_stream(self, symbol: str) -> None:
        """Connect to Binance WebSocket and stream ticker data."""
        symbol_lower = symbol.lower()
        url = f"{BINANCE_WS_URL}/{symbol_lower}@ticker"

        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                self._connections[symbol] = ws
                logger.info(f"Connected to Binance stream for {symbol}")

                async for message in ws:
                    if self._shutdown or symbol not in self._subscribed_symbols:
                        break

                    try:
                        data = json.loads(message)
                        self._parse_ticker_data(symbol, data)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse message from {symbol}: {e}")
                    except Exception as e:
                        logger.warning(f"Error processing ticker data for {symbol}: {e}")

        except ConnectionClosed as e:
            logger.warning(f"Connection closed for {symbol}: {e.rcvd_then_sent}")
        except WebSocketException as e:
            logger.warning(f"WebSocket error for {symbol}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in stream for {symbol}: {e}")
        finally:
            if symbol in self._connections:
                del self._connections[symbol]

    def _parse_ticker_data(self, symbol: str, data: dict) -> None:
        """
        Parse Binance ticker data and update price cache.

        Expected format from Binance @ticker stream:
        {
            "e": "24hrTicker",
            "E": 1234567890000,
            "s": "BTCUSDT",
            "c": "9988.00",  # close price
            "h": "9999.99",
            "l": "9888.00",
            "v": "1.00",
            "q": "9998.00",
            ...
        }
        """
        try:
            if data.get("e") != "24hrTicker":
                return

            symbol = data.get("s", symbol).upper()
            price_str = data.get("c", "0")
            price = float(price_str)

            if price <= 0:
                logger.warning(f"Invalid price {price} for {symbol}")
                return

            quote = PriceQuote(
                symbol=symbol,
                price=price,
                timestamp=datetime.now(timezone.utc),
                asset_class=AssetClass.CRYPTO,
                source="binance_ws",
            )

            self._prices[symbol] = quote
            logger.debug(f"Updated {symbol}: ${price}")

        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse ticker data for {symbol}: {e}")

    def get_subscription_status(self) -> dict:
        """Return current subscription status and reconnect info."""
        return {
            "subscribed_symbols": list(self._subscribed_symbols),
            "active_connections": list(self._connections.keys()),
            "reconnect_delays": self._reconnect_delays.copy(),
            "total_cached_prices": len(self._prices),
        }
