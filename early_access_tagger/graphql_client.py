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


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    cleaned = _TOKEN_RE.sub("shpat_[redacted]", text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


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


def resolve_access_token(
    config: Config,
    *,
    sleep: Sleep = time.sleep,
    transport: httpx.BaseTransport | None = None,
    trust_env: bool = True,
) -> str:
    """Use a client-credentials token when the Dev Dashboard pair is set, otherwise the static token."""
    if config.uses_client_credentials:
        return fetch_client_credentials_token(
            config,
            sleep=sleep,
            transport=transport,
            trust_env=trust_env,
        )
    if config.access_token:
        return config.access_token
    raise ShopifyRequestError(
        "Manca l'accesso a Shopify. Imposta SHOPIFY_CLIENT_ID e SHOPIFY_CLIENT_SECRET, "
        "oppure SHOPIFY_ADMIN_ACCESS_TOKEN.",
        retryable=False,
    )


def fetch_client_credentials_token(
    config: Config,
    *,
    sleep: Sleep = time.sleep,
    transport: httpx.BaseTransport | None = None,
    trust_env: bool = True,
) -> str:
    """POST one client-credentials grant. The token lasts 24 hours, so one fetch per run is enough."""
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    secrets = (config.client_secret,)
    logger.info(
        "Richiedo un token di accesso per %s. Dura 24 ore: una richiesta per esecuzione.",
        config.shop,
    )
    last_message = "Richiesta del token non riuscita."
    last_status: int | None = None
    with httpx.Client(
        timeout=httpx.Timeout(config.timeout_seconds),
        follow_redirects=False,
        trust_env=trust_env,
        transport=transport,
        headers={"Accept": "application/json", "User-Agent": "early-access-tagger/1.0"},
    ) as http:
        for attempt in range(1, config.max_retries + 1):
            try:
                response = http.post(
                    config.token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": config.client_id,
                        "client_secret": config.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.TimeoutException:
                last_message = "Timeout durante la richiesta del token"
                last_status = None
                if _sleep_before_retry(
                    attempt,
                    config.max_retries,
                    last_message,
                    sleep,
                    status_code=None,
                    payload=None,
                    transport_error=True,
                ):
                    continue
                raise ShopifyRequestError(last_message, retryable=True) from None
            except httpx.TransportError as exc:
                last_message = f"Endpoint del token irraggiungibile: {redact(str(exc), secrets)}"
                last_status = None
                if _sleep_before_retry(
                    attempt,
                    config.max_retries,
                    last_message,
                    sleep,
                    status_code=None,
                    payload=None,
                    transport_error=True,
                ):
                    continue
                raise ShopifyRequestError(last_message, retryable=True) from exc

            payload = _read_json(response)
            last_status = response.status_code
            if should_retry(
                status_code=response.status_code,
                payload=payload,
                attempt=attempt,
                max_retries=config.max_retries,
            ):
                reason = _failure_reason(response.status_code, payload)
                last_message = reason
                _sleep_before_retry(
                    attempt,
                    config.max_retries,
                    reason,
                    sleep,
                    status_code=response.status_code,
                    payload=payload,
                    transport_error=False,
                    retry_after=retry_after_seconds(response.headers),
                    forced=True,
                )
                continue

            if response.status_code in (408, 429) or 500 <= response.status_code <= 599:
                raise ShopifyRequestError(
                    redact(
                        f"{_failure_reason(response.status_code, payload)}. "
                        f"Tentativi esauriti ({config.max_retries}).",
                        secrets,
                    ),
                    retryable=True,
                    status_code=response.status_code,
                )

            token = ""
            if response.status_code == 200 and isinstance(payload, dict):
                raw_token = payload.get("access_token")
                if isinstance(raw_token, str):
                    token = raw_token.strip()
            if token:
                logger.info("Token di accesso ottenuto per questa esecuzione.")
                return token

            raise ShopifyRequestError(
                redact(_token_failure_message(response.status_code, payload), secrets),
                retryable=False,
                status_code=response.status_code,
            )

    raise ShopifyRequestError(
        f"{last_message} Tentativi esauriti ({config.max_retries}).",
        retryable=True,
        status_code=last_status,
    )


def _sleep_before_retry(
    attempt: int,
    max_retries: int,
    reason: str,
    sleep: Sleep,
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
        max_retries=max_retries,
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
        max_retries,
        reason,
        wait,
    )
    sleep(wait)
    return True


def _token_failure_message(status_code: int, payload: dict[str, Any] | None) -> str:
    if status_code in (401, 403):
        return (
            "Credenziali rifiutate. Controlla Client ID, Client secret e gli scope "
            "read_customers,write_customers."
        )
    parts: list[str] = []
    if isinstance(payload, dict):
        for key in ("error", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    detail = f": {'; '.join(parts)}" if parts else ""
    return f"Richiesta del token rifiutata (HTTP {status_code}){detail}."


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
        self._secrets = (config.access_token, config.client_secret)
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
                last_message = f"API Shopify irraggiungibile: {redact(str(exc), self._secrets)}"
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
                        "Autenticazione rifiutata. Controlla Client ID e Client secret, "
                        "oppure il token Admin, e gli scope read_customers e write_customers.",
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
                body = redact(response.text[:300], self._secrets) if response.text else ""
                raise ShopifyRequestError(
                    _with_request_id(
                        f"Risposta Shopify non valida (HTTP {response.status_code}). {body}".strip(),
                        request_id,
                    ),
                    retryable=False,
                    status_code=response.status_code,
                )
            if payload.get("errors"):
                message = _graphql_error_message(payload, self._secrets)
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


def _graphql_error_message(payload: dict[str, Any], secrets: tuple[str, ...] = ()) -> str:
    errors = payload.get("errors")
    if isinstance(errors, str):
        return redact(errors, secrets)
    if isinstance(errors, list):
        parts: list[str] = []
        for error in errors:
            if isinstance(error, dict):
                parts.append(str(error.get("message") or error))
            else:
                parts.append(str(error))
        if parts:
            return redact("; ".join(parts), secrets)
    return "Errore GraphQL senza messaggio."


def _with_request_id(message: str, request_id: str | None) -> str:
    if not request_id:
        return message
    return f"{message} (X-Request-Id: {request_id})"
