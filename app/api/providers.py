"""
Provider Management Endpoints.
Manages signal providers, API keys, and performance statistics.
"""

import logging
import secrets
import hashlib
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_supabase
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["providers"])


# ============================================================================
# Request/Response Models
# ============================================================================

class CreateProviderRequest(BaseModel):
    """Request model for creating a new provider."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)


class ProviderResponse(BaseModel):
    """Provider response model (without sensitive data)."""
    id: str
    name: str
    description: Optional[str]
    is_active: bool
    created_at: str
    updated_at: Optional[str]
    total_signals: Optional[int] = 0
    active_signals: Optional[int] = 0


class ProviderDetailResponse(ProviderResponse):
    """Provider detail response with statistics."""
    total_valid_signals: int = 0
    total_invalid_signals: int = 0
    closed_signals: int = 0
    win_rate: float = 0.0
    avg_rr_ratio: float = 0.0
    total_r_value: float = 0.0


class ProviderCreatedResponse(ProviderResponse):
    """Response when creating a new provider (includes secrets)."""
    api_key: str
    webhook_secret: str


class ProviderStatsResponse(BaseModel):
    """Provider performance statistics."""
    provider_id: str
    name: str
    total_signals: int
    active_signals: int
    closed_signals: int
    valid_signals: int
    invalid_signals: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_rr_ratio: float
    total_r_value: float
    largest_win: Optional[float]
    largest_loss: Optional[float]
    consecutive_wins: int
    consecutive_losses: int


# ============================================================================
# Helper Functions
# ============================================================================

def hash_key(key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    return secrets.token_urlsafe(32)


def generate_webhook_secret() -> str:
    """Generate a webhook secret for HMAC signing."""
    return secrets.token_urlsafe(32)


def get_provider_signals_count(sb, provider_id: str, status: Optional[str] = None) -> int:
    """Count signals for a provider with optional status filter."""
    try:
        query = sb.table("canonical_signals").select("*", count="exact").eq("provider_id", provider_id)
        if status:
            query = query.eq("status", status)
        result = query.execute()
        return result.count or 0
    except Exception as e:
        logger.error(f"Failed to count signals for provider {provider_id}: {e}")
        return 0


def calculate_provider_stats(sb, provider_id: str) -> dict:
    """Calculate performance statistics for a provider."""
    try:
        # Fetch all closed signals for the provider
        result = sb.table("canonical_signals").select("*").eq("provider_id", provider_id).in_("status", ["CLOSED", "TP3_HIT", "SL_HIT"]).execute()
        signals = result.data or []

        total_signals = get_provider_signals_count(sb, provider_id)
        active_signals = get_provider_signals_count(sb, provider_id, "ACTIVE")
        closed_signals = len(signals)
        valid_signals = get_provider_signals_count(sb, provider_id, "VALID")
        invalid_signals = get_provider_signals_count(sb, provider_id, "INVALID")

        # Calculate win/loss
        win_count = sum(1 for s in signals if s.get("status") == "TP3_HIT")
        loss_count = sum(1 for s in signals if s.get("status") == "SL_HIT")
        win_rate = (win_count / closed_signals * 100) if closed_signals > 0 else 0.0

        # Calculate R-value
        total_r_value = 0.0
        for signal in signals:
            rr = signal.get("rr_ratio", 1.0)
            if signal.get("status") == "TP3_HIT":
                total_r_value += rr
            elif signal.get("status") == "SL_HIT":
                total_r_value -= 1.0

        avg_rr_ratio = sum(s.get("rr_ratio", 1.0) for s in signals) / closed_signals if closed_signals > 0 else 0.0

        # Largest win/loss
        largest_win = max((s.get("rr_ratio", 0.0) for s in signals if s.get("status") == "TP3_HIT"), default=None)
        largest_loss = 1.0 if loss_count > 0 else None

        return {
            "total_signals": total_signals,
            "active_signals": active_signals,
            "closed_signals": closed_signals,
            "valid_signals": valid_signals,
            "invalid_signals": invalid_signals,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 2),
            "avg_rr_ratio": round(avg_rr_ratio, 2),
            "total_r_value": round(total_r_value, 2),
            "largest_win": largest_win,
            "largest_loss": largest_loss,
            "consecutive_wins": 0,  # TODO: calculate from closed signals
            "consecutive_losses": 0,  # TODO: calculate from closed signals
        }
    except Exception as e:
        logger.error(f"Failed to calculate provider stats: {e}")
        return {
            "total_signals": 0,
            "active_signals": 0,
            "closed_signals": 0,
            "valid_signals": 0,
            "invalid_signals": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "avg_rr_ratio": 0.0,
            "total_r_value": 0.0,
            "largest_win": None,
            "largest_loss": None,
            "consecutive_wins": 0,
            "consecutive_losses": 0,
        }


# ============================================================================
# Endpoints
# ============================================================================

@router.post("/providers", response_model=ProviderCreatedResponse)
async def create_provider(req: CreateProviderRequest):
    """
    Create a new signal provider.

    Returns the provider details including:
    - api_key: Use in X-API-Key header for webhook requests
    - webhook_secret: Use to sign outbound webhooks (HMAC-SHA256)

    These credentials will only be returned once. Store them securely.
    """
    sb = get_supabase()

    try:
        # Generate credentials
        api_key = generate_api_key()
        webhook_secret = generate_webhook_secret()
        api_key_hash = hash_key(api_key)

        now = datetime.now(timezone.utc).isoformat()

        # Insert provider
        result = sb.table("providers").insert({
            "name": req.name,
            "description": req.description,
            "api_key_hash": api_key_hash,
            "webhook_secret": webhook_secret,
            "is_active": True,
            "created_at": now,
        }).execute()

        if not result.data:
            raise Exception("Failed to insert provider")

        provider = result.data[0]

        logger.info(f"Provider created: {provider['id']} | {req.name}")

        return ProviderCreatedResponse(
            id=provider["id"],
            name=provider["name"],
            description=provider.get("description"),
            is_active=provider["is_active"],
            created_at=provider["created_at"],
            updated_at=None,
            api_key=api_key,
            webhook_secret=webhook_secret,
        )
    except Exception as e:
        logger.error(f"Failed to create provider: {e}")
        raise HTTPException(500, "Failed to create provider")


@router.get("/providers", response_model=List[ProviderResponse])
async def list_providers(
    is_active: Optional[bool] = Query(None),
):
    """
    List all signal providers.

    Query Parameters:
    - is_active: Filter by active status (optional)

    Returns list of providers without sensitive credentials.
    """
    sb = get_supabase()

    try:
        query = sb.table("providers").select("*")

        if is_active is not None:
            query = query.eq("is_active", is_active)

        result = query.order("created_at", desc=True).execute()

        providers = []
        for p in (result.data or []):
            total_signals = get_provider_signals_count(sb, p["id"])
            active_signals = get_provider_signals_count(sb, p["id"], "ACTIVE")

            providers.append(ProviderResponse(
                id=p["id"],
                name=p["name"],
                description=p.get("description"),
                is_active=p["is_active"],
                created_at=p["created_at"],
                updated_at=p.get("updated_at"),
                total_signals=total_signals,
                active_signals=active_signals,
            ))

        return providers
    except Exception as e:
        logger.error(f"Failed to list providers: {e}")
        raise HTTPException(500, "Failed to list providers")


@router.get("/providers/{provider_id}", response_model=ProviderDetailResponse)
async def get_provider(provider_id: str):
    """
    Get detailed provider information.

    Path Parameters:
    - provider_id: The provider ID

    Returns provider details with performance statistics.
    """
    sb = get_supabase()

    try:
        # Fetch provider
        result = sb.table("providers").select("*").eq("id", provider_id).execute()
        if not result.data:
            raise HTTPException(404, f"Provider not found: {provider_id}")

        provider = result.data[0]

        # Get counts
        total_signals = get_provider_signals_count(sb, provider_id)
        active_signals = get_provider_signals_count(sb, provider_id, "ACTIVE")
        valid_signals = get_provider_signals_count(sb, provider_id, "VALID")
        invalid_signals = get_provider_signals_count(sb, provider_id, "INVALID")

        # Calculate stats
        stats = calculate_provider_stats(sb, provider_id)

        return ProviderDetailResponse(
            id=provider["id"],
            name=provider["name"],
            description=provider.get("description"),
            is_active=provider["is_active"],
            created_at=provider["created_at"],
            updated_at=provider.get("updated_at"),
            total_signals=total_signals,
            active_signals=active_signals,
            total_valid_signals=valid_signals,
            total_invalid_signals=invalid_signals,
            closed_signals=stats.get("closed_signals", 0),
            win_rate=stats.get("win_rate", 0.0),
            avg_rr_ratio=stats.get("avg_rr_ratio", 0.0),
            total_r_value=stats.get("total_r_value", 0.0),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get provider {provider_id}: {e}")
        raise HTTPException(500, "Failed to retrieve provider")


@router.get("/providers/{provider_id}/stats", response_model=ProviderStatsResponse)
async def get_provider_stats(provider_id: str):
    """
    Get detailed performance statistics for a provider.

    Path Parameters:
    - provider_id: The provider ID

    Returns comprehensive performance metrics including win rate, R-value, and streaks.
    """
    sb = get_supabase()

    try:
        # Fetch provider
        result = sb.table("providers").select("*").eq("id", provider_id).execute()
        if not result.data:
            raise HTTPException(404, f"Provider not found: {provider_id}")

        provider = result.data[0]

        # Calculate stats
        stats = calculate_provider_stats(sb, provider_id)

        return ProviderStatsResponse(
            provider_id=provider_id,
            name=provider["name"],
            **stats,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get provider stats for {provider_id}: {e}")
        raise HTTPException(500, "Failed to calculate provider stats")
