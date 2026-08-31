"""Ordering: requested logical order versus actual write order.

These are two different things and conflating them is a common source of
"the transfer worked but my library is backwards" bugs::

    requested logical order  +  destination insertion behaviour
                            =  actual write order

If a destination shows the most recently added item at the top (``PREPEND``),
then writing items in the requested order produces the *reverse* of that order
on screen.  To obtain the requested visible order the executor must write in
reverse.

The ordering functions are generic over any item type: callers pass accessor
callables so this module never learns what a Track is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

from ..enums import InsertionBehavior, OrderingMode

Item = TypeVar("Item")

#: Accessor signature used to read a field used for sorting.
Accessor = Callable[[Any], Any]


def sort_key_for_date_added(value: Any) -> tuple[bool, str]:
    """Sort ISO-8601 dates lexically, placing missing dates last.

    Both parts of the key matter: the boolean pushes unknown values to the end
    regardless of sort direction, so "unknown" never masquerades as "oldest".
    """

    return (value is None, str(value or ""))


def sort_key_for_text(value: Any) -> tuple[bool, str]:
    """Sort text case-insensitively, placing empty values last."""

    text = str(value or "").casefold()
    return (not bool(text), text)


def apply_logical_order(
    items: Sequence[Item],
    mode: OrderingMode,
    *,
    date_added: Accessor | None = None,
    title: Accessor | None = None,
    artist: Accessor | None = None,
    album: Accessor | None = None,
) -> list[Item]:
    """Return items in the requested *logical* order.

    ``SOURCE_ORDER`` returns a copy unchanged, which is what a playlist needs:
    the source sequence is already the requested sequence.

    Sorts are stable, so equal keys keep their relative source order.

    Items whose sort value is missing are placed **last** in either direction.
    That is done by partitioning rather than by a flag in the sort key: a
    ``reverse=True`` sort would otherwise flip the flag and promote unknown
    values to the front, making "date unknown" look like "added most recently".
    """

    result = list(items)
    if mode is OrderingMode.SOURCE_ORDER:
        return result
    if mode in (OrderingMode.DATE_ADDED_NEWEST_FIRST, OrderingMode.DATE_ADDED_OLDEST_FIRST):
        newest_first = mode is OrderingMode.DATE_ADDED_NEWEST_FIRST
        dated = [item for item in result if _read(date_added, item) is not None]
        undated = [item for item in result if _read(date_added, item) is None]
        ordered = sorted(
            dated,
            key=lambda item: sort_key_for_date_added(_read(date_added, item)),
            reverse=newest_first,
        )
        return [*ordered, *undated]
    accessors = {
        OrderingMode.ALPHABETICAL: title,
        OrderingMode.ARTIST: artist,
        OrderingMode.ALBUM: album,
    }
    accessor = accessors.get(mode)
    if accessor is None:
        return result
    return sorted(result, key=lambda item: sort_key_for_text(_read(accessor, item)))


def to_write_order(
    items: Sequence[Item],
    mode: OrderingMode,
    insertion_behavior: InsertionBehavior,
    *,
    preserve_visible_order: bool = False,
    **accessors: Accessor,
) -> list[Item]:
    """Return the order in which items must actually be *written*.

    Args:
        items: The source items.
        mode: The requested logical order.
        insertion_behavior: How the destination visually orders new writes.
        preserve_visible_order: When ``True``, compensate for a ``PREPEND``
            destination by reversing the write order.  It defaults to ``False``
            so existing proven behaviour (write in logical order) is preserved
            unless a caller explicitly asks for compensation.

    Returns:
        The items in the order they should be sent to the destination.
    """

    logical = apply_logical_order(items, mode, **accessors)
    if not preserve_visible_order:
        return logical
    if insertion_behavior is InsertionBehavior.PREPEND:
        return list(reversed(logical))
    return logical


def restore_positions(items: Iterable[Item], position: Accessor | None = None) -> list[Item]:
    """Return items ordered by their recorded original position.

    Used when rebuilding a playlist at the destination: the executor stores
    ``original_position`` on every transfer item, so resume can restore the
    exact source sequence even after partial writes.
    """

    if position is None:
        return list(items)
    return sorted(items, key=position)


def _read(accessor: Accessor | None, item: Any) -> Any:
    """Read a sort field through an accessor, tolerating a missing accessor."""

    return accessor(item) if accessor is not None else None
