"""Find early-access signups in a date window and add the segment tag."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from early_access_tagger.config import Config, ConfigError, load_config, parse_shopify_datetime
from early_access_tagger.graphql_client import (
    CUSTOMERS_QUERY,
    TAGS_ADD_MUTATION,
    ShopifyGraphQLClient,
    ShopifyRequestError,
    resolve_access_token,
)

logger = logging.getLogger("early_access_tagger")

MAX_PAGES = 200


class RunFailed(RuntimeError):
    """The run itself failed (configuration already checked, or the API could not be used)."""


@dataclass(frozen=True)
class RunSummary:
    found: int
    tagged: int
    already_tagged: int
    excluded: int
    failed: int


def build_customer_search(
    *,
    start: datetime,
    end: datetime,
    source_tag: str,
    extra: str,
) -> str:
    """Shopify customer search string. Dates are inclusive and sent in UTC as customer_date."""
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    parts = [
        f"customer_date:>={_quote_search_value(_format_utc(start_utc))}",
        f"customer_date:<={_quote_search_value(_format_utc(end_utc))}",
    ]
    if source_tag.strip():
        parts.append(f"tag:{_quote_search_value(source_tag.strip())}")
    fragment = _normalize_extra(extra)
    if fragment:
        parts.append(fragment)
    return " AND ".join(parts)


def has_tag(tags: object, wanted: str) -> bool:
    """Exact tag match after trimming whitespace. Comparison is case-sensitive."""
    needle = wanted.strip()
    if not needle:
        return False
    return any(tag.strip() == needle for tag in normalize_tags(tags))


def needs_target_tag(tags: object, target_tag: str) -> bool:
    """True when the segment tag still has to be added."""
    return not has_tag(tags, target_tag)


def matches_source_tag(tags: object, source_tag: str) -> bool:
    """True when this customer carries the form tag. An empty source tag does not filter."""
    if not source_tag.strip():
        return True
    return has_tag(tags, source_tag)


def created_in_window(created_at: object, start: datetime, end: datetime) -> bool:
    """Inclusive created_at check. Raises ValueError when the timestamp cannot be parsed."""
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("createdAt mancante")
    created = parse_shopify_datetime(created_at)
    return _as_utc(start) <= created <= _as_utc(end)


def normalize_tags(tags: object) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [part.strip() for part in tags.split(",") if part.strip()]
    if isinstance(tags, list):
        return [str(tag) for tag in tags]
    return []


def run(
    config: Config,
    *,
    client: ShopifyGraphQLClient | None = None,
    sleep=time.sleep,
    transport=None,
    trust_env: bool = True,
) -> RunSummary:
    """Page through matching customers, then add the target tag. One bad customer does not stop the run."""
    search = build_customer_search(
        start=config.start_date,
        end=config.end_date,
        source_tag=config.source_tag,
        extra=config.query_extra,
    )
    logger.info(
        "Avvio su %s (API %s). Finestra created_at inclusiva: %s → %s.",
        config.shop,
        config.api_version,
        _format_utc(config.start_date),
        _format_utc(config.end_date),
    )
    logger.info(
        "Tag del modulo: %s. Tag del segmento: «%s».",
        f"«{config.source_tag}»" if config.source_tag else "(nessuno, uso solo CUSTOMER_QUERY_EXTRA)",
        config.target_tag,
    )
    logger.info("Ricerca clienti: %s", search)

    owns_client = client is None
    if client is None:
        try:
            token = resolve_access_token(
                config,
                sleep=sleep,
                transport=transport,
                trust_env=trust_env,
            )
        except ShopifyRequestError as exc:
            raise RunFailed(str(exc)) from exc
        client = ShopifyGraphQLClient(
            replace(config, access_token=token, client_secret=""),
            sleep=sleep,
            transport=transport,
            trust_env=trust_env,
        )
    try:
        matched, excluded = _collect_customers(client, config, search)
        logger.info("Trovati %d nuovi clienti", len(matched))
        tagged, already_tagged, failed = _tag_customers(client, config, matched)
        summary = RunSummary(
            found=len(matched),
            tagged=tagged,
            already_tagged=already_tagged,
            excluded=excluded,
            failed=failed,
        )
        logger.info("Tag aggiunto a %d clienti", summary.tagged)
        logger.info(
            "Riepilogo: trovati %d, tag aggiunti %d, già taggati %d, esclusi %d, errori %d",
            summary.found,
            summary.tagged,
            summary.already_tagged,
            summary.excluded,
            summary.failed,
        )
        return summary
    finally:
        if owns_client:
            client.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configurazione non valida: %s", exc)
        return 2
    try:
        run(config)
    except RunFailed as exc:
        logger.error("Esecuzione interrotta: %s", exc)
        return 1
    except Exception:
        logger.exception("Errore imprevisto, esecuzione interrotta.")
        return 1
    return 0


def _collect_customers(
    client: ShopifyGraphQLClient,
    config: Config,
    search: str,
) -> tuple[list[dict], int]:
    matched: list[dict] = []
    excluded = 0
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    for page in range(1, MAX_PAGES + 1):
        try:
            payload = client.execute(CUSTOMERS_QUERY, {"query": search, "cursor": cursor})
        except ShopifyRequestError as exc:
            raise RunFailed(str(exc)) from exc

        connection = (payload.get("data") or {}).get("customers")
        if not isinstance(connection, dict):
            raise RunFailed("Risposta Shopify senza l'elenco clienti.")

        edges = connection.get("edges") or []
        logger.info("Pagina %d: %d clienti restituiti dall'API", page, len(edges))
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if not isinstance(node, dict):
                excluded += 1
                logger.warning("Saltato un record cliente incompleto.")
                continue
            customer_id = str(node.get("id") or "")
            label = customer_label(node)
            if not customer_id or customer_id in seen_ids:
                continue
            seen_ids.add(customer_id)
            try:
                in_window = created_in_window(node.get("createdAt"), config.start_date, config.end_date)
            except ValueError:
                excluded += 1
                logger.warning("Saltato %s: data di creazione assente o non valida.", label)
                continue
            if not in_window:
                excluded += 1
                logger.info("Saltato %s: fuori dalla finestra created_at.", label)
                continue
            if not matches_source_tag(node.get("tags"), config.source_tag):
                excluded += 1
                logger.info(
                    "Saltato %s: senza il tag del modulo «%s».",
                    label,
                    config.source_tag,
                )
                continue
            matched.append(node)

        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return matched, excluded
        end_cursor = page_info.get("endCursor")
        if not end_cursor or end_cursor in seen_cursors:
            raise RunFailed("Paginazione Shopify interrotta: cursore mancante o ripetuto.")
        seen_cursors.add(str(end_cursor))
        cursor = str(end_cursor)

    raise RunFailed(
        f"Troppe pagine di clienti (oltre {MAX_PAGES}). Esecuzione interrotta per non restare a metà."
    )


def _tag_customers(
    client: ShopifyGraphQLClient,
    config: Config,
    customers: list[dict],
) -> tuple[int, int, int]:
    tagged = 0
    already_tagged = 0
    failed = 0
    for node in customers:
        label = customer_label(node)
        try:
            if not needs_target_tag(node.get("tags"), config.target_tag):
                already_tagged += 1
                logger.info("Saltato %s: ha già il tag «%s».", label, config.target_tag)
                continue
            _add_tag(client, str(node.get("id")), config.target_tag)
        except ShopifyRequestError as exc:
            failed += 1
            logger.error("Errore sul cliente %s: %s", label, exc)
            continue
        except Exception as exc:  # one bad record must not kill the daily run
            failed += 1
            logger.error("Errore sul cliente %s: %s", label, exc)
            continue
        tagged += 1
        logger.info("Aggiunto il tag «%s» a %s.", config.target_tag, label)
    return tagged, already_tagged, failed


def _add_tag(client: ShopifyGraphQLClient, customer_id: str, target_tag: str) -> None:
    payload = client.execute(TAGS_ADD_MUTATION, {"id": customer_id, "tags": [target_tag]})
    tags_add = (payload.get("data") or {}).get("tagsAdd")
    if not isinstance(tags_add, dict):
        raise ShopifyRequestError(
            "Risposta tagsAdd vuota.",
            retryable=False,
            status_code=200,
        )
    user_errors = tags_add.get("userErrors") or []
    if user_errors:
        parts = []
        for error in user_errors:
            if isinstance(error, dict) and error.get("message"):
                parts.append(str(error["message"]))
            else:
                parts.append(str(error))
        raise ShopifyRequestError("; ".join(parts), retryable=False, status_code=200)


def customer_label(node: dict) -> str:
    return str(node.get("id") or "sconosciuto")


def _quote_search_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _normalize_extra(extra: str) -> str:
    fragment = " ".join(extra.split())
    if fragment.lower().startswith("and "):
        fragment = fragment[4:].strip()
    if not fragment:
        return ""
    if fragment.startswith("(") and fragment.endswith(")"):
        return fragment
    return f"({fragment})"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
