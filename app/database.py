"""
Supabase client singleton.
Uses service_role key for server-side operations (bypasses RLS).
"""

from supabase import create_client, Client
from app.config import settings

_client: Client | None = None


def get_supabase() -> Client:
    """Get or create the Supabase client (service-role for backend)."""
    global _client
    if _client is None:
        _client = create_client(
            settings.supabase_url,
            settings.supabase_service_key or settings.supabase_key,
        )
    return _client


def get_supabase_anon() -> Client:
    """Get a Supabase client with the anon key (respects RLS)."""
    return create_client(settings.supabase_url, settings.supabase_key)
