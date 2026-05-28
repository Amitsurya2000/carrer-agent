"""App configuration, loaded from environment / .env via pydantic-settings."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path so .env always loads, no matter the current working directory
# (the server can be launched from anywhere — Startup folder, a service, etc.).
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    anthropic_api_key: str = ""
    supabase_url: str = ""
    supabase_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""

    @property
    def is_configured(self) -> bool:
        """True once Supabase credentials are present."""
        return bool(self.supabase_url and self.supabase_key)


settings = Settings()
