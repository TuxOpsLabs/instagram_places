# instagram-places

Sincroniza a diario las ubicaciones de Instagram de los directorios de
[Palma de Mallorca](https://www.instagram.com/explore/locations/c692339/palmademallorca-spain/) e
[Ibiza](https://www.instagram.com/explore/locations/c685540/ibiza-spain/)
y las guarda juntas en `data/locations.json`:

```json
{
  "locations": [
    { "id": "607881395899347", "name": "Palma De Mallorca, Spain", "lang": "en" }
  ]
}
```

`lang` es el idioma del nombre: `es`, `en`, `ca`, `other` (otro idioma) o `neutral` (solo nombres propios, topónimos o marcas). Ver [Idioma](#idioma).

## Cómo funciona

1. Abre Chromium headless (Playwright) con un perfil de navegador real (user agent, idioma `es-ES`, zona horaria `Europe/Madrid`).
2. Acepta el banner de cookies la primera vez y guarda las cookies en `.cache/storage_state.json`, así que en las ejecuciones siguientes ya no aparece.
3. Para cada directorio, recorre `?page=1`, `?page=2`… y extrae cada enlace del recuadro **Lugares de …** (`/explore/locations/<id>/<slug>/` → `id` + texto).
4. Termina cuando una página muestra *"Se ha producido un error"* (la siguiente a la última, que ya no tiene enlace "Ver más").
5. Une todos los directorios en orden (primero Palma, luego Ibiza), sin duplicados, y reescribe el JSON solo si ha cambiado. Si algún directorio falla, el JSON no se toca, para no dejarlo incompleto.

Los directorios se configuran con `IG_SOURCES` (`clave=url,clave=url`); por defecto, `palma` e `ibiza`.

## Caché

`.cache/pages-<clave>.json` guarda, para cada directorio y página, sus ubicaciones y un hash del contenido.

- La **página 1 es el centinela**: si no ha cambiado respecto a la caché, se reutilizan el resto de páginas cacheadas sin visitarlas (1 sola petición por directorio).
- Si la página 1 cambia, se recorren todas las páginas.
- Cada `IG_FULL_CRAWL_DAYS` días (7 por defecto) se fuerza un recorrido completo, para detectar cambios en páginas interiores aunque la 1 siga igual.
- Si una página falla pero está en caché, se usan los datos cacheados y se avisa.

Para forzar una pasada completa: `python -m ig_places --full`, o lanzar el workflow a mano con *full_refresh*.

En GitHub Actions la carpeta `.cache` se conserva entre ejecuciones con `actions/cache`.

## Exclusiones

El JSON solo incluye lugares reales: sitios, calles, playas o locales concretos. `data/excluded_locations.json` (`{"<id>": {"name", "reason"}}`) lista lo que Instagram mezcla en el directorio y no lo es: eventos y fiestas, personas, cruceros, marcas o cadenas sin un local concreto, negocios online, frases y lugares fuera de la zona. También los duplicados: de cada nombre repetido (sin distinguir mayúsculas ni acentos) se conserva la primera ID del directorio y el resto queda como `"reason": "duplicado de <id>"`. Se filtra en cada sincronización, así que tampoco se clasifica su idioma.

## Idioma

Instagram no guarda en qué idioma está el nombre de una ubicación, así que se infiere del texto (`ig_places/languages.py` y `ig_places/classify.py`):

1. **Diccionario** de palabras que solo existen en un idioma (*Teatre, Platja, Carrer* → `ca`; *Teatro, Playa, Calle, Restaurante* → `es`; *Beach, Old Town, Spain* → `en`). Gratis y determinista; resuelve aprox. un tercio de los nombres.
2. **Cloudflare Workers AI** (`@cf/mistralai/mistral-small-3.1-24b-instruct`, plan gratuito) para el resto. Si un nombre vale en español y en catalán, cuenta como `es`.
3. **`data/language_overrides.json`** (`{"<id>": "<lang>"}`): correcciones manuales, con prioridad sobre todo lo anterior.

Cada ubicación se clasifica una sola vez: el resultado se guarda por ID en `data/languages.json` (en el repo), tras cada petición. Solo se clasifican las ubicaciones nuevas o las que cambian de nombre; si se agota la cuota diaria, se continúa al día siguiente sin perder nada.

Requiere los secrets `CLOUDFLARE_ACCOUNT_ID` y `CLOUDFLARE_API_TOKEN` (token con permiso *Workers AI*).

## Logging

- Consola + fichero `logs/sync-AAAA-MM-DD.log` (nivel DEBUG en el fichero).
- En Actions, los logs se suben como artefacto (30 días) y se escribe un resumen en la página de la ejecución.

## Notificaciones

En caso de error (o de avisos), se notifica por los canales configurados como *secrets* del repositorio:

| Secret | Uso |
| --- | --- |
| `NOTIFY_WEBHOOK_URL` | Webhook de Slack, Discord, Google Chat, Mattermost… |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Mensaje de Telegram |

Además, si el workflow falla se abre (o se comenta) una issue en el repositorio y GitHub envía su email de fallo habitual.
Para recibir también un aviso cuando todo va bien, crea la *variable* de repositorio `NOTIFY_ON_SUCCESS=true`.

Protecciones que hacen fallar la ejecución (y notificar) en lugar de guardar datos malos:
- redirección a login/checkpoint (bloqueo de Instagram);
- la página 1 muestra error, o una página enlazada con "Ver más" falla tras los reintentos;
- el resultado tiene menos del 50 % de ubicaciones que el anterior (`IG_MIN_RATIO`).

## Uso local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m ig_places            # incremental
python -m ig_places --full -v  # todas las páginas, log detallado
python -m ig_places --headed   # ver el navegador
```

Otras variables de entorno: `IG_SOURCES`, `IG_OUTPUT_FILE`, `IG_MAX_PAGES`, `IG_PAGE_RETRIES`, `IG_MIN_DELAY`/`IG_MAX_DELAY` (pausa entre páginas, en segundos). Ver `ig_places/config.py`.

## Licencia

El código se publica bajo licencia [MIT](LICENSE) © 2026 Daniel Ripoll.

Los datos de `data/locations.json` (nombres e identificadores de ubicaciones) proceden de Instagram y pertenecen a sus respectivos titulares; la licencia MIT no se aplica a ellos.
