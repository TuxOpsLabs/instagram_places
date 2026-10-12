"""Idioma de cada ubicación: diccionario de palabras marcadoras y helpers para el JSON.

Este módulo no usa IA. El diccionario resuelve, gratis y de forma determinista,
los nombres que contienen palabras que solo existen en un idioma (Teatre → ca,
Teatro → es, Beach → en). Lo que el diccionario no resuelve lo clasifica
ig_places.classify con Workers AI.

Ficheros (en el repo, para no pagar nunca dos veces la misma clasificación):
  data/languages.json           {"<id>": {"name", "lang", "source": "dict"|"ai"}}
  data/language_overrides.json  {"<id>": "<lang>"} correcciones manuales (prioridad máxima)
  data/excluded_locations.json  {"<id>": {"name", "reason"}} lo que no es un lugar real (fuera del JSON)
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

LANGUAGES_FILE = Path("data/languages.json")
OVERRIDES_FILE = Path("data/language_overrides.json")
EXCLUDED_FILE = Path("data/excluded_locations.json")

# Palabras que solo existen (o solo se usan así) en un idioma. Los topónimos y los
# artículos mallorquines/ibicencos (Sa, Es, Ca'n...) no están a propósito: forman
# parte de nombres propios. Las palabras válidas en español y catalán tampoco
# (catedral, cala, mirador...): esos casos los decide la IA (español por defecto).
MARKERS: dict[str, set[str]] = {
    "es": {
        "calle", "avenida", "avda", "paseo", "plaza", "playa", "playas", "puerto", "mercado",
        "teatro", "restaurante", "restaurantes", "iglesia", "castillo", "museo", "parque",
        "jardín", "jardin", "jardines", "palacio", "centro", "casco", "antiguo", "antigua",
        "viejo", "vieja", "nuevo", "nueva", "españa", "espana", "baleares", "isla", "islas",
        "cueva", "cuevas", "faro", "bahía", "bahia", "campo", "escuela", "colegio",
        "panadería", "panaderia", "pastelería", "pasteleria", "heladería", "heladeria",
        "librería", "libreria", "peluquería", "peluqueria", "tienda", "hermanos", "barrio",
        "ayuntamiento", "fundación", "fundacion", "mallorquina", "ibicenca", "ibicenco",
        "las", "los", "y", "con", "grados",
    },
    "ca": {
        "carrer", "avinguda", "passeig", "plaça", "platja", "platges", "mercat",
        "teatre", "església", "esglesia", "castell", "museu", "parc", "jardins", "jardí",
        "palau", "illa", "illes", "balears", "espanya", "llotja", "forn",
        "celler", "escola", "disseny", "banys", "àrabs", "arabs", "ajuntament", "fundació",
        "nou", "vell", "vella", "antic", "antiga", "esports", "mallorquí",
        "eivissenc", "eivissenca", "cafè",
    },
    "en": {
        "beach", "town", "old", "cathedral", "spain", "majorca", "island", "islands",
        "harbour", "harbor", "bay", "airport", "coffee", "house", "the", "of", "and", "by",
        "rooftop", "lounge", "kitchen", "garden", "gardens", "market", "street", "square",
        "church", "castle", "shop", "school", "university", "boat", "sunset", "steakhouse",
        "grill", "nightclub", "wellness", "smokehouse", "cocktail", "cocktails",
    },
}
_WORD_LANG = {word: lang for lang, words in MARKERS.items() for word in words}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def dictionary_lang(name: str) -> str | None:
    """Idioma según las palabras marcadoras, o None si no hay o se contradicen."""
    found = {_WORD_LANG[w] for w in _WORD_RE.findall(name.casefold()) if w in _WORD_LANG}
    return found.pop() if len(found) == 1 else None


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def without_excluded(locations: list[dict], excluded_file: Path = EXCLUDED_FILE) -> list[dict]:
    """Quita eventos, personas, marcas, barcos... (ids de data/excluded_locations.json)."""
    excluded = load_json(excluded_file, {})
    return [loc for loc in locations if loc["id"] not in excluded]


def _name_key(name: str) -> str:
    """Nombre sin mayúsculas, acentos ni signos, para detectar duplicados."""
    name = unicodedata.normalize("NFKD", name.casefold())
    name = "".join(c for c in name if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", name))


def without_duplicates(locations: list[dict]) -> tuple[list[dict], list[tuple[dict, dict]]]:
    """Una ubicación por nombre: la de ID mayor (las IDs cortas son ubicaciones antiguas
    de Instagram que la API de publicación rechaza; las largas son páginas de Facebook).

    Devuelve (ubicaciones en el orden original, [(descartada, conservada)]).
    """
    best: dict[str, dict] = {}
    for loc in locations:
        key = _name_key(loc["name"])
        if key not in best or int(loc["id"]) > int(best[key]["id"]):
            best[key] = loc
    kept = {id(loc) for loc in best.values()}
    dropped = [(loc, best[_name_key(loc["name"])]) for loc in locations if id(loc) not in kept]
    return [loc for loc in locations if id(loc) in kept], dropped


def effective_langs(
    languages_file: Path = LANGUAGES_FILE, overrides_file: Path = OVERRIDES_FILE
) -> dict[str, str]:
    """{id: lang} con las correcciones manuales aplicadas encima."""
    langs = {loc_id: e["lang"] for loc_id, e in load_json(languages_file, {}).items()}
    langs.update(load_json(overrides_file, {}))
    return langs


def with_langs(locations: list[dict], langs: dict[str, str]) -> list[dict]:
    """Añade "lang" a cada ubicación ya clasificada (id, name, lang)."""
    out = []
    for loc in locations:
        item = {"id": loc["id"], "name": loc["name"]}
        if loc["id"] in langs:
            item["lang"] = langs[loc["id"]]
        out.append(item)
    return out
