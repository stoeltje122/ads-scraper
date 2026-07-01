"""Ad data sources (adapter pattern).

`AdSource` is the interface; `MetaAdLibraryAPI` is the first implementation.
A paid ad-intelligence provider can be added later as another AdSource
without touching the collector.
"""

from adscout.sources.base import AdSource, AdSourceError, TokenError
from adscout.sources.meta import MetaAdLibraryAPI
from adscout.sources.fixture import FixtureAdSource

__all__ = [
    "AdSource",
    "AdSourceError",
    "TokenError",
    "MetaAdLibraryAPI",
    "FixtureAdSource",
]
