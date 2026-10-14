"""Customer search string and configuration window."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from early_access_tagger.config import (
    DEFAULT_END_DATE,
    DEFAULT_SOURCE_TAG,
    DEFAULT_START_DATE,
    DEFAULT_TARGET_TAG,
    ConfigError,
    load_config,
    load_dotenv,
    parse_shopify_datetime,
)
from early_access_tagger.tagger import build_customer_search


UTC = timezone.utc


class CustomerSearchTests(unittest.TestCase):
    def test_inclusive_window_and_source_tag(self) -> None:
        query = build_customer_search(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
            source_tag="form-early-access",
            extra="",
        )
        self.assertEqual(
            query,
            "customer_date:>='2026-01-01T00:00:00Z' "
            "AND customer_date:<='2026-12-31T23:59:59Z' "
            "AND tag:'form-early-access'",
        )
        self.assertNotIn("early access targa", query)

    def test_converts_offsets_to_utc(self) -> None:
        rome = timezone(timedelta(hours=2))
        query = build_customer_search(
            start=datetime(2026, 6, 1, 2, 0, tzinfo=rome),
            end=datetime(2026, 6, 2, 1, 59, 59, tzinfo=rome),
            source_tag="form-early-access",
            extra="",
        )
        self.assertIn("customer_date:>='2026-06-01T00:00:00Z'", query)
        self.assertIn("customer_date:<='2026-06-01T23:59:59Z'", query)

    def test_quotes_spaces_and_escapes_quotes(self) -> None:
        query = build_customer_search(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            source_tag="o'brien form",
            extra="",
        )
        self.assertIn("tag:'o\\'brien form'", query)

    def test_extra_fragment_is_anded_and_optional(self) -> None:
        base = dict(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            source_tag="form-early-access",
        )
        plain = build_customer_search(**base, extra="")
        self.assertNotIn("email:", plain)

        with_extra = build_customer_search(**base, extra="  AND email:*@example.com ")
        self.assertTrue(with_extra.endswith("AND (email:*@example.com)"))

        already_grouped = build_customer_search(**base, extra="(country:IT)")
        self.assertIn("AND (country:IT)", already_grouped)
        self.assertNotIn("((country:IT))", already_grouped)

    def test_source_tag_can_be_omitted_when_extra_is_present(self) -> None:
        query = build_customer_search(
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            source_tag="",
            extra="tag:'altro-modulo'",
        )
        self.assertNotIn("tag:'form-early-access'", query)
        self.assertIn("(tag:'altro-modulo')", query)
        self.assertIn("customer_date:>=", query)


class ConfigTests(unittest.TestCase):
    def test_blank_env_uses_documented_defaults(self) -> None:
        config = load_config(
            {
                "SHOPIFY_SHOP": "https://Demo.myshopify.com/admin",
                "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_real_enough",
                "START_DATE": "",
                "END_DATE": " ",
                "EARLY_ACCESS_SOURCE_TAG": "",
                "TARGET_TAG": "",
                "CUSTOMER_QUERY_EXTRA": "",
                "SHOPIFY_API_VERSION": "",
            }
        )
        self.assertEqual(config.shop, "Demo.myshopify.com")
        self.assertEqual(config.api_version, "2026-10")
        self.assertEqual(config.source_tag, DEFAULT_SOURCE_TAG)
        self.assertEqual(config.target_tag, DEFAULT_TARGET_TAG)
        self.assertEqual(config.start_date, parse_shopify_datetime(DEFAULT_START_DATE))
        self.assertEqual(config.end_date, parse_shopify_datetime(DEFAULT_END_DATE))
        self.assertEqual(config.query_extra, "")

    def test_date_only_end_covers_the_whole_utc_day(self) -> None:
        config = load_config(
            {
                "SHOPIFY_SHOP": "demo.myshopify.com",
                "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_real_enough",
                "START_DATE": "2026-03-01",
                "END_DATE": "2026-03-31",
            }
        )
        self.assertEqual(config.start_date, datetime(2026, 3, 1, tzinfo=UTC))
        self.assertEqual(config.end_date, datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC))

    def test_refuses_to_run_without_a_form_filter(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(
                {
                    "SHOPIFY_SHOP": "demo.myshopify.com",
                    "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_real_enough",
                    "EARLY_ACCESS_SOURCE_TAG": "-",
                }
            )
        self.assertIn("acquirenti", str(caught.exception))

    def test_refuses_inverted_window_and_placeholders(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(
                {
                    "SHOPIFY_SHOP": "demo.myshopify.com",
                    "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_real_enough",
                    "START_DATE": "2026-05-02T00:00:00Z",
                    "END_DATE": "2026-05-01T00:00:00Z",
                }
            )
        with self.assertRaises(ConfigError):
            load_config(
                {
                    "SHOPIFY_SHOP": "your-store.myshopify.com",
                    "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_real_enough",
                }
            )

    def test_dotenv_does_not_override_existing_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "SHOPIFY_SHOP=from-file.myshopify.com",
                        "SHOPIFY_ADMIN_ACCESS_TOKEN=shpat_from_file",
                        "# commento",
                        "export EARLY_ACCESS_SOURCE_TAG='tag-dal-file'",
                    ]
                ),
                encoding="utf-8",
            )
            previous = {key: os.environ.get(key) for key in (
                "SHOPIFY_SHOP",
                "SHOPIFY_ADMIN_ACCESS_TOKEN",
                "EARLY_ACCESS_SOURCE_TAG",
            )}
            os.environ["SHOPIFY_SHOP"] = "already.myshopify.com"
            os.environ.pop("SHOPIFY_ADMIN_ACCESS_TOKEN", None)
            os.environ.pop("EARLY_ACCESS_SOURCE_TAG", None)
            try:
                load_dotenv(path)
                self.assertEqual(os.environ["SHOPIFY_SHOP"], "already.myshopify.com")
                self.assertEqual(os.environ["SHOPIFY_ADMIN_ACCESS_TOKEN"], "shpat_from_file")
                self.assertEqual(os.environ["EARLY_ACCESS_SOURCE_TAG"], "tag-dal-file")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
