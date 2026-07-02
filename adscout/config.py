"""Central configuration, loaded from .env / environment.

Everything tunable lives here so no other module reads os.environ directly.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Base URL of the Meta Graph API. The *version* is configurable via
# META_GRAPH_VERSION (.env); bump it there when Meta deprecates a version.
GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v23.0"

# Fields requested from /ads_archive. Commercial ads never return
# spend/impressions, so we don't ask for them; runtime is our winner metric.
ADS_ARCHIVE_FIELDS = [
    "id",
    "page_id",
    "page_name",
    "ad_creation_time",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_creative_bodies",
    "ad_creative_link_titles",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_snapshot_url",
    "publisher_platforms",
    "languages",
    "eu_total_reach",
    "beneficiary_payers",
    "target_ages",
    "target_gender",
    "target_locations",
]


def parse_countries(value: str | None) -> list[str]:
    """The one way to parse a comma-separated country string ('nl, be' → ['NL','BE'])."""
    return [c.strip().upper() for c in (value or "").split(",") if c.strip()]


@dataclass
class Settings:
    meta_access_token: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    data_dir: Path = Path("data")
    default_countries: list[str] = field(default_factory=lambda: ["NL"])
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "adscout.db"

    @property
    def creatives_dir(self) -> Path:
        return self.data_dir / "creatives"

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
    countries = parse_countries(os.environ.get("ADSCOUT_DEFAULT_COUNTRIES", "NL"))
    return Settings(
        meta_access_token=os.environ.get("META_ACCESS_TOKEN", "").strip(),
        graph_version=os.environ.get("META_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip()
        or DEFAULT_GRAPH_VERSION,
        data_dir=Path(os.environ.get("ADSCOUT_DATA_DIR", "data")),
        default_countries=countries or ["NL"],
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip(),
    )


def setup_logging(settings: Settings, verbose: bool = False) -> None:
    """Log to console and to logs/adscout.log (rotating)."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    # httpx logs full request URLs at INFO — with Meta that includes the
    # access token. Keep it at WARNING so no secret reaches console or file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root = logging.getLogger()
    if root.handlers:  # already configured (e.g. under pytest or web reload)
        return
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.logs_dir / "adscout.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)
