"""Orquestación: recorre las páginas del directorio, usa la caché y genera el JSON."""

from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .browser import InstagramBrowser, PageResult, PageStatus
from .cache import PageCache
from .config import Config
from .log import get_logger, setup_logging
from .notify import notify

log = get_logger()

# Si el nuevo resultado tiene menos de esta fracción de ubicaciones que el anterior,
# se considera una sincronización incompleta y no se sobrescribe el JSON.
MIN_RATIO = float(os.environ.get("IG_MIN_RATIO", "0.5"))


class SyncError(Exception):
    pass


@dataclass
class Stats:
    visited: list[int] = field(default_factory=list)
    from_cache: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total: int = 0
    added: int = 0
    removed: int = 0
    written: bool = False

    def warn(self, msg: str, *args) -> None:
        text = msg % args if args else msg
        log.warning(text)
        self.warnings.append(text)


def fetch_with_retries(browser: InstagramBrowser, page: int, retries: int, retry_on_end: bool) -> PageResult:
    """Visita una página reintentando ante errores transitorios.

    Un resultado END solo se reintenta si se esperaba que la página existiera.
    """
    result = PageResult(PageStatus.UNKNOWN, detail="no visitada")
    for attempt in range(1, retries + 1):
        try:
            result = browser.fetch(page)
        except Exception as exc:
            result = PageResult(PageStatus.UNKNOWN, detail=f"{type(exc).__name__}: {exc}")

        if result.status is PageStatus.OK or result.status is PageStatus.BLOCKED:
            return result
        if result.status is PageStatus.END and not retry_on_end:
            return result
        if attempt < retries:
            wait = 10 * attempt
            log.warning(
                "Página %d: %s (%s), reintento %d/%d en %ds",
                page, result.status.value, result.detail, attempt, retries - 1, wait,
            )
            time.sleep(wait)
    return result


def merge_pages(pages: dict[int, list[dict]]) -> list[dict]:
    """Une todas las páginas en orden, sin duplicados."""
    seen: set[str] = set()
    locations: list[dict] = []
    for page in sorted(pages):
        for loc in pages[page]:
            if loc["id"] not in seen:
                seen.add(loc["id"])
                locations.append({"id": loc["id"], "name": loc["name"]})
    return locations


def crawl(config: Config, cache: PageCache, stats: Stats) -> list[dict]:
    pages: dict[int, list[dict]] = {}
    prev_has_next = True
    prev_from_cache = False

    # La página 1 es el centinela: si no cambia, se reutiliza la caché del resto.
    use_sentinel = not config.full_refresh and cache.is_complete()
    if use_sentinel and cache.full_crawl_due(config.full_crawl_days):
        log.info("Toca recorrido completo (último: %s)", cache.last_full_crawl_at[:10] or "nunca")
        use_sentinel = False

    with InstagramBrowser(config) as browser:
        page = 1
        while page <= config.max_pages:
            entry = cache.get(page)
            if stats.visited:
                browser.human_pause()
            result = fetch_with_retries(browser, page, config.page_retries, retry_on_end=prev_has_next)
            stats.visited.append(page)

            if result.status is PageStatus.BLOCKED:
                raise SyncError(f"Instagram ha bloqueado el acceso en la página {page}: {result.detail}")

            if result.status is PageStatus.OK:
                changed = cache.update(page, result.locations, result.has_next)
                if changed:
                    stats.changed.append(page)
                log.info(
                    "Página %d: %d ubicaciones%s%s",
                    page, len(result.locations),
                    " (cambiada)" if changed else " (sin cambios)",
                    "" if result.has_next else " — sin enlace 'Ver más'",
                )
                if not prev_has_next:
                    stats.warn("La página %d existe aunque la anterior no enlazaba a ella", page)
                pages[page] = result.locations
                if page == 1 and use_sentinel and not changed:
                    rest = sorted(p for p in cache.pages if p > 1)
                    log.info(
                        "Página 1 sin cambios → se reutilizan %d páginas en caché sin visitarlas",
                        len(rest),
                    )
                    for p in rest:
                        pages[p] = cache.pages[p].locations
                    stats.from_cache.extend(rest)
                    return merge_pages(pages)
                prev_has_next, prev_from_cache = result.has_next, False
                page += 1
                continue

            if result.status is PageStatus.END:
                if page == 1:
                    raise SyncError("La página 1 muestra 'Se ha producido un error'")
                if prev_has_next and not prev_from_cache:
                    raise SyncError(
                        f"La página {page - 1} enlaza a la {page}, pero esta muestra un error tras "
                        f"{config.page_retries} intentos"
                    )
                log.info("Página %d: 'Se ha producido un error' → fin del directorio", page)
                cache.drop_from(page)
                if not stats.stale:
                    cache.mark_full_crawl()
                break

            # UNKNOWN tras agotar reintentos.
            if not prev_has_next:
                stats.warn("Página %d no concluyente (%s); se asume fin del directorio", page, result.detail)
                break
            if entry is not None:
                stats.warn(
                    "Página %d falló (%s); se usan los datos en caché del %s",
                    page, result.detail, entry.fetched_at[:10],
                )
                stats.stale.append(page)
                pages[page] = entry.locations
                prev_has_next, prev_from_cache = entry.has_next, True
                page += 1
                continue
            raise SyncError(f"No se pudo cargar la página {page}: {result.detail}")
        else:
            raise SyncError(f"Se alcanzó el límite de {config.max_pages} páginas sin encontrar el final")

    return merge_pages(pages)


def write_output(config: Config, locations: list[dict], stats: Stats) -> None:
    previous: list[dict] = []
    if config.output_file.exists():
        try:
            previous = json.loads(config.output_file.read_text(encoding="utf-8")).get("locations", [])
        except ValueError:
            stats.warn("El JSON anterior no era válido; se reemplazará")

    old_ids = {loc["id"] for loc in previous}
    new_ids = {loc["id"] for loc in locations}
    stats.total = len(locations)
    stats.added = len(new_ids - old_ids)
    stats.removed = len(old_ids - new_ids)

    if previous and len(locations) < len(previous) * MIN_RATIO:
        raise SyncError(
            f"Resultado sospechoso: {len(locations)} ubicaciones frente a {len(previous)} anteriores. "
            "No se sobrescribe el JSON."
        )

    content = json.dumps({"locations": locations}, ensure_ascii=False, indent=2) + "\n"
    if config.output_file.exists() and config.output_file.read_text(encoding="utf-8") == content:
        log.info("El JSON no ha cambiado (%d ubicaciones)", len(locations))
        return

    config.output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.output_file.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, config.output_file)
    stats.written = True
    log.info(
        "JSON actualizado: %s (%d ubicaciones, +%d / -%d)",
        config.output_file, len(locations), stats.added, stats.removed,
    )


def summary_text(stats: Stats, duration: float) -> str:
    lines = [
        f"- Ubicaciones totales: {stats.total} (+{stats.added} / -{stats.removed})",
        f"- Páginas visitadas: {len(stats.visited)} {stats.visited}",
        f"- Páginas desde caché: {len(stats.from_cache)} {stats.from_cache}",
        f"- Páginas con cambios: {stats.changed or 'ninguna'}",
        f"- JSON modificado: {'sí' if stats.written else 'no'}",
        f"- Duración: {duration:.0f}s",
    ]
    if stats.warnings:
        lines.append("- Avisos:")
        lines += [f"  - {w}" for w in stats.warnings]
    return "\n".join(lines)


def write_github_summary(title: str, body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## {title}\n\n{body}\n")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ig_places", description=__doc__)
    parser.add_argument("--full", action="store_true", help="ignora la caché y visita todas las páginas")
    parser.add_argument("--headed", action="store_true", help="muestra la ventana del navegador")
    parser.add_argument("--max-pages", type=int, help="límite de páginas a recorrer")
    parser.add_argument("-v", "--verbose", action="store_true", help="log de depuración en consola")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config()
    if args.full:
        config.full_refresh = True
    if args.headed:
        config.headless = False
    if args.max_pages:
        config.max_pages = args.max_pages

    log_file = setup_logging(config.log_dir, args.verbose)
    log.info("=== Sincronización de ubicaciones de Instagram ===")
    log.info("Origen: %s | modo: %s", config.base_url, "completo" if config.full_refresh else "incremental")
    log.debug("Log en %s", log_file)

    started = time.monotonic()
    stats = Stats()
    cache = PageCache.load(config.pages_cache_file)
    log.info("Caché: %d páginas conocidas", len(cache.pages))

    try:
        locations = crawl(config, cache, stats)
        write_output(config, locations, stats)
    except Exception as exc:
        cache.save()  # conserva lo que sí se descargó bien
        duration = time.monotonic() - started
        is_expected = isinstance(exc, SyncError)
        log.error("Sincronización fallida: %s", exc)
        if not is_expected:
            log.error(traceback.format_exc())
        body = f"**Error:** {exc}\n\n{summary_text(stats, duration)}"
        write_github_summary("❌ Sincronización fallida", body)
        notify(config, "Sincronización de ubicaciones de Instagram FALLIDA", body, ok=False)
        return 1

    cache.save()
    duration = time.monotonic() - started
    body = summary_text(stats, duration)
    log.info("Resumen:\n%s", body)
    write_github_summary("✅ Sincronización completada", body)
    if config.notify_on_success or stats.warnings:
        title = "Sincronización completada" + (" con avisos" if stats.warnings else "")
        notify(config, title, body, ok=not stats.warnings)
    return 0
