"""Збереження заявок у Google Таблиці (через сервісний акаунт)."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

# Заголовки першого рядка аркуша (створюються автоматично, якщо таблиця порожня)
_HEADERS = [
    "created_at_utc",
    "telegram_user_id",
    "telegram_username",
    "section",
    "phone",
    "contact_name",
    "task_text",
    "conditions_text",
    "package",
    "price_usd",
    "payment_status",
    "is_first_cooperation",
    "payment_rule",
    "call_status",
    "admin_notes",
]


_sheet_lock = threading.Lock()

CALL_STATUS_WAITING = "waiting"
CALL_STATUS_IN_PROGRESS = "in_progress"
CALL_STATUS_COMPLETED = "completed"
CALL_STATUSES = (
    CALL_STATUS_WAITING,
    CALL_STATUS_IN_PROGRESS,
    CALL_STATUS_COMPLETED,
)


def _parse_telegram_user_id(raw: str) -> int:
    t = str(raw).strip()
    if not t:
        raise ValueError("порожній telegram_user_id")
    return int(float(t))


def _ensure_full_headers(ws: gspread.Worksheet) -> None:
    """Якщо аркуш уже існував зі старими колонками — додає нові заголовки в кінець рядка 1."""
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(_HEADERS, value_input_option="USER_ENTERED")
        return
    existing_set = {h.strip() for h in existing if h}
    missing = [h for h in _HEADERS if h not in existing_set]
    if not missing:
        return
    start = len(existing) + 1
    for i, name in enumerate(missing):
        ws.update_cell(1, start + i, name)


def _spreadsheet_id() -> str:
    sid = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    if not sid:
        raise RuntimeError(
            "У .env потрібно GOOGLE_SHEETS_SPREADSHEET_ID (ID з URL Google Таблиці)."
        )
    return sid


def _project_dir() -> Path:
    return Path(__file__).resolve().parent


def _service_account_path() -> Path:
    """Шлях до JSON ключа сервісного акаунта: з .env або типові імена в папці проєкту."""
    root = _project_dir()
    candidates: list[Path] = []

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if raw:
        p = Path(raw)
        candidates.append(p if p.is_absolute() else root / p)

    for name in ("google-service-account.json", "credentials.json", "service-account.json"):
        candidates.append(root / name)

    seen: set[Path] = set()
    for p in candidates:
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved

    raise RuntimeError(
        "Файл JSON ключа сервісного акаунта не знайдено. Локально: поклади google-service-account.json у папку проєкту. "
        "На Railway/Render: додай змінну GOOGLE_SERVICE_ACCOUNT_JSON з повним вмістом JSON (одним рядком). "
        "Також: Google Sheets API, доступ сервісного email до таблиці."
    )


def _get_credentials() -> Credentials:
    """Ключ: або змінна GOOGLE_SERVICE_ACCOUNT_JSON (хмара), або файл на диску."""
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        try:
            info = json.loads(raw_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON не є валідним JSON. Перевір лапки та весь вміст у одній змінній."
            ) from e
        return Credentials.from_service_account_info(info, scopes=_SCOPES)
    path = _service_account_path()
    return Credentials.from_service_account_file(str(path), scopes=_SCOPES)


_gs_client: gspread.Client | None = None
_ws_cache: gspread.Worksheet | None = None


def _client() -> gspread.Client:
    """Один клієнт на процес — менше рукостискань TLS/Google при кожному рядку."""
    global _gs_client
    if _gs_client is None:
        _gs_client = gspread.authorize(_get_credentials())
    return _gs_client


def _worksheet() -> gspread.Worksheet:
    global _ws_cache
    if _ws_cache is None:
        gc = _client()
        sh = gc.open_by_key(_spreadsheet_id())
        _ws_cache = sh.sheet1
    return _ws_cache


def _ensure_headers(ws: gspread.Worksheet) -> None:
    _ensure_full_headers(ws)


@dataclass
class OrderRecord:
    telegram_user_id: int
    telegram_username: str | None
    section: str
    phone: str
    contact_name: str
    task_text: str
    conditions_text: str
    package: str
    price_usd: float
    is_first_cooperation: bool
    payment_rule: str  # prepay_first | after_work


def insert_order_sync(row: OrderRecord) -> None:
    """Синхронний запис рядка в таблицю (викликати з executor / to_thread)."""
    created = datetime.now(timezone.utc).isoformat()
    with _sheet_lock:
        ws = _worksheet()
        _ensure_headers(ws)
        ws.append_row(
            [
                created,
                row.telegram_user_id,
                row.telegram_username or "",
                row.section,
                row.phone,
                row.contact_name,
                row.task_text,
                row.conditions_text,
                row.package,
                row.price_usd,
                "pending",
                "yes" if row.is_first_cooperation else "no",
                row.payment_rule,
                CALL_STATUS_WAITING,
                "",
            ],
            value_input_option="USER_ENTERED",
        )


async def insert_order(row: OrderRecord) -> None:
    """Асинхронна обгортка для хендлерів python-telegram-bot."""
    await asyncio.to_thread(insert_order_sync, row)


def _header_map(headers: list[str]) -> dict[str, int]:
    """Ім'я колонки → індекс 0..len-1."""
    return {h.strip(): i for i, h in enumerate(headers) if h.strip()}


def list_orders_for_user_sync(telegram_user_id: int) -> list[dict[str, str]]:
    """Усі рядки з аркуша для цього telegram_user_id (нові зверху)."""
    with _sheet_lock:
        ws = _worksheet()
        _ensure_headers(ws)
        rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = rows[0]
    uid = str(telegram_user_id).strip()
    out: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        rec = {headers[i]: (row[i] or "").strip() for i in range(len(headers))}
        if rec.get("telegram_user_id", "").strip() == uid:
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at_utc", ""), reverse=True)
    return out


async def list_orders_for_user(telegram_user_id: int) -> list[dict[str, str]]:
    return await asyncio.to_thread(list_orders_for_user_sync, telegram_user_id)


def count_orders_for_user_sync(telegram_user_id: int) -> int:
    return len(list_orders_for_user_sync(telegram_user_id))


async def count_orders_for_user(telegram_user_id: int) -> int:
    return await asyncio.to_thread(count_orders_for_user_sync, telegram_user_id)


def list_all_orders_sync() -> list[dict[str, str]]:
    """Усі заявки з аркуша для адмінки (нові зверху). Кожен запис містить `_sheet_row` — номер рядка в Google Таблиці."""
    with _sheet_lock:
        ws = _worksheet()
        _ensure_headers(ws)
        rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    headers = [h.strip() for h in rows[0]]
    out: list[dict[str, str]] = []
    for sheet_row, row in enumerate(rows[1:], start=2):
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))
        rec: dict[str, str] = {
            headers[i]: (row[i] or "").strip() for i in range(len(headers))
        }
        rec["_sheet_row"] = str(sheet_row)
        out.append(rec)
    out.sort(key=lambda r: r.get("created_at_utc", ""), reverse=True)
    return out


def update_order_workflow_sync(
    sheet_row: int,
    call_status: str,
    admin_notes: str,
) -> dict[str, str | bool]:
    """Оновлює статус дзвінка та нотатки. Повертає старі значення та чи були зміни."""
    if call_status not in CALL_STATUSES:
        raise ValueError(f"Недопустимий call_status: {call_status}")
    notes = (admin_notes or "").strip()
    with _sheet_lock:
        ws = _worksheet()
        _ensure_headers(ws)
        rows = ws.get_all_values()
        if sheet_row < 2 or sheet_row > len(rows):
            raise ValueError("Некоректний номер рядка")
        headers = [h.strip() for h in rows[0]]
        hm = _header_map(headers)
        if "call_status" not in hm or "admin_notes" not in hm:
            raise RuntimeError("У таблиці немає колонок call_status / admin_notes")
        data_row = rows[sheet_row - 1]
        if len(data_row) < len(headers):
            data_row = data_row + [""] * (len(headers) - len(data_row))
        old_status = (data_row[hm["call_status"]] or "").strip()
        old_notes = (data_row[hm["admin_notes"]] or "").strip()
        changed = old_status != call_status or old_notes != notes
        if not changed:
            return {
                "changed": False,
                "old_status": old_status,
                "old_notes": old_notes,
            }
        uid_raw = (data_row[hm["telegram_user_id"]] or "").strip()
        created_at = (data_row[hm["created_at_utc"]] or "").strip()
        task_text = (data_row[hm["task_text"]] or "").strip()
        try:
            tg_uid = _parse_telegram_user_id(uid_raw)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Некоректний telegram_user_id у рядку {sheet_row}: {uid_raw!r}") from e
        # 1-based колонки для update_cell
        ws.update_cell(sheet_row, hm["call_status"] + 1, call_status)
        ws.update_cell(sheet_row, hm["admin_notes"] + 1, notes)
    return {
        "changed": True,
        "old_status": old_status,
        "old_notes": old_notes,
        "telegram_user_id": tg_uid,
        "created_at": created_at,
        "task_text": task_text,
    }


async def list_all_orders() -> list[dict[str, str]]:
    return await asyncio.to_thread(list_all_orders_sync)


def get_order_by_sheet_row_sync(sheet_row: int) -> dict[str, str] | None:
    """Один рядок за номером рядка в аркуші (як у Google Таблиці)."""
    with _sheet_lock:
        ws = _worksheet()
        _ensure_headers(ws)
        rows = ws.get_all_values()
    if sheet_row < 2 or sheet_row > len(rows):
        return None
    headers = [h.strip() for h in rows[0]]
    data_row = rows[sheet_row - 1]
    if len(data_row) < len(headers):
        data_row = data_row + [""] * (len(headers) - len(data_row))
    rec: dict[str, str] = {
        headers[i]: (data_row[i] or "").strip() for i in range(len(headers))
    }
    rec["_sheet_row"] = str(sheet_row)
    return rec


async def get_order_by_sheet_row(sheet_row: int) -> dict[str, str] | None:
    return await asyncio.to_thread(get_order_by_sheet_row_sync, sheet_row)


async def update_order_workflow(
    sheet_row: int,
    call_status: str,
    admin_notes: str,
) -> dict[str, str | bool]:
    return await asyncio.to_thread(
        update_order_workflow_sync, sheet_row, call_status, admin_notes
    )
