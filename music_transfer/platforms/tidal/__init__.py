"""The TIDAL platform adapter package.

Only ``client.py`` imports ``tidalapi``; everything else depends on this
package's own types, so a provider upgrade stays contained.
"""

from __future__ import annotations

from .adapter import TidalAdapter, build_tidal_adapter
from .auth import AccountRole, TidalAuthenticator
from .client import TidalLibraryClient
from .errors import ItemUnavailableError, TidalClientError, translate_provider_error
from .pagination import DEFAULT_POLICY, PLAYLIST_POLICY, PaginationPolicy, fetch_all

__all__ = [
    "AccountRole",
    "DEFAULT_POLICY",
    "PLAYLIST_POLICY",
    "ItemUnavailableError",
    "PaginationPolicy",
    "TidalAdapter",
    "TidalAuthenticator",
    "TidalClientError",
    "TidalLibraryClient",
    "build_tidal_adapter",
    "fetch_all",
    "translate_provider_error",
]
