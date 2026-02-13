"""
REST API Poller for crypto, forex, and futures prices.
Supports multiple sources with rate limiting and fallbacks.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
import httpx

from app.config import settings
from app.models.canonical_signal import PriceQuote, AssetClass

logger = logging.getLogger(__name__)

# Rate limits per source (requests per minute)
RATE_LIMITS = {
    "binance": 1200,
    "twelvedata": 800 / (24 * 60),  # 800/day
    "alphavantage": 5,
    "yahoo": 2000,
}

TIMEOUTS = {
    "binance": 5,
    "twelvedata": 10,
    "alphavantage": 10,
    "yahoo": 10,
}


class RateLimiter:
    """Simple rate limiter tracking requests per minute."""

    def __init__(self, name: str, max_per_minute: float):
        self.name = name
        self.max_per_minute = max_per_minute
        self.requests: list[datetime] = []

    async def wait_if_needed(self) -> None:
        """Block until a request can be made within rate limits."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)

        # Remove old requests outside the window
        self.requests = [r for r in self.requests if r > cutoff]

        if len(self.requests) >= self.max_per_minute:
            # Calculate wait time
            oldest = self.requests[0]
            wait_seconds = (oldest - cutoff).total_seconds()
            logger.debug(f"{self.name}: Rate limit reached, waiting {wait_seconds:.1f}s")
            await asyncio.sleep(wait_seconds + 0.1)
            self.requests = []
        else:
            self.requests.append(now)

    def get_status(self) -> dict:
        """Return current rate limit status."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        recent_count = len([r for r in self.requests if r > cutoff])
        return {
            "source": self.name,
            "requests_this_minute": recent_count,
            "limit": self.max_per_minute,
        }


class RESTPoller:
    """
    Async HTTP client for fetching prices from multiple sources.
    Implements rate limiting, retries, and source fallbacks.
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30)
        self.limiters = {
            "binance": RateLimiter("binance", RATE_LIMITS["binance"]),
            "twelvedata": RateLimiter("twelvedata", RATE_LIMITS["twelvedata"]),
            "alphavantage": RateLimiter("alphavantage", RATE_LIMITS["alphavantage"]),
            "yahoo": RateLimiter("yahoo", RATE_LIMITS["yahoo"]),
        }
        self._session_started = False

    async def __aenter__(self):
        """Async context manager entry."""
        if not self._session_started:
            self.client = httpx.AsyncClient(timeout=30)
            self._session_started = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def close(self) -> None:
        """Close HTTP client."""
        if self._session_started:
            await self.client.aclose()
            self._session_started = False

    async def get_crypto_price(self, symbol: str) -> Optional[PriceQuote]:
        """
        Get crypto price from Binance REST API.
        Symbol: "BTCUSDT", "ETHUSDT", etc.
        """
        symbol = symbol.upper()

        try:
            await self.limiters["binance"].wait_if_needed()

            url = "https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": symbol}

            response = await self.client.get(
                url,
                params=params,
                timeout=TIMEOUTS["binance"],
            )
            response.raise_for_status()

            data = response.json()
            price = float(data["price"])

            if price <= 0:
                logger.warning(f"Invalid price {price} for {symbol}")
                return None

            return PriceQuote(
                symbol=symbol,
                price=price,
                timestamp=datetime.now(timezone.utc),
                asset_class=AssetClass.CRYPTO,
                source="binance_rest",
            )

        except httpx.HTTPError as e:
            logger.warning(f"Binance API error for {symbol}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.warning(f"Failed to parse Binance response for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching crypto price for {symbol}: {e}")
            return None

    async def get_forex_price(self, symbol: str) -> Optional[PriceQuote]:
        """
        Get forex price from TwelveData or Alpha Vantage.
        Symbol: "EURUSD" -> converts to "EUR/USD" for API
        """
        symbol = symbol.upper()
        formatted_symbol = self._format_forex_symbol(symbol)

        # Try TwelveData first
        quote = await self._get_twelvedata_forex(symbol, formatted_symbol)
        if quote:
            return quote

        # Fallback to Alpha Vantage
        quote = await self._get_alphavantage_forex(symbol, formatted_symbol)
        return quote

    async def _get_twelvedata_forex(
        self, symbol: str, formatted_symbol: str
    ) -> Optional[PriceQuote]:
        """Fetch forex from TwelveData API."""
        try:
            if not settings.twelve_data_api_key:
                return None

            await self.limiters["twelvedata"].wait_if_needed()

            url = "https://api.twelvedata.com/price"
            params = {
                "symbol": formatted_symbol,
                "apikey": settings.twelve_data_api_key,
            }

            response = await self.client.get(
                url,
                params=params,
                timeout=TIMEOUTS["twelvedata"],
            )
            response.raise_for_status()

            data = response.json()

            if "price" not in data:
                logger.warning(f"No price in TwelveData response for {symbol}")
                return None

            price = float(data["price"])

            if price <= 0:
                logger.warning(f"Invalid price {price} for {symbol}")
                return None

            return PriceQuote(
                symbol=symbol,
                price=price,
                timestamp=datetime.now(timezone.utc),
                asset_class=AssetClass.FOREX,
                source="twelvedata",
            )

        except httpx.HTTPError as e:
            logger.warning(f"TwelveData API error for {symbol}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.warning(f"Failed to parse TwelveData response for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in TwelveData forex for {symbol}: {e}")
            return None

    async def _get_alphavantage_forex(
        self, symbol: str, formatted_symbol: str
    ) -> Optional[PriceQuote]:
        """Fetch forex from Alpha Vantage as fallback."""
        try:
            if not settings.alpha_vantage_api_key:
                return None

            await self.limiters["alphavantage"].wait_if_needed()

            from_currency = symbol[:3]
            to_currency = symbol[3:6] if len(symbol) >= 6 else "USD"

            url = "https://www.alphavantage.co/query"
            params = {
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_currency,
                "to_currency": to_currency,
                "apikey": settings.alpha_vantage_api_key,
            }

            response = await self.client.get(
                url,
                params=params,
                timeout=TIMEOUTS["alphavantage"],
            )
            response.raise_for_status()

            data = response.json()

            if "Realtime Currency Exchange Rate" not in data:
                logger.warning(f"No data in Alpha Vantage response for {symbol}")
                return None

            exchange_data = data["Realtime Currency Exchange Rate"]
            price = float(exchange_data["5. Exchange Rate"])

            if price <= 0:
                logger.warning(f"Invalid price {price} for {symbol}")
                return None

            return PriceQuote(
                symbol=symbol,
                price=price,
                timestamp=datetime.now(timezone.utc),
                asset_class=AssetClass.FOREX,
                source="alphavantage",
            )

        except httpx.HTTPError as e:
            logger.warning(f"Alpha Vantage API error for {symbol}: {e}")
            return None
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse Alpha Vantage response for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in Alpha Vantage forex for {symbol}: {e}")
            return None

    async def get_futures_price(self, symbol: str) -> Optional[PriceQuote]:
        """
        Get futures price from Yahoo Finance.
        Symbol: "NQ" -> converts to "NQ=F" for Yahoo
        """
        symbol = symbol.upper()
        yahoo_symbol = f"{symbol}=F" if not symbol.endswith("=F") else symbol

        try:
            await self.limiters["yahoo"].wait_if_needed()

            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
            params = {
                "interval": "1m",
                "range": "1d",
            }

            response = await self.client.get(
                url,
                params=params,
                timeout=TIMEOUTS["yahoo"],
            )
            response.raise_for_status()

            data = response.json()

            if "chart" not in data or "result" not in data["chart"]:
                logger.warning(f"No chart data in Yahoo response for {symbol}")
                return None

            result = data["chart"]["result"][0]

            if "indicators" not in result or "quote" not in result["indicators"]:
                logger.warning(f"No quote data in Yahoo response for {symbol}")
                return None

            quotes = result["indicators"]["quote"][0]
            closes = quotes.get("close", [])

            if not closes:
                logger.warning(f"No close prices in Yahoo response for {symbol}")
                return None

            # Get the last non-null close price
            price = None
            for close in reversed(closes):
                if close is not None:
                    price = float(close)
                    break

            if price is None or price <= 0:
                logger.warning(f"Invalid price {price} for {symbol}")
                return None

            return PriceQuote(
                symbol=symbol,
                price=price,
                timestamp=datetime.now(timezone.utc),
                asset_class=AssetClass.FUTURES,
                source="yahoo",
            )

        except httpx.HTTPError as e:
            logger.warning(f"Yahoo Finance API error for {symbol}: {e}")
            return None
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.warning(f"Failed to parse Yahoo response for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching futures price for {symbol}: {e}")
            return None

    async def get_price_any(self, symbol: str) -> Optional[PriceQuote]:
        """
        Try to fetch price from any available source.
        Auto-detects asset class based on symbol pattern.
        """
        symbol = symbol.upper()

        # Try to detect asset class
        if symbol.endswith(("USDT", "USD", "BTC", "ETH")):
            quote = await self.get_crypto_price(symbol)
            if quote:
                return quote

        if len(symbol) == 6 and symbol.isalpha():
            quote = await self.get_forex_price(symbol)
            if quote:
                return quote

        # Try futures
        quote = await self.get_futures_price(symbol)
        if quote:
            return quote

        logger.warning(f"Could not fetch price from any source for {symbol}")
        return None

    async def get_historical_candles(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> Optional[Dict]:
        """
        Fetch historical candles for backtesting.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT")
            interval: "1m", "5m", "1h", "1d", etc.
            start: Start datetime
            end: End datetime

        Returns:
            Dict with structure: {
                "symbol": str,
                "interval": str,
                "candles": [
                    {"timestamp": datetime, "open": float, "high": float, "low": float, "close": float, "volume": float},
                    ...
                ]
            }
        """
        symbol = symbol.upper()

        try:
            await self.limiters["binance"].wait_if_needed()

            # Convert interval format for Binance
            binance_interval = self._convert_interval_to_binance(interval)
            if not binance_interval:
                logger.warning(f"Unsupported interval: {interval}")
                return None

            url = "https://api.binance.com/api/v3/klines"
            params = {
                "symbol": symbol,
                "interval": binance_interval,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
                "limit": 1000,
            }

            response = await self.client.get(
                url,
                params=params,
                timeout=TIMEOUTS["binance"],
            )
            response.raise_for_status()

            data = response.json()

            candles = []
            for kline in data:
                candle = {
                    "timestamp": datetime.fromtimestamp(kline[0] / 1000, tz=timezone.utc),
                    "open": float(kline[1]),
                    "high": float(kline[2]),
                    "low": float(kline[3]),
                    "close": float(kline[4]),
                    "volume": float(kline[7]),
                }
                candles.append(candle)

            return {
                "symbol": symbol,
                "interval": interval,
                "candles": candles,
            }

        except httpx.HTTPError as e:
            logger.warning(f"Binance historical data error for {symbol}: {e}")
            return None
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.warning(f"Failed to parse historical data for {symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching historical candles for {symbol}: {e}")
            return None

    @staticmethod
    def _format_forex_symbol(symbol: str) -> str:
        """Convert symbol format from EURUSD to EUR/USD."""
        if len(symbol) >= 6 and "/" not in symbol:
            return f"{symbol[:3]}/{symbol[3:6]}"
        return symbol

    @staticmethod
    def _convert_interval_to_binance(interval: str) -> Optional[str]:
        """Convert standard interval format to Binance format."""
        mapping = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1h",
            "4h": "4h",
            "1d": "1d",
            "1w": "1w",
            "1M": "1M",
        }
        return mapping.get(interval)

    def get_rate_limit_status(self) -> Dict:
        """Return rate limit status for all sources."""
        return {source: limiter.get_status() for source, limiter in self.limiters.items()}
