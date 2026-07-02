"""Source adapters — every feedback channel implements SourceAdapter."""

from pulse.sources.base import CredentialsError, SourceAdapter, SourceError
from pulse.sources.bol import BolAdapter
from pulse.sources.gmail import GmailAdapter
from pulse.sources.manual import ManualImportAdapter
from pulse.sources.meta_comments import MetaCommentsAdapter
from pulse.sources.trustpilot import TrustpilotAdapter

__all__ = [
    "SourceAdapter",
    "SourceError",
    "CredentialsError",
    "GmailAdapter",
    "MetaCommentsAdapter",
    "ManualImportAdapter",
    "TrustpilotAdapter",
    "BolAdapter",
    "build_adapter",
]


def build_adapter(type_, settings, fixture_dir=None):
    """The one place a source type string becomes an adapter instance.

    New channel? Add the adapter module, register it here, and add a row to
    store.STANDARD_SOURCES — nothing else changes (see PULSE.md).
    """
    adapters = {
        "gmail": GmailAdapter,
        "meta_comments": MetaCommentsAdapter,
        "trustpilot": TrustpilotAdapter,
        "bol": BolAdapter,
        "manual": ManualImportAdapter,
    }
    if type_ not in adapters:
        raise SourceError(f"Onbekend brontype: {type_}")
    return adapters[type_](settings, fixture_dir=fixture_dir)
