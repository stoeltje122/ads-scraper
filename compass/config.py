"""Central configuration, loaded from .env / environment.

Everything tunable lives here so no other module reads os.environ directly.
Compass shares the repo-root .env with AdScout: META_ACCESS_TOKEN and
META_GRAPH_VERSION are deliberately the same variables (one Meta app, one
token to renew), the rest is Compass-specific.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_GRAPH_VERSION = "v23.0"

BOL_API_BASE = "https://api.bol.com"
BOL_TOKEN_URL = "https://login.bol.com/token"

# Shopify Admin REST API version. One place to bump when Shopify retires it
# (they support each version for 12 months).
DEFAULT_SHOPIFY_API_VERSION = "2025-04"


@dataclass
class Settings:
    # Shopify (read-only custom app token)
    shopify_shop: str = ""            # e.g. cloudplunge.myshopify.com
    shopify_access_token: str = ""
    shopify_api_version: str = DEFAULT_SHOPIFY_API_VERSION
    # Meta Marketing API (own ad account; token shared with AdScout's app)
    meta_access_token: str = ""
    meta_ad_account_id: str = ""      # e.g. act_1234567890
    graph_version: str = DEFAULT_GRAPH_VERSION
    # bol Retailer API (client credentials)
    bol_client_id: str = ""
    bol_client_secret: str = ""
    # Storage
    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "compass.db"

    @property
    def demo_db_path(self) -> Path:
        """Separate database for `compass demo`: fixture data can never
        end up between real orders."""
        return self.data_dir / "compass-demo.db"

    @property
    def reports_dir(self) -> Path:
        return Path("reports")

    @property
    def logs_dir(self) -> Path:
        return Path("logs")

    @property
    def graph_base_url(self) -> str:
        return f"{GRAPH_API_BASE}/{self.graph_version}"

    @property
    def shopify_base_url(self) -> str:
        return f"https://{self.shopify_shop}/admin/api/{self.shopify_api_version}"

    def has_shopify(self) -> bool:
        return bool(self.shopify_shop and self.shopify_access_token)

    def has_meta(self) -> bool:
        return bool(self.meta_access_token and self.meta_ad_account_id)

    def has_bol(self) -> bool:
        return bool(self.bol_client_id and self.bol_client_secret)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    """Load settings from .env (if present) and the environment."""
    load_dotenv(env_file or ".env")
    env = os.environ
    return Settings(
        shopify_shop=env.get("SHOPIFY_SHOP", "").strip().removeprefix("https://").strip("/"),
        shopify_access_token=env.get("SHOPIFY_ACCESS_TOKEN", "").strip(),
        shopify_api_version=env.get("SHOPIFY_API_VERSION", DEFAULT_SHOPIFY_API_VERSION).strip()
        or DEFAULT_SHOPIFY_API_VERSION,
        meta_access_token=env.get("META_ACCESS_TOKEN", "").strip(),
        meta_ad_account_id=env.get("META_AD_ACCOUNT_ID", "").strip(),
        graph_version=env.get("META_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip()
        or DEFAULT_GRAPH_VERSION,
        bol_client_id=env.get("BOL_CLIENT_ID", "").strip(),
        bol_client_secret=env.get("BOL_CLIENT_SECRET", "").strip(),
        data_dir=Path(env.get("COMPASS_DATA_DIR", "data")),
    )


def setup_logging(settings: Settings, verbose: bool = False) -> None:
    """Log to console and to logs/compass.log (rotating)."""
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
        settings.logs_dir / "compass.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)
