"""
Signal Bridge Configuration
Loads from environment variables / .env file via Pydantic Settings.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # --- Supabase ---
    supabase_url: str = "https://your-project.supabase.co"
    supabase_key: str = ""
    supabase_service_key: str = ""

    # --- API Security ---
    webhook_secret: str = "change-me-in-production"
    api_key_salt: str = "change-me-in-production"

    # --- Price Feeds ---
    binance_rest_url: str = "https://api.binance.com/api/v3"
    twelve_data_api_key: Optional[str] = None
    alpha_vantage_api_key: Optional[str] = None

    # --- Price Monitoring ---
    poll_interval_close: int = 5      # seconds
    poll_interval_mid: int = 15       # seconds
    poll_interval_far: int = 60       # seconds
    proximity_close: float = 0.002    # 0.2% distance ratio
    proximity_mid: float = 0.005      # 0.5% distance ratio

    # --- Notifications ---
    max_webhook_retries: int = 3
    webhook_timeout_seconds: int = 10

    # --- App ---
    log_level: str = "INFO"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Singleton
settings = Settings()
