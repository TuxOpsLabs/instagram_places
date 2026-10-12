# instagram-places

Sincroniza a diario las ubicaciones de Instagram del directorio de
[Palma de Mallorca](https://www.instagram.com/explore/locations/c692339/palmademallorca-spain/)
y las guarda en `data/locations.json`:

```json
{
  "locations": [
    { "id": "607881395899347", "name": "Palma De Mallorca, Spain" }
  ]
}
```

## Cómo funciona

1. Abre Chromium headless (Playwright) con un perfil de navegador real (user agent, idioma `es-ES`, zona horaria `Europe/Madrid`).
2. Acepta el banner de cookies la primera vez y guarda las cookies en `.cache/storage_state.json`, así que en las ejecuciones siguientes ya no aparece.
3. Recorre `?page=1`, `?page=2`… y extrae cada enlace del recuadro **Lugares de PalmadeMallorca** (`/explore/locations/<id>/<slug>/` → `id` + texto).
4. Termina cuando una página muestra *"Se ha producido un error"* (la siguiente a la última, que ya no tiene enlace "Ver más").
5. Une las páginas sin duplicados y reescribe el JSON solo si ha cambiado.

## Caché

`.cache/pages.json` guarda, por cada página, sus ubicaciones y un hash del contenido.

- La **página 1 es el centinela**: si no ha cambiado respecto a la caché, se reutilizan el resto de páginas cacheadas sin visitarlas (1 sola petición).
- Si la página 1 cambia, se recorren todas las páginas.
- Cada `IG_FULL_CRAWL_DAYS` días (7 por defecto) se fuerza un recorrido completo, para detectar cambios en páginas interiores aunque la 1 siga igual.
- Si una página falla pero está en caché, se usan los datos cacheados y se avisa.

Para forzar una pasada completa: `python -m ig_places --full`, o lanzar el workflow a mano con *full_refresh*.

En GitHub Actions la carpeta `.cache` se conserva entre ejecuciones con `actions/cache`.

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

Otras variables de entorno: `IG_BASE_URL`, `IG_OUTPUT_FILE`, `IG_MAX_PAGES`, `IG_PAGE_RETRIES`, `IG_MIN_DELAY`/`IG_MAX_DELAY` (pausa entre páginas, en segundos). Ver `ig_places/config.py`.

## Licencia

El código se publica bajo licencia [MIT](LICENSE) © 2026 Daniel Ripoll.

Los datos de `data/locations.json` (nombres e identificadores de ubicaciones) proceden de Instagram y pertenecen a sus respectivos titulares; la licencia MIT no se aplica a ellos.
