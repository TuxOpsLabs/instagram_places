"""Caché de páginas del directorio de ubicaciones.

Por cada página guarda las ubicaciones extraídas y un hash de su contenido.
La página 1 actúa de centinela: si no ha cambiado y la caché está completa
(se llegó hasta la página final), se reutilizan el resto de páginas sin
visitarlas. Cada cierto número de días se fuerza un recorrido completo para
detectar cambios en páginas interiores.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_VERSION = 2


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(locations: list[dict], has_next: bool) -> str:
    raw = json.dumps({"l": locations, "n": has_next}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class PageEntry:
    locations: list[dict]
    has_next: bool
    hash: str
    fetched_at: str
    changed_at: str


@dataclass
class PageCache:
    path: Path
    pages: dict[int, PageEntry] = field(default_factory=dict)
    # Fecha del último recorrido que llegó hasta el final sin usar datos cacheados.
    last_full_crawl_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "PageCache":
        cache = cls(path)
        if not path.exists():
            return cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != CACHE_VERSION:
                return cache
            cache.pages = {int(k): PageEntry(**v) for k, v in data.get("pages", {}).items()}
            cache.last_full_crawl_at = data.get("last_full_crawl_at", "")
        except (ValueError, TypeError, KeyError):
            # Caché corrupta: se ignora y se reconstruye visitando todas las páginas.
            cache.pages = {}
            cache.last_full_crawl_at = ""
        return cache

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": CACHE_VERSION,
            "last_full_crawl_at": self.last_full_crawl_at,
            "pages": {str(k): asdict(v) for k, v in sorted(self.pages.items())},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, page: int) -> PageEntry | None:
        return self.pages.get(page)

    def update(self, page: int, locations: list[dict], has_next: bool) -> bool:
        """Guarda el resultado de visitar una página. Devuelve True si ha cambiado."""
        at = now_utc().isoformat()
        new_hash = content_hash(locations, has_next)
        old = self.pages.get(page)
        changed = old is None or old.hash != new_hash
        self.pages[page] = PageEntry(
            locations=locations,
            has_next=has_next,
            hash=new_hash,
            fetched_at=at,
            changed_at=at if changed else old.changed_at,
        )
        return changed

    def is_complete(self) -> bool:
        """Páginas 1..N consecutivas y la última sin enlace a la siguiente."""
        if not self.pages:
            return False
        last = max(self.pages)
        return set(self.pages) == set(range(1, last + 1)) and not self.pages[last].has_next

    def full_crawl_due(self, max_days: int) -> bool:
        if not self.last_full_crawl_at:
            return True
        last = datetime.fromisoformat(self.last_full_crawl_at)
        # Margen de unas horas para que la ejecución diaria del día N cuente como vencida.
        return now_utc() - last >= timedelta(days=max_days) - timedelta(hours=2)

    def mark_full_crawl(self) -> None:
        self.last_full_crawl_at = now_utc().isoformat()

    def drop_from(self, page: int) -> None:
        """Elimina las páginas >= page (el directorio ha encogido)."""
        for p in [p for p in self.pages if p >= page]:
            del self.pages[p]
