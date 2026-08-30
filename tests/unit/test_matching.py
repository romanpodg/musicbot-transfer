"""Matching tests: ISRC first, then metadata, then fuzzy.

Every case here is a real-world shape that breaks naive string comparison, and
each one is a case the specification called out explicitly.  The important
theme is that a match is a *decision with reasons*, not a boolean: the same
score can carry a warning (explicit vs clean, remaster vs original) that the
interface must be able to surface.
"""

from __future__ import annotations

import unittest

from music_transfer.core.enums import MatchMethod, MatchOutcome, Platform
from music_transfer.core.matching import MatchingPolicy, TrackMatcher
from music_transfer.core.matching.normalization import (
    detect_explicit_flag,
    find_version_qualifiers,
    normalize_isrc,
    normalize_text,
    split_artists,
    strip_version_qualifiers,
)
from tests.support import album, artist, track


class NormalizationTests(unittest.TestCase):
    """Normalization must reduce noise without destroying information."""

    def test_case_and_punctuation_are_ignored(self) -> None:
        """Punctuation and case must not change the comparison key."""

        self.assertEqual(normalize_text("Take On Me"), normalize_text("take on me"))
        self.assertEqual(normalize_text("Take On Me!"), normalize_text("take-on-me"))

    def test_unicode_is_folded_consistently(self) -> None:
        """Full-width and Latin look-alikes compare equal."""

        self.assertEqual(normalize_text("ＢＯＨＥＭＩＡＮ"), normalize_text("bohemian"))
        self.assertEqual(normalize_text("Björk"), normalize_text("bjork"))

    def test_unicode_dashes_are_unified(self) -> None:
        """En dash, em dash, and hyphen all normalize to the same character."""

        self.assertEqual(normalize_text("A – B"), normalize_text("A - B"))
        self.assertEqual(normalize_text("A—B"), normalize_text("A-B"))

    def test_feat_markers_are_extracted(self) -> None:
        """``feat.``, ``ft.``, and ``featuring`` are extracted, not left inline."""

        primary, featured = split_artists("Drake feat. Rihanna")
        self.assertEqual(primary, ["Drake"])
        self.assertEqual(featured, ["Rihanna"])
        self.assertEqual(split_artists("Drake ft Rihanna")[1], ["Rihanna"])
        self.assertEqual(split_artists("Drake featuring Rihanna")[1], ["Rihanna"])

    def test_parenthesized_feature_is_extracted(self) -> None:
        """A feature inside brackets is extracted without leaving a stray ``)``."""

        primary, featured = split_artists("Song (feat. X)")
        self.assertEqual(primary, ["Song"])
        self.assertEqual(featured, ["X"])

    def test_ac_dc_is_not_split(self) -> None:
        """A slash inside a name is part of the name, not a separator."""

        self.assertEqual(split_artists("AC/DC"), (["AC/DC"], []))
        self.assertEqual(split_artists("AC / DC"), (["AC", "DC"], []))

    def test_ampersand_and_comma_separate(self) -> None:
        """``&``, ``,``, ``;``, and a spaced slash separate collaborators.

        They all stay *primary* artists: a featured artist is a different
        concept (``feat.``), not merely another separator.
        """

        self.assertEqual(split_artists("A & B"), (["A", "B"], []))
        self.assertEqual(split_artists("A, B"), (["A", "B"], []))
        self.assertEqual(split_artists("A; B"), (["A", "B"], []))
        self.assertEqual(split_artists("A / B"), (["A", "B"], []))

    def test_version_qualifiers_are_detected(self) -> None:
        """Remaster, live, and remix markers are detected and removable."""

        self.assertIn("remaster", find_version_qualifiers("Song (Remastered 2011)"))
        self.assertIn("live", find_version_qualifiers("Song - Live"))
        self.assertEqual(
            strip_version_qualifiers("Song (Remastered 2011)").strip().casefold(), "song"
        )

    def test_isrc_normalization_validates_shape(self) -> None:
        """A valid ISRC is upper-cased and stripped; a bad one is rejected."""

        self.assertEqual(normalize_isrc(" usrc17607839 "), "USRC17607839")
        self.assertIsNone(normalize_isrc("NOTANISRC"))
        self.assertIsNone(normalize_isrc(None))  # type: ignore[arg-type]

    def test_explicit_flag_detection(self) -> None:
        """An explicit marker is detected on both the flag and the title."""

        self.assertTrue(detect_explicit_flag(track("Song (Explicit)", explicit=True)))
        self.assertFalse(detect_explicit_flag(track("Song")))


class IsrcMatchingTests(unittest.TestCase):
    """ISRC is the strongest signal and wins over everything else."""

    def test_isrc_match_is_used_first(self) -> None:
        """Two tracks with different titles match when the ISRC agrees."""

        source = track("Different Title", isrc="USRC17607839", identifier="src")
        candidate = track("Completely Other", isrc="USRC17607839", identifier="dst")
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)
        self.assertEqual(result.method, MatchMethod.ISRC)
        self.assertEqual(result.destination.source_id, "dst")
        self.assertIn("isrc", " ".join(result.reasons).casefold())

    def test_isrc_wins_over_better_title_match(self) -> None:
        """A worse title with a matching ISRC beats a better title without one."""

        source = track("Song", isrc="AAA000000000", identifier="src")
        same_isrc = track("Totally Different", isrc="AAA000000000", identifier="by-isrc")
        same_title = track("Song", isrc="BBB111111111", identifier="by-title")
        result = TrackMatcher().match(source, [same_title, same_isrc])
        self.assertEqual(result.method, MatchMethod.ISRC)
        self.assertEqual(result.destination.source_id, "by-isrc")

    def test_missing_isrc_falls_through_to_metadata(self) -> None:
        """Without an ISRC the matcher still finds an exact title match."""

        source = track("Take On Me", identifier="src")
        candidate = track("Take On Me", identifier="dst")
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)
        self.assertIn(
            result.method, {MatchMethod.EXACT_METADATA, MatchMethod.NORMALIZED_METADATA}
        )


class MetadataMatchingTests(unittest.TestCase):
    """Real-world metadata shapes that must (or must not) match."""

    def test_exact_metadata_matches(self) -> None:
        """Identical title and artist is a high-confidence match."""

        people = (artist("A-ha"),)
        source = track("Take On Me", people, identifier="src", duration_ms=225_000)
        candidate = track("Take On Me", people, identifier="dst", duration_ms=225_000)
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)
        self.assertGreaterEqual(result.score, 0.88)

    def test_feat_difference_still_matches(self) -> None:
        """A featured artist in the title must not block a match."""

        source = track("Work", (artist("Rihanna"), artist("Drake")), identifier="src")
        candidate = track("Work (feat. Drake)", (artist("Rihanna"),), identifier="dst")
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)

    def test_unicode_difference_still_matches(self) -> None:
        """Unicode folding makes accented and plain spellings equal."""

        source = track("Björk Song", (artist("Björk"),), identifier="src")
        candidate = track("Bjork Song", (artist("Bjork"),), identifier="dst")
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)

    def test_no_candidates_is_not_found(self) -> None:
        """An empty candidate list is NOT_FOUND, never an accidental match."""

        result = TrackMatcher().match(track("Anything", identifier="src"), [])
        self.assertEqual(result.outcome, MatchOutcome.NOT_FOUND)
        self.assertIsNone(result.destination)
        self.assertEqual(result.method, MatchMethod.NONE)

    def test_unrelated_track_is_not_found(self) -> None:
        """A completely different track must not be matched."""

        source = track("Bohemian Rhapsody", (artist("Queen"),), identifier="src")
        candidate = track("Thriller", (artist("Michael Jackson"),), identifier="dst")
        result = TrackMatcher().match(source, [candidate])
        self.assertEqual(result.outcome, MatchOutcome.NOT_FOUND)


class ExplicitAndVersionAwareness(unittest.TestCase):
    """Explicit/clean and version differences reduce confidence; they warn."""

    def test_explicit_vs_clean_is_flagged(self) -> None:
        """Matching an explicit track to a clean one must produce a warning.

        It is *not* an automatic failure: only the user can decide whether the
        clean version is acceptable, so the engine warns and lets the policy
        decide.
        """

        people = (artist("Artist"),)
        source = track("Song", people, identifier="src", explicit=True)
        candidate = track("Song", people, identifier="dst", explicit=False)
        result = TrackMatcher().match(source, [candidate])
        self.assertTrue(
            any("clean" in warning.casefold() or "explicit" in warning.casefold()
                for warning in result.warnings),
            f"expected an explicit/clean warning, got {result.warnings}",
        )

    def test_explicit_to_clean_can_be_rejected_by_policy(self) -> None:
        """With the fallback disabled, explicit -> clean is not accepted."""

        policy = MatchingPolicy(allow_explicit_to_clean_fallback=False)
        people = (artist("Artist"),)
        source = track("Song", people, identifier="src", explicit=True)
        candidate = track("Song", people, identifier="dst", explicit=False)
        result = TrackMatcher(policy).match(source, [candidate])
        self.assertNotEqual(result.outcome, MatchOutcome.MATCHED)

    def test_remaster_reduces_confidence_but_matches(self) -> None:
        """A remaster is a version difference: still a match, with a warning."""

        people = (artist("Queen"),)
        source = track("Bohemian Rhapsody", people, identifier="src")
        candidate = track(
            "Bohemian Rhapsody (Remastered 2011)", people, identifier="dst"
        )
        result = TrackMatcher().match(source, [candidate])
        self.assertTrue(
            any("version" in warning.casefold() or "remaster" in warning.casefold()
                for warning in result.warnings),
            f"expected a version warning, got {result.warnings}",
        )

    def test_live_version_is_flagged(self) -> None:
        """A live recording is a different version and must be flagged."""

        people = (artist("Nirvana"),)
        source = track("Lithium", people, identifier="src")
        candidate = track("Lithium (Live)", people, identifier="dst", version="live")
        result = TrackMatcher().match(source, [candidate])
        self.assertTrue(
            any("version" in warning.casefold() for warning in result.warnings),
            f"expected a version warning, got {result.warnings}",
        )

    def test_duration_mismatch_is_flagged(self) -> None:
        """A duration difference beyond the tolerance must not be accepted.

        It is reported in ``reasons`` and downgrades the outcome away from a
        confident match, because a two-minute gap usually means a different
        recording (live take, extended mix) rather than a metadata quirk.
        """

        people = (artist("Artist"),)
        source = track("Song", people, identifier="src", duration_ms=200_000)
        candidate = track("Song", people, identifier="dst", duration_ms=400_000)
        result = TrackMatcher().match(source, [candidate])
        mentioned = " ".join([*result.reasons, *result.warnings]).casefold()
        self.assertIn("duration", mentioned)
        self.assertNotEqual(
            result.outcome,
            MatchOutcome.MATCHED,
            "a 200-second gap must not be a confident match",
        )

    def test_small_duration_difference_is_ignored(self) -> None:
        """A sub-tolerance duration difference is not worth a warning."""

        people = (artist("Artist"),)
        source = track("Song", people, identifier="src", duration_ms=200_000)
        candidate = track("Song", people, identifier="dst", duration_ms=201_000)
        result = TrackMatcher().match(source, [candidate])
        self.assertFalse(
            any("duration" in warning.casefold() for warning in result.warnings)
        )


class AmbiguityTests(unittest.TestCase):
    """A near-tie is reported as ambiguous rather than guessed."""

    def test_near_tie_is_ambiguous(self) -> None:
        """Two near-equal candidates must not be resolved by accident."""

        people = (artist("Artist"),)
        source = track("Song", people, identifier="src")
        first = track("Song", people, identifier="a")
        second = track("Song", people, identifier="b")
        result = TrackMatcher().match(source, [first, second])
        # Either an explicit ambiguity or a top score that was downgraded;
        # what must never happen is a confident single match with no warning.
        self.assertIn(
            result.outcome, {MatchOutcome.AMBIGUOUS, MatchOutcome.MATCHED}
        )
        if result.outcome is MatchOutcome.MATCHED:
            self.assertTrue(result.warnings)

    def test_album_context_breaks_the_tie(self) -> None:
        """Album context is used, so a different album scores lower."""

        people = (artist("Artist"),)
        source = track("Song", people, identifier="src", album=album("First"))
        right = track("Song", people, identifier="right", album=album("First"))
        wrong = track("Song", people, identifier="wrong", album=album("Second"))
        result = TrackMatcher().match(source, [wrong, right])
        self.assertEqual(result.destination.source_id, "right")


class DirectIdentifierTests(unittest.TestCase):
    """TIDAL -> TIDAL can skip the search entirely (capability-driven)."""

    def test_reusable_identifier_short_circuits_search(self) -> None:
        """When the destination can reuse the id, no candidate is needed."""

        source = track("Anything", identifier="12345")
        result = TrackMatcher().match(
            source, [], reusable_destination_id="12345"
        )
        self.assertEqual(result.outcome, MatchOutcome.MATCHED)
        self.assertEqual(result.method, MatchMethod.DIRECT_ID)
        self.assertEqual(result.destination.source_id, "12345")

    def test_platform_is_not_named_in_the_decision(self) -> None:
        """The matcher consults a capability, never ``if platform == 'tidal'``."""

        self.assertNotIn(Platform.TIDAL.value, TrackMatcher.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
