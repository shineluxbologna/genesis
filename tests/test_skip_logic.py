"""Skip customers who already have the segment tag, or who are not form signups."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from early_access_tagger.tagger import (
    created_in_window,
    matches_source_tag,
    needs_target_tag,
)

UTC = timezone.utc
TARGET = "early access targa"
SOURCE = "form-early-access"


class SkipLogicTests(unittest.TestCase):
    def test_skips_when_the_segment_tag_is_already_present(self) -> None:
        tags = [SOURCE, TARGET]
        self.assertFalse(needs_target_tag(tags, TARGET))

    def test_tags_when_only_the_form_tag_is_present(self) -> None:
        self.assertTrue(needs_target_tag([SOURCE], TARGET))

    def test_partial_or_different_case_is_not_the_same_tag(self) -> None:
        self.assertTrue(needs_target_tag(["early access"], TARGET))
        self.assertTrue(needs_target_tag(["Early Access Targa"], TARGET))
        self.assertTrue(needs_target_tag(["early access targa extra"], TARGET))

    def test_trims_whitespace_around_an_existing_tag(self) -> None:
        self.assertFalse(needs_target_tag(["  early access targa  "], TARGET))

    def test_ordinary_buyers_do_not_match_the_form(self) -> None:
        self.assertFalse(matches_source_tag(["newsletter", "ordine"], SOURCE))
        self.assertFalse(matches_source_tag(["form-early-access-vip"], SOURCE))
        self.assertTrue(matches_source_tag([SOURCE, "altro"], SOURCE))

    def test_empty_source_tag_does_not_filter(self) -> None:
        self.assertTrue(matches_source_tag(["newsletter"], ""))

    def test_created_at_window_is_inclusive(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        end = datetime(2026, 1, 2, tzinfo=UTC)
        self.assertTrue(created_in_window("2026-01-01T00:00:00Z", start, end))
        self.assertTrue(created_in_window("2026-01-02T00:00:00Z", start, end))
        self.assertFalse(created_in_window("2025-12-31T23:59:59Z", start, end))
        self.assertFalse(created_in_window("2026-01-02T00:00:01Z", start, end))

    def test_comma_separated_tag_string(self) -> None:
        self.assertFalse(needs_target_tag("form-early-access, early access targa", TARGET))


if __name__ == "__main__":
    unittest.main()
