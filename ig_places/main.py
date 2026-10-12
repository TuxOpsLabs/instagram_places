"""Orquestación: recorre los directorios de cada ciudad, usa la caché y genera el JSON."""

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
from .config import Config, Source
from .languages import effective_langs, with_langs, without_excluded
from .log import get_logger, setup_logging
from .notify import notify

log = get_logger()

# Si el nuevo resultado tiene menos de esta fracción de ubicaciones que el anterior,
# se considera una sincronización incompleta y no se sobrescribe el JSON.
MIN_RATIO = float(os.environ.get("IG_MIN_RATIO", "0.5"))


class SyncError(Exception):
    pass


class BlockedError(SyncError):
    """Instagram ha bloqueado el acceso; afectará también al resto de directorios."""


@dataclass
class Stats:
    """Estadísticas de un directorio (una ciudad)."""

    key: str
    locations: int = 0
    error: str = ""
    visited: list[int] = field(default_factory=list)
    from_cache: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def info(self, msg: str, *args) -> None:
        log.info(f"[{self.key}] {msg}", *args)

    def warn(self, msg: str, *args) -> None:
        text = f"[{self.key}] " + (msg % args if args else msg)
        log.warning(text)
        self.warnings.append(text)


@dataclass
class Report:
    """Resultado global de la ejecución."""

    sources: list[Stats] = field(default_factory=list)
    total: int = 0
    added: int = 0
    removed: int = 0
    written: bool = False

    @property
    def warnings(self) -> list[str]:
        return [w for s in self.sources for w in s.warnings]


def fetch_with_retries(
    browser: InstagramBrowser, source: Source, page: int, retries: int, retry_on_end: bool
) -> PageResult:
    """Visita una página reintentando ante errores transitorios.

    Un resultado END solo se reintenta si se esperaba que la página existiera.
    """
    result = PageResult(PageStatus.UNKNOWN, detail="no visitada")
    for attempt in range(1, retries + 1):
        try:
            result = browser.fetch(source, page)
        except Exception as exc:
            result = PageResult(PageStatus.UNKNOWN, detail=f"{type(exc).__name__}: {exc}")

        if result.status is PageStatus.OK or result.status is PageStatus.BLOCKED:
            return result
        if result.status is PageStatus.END and not retry_on_end:
            return result
        if attempt < retries:
            wait = 10 * attempt
            log.warning(
                "[%s] Página %d: %s (%s), reintento %d/%d en %ds",
                source.key, page, result.status.value, result.detail, attempt, retries - 1, wait,
            )
            time.sleep(wait)
    return result


def merge(pages: list[list[dict]]) -> list[dict]:
    """Une listas de ubicaciones en orden, sin duplicados."""
    seen: set[str] = set()
    locations: list[dict] = []
    for page in pages:
        for loc in page:
            if loc["id"] not in seen:
                seen.add(loc["id"])
                locations.append({"id": loc["id"], "name": loc["name"]})
    return locations


def merge_pages(pages: dict[int, list[dict]]) -> list[dict]:
    return merge([pages[p] for p in sorted(pages)])


def crawl(
    config: Config, source: Source, browser: InstagramBrowser, cache: PageCache, stats: Stats
) -> list[dict]:
    pages: dict[int, list[dict]] = {}
    prev_has_next = True
    prev_from_cache = False

    # La página 1 es el centinela: si no cambia, se reutiliza la caché del resto.
    use_sentinel = not config.full_refresh and cache.is_complete()
    if use_sentinel and cache.full_crawl_due(config.full_crawl_days):
        stats.info("Toca recorrido completo (último: %s)", cache.last_full_crawl_at[:10] or "nunca")
        use_sentinel = False

    page = 1
    while page <= config.max_pages:
        entry = cache.get(page)
        if browser.fetches:
            browser.human_pause()
        result = fetch_with_retries(browser, source, page, config.page_retries, retry_on_end=prev_has_next)
        stats.visited.append(page)

        if result.status is PageStatus.BLOCKED:
            raise BlockedError(f"Instagram ha bloqueado el acceso en la página {page}: {result.detail}")

        if result.status is PageStatus.OK:
            changed = cache.update(page, result.locations, result.has_next)
            if changed:
                stats.changed.append(page)
            stats.info(
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
                stats.info(
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
            stats.info("Página %d: 'Se ha producido un error' → fin del directorio", page)
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


def write_output(config: Config, locations: list[dict], report: Report) -> None:
    kept = without_excluded(locations)
    if len(kept) != len(locations):
        log.info("%d ubicaciones excluidas (data/excluded_locations.json)", len(locations) - len(kept))
    locations = kept

    previous: list[dict] = []
    if config.output_file.exists():
        try:
            previous = json.loads(config.output_file.read_text(encoding="utf-8")).get("locations", [])
        except ValueError:
            log.warning("El JSON anterior no era válido; se reemplazará")

    old_ids = {loc["id"] for loc in previous}
    new_ids = {loc["id"] for loc in locations}
    report.total = len(locations)
    report.added = len(new_ids - old_ids)
    report.removed = len(old_ids - new_ids)

    if previous and len(locations) < len(previous) * MIN_RATIO:
        raise SyncError(
            f"Resultado sospechoso: {len(locations)} ubicaciones frente a {len(previous)} anteriores. "
            "No se sobrescribe el JSON."
        )

    # Conserva el idioma ya clasificado (ig_places.classify añade el de las nuevas).
    content = json.dumps(
        {"locations": with_langs(locations, effective_langs())}, ensure_ascii=False, indent=2
    ) + "\n"
    if config.output_file.exists() and config.output_file.read_text(encoding="utf-8") == content:
        log.info("El JSON no ha cambiado (%d ubicaciones)", len(locations))
        return

    config.output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.output_file.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, config.output_file)
    report.written = True
    log.info(
        "JSON actualizado: %s (%d ubicaciones, +%d / -%d)",
        config.output_file, len(locations), report.added, report.removed,
    )


def summary_text(report: Report, duration: float) -> str:
    lines = [
        f"- Ubicaciones totales: {report.total} (+{report.added} / -{report.removed})",
        f"- JSON modificado: {'sí' if report.written else 'no'}",
        f"- Duración: {duration:.0f}s",
    ]
    for stats in report.sources:
        status = f"❌ {stats.error}" if stats.error else f"{stats.locations} ubicaciones"
        lines += [
            f"- **{stats.key}**: {status}",
            f"  - Páginas visitadas: {len(stats.visited)} {stats.visited}",
            f"  - Páginas desde caché: {len(stats.from_cache)} {stats.from_cache}",
            f"  - Páginas con cambios: {stats.changed or 'ninguna'}",
        ]
    if report.warnings:
        lines.append("- Avisos:")
        lines += [f"  - {w}" for w in report.warnings]
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
    log.info(
        "Directorios: %s | modo: %s",
        ", ".join(s.key for s in config.sources),
        "completo" if config.full_refresh else "incremental",
    )
    log.debug("Log en %s", log_file)

    # Caché antigua de un solo directorio → pasa a ser la del primero (Palma).
    legacy = config.cache_dir / "pages.json"
    first_cache = config.pages_cache_file(config.sources[0])
    if legacy.exists() and not first_cache.exists():
        legacy.rename(first_cache)

    started = time.monotonic()
    report = Report()
    results: list[list[dict]] = []
    fatal: Exception | None = None

    try:
        with InstagramBrowser(config) as browser:
            for source in config.sources:
                stats = Stats(source.key)
                report.sources.append(stats)
                cache = PageCache.load(config.pages_cache_file(source))
                stats.info("Inicio: %s (caché: %d páginas)", source.url, len(cache.pages))
                try:
                    locations = crawl(config, source, browser, cache, stats)
                    stats.locations = len(locations)
                    results.append(locations)
                except Exception as exc:
                    stats.error = str(exc)
                    log.error("[%s] Fallido: %s", source.key, exc)
                    if not isinstance(exc, SyncError):
                        log.error(traceback.format_exc())
                    if isinstance(exc, BlockedError):
                        break
                finally:
                    cache.save()  # conserva lo que sí se descargó bien

        failed = [s for s in report.sources if s.error]
        skipped = len(config.sources) - len(report.sources)
        if failed or skipped:
            raise SyncError(
                f"{len(failed)} directorio(s) con error"
                + (f" y {skipped} sin procesar" if skipped else "")
                + ". No se modifica el JSON para no dejarlo incompleto."
            )
        write_output(config, merge(results), report)
    except Exception as exc:
        fatal = exc

    duration = time.monotonic() - started
    if fatal is not None:
        log.error("Sincronización fallida: %s", fatal)
        if not isinstance(fatal, SyncError):
            log.error(traceback.format_exc())
        body = f"**Error:** {fatal}\n\n{summary_text(report, duration)}"
        write_github_summary("❌ Sincronización fallida", body)
        notify(config, "Sincronización de ubicaciones de Instagram FALLIDA", body, ok=False)
        return 1

    body = summary_text(report, duration)
    log.info("Resumen:\n%s", body)
    write_github_summary("✅ Sincronización completada", body)
    if config.notify_on_success or report.warnings:
        title = "Sincronización completada" + (" con avisos" if report.warnings else "")
        notify(config, title, body, ok=not report.warnings)
    return 0
