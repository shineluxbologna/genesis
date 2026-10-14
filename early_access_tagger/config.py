"""Environment configuration for the daily early-access tagger."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_API_VERSION = "2026-10"
DEFAULT_START_DATE = "2026-01-01T00:00:00Z"
DEFAULT_END_DATE = "2027-12-31T23:59:59Z"
DEFAULT_SOURCE_TAG = "form-early-access"
DEFAULT_TARGET_TAG = "early access targa"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 5

_PLACEHOLDER_SHOPS = {"your-store.myshopify.com"}
_PLACEHOLDER_TOKENS = {
    "shpat_xxxxxxxx",
    "shpat_your_admin_api_token",
    "shpca_your_admin_api_token",
}
_SOURCE_TAG_DISABLED = {"-", "none", "off"}
_API_VERSION_RE = re.compile(r"^\d{4}-\d{2}$|^unstable$")


class ConfigError(ValueError):
    """The run cannot start because configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    shop: str
    access_token: str
    api_version: str
    start_date: datetime
    end_date: datetime
    source_tag: str
    target_tag: str
    query_extra: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @property
    def graphql_url(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs into the environment without overriding real variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(
    environ: dict[str, str] | None = None,
    *,
    dotenv_path: Path | None = None,
) -> Config:
    """Read configuration from the process environment, after an optional .env file."""
    if environ is None:
        env_file = dotenv_path if dotenv_path is not None else Path.cwd() / ".env"
        load_dotenv(env_file)
        environ = dict(os.environ)

    shop = _normalize_shop(_required(environ, "SHOPIFY_SHOP"))
    token = _required(environ, "SHOPIFY_ADMIN_ACCESS_TOKEN")
    if shop in _PLACEHOLDER_SHOPS:
        raise ConfigError(
            "Sostituisci SHOPIFY_SHOP con il dominio reale, per esempio nome-negozio.myshopify.com."
        )
    if token in _PLACEHOLDER_TOKENS:
        raise ConfigError(
            "Sostituisci SHOPIFY_ADMIN_ACCESS_TOKEN con il token Admin della custom app."
        )

    api_version = _blank_as_missing(environ.get("SHOPIFY_API_VERSION")) or DEFAULT_API_VERSION
    if not _API_VERSION_RE.match(api_version):
        raise ConfigError(
            "SHOPIFY_API_VERSION deve essere una versione stabile tipo 2026-10, oppure unstable."
        )

    start_raw = _blank_as_missing(environ.get("START_DATE")) or DEFAULT_START_DATE
    end_raw = _blank_as_missing(environ.get("END_DATE")) or DEFAULT_END_DATE
    try:
        start_date = parse_shopify_datetime(start_raw)
        end_date = parse_shopify_datetime(end_raw, date_only_end=True)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if end_date < start_date:
        raise ConfigError("END_DATE è precedente a START_DATE. La finestra è vuota.")

    source_raw = _blank_as_missing(environ.get("EARLY_ACCESS_SOURCE_TAG"))
    if source_raw is None:
        source_tag = DEFAULT_SOURCE_TAG
    elif source_raw.lower() in _SOURCE_TAG_DISABLED:
        source_tag = ""
    else:
        source_tag = source_raw

    target_tag = _blank_as_missing(environ.get("TARGET_TAG")) or DEFAULT_TARGET_TAG
    query_extra = (_blank_as_missing(environ.get("CUSTOMER_QUERY_EXTRA")) or "").strip()
    if not source_tag and not query_extra:
        raise ConfigError(
            "Imposta EARLY_ACCESS_SOURCE_TAG oppure CUSTOMER_QUERY_EXTRA. "
            "Senza un filtro sul modulo verrebbero considerati anche gli acquirenti normali."
        )

    return Config(
        shop=shop,
        access_token=token,
        api_version=api_version,
        start_date=start_date,
        end_date=end_date,
        source_tag=source_tag,
        target_tag=target_tag,
        query_extra=query_extra,
    )


def parse_shopify_datetime(value: str, *, date_only_end: bool = False) -> datetime:
    """Parse an ISO 8601 timestamp. A date with no time covers that whole UTC day when asked."""
    raw = value.strip()
    if not raw:
        raise ValueError("Data vuota: usa il formato ISO 8601, per esempio 2026-01-01T00:00:00Z.")
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Data non valida {value!r}. Usa ISO 8601, per esempio 2026-01-01T00:00:00Z."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if date_only and date_only_end:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    return parsed.astimezone(timezone.utc)


def _required(environ: dict[str, str], name: str) -> str:
    value = _blank_as_missing(environ.get(name))
    if value is None:
        raise ConfigError(f"Manca {name}. Compila il file .env (vedi .env.example).")
    return value


def _blank_as_missing(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_shop(value: str) -> str:
    shop = value.strip()
    shop = re.sub(r"^https?://", "", shop, flags=re.IGNORECASE)
    shop = shop.split("/")[0].strip()
    if "@" in shop or " " in shop or "." not in shop:
        raise ConfigError(
            "SHOPIFY_SHOP deve essere il dominio del negozio, per esempio nome-negozio.myshopify.com."
        )
    return shop
