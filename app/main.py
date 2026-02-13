"""
Signal Bridge — Ultimate Signal Provider Network
Main FastAPI Application

Startup:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Production:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
    (single worker because we run a background price monitor task)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.price.price_manager import PriceManager
from app.workers.price_monitor import PriceMonitorWorker

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("signal_bridge")

# Global instances (shared across the app)
price_manager = PriceManager()
price_monitor = PriceMonitorWorker(price_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle management."""
    # --- STARTUP ---
    logger.info("=" * 60)
    logger.info("Signal Bridge starting up...")
    logger.info("=" * 60)

    # Initialize price feeds
    try:
        await price_manager.initialize()
        logger.info("Price feeds initialized")
    except Exception as e:
        logger.error(f"Price feed initialization failed: {e}")

    # Start background price monitor
    try:
        await price_monitor.start()
        logger.info("Price monitor worker started")
    except Exception as e:
        logger.error(f"Price monitor start failed: {e}")

    logger.info("Signal Bridge is LIVE")

    yield

    # --- SHUTDOWN ---
    logger.info("Signal Bridge shutting down...")
    await price_monitor.stop()
    await price_manager.shutdown()
    logger.info("Signal Bridge stopped")


# Create the FastAPI app
app = FastAPI(
    title="Signal Bridge",
    description=(
        "Ultimate Signal Provider Network — "
        "Ingest, validate, monitor, and report on trading signals "
        "from any source. Built for signal providers, prop firms, and traders."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allow TradingView and any frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Register Routers ---
from app.api.webhook_ingest import router as webhook_router
from app.api.signals import router as signals_router
from app.api.providers import router as providers_router
from app.api.webhooks_outbound import router as webhooks_outbound_router
from app.api.reports import router as reports_router

app.include_router(webhook_router)
app.include_router(signals_router)
app.include_router(providers_router)
app.include_router(webhooks_outbound_router)
app.include_router(reports_router)


# --- Health Check ---
@app.get("/health", tags=["system"])
async def health_check():
    """System health check — monitors all components."""
    return {
        "status": "ok",
        "service": "signal-bridge",
        "version": "1.0.0",
        "components": {
            "price_manager": "initialized" if price_manager._initialized else "not_initialized",
            "price_monitor": {
                "running": price_monitor.is_running,
                "stats": price_monitor.stats,
            },
        },
    }


@app.get("/", tags=["system"])
async def root():
    """Root endpoint — API info."""
    return {
        "service": "Signal Bridge",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "webhook_tradingview": "POST /api/v1/webhook/tradingview",
            "webhook_pinescript": "POST /api/v1/webhook/pinescript",
            "signals": "GET /api/v1/signals",
            "providers": "GET /api/v1/providers",
            "reports": "GET /api/v1/reports/performance",
            "leaderboard": "GET /api/v1/reports/leaderboard",
        },
    }


# --- Historical Resolver Endpoint ---
@app.post("/api/v1/admin/resolve-historical", tags=["admin"])
async def resolve_historical_signals(
    provider_id: str = None,
    start_date: str = None,
    end_date: str = None,
    symbol: str = None,
):
    """
    Manually trigger historical signal resolution.
    Backtests past signals against historical price data.
    """
    from datetime import date as date_type
    from app.workers.historical_resolver import HistoricalResolver

    resolver = HistoricalResolver()

    start = date_type.fromisoformat(start_date) if start_date else None
    end = date_type.fromisoformat(end_date) if end_date else None

    report = await resolver.resolve_batch(
        provider_id=provider_id,
        start_date=start,
        end_date=end,
        symbol=symbol,
    )
    return report
