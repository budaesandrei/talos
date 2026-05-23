from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT_PATH = PACKAGE_ROOT / "prompts" / "system.md"


class Settings(BaseSettings):
    app_name: str = "talos"
    env: str = "dev"
    anthropic_api_key: str | None = None
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TALOS_",
        extra="ignore",
    )

settings = Settings()