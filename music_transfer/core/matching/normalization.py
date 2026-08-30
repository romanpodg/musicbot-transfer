"""Reusable metadata normalization.

Normalization exists purely for comparison.  The original title and artist
strings always remain available on the domain :class:`~music_transfer.core
.domain.track.Track` (Invariant L); nothing here mutates a source track.

The rules below were chosen for real-world catalog differences:

* Unicode look-alikes and full-width characters (NFKC);
* case and whitespace;
* punctuation and the many Unicode dash variants;
* artist separators and ``feat.`` / ``ft.`` / ``featuring`` credits;
* bracketed qualifiers: ``(Remastered)``, ``[Live]``, ``- Radio Edit``;
* explicit labels and remaster/version labels.

Qualifiers are *extracted*, not discarded: a ``(Live)`` difference must reduce
confidence rather than be normalized away (see :mod:`music_transfer.core.
matching.scoring`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..domain import Track

#: The many dash-like characters that appear in music metadata.
_DASHES = "‐‑‒–—―−－"

#: Punctuation removed before comparison.  Word characters and spaces survive.
_NON_COMPARABLE = re.compile(r"[^\w\s]", re.UNICODE)

_WHITESPACE = re.compile(r"\s+", re.UNICODE)

#: Qualifiers that change which recording a title refers to.
VERSION_QUALIFIERS: tuple[str, ...] = (
    "remaster",
    "remastered",
    "remastered version",
    "re-master",
    "re-mastered",
    "live",
    "radio edit",
    "radio version",
    "edit",
    "extended",
    "extended version",
    "remix",
    "rework",
    "acoustic",
    "acoustic version",
    "instrumental",
    "demo",
    "mono",
    "stereo",
    "deluxe",
    "anniversary",
    "reissue",
    "re-recording",
    "rerecording",
    "cover",
    "version",
    "original mix",
    "sped up",
    "slowed",
    "nightcore",
    "8d audio",
    "explicit",
    "clean",
)

#: A bracketed or dashed qualifier such as ``(Remastered 2011)``.
_BRACKETED = re.compile(r"[(\[]([^)\]]*)[)\]]")

#: A trailing dashed qualifier such as ``Song - Live``.
_TRAILING_DASH = re.compile(r"\s+[-\u2013\u2014]\s+(?=[^\s]+)")
_TRAILING_DASH_CLASS = re.compile(rf"\s+[{re.escape(_DASHES)}-]\s+")

#: ``feat.`` / ``ft.`` / ``featuring`` / ``with`` / ``vs.`` markers.
#:
#: The trailing word boundary is applied *before* the optional period: after
#: ``feat.`` the next character is a space, which is not a word boundary, so a
#: naive ``\b`` after the group would stop the match at ``feat`` and leave the
#: period behind in the artist name.
_FEATURE_MARKER = re.compile(
    r"[\(\[]?\s*\b(?:featuring|feat|ft|versus|vs|with)\b\.?\s*[\)\]]?",
    re.IGNORECASE,
)

#: Separators inside one credited-artist string.
#:
#: ``/`` only separates when it is surrounded by whitespace, so names such as
#: ``AC/DC`` survive intact.  ``and`` is treated the same way for the same
#: reason (``Mary and the Boyfriends`` is one artist name).
_ARTIST_SEPARATORS = re.compile(
    r"\s*(?:,|;|&)\s*|\s+(?:and|/)\s+", re.IGNORECASE
)

#: A bracketed "explicit"/"clean" marker used by some platforms.
_EXPLICIT_MARKER = re.compile(r"\b(explicit|clean)\b", re.IGNORECASE)


def normalize_text(value: str | None) -> str:
    """Return a comparable form of ``value``.

    Applies NFKC normalization, case folding, dash unification, punctuation
    removal, and whitespace collapsing.  Returns an empty string for ``None``
    so callers never have to special-case missing metadata.
    """

    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    # Diacritics are folded away so "Björk" and "Bjork" compare equal.  This is
    # applied to the *comparison key* only: the original metadata is untouched.
    text = _strip_accents(text)
    text = "".join(" " if character in _DASHES else character for character in text)
    text = _NON_COMPARABLE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _strip_accents(value: str) -> str:
    """Remove combining marks, folding accented letters onto their base."""

    decomposed = unicodedata.normalize("NFD", value)
    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )


def normalize_light(value: str | None) -> str:
    """Return a lightly normalized form: case, dashes, whitespace only.

    Used for "exact metadata" comparison, where punctuation differences are
    tolerated but qualifiers are still significant.
    """

    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = "".join(" " if character in _DASHES else character for character in text)
    return _WHITESPACE.sub(" ", text).strip()


def find_version_qualifiers(title: str | None) -> tuple[str, ...]:
    """Return the version qualifiers present in a title.

    Both bracketed qualifiers (``Song (Live)``) and trailing dashed ones
    (``Song - Remastered``) are recognized.
    """

    if not title:
        return ()
    found: list[str] = []
    haystack = normalize_light(title)
    for match in _BRACKETED.finditer(haystack):
        found.extend(_match_qualifiers(match.group(1)))
    for part in _TRAILING_DASH_CLASS.split(haystack)[1:]:
        found.extend(_match_qualifiers(part))
    return tuple(dict.fromkeys(found))


def _match_qualifiers(text: str) -> list[str]:
    """Return every known qualifier contained in ``text``."""

    normalized = normalize_text(text)
    if not normalized:
        return []
    return [qualifier for qualifier in VERSION_QUALIFIERS if qualifier in normalized]


def strip_version_qualifiers(title: str | None) -> str:
    """Return the base title with bracketed and dashed qualifiers removed."""

    if not title:
        return ""
    text = normalize_light(title)
    text = _BRACKETED.sub(" ", text)
    text = _TRAILING_DASH_CLASS.split(text)[0]
    return normalize_text(text)


def split_artists(value: str | None) -> tuple[list[str], list[str]]:
    """Split a credited-artist string into ``(primary, featured)`` names.

    ``"Drake feat. Rihanna"`` returns ``(["Drake"], ["Rihanna"])``;
    ``"Simon & Garfunkel"`` returns ``(["Simon", "Garfunkel"], [])``.

    ``AC/DC`` is preserved because ``/`` only separates when it is surrounded
    by whitespace.
    """

    if not value:
        return ([], [])
    chunks = _FEATURE_MARKER.split(str(value))
    primary = _split_separators(chunks[0])
    featured: list[str] = []
    for chunk in chunks[1:]:
        featured.extend(_split_separators(chunk))
    return (_dedupe(primary), _dedupe(featured))


def _split_separators(value: str) -> list[str]:
    """Split one artist chunk on separators, keeping names with internal ``/``.

    Leftover brackets are removed as well: ``Song (feat. X)`` leaves ``X)``
    after the feature marker is consumed, and unbalanced brackets are never part
    of an artist name.
    """

    names = [name.strip().strip("()[]{}").strip() for name in _ARTIST_SEPARATORS.split(value)]
    return [name for name in names if name]


def _dedupe(values: list[str]) -> list[str]:
    """Remove duplicates case-insensitively while preserving order."""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def normalize_isrc(value: str | None) -> str | None:
    """Return a comparable ISRC, or ``None`` when the value is not usable.

    An ISRC is 12 characters: 2 country, 3 registrant, 2 year, 5 designation.
    Values that fail this shape are rejected rather than compared, because
    platforms sometimes put placeholder text in the field.
    """

    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    if len(cleaned) != 12:
        return None
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", cleaned):
        return None
    return cleaned


def detect_explicit_flag(track: Track) -> bool | None:
    """Return the explicit flag from the track, or infer it from the title."""

    if track.explicit is not None:
        return bool(track.explicit)
    match = _EXPLICIT_MARKER.search(track.title or "")
    if match is None:
        return None
    return match.group(1).lower() == "explicit"


@dataclass(frozen=True, slots=True)
class NormalizedTrack:
    """A comparison-ready projection of a :class:`Track`.

    The raw values are kept alongside the normalized ones so that a caller can
    always show or log what the platform actually said.
    """

    raw_title: str
    raw_artists: tuple[str, ...]
    title: str
    base_title: str
    version_qualifiers: tuple[str, ...]
    primary_artists: tuple[str, ...]
    featured_artists: tuple[str, ...]
    all_artists: tuple[str, ...]
    album_title: str
    raw_album_title: str
    duration_ms: int | None
    explicit: bool | None
    isrc: str | None

    @property
    def search_query(self) -> str:
        """Return a compact query string for a destination catalog search."""

        artist = self.primary_artists[0] if self.primary_artists else ""
        return f"{artist} {self.base_title}".strip()

    def as_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible values (diagnostics and tests)."""

        return {
            "raw_title": self.raw_title,
            "raw_artists": list(self.raw_artists),
            "title": self.title,
            "base_title": self.base_title,
            "version_qualifiers": list(self.version_qualifiers),
            "primary_artists": list(self.primary_artists),
            "featured_artists": list(self.featured_artists),
            "all_artists": list(self.all_artists),
            "album_title": self.album_title,
            "duration_ms": self.duration_ms,
            "explicit": self.explicit,
            "isrc": self.isrc,
        }


def normalize_track(track: Track) -> NormalizedTrack:
    """Project a track into its comparison-ready form.

    The input track is never modified, and the returned object carries both the
    original and normalized text.
    """

    raw_artists = tuple(track.artist_names)
    primary: list[str] = []
    featured: list[str] = []
    for name in raw_artists:
        chunk_primary, chunk_featured = split_artists(name)
        primary.extend(chunk_primary)
        featured.extend(chunk_featured)
    album_title = track.album.title if track.album is not None else ""
    return NormalizedTrack(
        raw_title=track.title,
        raw_artists=raw_artists,
        title=normalize_text(track.title),
        base_title=strip_version_qualifiers(track.title),
        version_qualifiers=find_version_qualifiers(track.title),
        primary_artists=tuple(normalize_text(name) for name in _dedupe(primary)),
        featured_artists=tuple(normalize_text(name) for name in _dedupe(featured)),
        all_artists=tuple(
            normalize_text(name) for name in _dedupe([*primary, *featured])
        ),
        album_title=normalize_text(album_title),
        raw_album_title=album_title,
        duration_ms=track.duration_ms,
        explicit=detect_explicit_flag(track),
        isrc=normalize_isrc(track.isrc),
    )
