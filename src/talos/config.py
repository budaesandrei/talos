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

    # 🔓 TLS certificate verification for LLM + web_fetch traffic.
    # Set TALOS_VERIFY_SSL=false ONLY if a corporate proxy re-signs your
    # traffic and you can't get its CA bundle. The better fix is to keep
    # this true and point the SSL_CERT_FILE env var at the proxy's CA.
    verify_ssl: bool = True

    # -- 🧠 Agent behaviour ----------------------------------------------
    # Max "super-steps" the agent loop may take before LangGraph raises
    # GraphRecursionError. One think->act round trip costs 2 steps.
    max_iterations: int = 50

    # 🗜️ Auto-compaction: when context usage crosses this fraction of the
    # model's max_input_tokens, fold older turns into a summary. Set to 0
    # to disable. keep_recent = verbatim messages kept after a compaction.
    compact_at: float = 0.70
    keep_recent: int = 6

    # ⌨️ Interjections: keep reading stdin WHILE the agent streams so you
    # can type status questions / stop mid-task. Off by default because it
    # requires a pinned prompt that some terminals render with flicker; the
    # default turn-based UI streams cleanly with native scrollback.
    interject: bool = False

    # 💭 Think mode: ask the model to reason in a <thinking> scratchpad
    # before answering (works on ANY model, not just reasoning models).
    # Rendered dim and never saved to history. /think toggles it live.
    think: bool = False

    # 🧠 Reasoning effort for thinking models (o-series, deepseek-r1,
    # claude with extended thinking via compat, …): low | medium | high.
    # Leave unset for non-reasoning models — providers reject unknown params.
    reasoning_effort: str | None = None

    # 🖥️ Which shell the `shell` tool uses: auto | powershell | pwsh |
    # cmd | bash | zsh | sh.  auto → PowerShell on Windows, $SHELL elsewhere.
    shell: str = "auto"

    # 📊 Print a dim per-turn token-usage footer (input/output/total).
    show_usage: bool = True

    # 🎨 Render assistant responses as markdown in the terminal (headings,
    # tables, syntax-highlighted code). Set false for raw text streaming.
    markdown: bool = True

    # 🗂️ Inject a cheap workspace snapshot (tree, git, README head) into
    # the system prompt so "what is this project?" is instantly answerable.
    workspace_snapshot: bool = True

    # 🔭 Emit OpenTelemetry spans (GenAI semantic conventions). Off = zero
    # overhead. Set OTEL_EXPORTER_OTLP_ENDPOINT to ship to a collector.
    trace: bool = False

    # 📦 Shell execution sandbox: "off" | "docker". docker runs each shell
    # command in a throwaway network-isolated container (zero overhead off).
    sandbox: str = "off"
    sandbox_image: str | None = None

    # 🛡️ Skip all permission prompts (same idea as kiro's --yolo /
    # claude's --dangerously-skip-permissions). CLI flag overrides this.
    yolo: bool = False

    # ⏱ Time-awareness: when a new user message arrives more than this
    # many minutes after the last message, inject a brief gap-notice
    # SystemMessage so the model knows the conversation is being resumed
    # rather than continued. Set to 0 to disable. The dim "gap noted"
    # line in the terminal shows the same to you.
    gap_minutes: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TALOS_",
        extra="ignore",
    )


# A single shared instance, imported everywhere else as
# ``from talos.config import settings``.
settings = Settings()
