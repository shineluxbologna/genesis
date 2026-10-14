"""Client-credentials grant: one token fetch per run, then that token calls GraphQL."""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs

import httpx

from early_access_tagger.graphql_client import resolve_access_token
from early_access_tagger.tagger import run
from tests.test_retry import customer, customers_payload, sample_config

SECRET = "client-secret-do-not-log"
ISSUED = "issued-access-token-do-not-log"
CLIENT_ID = "shinelux-client-id"


class ClientCredentialsTests(unittest.TestCase):
    def test_token_request_form_and_graphql_use_the_issued_token(self) -> None:
        token_calls: list[httpx.Request] = []
        graphql_tokens: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/admin/oauth/access_token":
                token_calls.append(request)
                return httpx.Response(
                    200,
                    json={
                        "access_token": ISSUED,
                        "scope": "read_customers,write_customers",
                        "expires_in": 86399,
                    },
                )
            graphql_tokens.append(request.headers["X-Shopify-Access-Token"])
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
            self.assertNotIn("firstName", body["query"])
            self.assertNotIn("email", body["query"].lower())
            return httpx.Response(
                200,
                json=customers_payload(
                    [customer("9", ["form-early-access"], "2026-06-01T00:00:00Z")],
                    has_next=False,
                    cursor=None,
                ),
            )

        config = sample_config(
            shop="shinelux.myshopify.com",
            access_token="",
            client_id=CLIENT_ID,
            client_secret=SECRET,
        )
        with self.assertLogs("early_access_tagger", level="DEBUG") as captured:
            summary = run(
                config,
                transport=httpx.MockTransport(handler),
                trust_env=False,
                sleep=lambda seconds: None,
            )

        self.assertEqual(summary.tagged, 1)
        self.assertEqual(len(token_calls), 1)
        request = token_calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://shinelux.myshopify.com/admin/oauth/access_token")
        self.assertIn("application/x-www-form-urlencoded", request.headers["content-type"])
        form = parse_qs(request.content.decode())
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(form["client_secret"], [SECRET])
        self.assertEqual(graphql_tokens, [ISSUED, ISSUED])
        self.assertNotIn(SECRET, graphql_tokens)

        logged = "\n".join(captured.output)
        self.assertNotIn(SECRET, logged)
        self.assertNotIn(ISSUED, logged)
        self.assertIn("Token di accesso ottenuto", logged)

    def test_static_token_skips_the_oauth_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            self.assertNotEqual(request.url.path, "/admin/oauth/access_token")
            self.assertEqual(request.headers["X-Shopify-Access-Token"], "shpat_test_token_not_real")
            return httpx.Response(200, json=customers_payload([], has_next=False, cursor=None))

        summary = run(
            sample_config(),
            transport=httpx.MockTransport(handler),
            trust_env=False,
            sleep=lambda seconds: None,
        )
        self.assertEqual(summary.found, 0)
        self.assertEqual(seen, ["/admin/api/2026-10/graphql.json"])
        self.assertEqual(
            resolve_access_token(sample_config(), trust_env=False),
            "shpat_test_token_not_real",
        )


if __name__ == "__main__":
    unittest.main()
