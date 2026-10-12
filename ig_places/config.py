"""Configuración leída de variables de entorno (con valores por defecto razonables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "si", "sí"}


DEFAULT_SOURCES = (
    "palma=https://www.instagram.com/explore/locations/c692339/palmademallorca-spain/,"
    "ibiza=https://www.instagram.com/explore/locations/c685540/ibiza-spain/"
)


@dataclass(frozen=True)
class Source:
    """Un directorio de ubicaciones de Instagram (una ciudad)."""

    key: str
    url: str

    def page_url(self, page: int) -> str:
        return f"{self.url}?page={page}"


def _parse_sources(raw: str) -> list[Source]:
    """IG_SOURCES="clave=url,clave=url"; el orden es el del JSON final."""
    sources = []
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, sep, url = item.partition("=")
        if not sep or not key.strip() or not url.strip():
            raise ValueError(f"IG_SOURCES mal formado: {item!r} (se espera clave=url)")
        sources.append(Source(key.strip(), url.strip()))
    return sources


@dataclass
class Config:
    sources: list[Source] = field(
        default_factory=lambda: _parse_sources(os.environ.get("IG_SOURCES", DEFAULT_SOURCES))
    )
    output_file: Path = Path(os.environ.get("IG_OUTPUT_FILE", "data/locations.json"))
    cache_dir: Path = Path(os.environ.get("IG_CACHE_DIR", ".cache"))
    log_dir: Path = Path(os.environ.get("IG_LOG_DIR", "logs"))

    # Límite de seguridad para no entrar nunca en un bucle infinito.
    max_pages: int = _env_int("IG_MAX_PAGES", 200)
    # Reintentos por página ante errores de red o páginas de error transitorias.
    page_retries: int = _env_int("IG_PAGE_RETRIES", 3)
    # Pausa aleatoria entre páginas (segundos) para comportarse como una persona.
    min_delay: float = float(os.environ.get("IG_MIN_DELAY", "2.5"))
    max_delay: float = float(os.environ.get("IG_MAX_DELAY", "6.0"))
    nav_timeout_ms: int = _env_int("IG_NAV_TIMEOUT_MS", 45_000)

    # Si la página 1 no cambia se reutiliza la caché del resto, pero cada este
    # número de días se fuerza un recorrido completo para detectar cambios interiores.
    full_crawl_days: int = _env_int("IG_FULL_CRAWL_DAYS", 7)
    # Ignora la caché de páginas y las visita todas.
    full_refresh: bool = _env_bool("IG_FULL_REFRESH")

    headless: bool = _env_bool("IG_HEADLESS", True)
    locale: str = os.environ.get("IG_LOCALE", "es-ES")
    timezone: str = os.environ.get("IG_TIMEZONE", "Europe/Madrid")

    # Notificaciones (todas opcionales).
    webhook_url: str = os.environ.get("NOTIFY_WEBHOOK_URL", "")
    telegram_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    notify_on_success: bool = _env_bool("NOTIFY_ON_SUCCESS")

    run_url: str = field(default_factory=lambda: _github_run_url())

    @property
    def storage_state_file(self) -> Path:
        return self.cache_dir / "storage_state.json"

    def pages_cache_file(self, source: Source) -> Path:
        return self.cache_dir / f"pages-{source.key}.json"


def _github_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""
