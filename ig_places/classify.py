"""Clasificación del idioma de los nombres de ubicación con Cloudflare Workers AI.

Categorías:
  es       español (también los nombres válidos en español y catalán a la vez)
  en       inglés
  ca       catalán (solo con alguna palabra inequívocamente catalana)
  other    otro idioma (alemán, francés, italiano...)
  neutral  sin idioma: solo nombres propios, topónimos, marcas o números

Cada ubicación se clasifica una sola vez y se guarda por ID en
data/languages.json; solo se vuelven a clasificar las nuevas o las que cambian
de nombre. data/language_overrides.json ({"<id>": "<lang>"}) tiene prioridad
sobre el modelo para corregir a mano.

Uso:
  python -m ig_places.classify            # incremental
  python -m ig_places.classify --all      # reclasifica todo (ignora data/languages.json)
  python -m ig_places.classify --review review.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from .log import get_logger, setup_logging

log = get_logger()

LANGS = ("es", "en", "ca", "other", "neutral")
DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

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
- Palabras internacionales (bar, hotel, club, restaurant, café, gym, spa) no
  determinan el idioma por sí solas.
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
        return "; ".join(notes)

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
                if exc.code in (401, 403):
                    raise ClassifyError(
                        f"Workers AI rechaza el token (HTTP {exc.code}): {detail} | {self.verify_token()}"
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
        self.usage.update(result.get("usage") or {})
        response = result.get("response")
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


def classify_names(ai: WorkersAI, names: list[str], batch_size: int) -> list[str | None]:
    """Clasifica en lotes; los nombres que el modelo omite se reintentan en lotes más pequeños."""
    results: list[str | None] = [None] * len(names)
    pending = list(range(len(names)))
    for size in (batch_size, max(1, batch_size // 4), 1):
        if not pending:
            break
        missing = []
        for start in range(0, len(pending), size):
            idx = pending[start : start + size]
            labels = classify_batch(ai, [names[i] for i in idx])
            for pos, i in enumerate(idx, 1):
                if pos in labels:
                    results[i] = labels[pos]
                else:
                    missing.append(i)
            done = len(names) - len(pending) + start + len(idx)
            log.info("Clasificados %d/%d", min(done, len(names)), len(names))
        if missing:
            log.warning("%d nombres sin respuesta; se reintentan en lotes de %d", len(missing), max(1, size // 4))
        pending = missing
    return results


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ig_places.classify", description="Clasifica el idioma de las ubicaciones")
    parser.add_argument("--locations", type=Path, default=Path("data/locations.json"))
    parser.add_argument("--languages", type=Path, default=Path("data/languages.json"))
    parser.add_argument("--overrides", type=Path, default=Path("data/language_overrides.json"))
    parser.add_argument("--all", action="store_true", help="reclasifica todas las ubicaciones")
    parser.add_argument("--review", type=Path, help="escribe un CSV (lang,name,id) para revisar")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("CF_AI_BATCH_SIZE", "40")))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(Path(os.environ.get("IG_LOG_DIR", "logs")), args.verbose)
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        log.error("Faltan CLOUDFLARE_ACCOUNT_ID y/o CLOUDFLARE_API_TOKEN")
        return 1
    ai = WorkersAI(account_id, token, os.environ.get("CF_AI_MODEL", DEFAULT_MODEL))

    locations = load_json(args.locations, {"locations": []})["locations"]
    known: dict = {} if args.all else load_json(args.languages, {})
    overrides: dict = load_json(args.overrides, {})

    todo = [loc for loc in locations if known.get(loc["id"], {}).get("name") != loc["name"]]
    log.info(
        "Modelo %s | %d ubicaciones, %d por clasificar, %d overrides",
        ai.model, len(locations), len(todo), len(overrides),
    )

    started = time.monotonic()
    try:
        labels = classify_names(ai, [loc["name"] for loc in todo], args.batch_size) if todo else []
    except ClassifyError as exc:
        log.error("%s", exc)
        return 1

    for loc, lang in zip(todo, labels):
        if lang:
            known[loc["id"]] = {"name": loc["name"], "lang": lang}
        else:
            log.warning("Sin clasificar: %s (%s)", loc["name"], loc["id"])

    # Solo se conservan las ubicaciones que siguen existiendo, en el mismo orden.
    result = {}
    for loc in locations:
        entry = known.get(loc["id"])
        if loc["id"] in overrides:
            entry = {"name": loc["name"], "lang": overrides[loc["id"]], "override": True}
        if entry:
            result[loc["id"]] = entry
    write_json(args.languages, result)

    counts = Counter(e["lang"] for e in result.values())
    summary = ", ".join(f"{k}: {counts.get(k, 0)}" for k in LANGS)
    log.info("Resultado (%ds): %s | uso: %s", time.monotonic() - started, summary, dict(ai.usage) or "n/d")

    if args.review:
        order = {k: n for n, k in enumerate(LANGS)}
        rows = sorted(result.items(), key=lambda kv: (order[kv[1]["lang"]], kv[1]["name"].lower()))
        with args.review.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["lang", "name", "id"])
            for loc_id, entry in rows:
                writer.writerow([entry["lang"], entry["name"], loc_id])
        log.info("CSV de revisión: %s", args.review)

    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## Clasificación de idioma\n\n- Modelo: `{ai.model}`\n- {summary}\n- Uso: {dict(ai.usage)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
