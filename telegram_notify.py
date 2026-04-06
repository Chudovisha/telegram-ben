"""Сповіщення користувачів через Telegram Bot API (без окремого Application)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

CALL_STATUS_LABELS_UA: dict[str, str] = {
    "waiting": "⏳ Очікування",
    "in_progress": "🔧 У роботі",
    "completed": "✅ Завершено",
}


def format_call_status_ua(code: str) -> str:
    c = (code or "").strip()
    if not c or c == "waiting":
        return CALL_STATUS_LABELS_UA["waiting"]
    return CALL_STATUS_LABELS_UA.get(c, c)


def send_telegram_text(chat_id: int, text: str) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN не задано"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not data.get("ok"):
            return False, raw[:800]
        return True, ""
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        return False, err[:800]
    except OSError as e:
        return False, str(e)


def send_order_update_notification(
    telegram_user_id: int,
    created_at: str,
    task_text: str,
    new_status: str,
    admin_notes: str,
) -> tuple[bool, str]:
    st = format_call_status_ua(new_status)
    task_short = (task_text or "").strip().replace("\n", " ")
    if len(task_short) > 220:
        task_short = task_short[:219] + "…"
    lines = [
        "🔔 Оновлення вашої заявки",
        "",
        f"📍 Статус: {st}",
    ]
    notes = (admin_notes or "").strip()
    if notes:
        lines.append(f"📝 Нотатки: {notes}")
    lines.extend(
        [
            "",
            f"📅 Дата заявки: {created_at or '—'}",
            f"🎯 Завдання: {task_short or '—'}",
        ]
    )
    return send_telegram_text(telegram_user_id, "\n".join(lines))
