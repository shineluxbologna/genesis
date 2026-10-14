"""Retry decisions, backoff, and the tagger against a mocked Admin API."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

import httpx

from early_access_tagger.config import Config, ConfigError
from early_access_tagger.graphql_client import (
    ShopifyGraphQLClient,
    backoff_seconds,
    is_throttled,
    should_retry,
    throttle_wait_seconds,
)
from early_access_tagger.tagger import RunFailed, RunSummary, main, run

UTC = timezone.utc


def sample_config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "shop": "demo.myshopify.com",
        "access_token": "shpat_test_token_not_real",
        "api_version": "2026-10",
        "start_date": datetime(2026, 1, 1, tzinfo=UTC),
        "end_date": datetime(2027, 12, 31, 23, 59, 59, tzinfo=UTC),
        "source_tag": "form-early-access",
        "target_tag": "early access targa",
        "query_extra": "",
        "timeout_seconds": 30.0,
        "max_retries": 5,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def customer(customer_id: str, tags: list[str], created_at: str) -> dict:
    return {
        "node": {
            "id": f"gid://shopify/Customer/{customer_id}",
            "tags": tags,
            "createdAt": created_at,
        }
    }


def customers_payload(edges: list[dict], *, has_next: bool, cursor: str | None) -> dict:
    return {
        "data": {
            "customers": {
                "edges": edges,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    }


class RetryDecisionTests(unittest.TestCase):
    def test_retries_429_500_and_timeouts_until_the_max(self) -> None:
        self.assertTrue(should_retry(status_code=429, payload=None, attempt=1, max_retries=5))
        self.assertTrue(should_retry(status_code=503, payload=None, attempt=4, max_retries=5))
        self.assertTrue(should_retry(status_code=408, payload=None, attempt=1, max_retries=5))
        self.assertTrue(
            should_retry(status_code=None, payload=None, attempt=1, max_retries=5, transport_error=True)
        )
        self.assertFalse(should_retry(status_code=429, payload=None, attempt=5, max_retries=5))
        self.assertFalse(
            should_retry(status_code=None, payload=None, attempt=5, max_retries=5, transport_error=True)
        )

    def test_does_not_retry_auth_or_a_normal_success(self) -> None:
        self.assertFalse(should_retry(status_code=401, payload=None, attempt=1, max_retries=5))
        self.assertFalse(should_retry(status_code=403, payload=None, attempt=1, max_retries=5))
        self.assertFalse(should_retry(status_code=404, payload=None, attempt=1, max_retries=5))
        self.assertFalse(
            should_retry(status_code=200, payload={"data": {"customers": {}}}, attempt=1, max_retries=5)
        )

    def test_retries_graphql_throttled(self) -> None:
        payload = {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 102,
                    "actualQueryCost": None,
                    "throttleStatus": {
                        "maximumAvailable": 1000.0,
                        "currentlyAvailable": 20,
                        "restoreRate": 100.0,
                    },
                }
            },
        }
        self.assertTrue(is_throttled(payload))
        self.assertTrue(should_retry(status_code=200, payload=payload, attempt=1, max_retries=5))
        self.assertAlmostEqual(throttle_wait_seconds(payload), 1.84, places=2)

    def test_backoff_grows_and_respects_retry_after(self) -> None:
        self.assertEqual(backoff_seconds(1, jitter=0), 1)
        self.assertEqual(backoff_seconds(2, jitter=0), 2)
        self.assertEqual(backoff_seconds(3, jitter=0), 4)
        self.assertLess(backoff_seconds(2, jitter=0), backoff_seconds(3, jitter=0))
        self.assertEqual(backoff_seconds(1, retry_after=10, throttle_wait=3, jitter=0), 10)
        self.assertEqual(backoff_seconds(1, throttle_wait=4, jitter=0), 4)
        self.assertEqual(backoff_seconds(10, jitter=0), 60)


class MockApiTests(unittest.TestCase):
    def test_client_sets_an_explicit_timeout(self) -> None:
        client = ShopifyGraphQLClient(
            sample_config(),
            transport=httpx.MockTransport(lambda request: httpx.Response(204)),
            trust_env=False,
        )
        try:
            self.assertEqual(client._http.timeout.connect, 30.0)
            self.assertEqual(client._http.timeout.read, 30.0)
            self.assertEqual(client._http.timeout.write, 30.0)
            self.assertEqual(client._http.timeout.pool, 30.0)
            self.assertFalse(client._http.follow_redirects)
        finally:
            client.close()

    def test_pages_skip_existing_tag_and_keep_going_after_one_failure(self) -> None:
        mutations: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Shopify-Access-Token"], "shpat_test_token_not_real")
            self.assertTrue(request.url.path.endswith("/admin/api/2026-10/graphql.json"))
            body = json.loads(request.content.decode())
            if "tagsAdd" in body["query"]:
                mutations.append(body["variables"])
                customer_id = body["variables"]["id"]
                if customer_id.endswith("/2"):
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "tagsAdd": {
                                    "node": None,
                                    "userErrors": [{"field": ["id"], "message": "Customer does not exist"}],
                                }
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    json={"data": {"tagsAdd": {"node": {"id": customer_id}, "userErrors": []}}},
                )
            cursor = body["variables"].get("cursor")
            self.assertIn("tag:'form-early-access'", body["variables"]["query"])
            self.assertIn("customer_date:>='2026-01-01T00:00:00Z'", body["variables"]["query"])
            if cursor is None:
                return httpx.Response(
                    200,
                    json=customers_payload(
                        [
                            customer("1", ["form-early-access", "early access targa"], "2026-06-01T12:00:00Z"),
                            customer("2", ["form-early-access"], "2026-06-02T12:00:00Z"),
                        ],
                        has_next=True,
                        cursor="cursor-1",
                    ),
                )
            self.assertEqual(cursor, "cursor-1")
            return httpx.Response(
                200,
                json=customers_payload(
                    [
                        customer("3", ["newsletter"], "2026-06-03T12:00:00Z"),
                        customer("4", ["form-early-access"], "2026-06-04T12:00:00Z"),
                        customer("5", ["form-early-access"], "2019-01-01T00:00:00Z"),
                    ],
                    has_next=False,
                    cursor="cursor-2",
                ),
            )

        config = sample_config()
        client = ShopifyGraphQLClient(
            config,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            sleep=lambda seconds: None,
        )
        with self.assertLogs("early_access_tagger", level="INFO") as captured:
            summary = run(config, client=client)
        client.close()

        self.assertEqual(
            summary,
            RunSummary(found=3, tagged=1, already_tagged=1, excluded=2, failed=1),
        )
        self.assertEqual(
            [item["id"] for item in mutations],
            ["gid://shopify/Customer/2", "gid://shopify/Customer/4"],
        )
        self.assertEqual(mutations[0]["tags"], ["early access targa"])
        text = "\n".join(captured.output)
        self.assertIn("Trovati 3 nuovi clienti", text)
        self.assertIn("Tag aggiunto a 1 clienti", text)
        self.assertIn("ha già il tag «early access targa»", text)
        self.assertIn("senza il tag del modulo", text)
        self.assertIn("fuori dalla finestra", text)
        self.assertIn("Errore sul cliente gid://shopify/Customer/2", text)
        self.assertIn("errori 1", text)

    def test_retries_throttled_then_tags(self) -> None:
        state = {"customers": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if "tagsAdd" in body["query"]:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "tagsAdd": {
                                "node": {"id": body["variables"]["id"]},
                                "userErrors": [],
                            }
                        }
                    },
                )
            state["customers"] += 1
            if state["customers"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                        "extensions": {
                            "cost": {
                                "requestedQueryCost": 102,
                                "actualQueryCost": None,
                                "throttleStatus": {
                                    "maximumAvailable": 1000.0,
                                    "currentlyAvailable": 20,
                                    "restoreRate": 100.0,
                                },
                            }
                        },
                    },
                )
            return httpx.Response(
                200,
                json=customers_payload(
                    [customer("9", ["form-early-access"], "2026-06-01T00:00:00Z")],
                    has_next=False,
                    cursor=None,
                ),
            )

        config = sample_config(max_retries=4)
        client = ShopifyGraphQLClient(
            config,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            sleep=sleeps.append,
        )
        summary = run(config, client=client)
        client.close()
        self.assertEqual(summary.tagged, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(state["customers"], 2)
        self.assertAlmostEqual(sleeps[0], 1.84, places=2)

    def test_retries_timeout_and_503(self) -> None:
        state = {"n": 0}
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectTimeout("timed out")
            if state["n"] == 2:
                return httpx.Response(503, json={"errors": "unavailable"})
            return httpx.Response(200, json=customers_payload([], has_next=False, cursor=None))

        config = sample_config(max_retries=4)
        client = ShopifyGraphQLClient(
            config,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            sleep=sleeps.append,
        )
        summary = run(config, client=client)
        client.close()
        self.assertEqual(summary.found, 0)
        self.assertEqual(summary.tagged, 0)
        self.assertEqual(sleeps, [1, 2])

    def test_auth_failure_stops_the_run_and_does_not_tag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            self.assertNotIn("tagsAdd", body["query"])
            return httpx.Response(401, json={"errors": "Unauthorized"})

        config = sample_config()
        client = ShopifyGraphQLClient(
            config,
            transport=httpx.MockTransport(handler),
            trust_env=False,
            sleep=lambda seconds: None,
        )
        with self.assertRaises(RunFailed) as caught:
            run(config, client=client)
        client.close()
        self.assertIn("Autenticazione rifiutata", str(caught.exception))

    def test_main_exit_codes(self) -> None:
        with mock.patch("early_access_tagger.tagger.load_config", side_effect=ConfigError("manca il token")):
            self.assertEqual(main(), 2)
        with mock.patch("early_access_tagger.tagger.load_config", return_value=sample_config()):
            with mock.patch("early_access_tagger.tagger.run", side_effect=RunFailed("API giù")):
                self.assertEqual(main(), 1)
            with mock.patch(
                "early_access_tagger.tagger.run",
                return_value=RunSummary(0, 0, 0, 0, 2),
            ):
                self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
