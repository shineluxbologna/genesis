"""Shopify Admin GraphQL client with timeouts, pagination helpers, and retries."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import httpx

from early_access_tagger.config import Config

logger = logging.getLogger("early_access_tagger")

PAGE_SIZE = 50

CUSTOMERS_QUERY = """
query EarlyAccessCustomers($query: String!, $cursor: String) {
  customers(first: 50, after: $cursor, query: $query, sortKey: CREATED_AT) {
    edges {
      node {
        id
        tags
        createdAt
        firstName
        lastName
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
""".strip()

TAGS_ADD_MUTATION = """
mutation AddEarlyAccessTag($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) {
    node {
      id
    }
    userErrors {
      field
      message
    }
  }
}
""".strip()

_TOKEN_RE = re.compile(r"sh(pat|pca)_[A-Za-z0-9]+")
Sleep = Callable[[float], None]


class ShopifyRequestError(RuntimeError):
    """The Admin API rejected a request or stayed unavailable after the retries."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def redact(text: str) -> str:
    return _TOKEN_RE.sub("shpat_[redacted]", text)


def is_throttled(payload: dict[str, Any] | None) -> bool:
    """True when GraphQL reports THROTTLED or a Throttled message."""
    if not payload:
        return False
    errors = payload.get("errors")
    if isinstance(errors, str):
        return "throttl" in errors.lower()
    if not isinstance(errors, list):
        return False
    for error in errors:
        if not isinstance(error, dict):
            if "throttl" in str(error).lower():
                return True
            continue
        code = str((error.get("extensions") or {}).get("code") or "").upper()
        if code == "THROTTLED":
            return True
        if "throttl" in str(error.get("message") or "").lower():
            return True
    return False


def throttle_wait_seconds(payload: dict[str, Any] | None) -> float:
    """Seconds to wait so the cost bucket can cover another query like the last one."""
    if not payload:
        return 0.0
    cost = (payload.get("extensions") or {}).get("cost") or {}
    status = cost.get("throttleStatus") or {}
    available = status.get("currentlyAvailable")
    restore = status.get("restoreRate")
    if available is None or not restore:
        return 0.0
    try:
        available_f = float(available)
        restore_f = float(restore)
    except (TypeError, ValueError):
        return 0.0
    if restore_f <= 0:
        return 0.0
    requested = cost.get("actualQueryCost")
    if requested is None:
        requested = cost.get("requestedQueryCost")
    try:
        needed = float(requested if requested is not None else 50) * 2
    except (TypeError, ValueError):
        needed = 100.0
    if available_f >= needed:
        return 0.0
    return max(0.0, (needed - available_f) / restore_f)


def should_retry(
    *,
    status_code: int | None,
    payload: dict[str, Any] | None,
    attempt: int,
    max_retries: int,
    transport_error: bool = False,
) -> bool:
    """Decide whether another attempt is warranted. `attempt` is 1-based and already used."""
    if attempt >= max_retries:
        return False
    if transport_error:
        return True
    if status_code is None:
        return False
    if status_code in (408, 429) or 500 <= status_code <= 599:
        return True
    if status_code == 200 and is_throttled(payload):
        return True
    return False


def backoff_seconds(
    attempt: int,
    *,
    retry_after: float | None = None,
    throttle_wait: float | None = None,
    jitter: float = 0.0,
) -> float:
    """Exponential wait (1s, 2s, 4s, …) raised by Retry-After or the cost bucket when those are longer."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    exponential = min(60.0, float(2 ** (attempt - 1)))
    wait = exponential
    if retry_after is not None:
        wait = max(wait, float(retry_after))
    if throttle_wait is not None:
        wait = max(wait, float(throttle_wait))
    return min(120.0, wait) + max(0.0, jitter)


def retry_after_seconds(headers: httpx.Headers | dict[str, str]) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class ShopifyGraphQLClient:
    """Small Admin GraphQL client. One instance per run."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
        trust_env: bool = True,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.shop = config.shop
        self.api_version = config.api_version
        self.max_retries = config.max_retries
        self._sleep = sleep
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._http = httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={
                "X-Shopify-Access-Token": config.access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "early-access-tagger/1.0",
            },
            follow_redirects=False,
            trust_env=trust_env,
            transport=transport,
        )
        self._url = config.graphql_url

    def close(self) -> None:
        self._http.close()

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST one operation. Retries throttle, timeout, and 5xx. Raises on fatal errors."""
        last_message = "Richiesta Shopify non riuscita."
        last_status: int | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._http.post(self._url, json={"query": query, "variables": variables})
            except httpx.TimeoutException as exc:
                last_message = "Timeout verso l'API Shopify"
                last_status = None
                if self._wait_for_retry(attempt, last_message, status_code=None, payload=None, transport_error=True):
                    continue
                raise ShopifyRequestError(last_message, retryable=True, status_code=None) from exc
            except httpx.TransportError as exc:
                last_message = f"API Shopify irraggiungibile: {redact(str(exc))}"
                last_status = None
                if self._wait_for_retry(attempt, last_message, status_code=None, payload=None, transport_error=True):
                    continue
                raise ShopifyRequestError(last_message, retryable=True, status_code=None) from exc

            payload = _read_json(response)
            last_status = response.status_code
            request_id = response.headers.get("X-Request-Id")
            retryable = should_retry(
                status_code=response.status_code,
                payload=payload,
                attempt=attempt,
                max_retries=self.max_retries,
            )
            if retryable:
                reason = _failure_reason(response.status_code, payload)
                last_message = reason
                self._wait_for_retry(
                    attempt,
                    reason,
                    status_code=response.status_code,
                    payload=payload,
                    transport_error=False,
                    retry_after=retry_after_seconds(response.headers),
                    forced=True,
                )
                continue

            if response.status_code in (401, 403):
                raise ShopifyRequestError(
                    _with_request_id(
                        "Autenticazione rifiutata. Controlla il token e gli scope "
                        "read_customers e write_customers.",
                        request_id,
                    ),
                    retryable=False,
                    status_code=response.status_code,
                )
            if response.status_code == 404:
                raise ShopifyRequestError(
                    _with_request_id(
                        "Negozio o versione API non trovati. Controlla SHOPIFY_SHOP e SHOPIFY_API_VERSION.",
                        request_id,
                    ),
                    retryable=False,
                    status_code=404,
                )
            if 300 <= response.status_code < 400:
                raise ShopifyRequestError(
                    _with_request_id("Redirect inatteso dall'API Shopify.", request_id),
                    retryable=False,
                    status_code=response.status_code,
                )
            if response.status_code in (408, 429) or 500 <= response.status_code <= 599:
                raise ShopifyRequestError(
                    _with_request_id(
                        f"{_failure_reason(response.status_code, payload)}. "
                        f"Tentativi esauriti ({self.max_retries}).",
                        request_id,
                    ),
                    retryable=True,
                    status_code=response.status_code,
                )
            if response.status_code != 200 or payload is None:
                body = redact(response.text[:300]) if response.text else ""
                raise ShopifyRequestError(
                    _with_request_id(
                        f"Risposta Shopify non valida (HTTP {response.status_code}). {body}".strip(),
                        request_id,
                    ),
                    retryable=False,
                    status_code=response.status_code,
                )
            if payload.get("errors"):
                message = _graphql_error_message(payload)
                if is_throttled(payload):
                    message = f"{message}. Tentativi esauriti ({self.max_retries})."
                raise ShopifyRequestError(
                    _with_request_id(message, request_id),
                    retryable=is_throttled(payload),
                    status_code=200,
                )

            pause = throttle_wait_seconds(payload)
            if pause >= 1:
                logger.info("Attendo %.1f secondi per il limite di costo dell'API Shopify.", pause)
                self._sleep(pause)
            return payload

        raise ShopifyRequestError(
            f"{last_message} Tentativi esauriti ({self.max_retries}).",
            retryable=True,
            status_code=last_status,
        )

    def _wait_for_retry(
        self,
        attempt: int,
        reason: str,
        *,
        status_code: int | None,
        payload: dict[str, Any] | None,
        transport_error: bool,
        retry_after: float | None = None,
        forced: bool = False,
    ) -> bool:
        if not forced and not should_retry(
            status_code=status_code,
            payload=payload,
            attempt=attempt,
            max_retries=self.max_retries,
            transport_error=transport_error,
        ):
            return False
        wait = backoff_seconds(
            attempt,
            retry_after=retry_after,
            throttle_wait=throttle_wait_seconds(payload),
        )
        logger.warning(
            "Tentativo %d/%d non riuscito (%s). Nuovo tentativo tra %.1f secondi.",
            attempt,
            self.max_retries,
            reason,
            wait,
        )
        self._sleep(wait)
        return True


def _read_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        return data
    return {"errors": [{"message": str(data)}]}


def _failure_reason(status_code: int, payload: dict[str, Any] | None) -> str:
    if is_throttled(payload):
        return "THROTTLED"
    if status_code == 429:
        return "HTTP 429"
    if status_code == 408:
        return "HTTP 408"
    if 500 <= status_code <= 599:
        return f"HTTP {status_code}"
    return f"HTTP {status_code}"


def _graphql_error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, str):
        return redact(errors)
    if isinstance(errors, list):
        parts: list[str] = []
        for error in errors:
            if isinstance(error, dict):
                parts.append(str(error.get("message") or error))
            else:
                parts.append(str(error))
        if parts:
            return redact("; ".join(parts))
    return "Errore GraphQL senza messaggio."


def _with_request_id(message: str, request_id: str | None) -> str:
    if not request_id:
        return message
    return f"{message} (X-Request-Id: {request_id})"
