"""CADENCE runtime configuration.

Configuration is loaded from three layers, in order of decreasing precedence:

  1. Constructor kwargs / CLI flags (passed by `cadence.cli`).
  2. Environment variables prefixed `CADENCE_`.
  3. `~/.config/cadence/config.toml` (or `$XDG_CONFIG_HOME/cadence/config.toml`).
  4. Built-in defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import TomlConfigSettingsSource


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


CONFIG_PATH = _xdg_config_home() / "cadence" / "config.toml"


class Settings(BaseSettings):
    """Top-level runtime settings for CADENCE."""

    model_config = SettingsConfigDict(
        env_prefix="CADENCE_",
        env_file=None,
        toml_file=CONFIG_PATH,
        extra="ignore",
    )

    db_path: Path = Field(
        default_factory=lambda: _xdg_data_home() / "cadence" / "cadence.db",
        description="SQLite database path.",
    )
    cache_dir: Path = Field(
        default_factory=lambda: _xdg_cache_home() / "cadence",
        description="On-disk HTTP cache directory.",
    )
    rate_limit_per_host: float = Field(
        default=1.0,
        description="Maximum requests per second per upstream host.",
    )
    cache_ttl_stable_seconds: int = Field(
        default=86_400,
        description="Cache TTL for stable historical responses (default 24h).",
    )
    cache_ttl_current_seconds: int = Field(
        default=3_600,
        description="Cache TTL for current-state responses (default 1h).",
    )
    user_agent: str = Field(
        default="ne-cadence/0.1 (+https://github.com/north-echo/cadence)",
        description="HTTP User-Agent string sent to upstreams.",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
        )


def load() -> Settings:
    """Convenience factory used by non-CLI entry points."""
    return Settings()  # type: ignore[call-arg]


__all__: list[str] = ["CONFIG_PATH", "Settings", "load"]


# Silence "unused import" for type-only helpers that consumers may want.
_ = Any
