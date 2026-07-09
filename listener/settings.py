"""Environment + tunables. Loads .env for local runs; in CI the values come from
GitHub Actions encrypted secrets. No secret is ever hard-coded (engineering standards)."""

from __future__ import annotations
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()  # local .env; no-op in CI where env is injected
except Exception:
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- Source mode: "rss" (no auth, the default) or "oauth" (if you ever get keys) ---
    source_mode: str = os.environ.get("SOURCE_MODE", "rss").lower()

    # RSS fetching — combined multi-sub feeds (r/a+b+c/new/.rss) = far fewer requests, no 429s
    rss_group_size: int = _i("RSS_GROUP_SIZE", 12)            # subs per combined request (24 subs -> 2 requests)
    rss_combined_limit: int = _i("RSS_COMBINED_LIMIT", 100)    # posts per combined feed (max 100)
    rss_delay_seconds: float = _f("RSS_DELAY_SECONDS", 5.0)    # pause between the (few) groups
    rss_backoff_seconds: float = _f("RSS_BACKOFF_SECONDS", 8.0)   # initial 429/403 backoff
    rss_jitter_seconds: float = _f("RSS_JITTER_SECONDS", 3.0)  # random jitter added to every wait
    rss_max_retries: int = _i("RSS_MAX_RETRIES", 3)
    # HARD cap on total fetch time so a fully-throttled IP fails fast + clean, never hangs past
    # the CI job timeout. Past this, stop retrying and use whatever we got (fail-open).
    rss_deadline_seconds: float = _f("RSS_DEADLINE_SECONDS", 240.0)

    # --- Reddit OAuth (only used when SOURCE_MODE=oauth) ---
    reddit_client_id: str = os.environ.get("REDDIT_CLIENT_ID", "")
    reddit_client_secret: str = os.environ.get("REDDIT_CLIENT_SECRET", "")
    reddit_username: str = os.environ.get("REDDIT_USERNAME", "")
    reddit_password: str = os.environ.get("REDDIT_PASSWORD", "")
    reddit_user_agent: str = os.environ.get(
        "REDDIT_USER_AGENT", "utniv-listener/1.0 (knowledge-loss research)"
    )

    # --- Supabase (shared project, reddit_* tables; service-role key, server-only) ---
    supabase_url: str = os.environ.get("SUPABASE_URL", "")
    supabase_service_key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # --- LLM scoring: OpenAI primary (user credits), NVIDIA NIM fallback ---
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
    openai_model: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    nim_api_key: str = os.environ.get("NIM_API_KEY", "")
    nim_base_url: str = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    nim_model: str = os.environ.get("NIM_MODEL", "meta/llama-3.3-70b-instruct")

    # --- Slack ---
    slack_webhook_url: str = os.environ.get("SLACK_WEBHOOK_URL", "")

    # --- tunables ---
    fetch_limit: int = _i("FETCH_LIMIT", 50)          # posts per sub per /new pull
    alert_threshold: int = _i("ALERT_THRESHOLD", 60)  # composite >= this -> Slack
    hot_pain_floor: int = _i("HOT_PAIN_FLOOR", 80)    # pain >= this alerts even under threshold
    reply_icp_floor: int = _i("REPLY_ICP_FLOOR", 40)  # LLM said "reply" + icp >= this -> always alert
    search_enabled: bool = os.environ.get("SEARCH_ENABLED", "false").lower() == "true"
    max_age_hours: float = _f("MAX_AGE_HOURS", 48.0)  # ignore posts older than this (decay insight)

    # composite weights (must roughly sum to 1.0)
    w_icp: float = _f("W_ICP", 0.40)
    w_pain: float = _f("W_PAIN", 0.25)
    w_decay: float = _f("W_DECAY", 0.15)
    w_velocity: float = _f("W_VELOCITY", 0.10)
    w_engagement: float = _f("W_ENGAGEMENT", 0.05)
    w_promo: float = _f("W_PROMO", 0.05)

    def validate(self) -> list[str]:
        """Return a list of missing-but-required settings (empty = good to run)."""
        missing = []
        if self.source_mode == "oauth":
            if not self.reddit_client_id: missing.append("REDDIT_CLIENT_ID")
            if not self.reddit_client_secret: missing.append("REDDIT_CLIENT_SECRET")
        # rss mode needs no Reddit credentials at all
        if not self.supabase_url: missing.append("SUPABASE_URL")
        if not self.supabase_service_key: missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.slack_webhook_url: missing.append("SLACK_WEBHOOK_URL")
        if not self.openai_api_key and not self.nim_api_key:
            missing.append("OPENAI_API_KEY or NIM_API_KEY")
        return missing


settings = Settings()
