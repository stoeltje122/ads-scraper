"""Central configuration, loaded from .env / environment.

Everything tunable lives here so no other module reads os.environ directly.
Pulse shares the repo (and the .env file) with AdScout; Pulse-specific
variables are prefixed PULSE_, third-party credentials keep their own name.
ANTHROPIC_API_KEY is shared with AdScout on purpose (one key, one bill).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Base URL of the Meta Graph API (used by the comments adapter). The
# *version* is configurable via META_GRAPH_VERSION (.env), shared with
# AdScout so one bump fixes both tools.
GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v23.0"

# Cost-efficient default for classification work; override via
# PULSE_ANTHROPIC_MODEL in .env (AdScout's ANTHROPIC_MODEL is left alone —
# tagging ads and triaging feedback may want different models).
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# How far back the very first Gmail collect looks (days). After the first
# run Pulse continues from the last successful run.
DEFAULT_MAIL_BACKFILL_DAYS = 90

# Retention default (months). The *live* value is a user setting in the
# database (Beheer → retentie); this is only the initial value.
DEFAULT_RETENTION_MONTHS = 24


@dataclass
class Settings:
    data_dir: Path = Path("data")
    mailbox: str = "info@cloudplunge.com"
    mail_backfill_days: int = DEFAULT_MAIL_BACKFILL_DAYS
    meta_page_id: str = ""
    meta_page_token: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pulse.db"

    @property
    def gmail_credentials_path(self) -> Path:
        """OAuth client file downloaded from Google Cloud (see PULSE.md)."""
        return self.data_dir / "gmail" / "credentials.json"

    @property
    def gmail_token_path(self) -> Path:
        """Token written after the one-time login flow (`pulse verify gmail`)."""
        return self.data_dir / "gmail" / "token.json"

    @property
    def reports_dir(self) -> Path:
        return Path("reports")

    @property
    def logs_dir(self) -> Path:
        return Path("logs")

    @property
    def graph_base_url(self) -> str:
        return f"{GRAPH_API_BASE}/{self.graph_version}"


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Load settings from .env (if present) and the environment."""
    load_dotenv(env_file or ".env")

    def _env(name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()

    try:
        backfill = int(_env("PULSE_MAIL_BACKFILL_DAYS") or DEFAULT_MAIL_BACKFILL_DAYS)
    except ValueError:
        backfill = DEFAULT_MAIL_BACKFILL_DAYS
    return Settings(
        data_dir=Path(_env("PULSE_DATA_DIR", "data") or "data"),
        mailbox=_env("PULSE_MAILBOX") or "info@cloudplunge.com",
        mail_backfill_days=backfill,
        meta_page_id=_env("PULSE_META_PAGE_ID"),
        meta_page_token=_env("META_PAGE_TOKEN"),
        graph_version=_env("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION,
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        anthropic_model=_env("PULSE_ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
    )


def setup_logging(settings: Settings, verbose: bool = False) -> None:
    """Log to console and to logs/pulse.log (rotating)."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. under pytest or web reload)
        return
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "pulse.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)
