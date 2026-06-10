"""⚙️ Talos configuration.

Every knob lives here, loaded by pydantic-settings in priority order:

1. real environment variables  (``TALOS_BASE_URL=... talos chat``)
2. the ``.env`` file in the current directory
3. the defaults below

The ``TALOS_`` prefix is stripped automatically, so ``TALOS_MODEL`` in the
environment becomes ``settings.model`` in code.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # -- 🔌 LLM connection (any OpenAI-compatible endpoint) --------------
    # Examples:
    #   OpenAI      -> base_url unset (uses api.openai.com)
    #   Anthropic   -> https://api.anthropic.com/v1/
    #   OpenRouter  -> https://openrouter.ai/api/v1
    #   Ollama      -> http://localhost:11434/v1
    base_url: str | None = None
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.0

    # -- 🧠 Agent behaviour ----------------------------------------------
    # Max "super-steps" the agent loop may take before LangGraph raises
    # GraphRecursionError. One think->act round trip costs 2 steps.
    max_iterations: int = 50

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TALOS_",
        extra="ignore",
    )


# A single shared instance, imported everywhere else as
# ``from talos.config import settings``.
settings = Settings()
