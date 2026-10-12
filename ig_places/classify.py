"""Clasificación del idioma de los nombres de ubicación con Cloudflare Workers AI.

Categorías:
  es       español (también los nombres válidos en español y catalán a la vez)
  en       inglés
  ca       catalán (solo con alguna palabra inequívocamente catalana)
  other    otro idioma (alemán, francés, italiano...)
  neutral  sin idioma: solo nombres propios, topónimos, marcas o números

Primero se aplica el diccionario de ig_places.languages (gratis); solo los
nombres sin palabras marcadoras van a Workers AI. Cada ubicación se clasifica
una sola vez y se guarda por ID en data/languages.json (en el repo), tras cada
petición: si se agota la cuota diaria no se pierde nada y se continúa al día
siguiente. data/language_overrides.json ({"<id>": "<lang>"}) tiene prioridad.
Al terminar escribe "lang" en data/locations.json.

Uso:
  python -m ig_places.classify            # incremental
  python -m ig_places.classify --review review.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .config import Config
from .languages import (
    LANGUAGES_FILE,
    OVERRIDES_FILE,
    dictionary_lang,
    effective_langs,
    with_langs,
    without_excluded,
)
from .log import get_logger, setup_logging
from .notify import notify

log = get_logger()

LANGS = ("es", "en", "ca", "other", "neutral")
DEFAULT_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"

SYSTEM_PROMPT = """\
Eres un lingüista experto en español, catalán (incluido el mallorquín) e inglés.
Clasificas nombres de ubicaciones de Instagram de Mallorca según el idioma de sus
palabras comunes. Responde solo con el JSON pedido.

Categorías:
- "es": español. También cuando TODAS las palabras comunes son válidas a la vez en
  español y en catalán (en caso de duda entre español y catalán, "es").
- "ca": catalán. Solo si hay al menos una palabra común que es catalana y NO española
  (p. ej. platja, carrer, plaça, església, mercat, museu, jardins, banys, mallorquí,
  forn, celler, illes, Espanya).
- "en": inglés (beach, cathedral, old town, coffee, rooftop, Spain, Majorca...).
- "other": otro idioma (alemán, francés, italiano, etc.).
- "neutral": no hay ninguna palabra común, solo nombres propios, topónimos, marcas
  o números.

Reglas:
- Los nombres propios, topónimos (Palma, Mallorca, Sóller, Deià, Es Trenc,
  Sa Calobra, Santa Catalina...), nombres de negocio y marcas NO cuentan para el
  idioma; aunque contengan artículos mallorquines (Sa, Es, Ca'n) forman parte del
  nombre propio.
- Los nombres de país sí cuentan: Spain → en, España → es, Espanya → ca.
- Solo las palabras internacionales idénticas en todos los idiomas (bar, hotel,
  club, spa, gym) no determinan el idioma. Las formas propias de un idioma sí:
  Restaurante, Teatro, Calle, Playa, Puerto → es; Restaurant (sin -e) es neutro.
- Si hay palabras de varios idiomas, elige el idioma de la palabra común que
  describe el lugar (p. ej. "Es Trenc Beach" → en).

Ejemplos:
Palma De Mallorca, Spain → en
Palma de Mallorca, España → es
Palma de Mallorca, Espanya → ca
Catedral de Mallorca → es
Cathedral Palma → en
Kathedrale In Palma → other
Platja de Palma → ca
Playa de Palma → es
Mercat de l'Olivar → ca
Palma Old Town → en
Casco Antiguo de Palma → es
Palma → neutral
Mallorca → neutral
Taco Bell → neutral
Ca'n Joan de s'Aigo → neutral
Bar Abaco → neutral
Hard Rock Cafe Mallorca → en
Cappuccino Grand Cafe → en
Paseo Marítimo → es
Plaza Mayor de Palma de Mallorca → es
Cattedrale di Santa Maria (Palma di Maiorca) → other
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "lang": {"type": "string", "enum": list(LANGS)},
                },
                "required": ["i", "lang"],
            },
        }
    },
    "required": ["labels"],
}


class ClassifyError(Exception):
    pass


class QuotaExceeded(ClassifyError):
    """Cuota diaria gratuita agotada: se continúa en la siguiente ejecución."""


class WorkersAI:
    def __init__(self, account_id: str, token: str, model: str):
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
        self.token = token
        self.model = model
        self.usage = Counter()

    def verify_token(self) -> str:
        """Diagnóstico de un 401/403: comprueba el token sin revelarlo."""
        account_url = self.url.split("/ai/run/")[0]
        notes = [f"longitud del token: {len(self.token)}"]
        for label, url in (("token de usuario", "https://api.cloudflare.com/client/v4/user/tokens/verify"),
                           ("token de cuenta", f"{account_url}/tokens/verify")):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    status = json.loads(resp.read()).get("result", {}).get("status")
                notes.append(f"{label}: válido ({status})")
            except urllib.error.HTTPError as exc:
                notes.append(f"{label}: no válido (HTTP {exc.code})")
            except Exception as exc:
                notes.append(f"{label}: no comprobado ({exc})")

        # ¿Ve el token la cuenta configurada? (no se registran los IDs: los logs son públicos)
        account_id = account_url.rsplit("/", 1)[-1]
        notes.append(f"CLOUDFLARE_ACCOUNT_ID: {len(account_id)} caracteres"
                     + ("" if re.fullmatch(r"[0-9a-f]{32}", account_id) else " (formato no esperado: 32 hex)"))
        req = urllib.request.Request(
            "https://api.cloudflare.com/client/v4/accounts?per_page=50",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                ids = [a.get("id") for a in json.loads(resp.read()).get("result") or []]
            notes.append(
                f"el token ve {len(ids)} cuenta(s); la configurada "
                + ("SÍ está entre ellas" if account_id in ids else "NO está entre ellas")
            )
        except urllib.error.HTTPError as exc:
            notes.append(f"no se pudieron listar las cuentas del token (HTTP {exc.code})")
        except Exception as exc:
            notes.append(f"no se pudieron listar las cuentas del token ({exc})")
        return "; ".join(notes)

    def list_models(self) -> list[str]:
        base = self.url.split("/ai/run/")[0]
        req = urllib.request.Request(
            f"{base}/ai/models/search?task=Text%20Generation&per_page=100",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return sorted(m["name"] for m in json.loads(resp.read()).get("result") or [])

    def run(self, messages: list[dict], max_tokens: int) -> dict:
        body = {
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, 5):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if "daily free allocation" in detail or '"code":4006' in detail.replace(" ", ""):
                    raise QuotaExceeded("Cuota diaria gratuita de Workers AI agotada")
                if "not available on the Workers Free plan" in detail:
                    raise ClassifyError(f"El modelo {self.model} no está disponible en el plan gratuito de Workers AI")
                if exc.code in (401, 403):
                    raise ClassifyError(
                        f"Workers AI rechaza la petición (HTTP {exc.code}): {detail} | {self.verify_token()}"
                    )
                if attempt == 4 or exc.code not in (408, 429, 500, 502, 503, 504):
                    raise ClassifyError(f"Workers AI HTTP {exc.code}: {detail}")
                log.warning("Workers AI HTTP %d, reintento %d/3", exc.code, attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 4:
                    raise ClassifyError(f"Workers AI no responde: {exc}")
                log.warning("Workers AI no responde (%s), reintento %d/3", exc, attempt)
            time.sleep(5 * attempt)

        if not data.get("success", True):
            raise ClassifyError(f"Workers AI devolvió error: {data.get('errors')}")
        result = data.get("result", {})
        usage = result.get("usage") or {}
        self.usage.update({k: v for k, v in usage.items() if isinstance(v, (int, float))})
        response = result.get("response")
        if response is None and result.get("choices"):  # formato OpenAI (gpt-oss, etc.)
            response = (result["choices"][0].get("message") or {}).get("content")
        # Con JSON mode la respuesta puede llegar ya como objeto o como texto JSON.
        if isinstance(response, str):
            start, end = response.find("{"), response.rfind("}")
            response = json.loads(response[start : end + 1]) if start >= 0 else {}
        return response or {}


def classify_batch(ai: WorkersAI, names: list[str]) -> dict[int, str]:
    numbered = "\n".join(f"{i}. {name}" for i, name in enumerate(names, 1))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f'Clasifica estos {len(names)} nombres. Devuelve {{"labels": '
            f'[{{"i": <número>, "lang": "<categoría>"}}, ...]}} con una entrada por nombre.\n\n{numbered}',
        },
    ]
    response = ai.run(messages, max_tokens=40 + 16 * len(names))
    labels: dict[int, str] = {}
    for item in response.get("labels", []):
        try:
            i, lang = int(item["i"]), str(item["lang"]).strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= i <= len(names) and lang in LANGS:
            labels[i] = lang
    return labels


def classify_names(ai: WorkersAI, names: list[str], batch_size: int, on_batch) -> None:
    """Clasifica en lotes y llama a on_batch({índice: lang}) tras cada petición.

    Los nombres que el modelo omite se reintentan en lotes más pequeños.
    """
    pending = list(range(len(names)))
    done = 0
    for size in (batch_size, max(1, batch_size // 4), 1):
        if not pending:
            break
        missing = []
        for start in range(0, len(pending), size):
            idx = pending[start : start + size]
            labels = classify_batch(ai, [names[i] for i in idx])
            on_batch({i: labels[pos] for pos, i in enumerate(idx, 1) if pos in labels})
            missing += [i for pos, i in enumerate(idx, 1) if pos not in labels]
            done += sum(1 for pos in range(1, len(idx) + 1) if pos in labels)
            log.info("Clasificados con IA %d/%d", done, len(names))
        if len(missing) == len(pending) and size == batch_size:
            # Ninguna respuesta útil: el modelo no sigue el formato; reintentar uno a uno solo gastaría cuota.
            raise ClassifyError(f"El modelo {ai.model} no devuelve respuestas en el formato esperado")
        if missing:
            log.warning("%d nombres sin respuesta; se reintentan en lotes de %d", len(missing), max(1, size // 4))
        pending = missing


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_review(path: Path, locations: list[dict], known: dict, overrides: dict, langs: dict[str, str]) -> None:
    order = {k: n for n, k in enumerate(LANGS)}
    rows = [
        (langs[loc["id"]], loc["name"], loc["id"], "override" if loc["id"] in overrides else known[loc["id"]]["source"])
        for loc in locations
        if loc["id"] in langs
    ]
    rows.sort(key=lambda r: (order[r[0]], r[1].lower()))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["lang", "name", "id", "source"])
        writer.writerows(rows)
    log.info("CSV de revisión: %s", path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ig_places.classify", description="Clasifica el idioma de las ubicaciones")
    parser.add_argument("--locations", type=Path, default=Path("data/locations.json"))
    parser.add_argument("--languages", type=Path, default=LANGUAGES_FILE)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_FILE)
    parser.add_argument("--review", type=Path, help="escribe un CSV (lang,name,id,source) para revisar")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("CF_AI_BATCH_SIZE", "40")))
    parser.add_argument("--list-models", action="store_true", help="lista los modelos de texto disponibles")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    config = Config()
    setup_logging(config.log_dir, args.verbose)
    log.info("=== Clasificación de idioma ===")

    # Lo excluido no se clasifica (no gasta cuota) ni se escribe en el JSON.
    locations = without_excluded(load_json(args.locations, {"locations": []})["locations"])
    known: dict = load_json(args.languages, {})
    names_by_id = {loc["id"]: loc["name"] for loc in locations}

    def save() -> None:
        # Solo ubicaciones que siguen existiendo, en el orden del JSON.
        current = {i: known[i] for i in names_by_id if i in known}
        write_json(args.languages, current)

    # 1) Diccionario: gratis y determinista.
    pending = [loc for loc in locations if known.get(loc["id"], {}).get("name") != loc["name"]]
    by_dict = 0
    for loc in pending:
        lang = dictionary_lang(loc["name"])
        if lang:
            known[loc["id"]] = {"name": loc["name"], "lang": lang, "source": "dict"}
            by_dict += 1
    todo = [loc for loc in pending if loc["id"] not in known or known[loc["id"]]["name"] != loc["name"]]
    save()
    log.info(
        "%d ubicaciones | %d ya clasificadas | %d por diccionario | %d para IA",
        len(locations), len(locations) - len(pending), by_dict, len(todo),
    )

    # 2) Workers AI solo para lo que queda.
    status, error = 0, ""
    started = time.monotonic()
    ai = None
    if todo or args.list_models:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
        if not account_id or not token:
            status, error = 1, "Faltan CLOUDFLARE_ACCOUNT_ID y/o CLOUDFLARE_API_TOKEN"
            log.error("%s", error)
        else:
            ai = WorkersAI(account_id, token, os.environ.get("CF_AI_MODEL") or DEFAULT_MODEL)
            if args.list_models:
                for name in ai.list_models():
                    log.info("Modelo disponible: %s", name)
                return 0
            log.info("Modelo %s", ai.model)

            def on_batch(labels: dict[int, str]) -> None:
                for i, lang in labels.items():
                    loc = todo[i]
                    known[loc["id"]] = {"name": loc["name"], "lang": lang, "source": "ai"}
                save()

            try:
                classify_names(ai, [loc["name"] for loc in todo], args.batch_size, on_batch)
            except QuotaExceeded as exc:
                error = f"{exc}; se continuará en la próxima ejecución"
                log.warning("%s", error)
            except ClassifyError as exc:
                status, error = 1, str(exc)
                log.error("%s", exc)

    # 3) Escribe "lang" en el JSON de ubicaciones.
    save()
    langs = effective_langs(args.languages, args.overrides)
    content = json.dumps({"locations": with_langs(locations, langs)}, ensure_ascii=False, indent=2) + "\n"
    if args.locations.read_text(encoding="utf-8") != content:
        tmp = args.locations.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, args.locations)
        log.info("Idiomas escritos en %s", args.locations)

    missing = sum(1 for loc in locations if loc["id"] not in langs)
    counts = Counter(langs[loc["id"]] for loc in locations if loc["id"] in langs)
    summary = ", ".join(f"{k}: {counts.get(k, 0)}" for k in LANGS) + f", sin clasificar: {missing}"
    usage = dict(ai.usage) if ai else {}
    log.info("Resultado (%ds): %s | uso IA: %s", time.monotonic() - started, summary, usage or "ninguno")

    if args.review:
        write_review(args.review, locations, known, load_json(args.overrides, {}), langs)

    body = f"- {summary}\n- Por diccionario: {by_dict}, con IA: {sum(1 for e in known.values() if e.get('source') == 'ai')}\n- Uso IA: {usage or 'ninguno'}"
    if error:
        body = f"**{'Error' if status else 'Aviso'}:** {error}\n\n{body}"
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## Clasificación de idioma\n\n{body}\n")
    if error:
        notify(config, "Clasificación de idioma " + ("FALLIDA" if status else "incompleta"), body, ok=False)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
