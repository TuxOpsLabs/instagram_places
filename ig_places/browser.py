"""Navegador real (Chromium vía Playwright) que visita y extrae las páginas del directorio."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from enum import Enum

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .config import Config
from .log import get_logger

log = get_logger()

COOKIE_BUTTON = re.compile(
    r"^(Permitir todas las cookies|Allow all cookies|Aceptar todo|Accept all|Permetre totes les galetes)$",
    re.IGNORECASE,
)
# Texto que Instagram muestra cuando una página del directorio no existe (sin dar 404).
ERROR_TEXT = re.compile(
    r"Se ha producido un error|Something went wrong|Hay un problema y no se ha podido cargar",
    re.IGNORECASE,
)

# Extrae los enlaces del recuadro "Lugares de ..." (las <li> dentro de <main>) y
# detecta si hay enlace "Ver más" hacia la página siguiente.
EXTRACT_JS = r"""
(nextPage) => {
  const locations = [];
  let hasNext = false;
  for (const a of document.querySelectorAll('main a[href]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/^(?:https:\/\/www\.instagram\.com)?\/explore\/locations\/(\d+)\/[^?#]*$/);
    if (m && a.closest('li')) {
      locations.push({ id: m[1], name: (a.innerText || a.textContent || '').trim() });
    } else if (href.includes('/explore/locations/') && new RegExp('[?&]page=' + nextPage + '(&|$)').test(href)) {
      hasNext = true;
    }
  }
  return { locations, hasNext };
}
"""


class PageStatus(Enum):
    OK = "ok"  # página con ubicaciones
    END = "end"  # "Se ha producido un error": no hay más páginas
    BLOCKED = "blocked"  # redirección a login / checkpoint
    UNKNOWN = "unknown"  # ni ubicaciones ni mensaje de error reconocible


@dataclass
class PageResult:
    status: PageStatus
    locations: list[dict] = field(default_factory=list)
    has_next: bool = False
    detail: str = ""


class InstagramBrowser:
    def __init__(self, config: Config):
        self.config = config
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.cookies_accepted = False

    # -- ciclo de vida -----------------------------------------------------

    def __enter__(self) -> "InstagramBrowser":
        cfg = self.config
        self._pw = sync_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled", "--lang=es-ES"]
        try:
            # "chromium" usa el Chrome completo en modo headless nuevo, más parecido a un navegador real.
            self._browser = self._pw.chromium.launch(
                channel="chromium", headless=cfg.headless, args=launch_args
            )
        except Exception:
            self._browser = self._pw.chromium.launch(headless=cfg.headless, args=launch_args)

        major = self._browser.version.split(".")[0]
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        )
        storage_state = cfg.storage_state_file if cfg.storage_state_file.exists() else None
        log.info(
            "Navegador Chromium %s iniciado (%s cookies guardadas)",
            self._browser.version,
            "con" if storage_state else "sin",
        )
        self._context = self._browser.new_context(
            user_agent=user_agent,
            locale=cfg.locale,
            timezone_id=cfg.timezone,
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
            storage_state=str(storage_state) if storage_state else None,
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._context.set_default_navigation_timeout(cfg.nav_timeout_ms)
        self._page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.save_state()
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()

    def save_state(self) -> None:
        """Guarda cookies/localStorage para reutilizar el consentimiento en la siguiente ejecución."""
        if not self._context:
            return
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self.config.storage_state_file))

    # -- navegación ----------------------------------------------------------

    def human_pause(self) -> None:
        delay = random.uniform(self.config.min_delay, self.config.max_delay)
        log.debug("Pausa de %.1fs", delay)
        self._page.wait_for_timeout(delay * 1000)

    def fetch(self, page_number: int) -> PageResult:
        page = self._page
        url = self.config.page_url(page_number)
        log.debug("GET %s", url)
        response = page.goto(url, wait_until="domcontentloaded")
        status = response.status if response else None

        if re.search(r"/accounts/login|/challenge|/checkpoint", page.url):
            return PageResult(PageStatus.BLOCKED, detail=f"Redirigido a {page.url}")
        if status and status >= 400:
            return PageResult(PageStatus.UNKNOWN, detail=f"HTTP {status}")

        # Espera a que aparezca la lista de ubicaciones o el mensaje de error.
        try:
            page.wait_for_function(
                """() => document.querySelector('main li a[href*="/explore/locations/"]')
                        || /Se ha producido un error|Something went wrong/i.test(document.body.innerText)""",
                timeout=20_000,
            )
        except PlaywrightTimeout:
            pass

        self._accept_cookies()

        data = page.evaluate(EXTRACT_JS, page_number + 1)
        locations = [loc for loc in data["locations"] if loc["name"]]
        if locations:
            return PageResult(PageStatus.OK, locations=locations, has_next=bool(data["hasNext"]))

        body = page.inner_text("body")
        if ERROR_TEXT.search(body):
            return PageResult(PageStatus.END, detail="Se ha producido un error")
        if re.search(r"/accounts/login", page.url) or "Inicia sesión para ver" in body:
            return PageResult(PageStatus.BLOCKED, detail="Muro de inicio de sesión")
        return PageResult(PageStatus.UNKNOWN, detail=f"Sin ubicaciones ni mensaje de error (HTTP {status})")

    def _accept_cookies(self) -> None:
        button = self._page.get_by_role("button", name=COOKIE_BUTTON)
        try:
            if button.count() and button.first.is_visible():
                button.first.click(timeout=5_000)
                self._page.wait_for_timeout(1_000)
                self.cookies_accepted = True
                log.info("Banner de cookies aceptado")
                self.save_state()
        except Exception as exc:
            log.warning("No se pudo aceptar el banner de cookies: %s", exc)
