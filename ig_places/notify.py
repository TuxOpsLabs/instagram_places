"""Notificaciones por webhook (Slack, Discord o genérico) y Telegram.

Todos los canales son opcionales; si no hay ninguno configurado solo se registra
en el log (y en GitHub Actions el workflow abre una issue cuando falla).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .config import Config
from .log import get_logger

log = get_logger()


def notify(config: Config, title: str, body: str, ok: bool) -> None:
    icon = "✅" if ok else "❌"
    text = f"{icon} {title}\n\n{body}"
    if config.run_url:
        text += f"\n\nEjecución: {config.run_url}"

    sent = False
    if config.webhook_url:
        sent |= _send_webhook(config.webhook_url, text)
    if config.telegram_token and config.telegram_chat_id:
        sent |= _send_telegram(config.telegram_token, config.telegram_chat_id, text)
    if not sent:
        log.debug("Ningún canal de notificación configurado o disponible")


def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _send_webhook(url: str, text: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if "discord" in host:
        payload = {"content": text[:1990]}
    else:
        # Slack y la mayoría de webhooks genéricos (Google Chat, Mattermost...) aceptan "text".
        payload = {"text": text}
    try:
        _post_json(url, payload)
        log.info("Notificación enviada al webhook")
        return True
    except Exception as exc:  # una notificación fallida nunca debe romper la ejecución
        log.warning("No se pudo enviar la notificación al webhook: %s", exc)
        return False


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        _post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        )
        log.info("Notificación enviada a Telegram")
        return True
    except Exception as exc:
        log.warning("No se pudo enviar la notificación a Telegram: %s", exc)
        return False
