# =============================================================
# tg_notify.py — server-side, added 2026-09-26
#
# Tiny helper for the API process (main.py) to send a Telegram message
# to a license's configured standalone chat(s) — e.g. the desktop-style
# "📋 Bid text copied — $X ($Y/mi)" confirmation after a price is
# confirmed on the BID PC price page. The poller has its own richer
# Telegram layer; this exists so main.py doesn't import poller.py.
# Honors the license's telegram_enabled flag and no-ops (returns 0) if
# no bot token / chat ID is configured.
# =============================================================

import logging
import threading

import requests

import license_db

logger = logging.getLogger("tg_notify")


def send_to_license(license_key: str, text: str, keyboard=None, respect_enabled: bool = True) -> int:
    """Returns how many chats were successfully messaged.
    respect_enabled=False bypasses the license's Telegram on/off flag —
    the desktop's driver-bot forwards do the same."""
    if respect_enabled and not license_db.get_telegram_enabled(license_key):
        return 0
    s = license_db.get_standalone_settings(license_key) or {}
    token = s.get("bot_token")
    chat_ids = [int(c.strip()) for c in (s.get("chat_ids") or "").split(",")
                if c.strip().lstrip("-").isdigit()]
    if not token or not chat_ids:
        return 0

    results = []

    def _one(cid):
        payload = {"chat_id": cid, "text": text}
        if keyboard:
            import json
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json=payload, timeout=15)
            results.append(r.ok)
        except Exception as e:
            logger.warning(f"Telegram notify failed (chat {cid}): {e}")
            results.append(False)

    threads = [threading.Thread(target=_one, args=(c,), daemon=True) for c in chat_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)
    return sum(1 for ok in results if ok)
