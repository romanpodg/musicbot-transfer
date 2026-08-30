"""Offset pagination for TIDAL.

Invariant A -- a short, non-empty page does **not** mean end of data.

TIDAL can return fewer items than requested for a page that is not the last
one (rights filtering, deduplication, or backend sharding).  The previously
correct-looking shortcut::

    if len(page) < page_size:
        return values

silently truncates a library.  It also advanced the offset by ``len(page)``,
which walks off the page grid and can re-request overlapping or garbage ranges.

The rule implemented here is the safe one::

    request page at offset
    if page is empty: stop
    process items
    offset += requested_page_size

Termination therefore happens on an **empty page** or on an explicit safety
bound -- never on a short page.

Additional protections kept from the proven implementation and extended here:

* repeated identical pages are detected and rejected (a server ignoring
  ``offset`` would otherwise loop forever);
* a maximum page count bounds a misbehaving endpoint;
* a maximum item count bounds runaway growth.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...core.errors import PaginationError

_LOGGER = logging.getLogger("music_transfer.platforms.tidal.pagination")

#: Signature of a paginated TIDAL getter: ``getter(limit=..., offset=...)``.
PageGetter = Callable[..., Any]

#: Signature of a per-page progress callback: ``(collected, pages)``.
PageProgress = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class PaginationPolicy:
    """Bounds and page size for one paginated read."""

    page_size: int = 50
    max_pages: int = 10_000
    max_items: int = 1_000_000


#: Defaults matching the proven per-section page sizes.
DEFAULT_POLICY = PaginationPolicy(page_size=50)
PLAYLIST_POLICY = PaginationPolicy(page_size=100)


def _fingerprint(page: list[Any]) -> tuple[str, ...]:
    """Return a stable identity for a page, used to detect repeats.

    Object identity is not stable across calls, so identifiers are used.  A page
    without identifiers is fingerprinted by its length combined with its
    position-free repr, which still catches a server replaying the same page.
    """

    identifiers: list[str] = []
    for item in page:
        identifier = getattr(item, "id", None)
        identifiers.append(str(identifier) if identifier is not None else repr(item))
    return tuple(identifiers)


def fetch_all(
    getter: PageGetter,
    *,
    policy: PaginationPolicy = DEFAULT_POLICY,
    operation: str = "paginated_read",
    progress: PageProgress | None = None,
    logger: logging.Logger | None = None,
) -> list[Any]:
    """Collect every item of a TIDAL offset-paginated collection.

    Args:
        getter: Called as ``getter(limit=page_size, offset=offset)``.
        policy: Page size and safety bounds.
        operation: Short name used in log events.
        progress: Optional callback invoked after each non-empty page.
        logger: Logger override.

    Returns:
        Every collected item, in page order.

    Raises:
        PaginationError: If the endpoint stops making progress or exceeds a
            configured bound.  Raising is deliberate: silently returning a
            truncated library is worse than a visible failure.
    """

    log = logger or _LOGGER
    values: list[Any] = []
    seen: set[tuple[str, ...]] = set()
    offset = 0
    pages = 0
    while pages < policy.max_pages:
        page = getter(limit=policy.page_size, offset=offset)
        page_items = list(page) if page else []
        pages += 1
        if not page_items:
            log.info(
                "event=pagination_finished operation=%s pages=%d items=%d reason=empty_page",
                operation,
                pages,
                len(values),
            )
            return values
        fingerprint = _fingerprint(page_items)
        if fingerprint in seen:
            log.error(
                "event=pagination_repeated_page operation=%s offset=%d items=%d",
                operation,
                offset,
                len(page_items),
            )
            raise PaginationError("pagination_repeated_page")
        seen.add(fingerprint)
        values.extend(page_items)
        if progress is not None:
            progress(len(values), pages)
        if len(values) >= policy.max_items:
            log.error(
                "event=pagination_limit_exceeded operation=%s items=%d", operation, len(values)
            )
            raise PaginationError("pagination_max_items_exceeded")
        # Advance by the *requested* page size: a short page is not the last
        # page, so the next request must land on the next page boundary.
        offset += policy.page_size
    log.error("event=pagination_limit_exceeded operation=%s pages=%d", operation, pages)
    raise PaginationError("pagination_max_pages_exceeded")
