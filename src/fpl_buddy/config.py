"""Central configuration. Everything is env-driven so the container needs no code changes."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # ---------------------------------------------------------------- identity
    fpl_email: str = Field(default="", description="Premier League account email.")
    fpl_password: SecretStr = Field(default=SecretStr(""))
    fpl_entry_id: int = Field(default=0, description="Your FPL manager/entry id.")

    # Fallback when programmatic login is blocked: paste the cookie header from
    # DevTools (Network -> any /api/me/ request -> Request Headers -> cookie).
    fpl_cookie_header: SecretStr = Field(default=SecretStr(""))

    # ------------------------------------------------------------------- azure
    azure_openai_endpoint: str = Field(default="")
    azure_openai_api_key: SecretStr = Field(default=SecretStr(""))
    azure_openai_deployment: str = Field(default="gpt-4.1")
    azure_openai_api_version: str = Field(default="2024-10-21")
    # If empty, falls back to key auth. Set to "managed_identity" on Container Apps.
    azure_openai_auth: Literal["api_key", "managed_identity"] = "api_key"

    # ------------------------------------------------------------------- state
    # "file" keeps proposals on the container filesystem (use a mounted volume),
    # "azure_table" uses Azure Table Storage (survives revisions/restarts).
    state_backend: Literal["file", "azure_table"] = "file"
    state_dir: str = "./.state"
    azure_table_connection_string: SecretStr = Field(default=SecretStr(""))
    azure_table_name: str = "fplproposals"

    # -------------------------------------------------------------- behaviour
    propose_hours_before_deadline: float = Field(
        default=36.0, description="When to generate the proposal."
    )
    commit_minutes_before_deadline: float = Field(
        default=45.0, description="Auto-execute an untouched proposal this long before the deadline."
    )
    max_points_hit: int = Field(
        default=0,
        description="Hard ceiling on points spent on extra transfers. 0 = never take a hit.",
    )
    auto_commit_enabled: bool = Field(
        default=True,
        description="If false, a proposal that nobody touches simply expires.",
    )
    execute_on_approval: bool = Field(
        default=True,
        description=(
            "Submit immediately when you approve. Set false to hold an approved "
            "proposal until the commit job, which re-validates against fresher data."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description="When true, no POST ever reaches FPL. Flip to false only once you trust it.",
    )
    min_captain_confidence: float = Field(
        default=0.0,
        description="Skip auto-commit if the agent's confidence is below this (0-1).",
    )
    fixture_horizon_gameweeks: int = Field(
        default=5,
        ge=1,
        description=(
            "How many gameweeks of fixtures to show the agent. A transfer is "
            "judged over a run of fixtures, not just the one being submitted."
        ),
    )

    # ------------------------------------------------------------- knowledge
    knowledge_sources_file: str = Field(
        default="",
        description=(
            "Path to the YAML file listing article sources to harvest. Empty "
            "disables harvesting entirely."
        ),
    )
    knowledge_harvest_hour: int = Field(
        default=5,
        ge=0,
        le=23,
        description="Local hour for the daily article harvest.",
    )
    knowledge_index_days: int = Field(
        default=10,
        ge=1,
        description="Only articles this recent appear in the brief's index.",
    )
    knowledge_index_limit: int = Field(
        default=15,
        ge=1,
        description="Most articles to list in the brief. Detail is loaded by tool.",
    )

    knowledge_fetch_backends: str = Field(
        default="firecrawl,scrapling,httpx",
        description=(
            "Ordered, comma-separated list of ways to fetch an article. Each is "
            "skipped when unavailable, so this can name backends you have not "
            "installed. httpx always works and should stay last."
        ),
    )
    firecrawl_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Enables the Firecrawl backend. Free tier is 1000 credits/month.",
    )
    firecrawl_credit_reserve: int = Field(
        default=50,
        ge=0,
        description=(
            "Stop using Firecrawl with this many credits left, so a harvest "
            "cannot spend the last of the month's budget."
        ),
    )
    scrapling_stealth: bool = Field(
        default=False,
        description=(
            "Use Scrapling's browser-based StealthyFetcher instead of its plain "
            "one. Needs `scrapling install` to download browsers first."
        ),
    )

    @property
    def has_knowledge(self) -> bool:
        return bool(self.knowledge_sources_file)

    # ------------------------------------------------------------ notification
    notify_channel: Literal["none", "smtp", "webhook", "log", "discord"] = "log"
    public_base_url: str = Field(
        default="http://localhost:8080",
        description="Public URL of this service; used to build approve/reject links.",
    )
    approval_secret: SecretStr = Field(
        default=SecretStr("change-me"),
        description="Signs approval links. Rotate it and old links die.",
    )
    # At least 1: a zero TTL reads like "expire immediately" but itsdangerous
    # treats it as "no age limit at the moment of signing", which is the opposite.
    approval_link_ttl_hours: int = Field(default=72, ge=1)
    api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "When set, read endpoints require this in the X-API-Key header. "
            "Mutating endpoints always need a signed approval token regardless."
        ),
    )

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = Field(default=SecretStr(""))
    smtp_from: str = ""
    smtp_to: str = ""
    webhook_url: str = ""

    # ---------------------------------------------------------------- discord
    discord_bot_token: SecretStr = Field(default=SecretStr(""))
    discord_channel_id: int = Field(
        default=0, description="Channel the bot posts proposals and approval buttons to."
    )

    # ------------------------------------------------------------------- data
    solio_url: str = "https://fpl.solioanalytics.com/api/data/latest.json"
    fpl_api_base: str = "https://fantasy.premierleague.com/api"
    fpl_login_url: str = "https://users.premierleague.com/accounts/login/"
    http_timeout_seconds: float = 30.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # ------------------------------------------------------------------- misc
    log_level: str = "INFO"
    port: int = 8080
    timezone: str = "Asia/Kolkata"

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """An empty environment variable falls back to the default.

        A ``.env`` template with ``FPL_ENTRY_ID=`` left blank, or a host that
        injects an unset secret as an empty string, would otherwise fail integer
        parsing with an opaque pydantic error at import time -- instead of the
        clear "no entry id configured" message from the code that needs it.

        This is also the safe direction for the switches that matter: a blank
        ``DRY_RUN`` becomes ``True``, not ``False``.
        """
        if isinstance(data, dict):
            return {
                key: value
                for key, value in data.items()
                if not (isinstance(value, str) and not value.strip())
            }
        return data

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def has_login_credentials(self) -> bool:
        return bool(self.fpl_email and self.fpl_password.get_secret_value())

    @property
    def has_cookie_header(self) -> bool:
        return bool(self.fpl_cookie_header.get_secret_value())

    @property
    def has_discord(self) -> bool:
        return bool(self.discord_bot_token.get_secret_value() and self.discord_channel_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
