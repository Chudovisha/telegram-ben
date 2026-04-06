"""Telegram-бот: розділи, «Прозвон сервіс», збір даних, запис у Google Таблицю, вибір тарифу."""
from __future__ import annotations

import asyncio
import logging
import os
import re

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from storage import (
    CALL_STATUSES,
    CALL_STATUS_WAITING,
    OrderRecord,
    count_orders_for_user,
    get_order_by_sheet_row_sync,
    insert_order,
    list_all_orders,
    list_orders_for_user,
    update_order_workflow_sync,
)
from telegram_notify import format_call_status_ua, send_order_update_notification

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PHONE, NAME, TASK, CONDITIONS, SELECT_TARIFF = range(5)
ADMIN_NOTES_WAITING = 0

ADMIN_PAGE_SIZE = 5
_ADMIN_STATUS_CB = {"w": "waiting", "i": "in_progress", "c": "completed"}


def _parse_admin_ids() -> set[int]:
    raw = os.environ.get("ADMIN_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except ValueError:
            continue
    return out


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return user_id in _parse_admin_ids()


def _norm_call_status(raw: str) -> str:
    t = (raw or "").strip()
    if t in CALL_STATUSES:
        return t
    return CALL_STATUS_WAITING


def _format_admin_detail(rec: dict[str, str]) -> str:
    sr = rec.get("_sheet_row", "")
    task = rec.get("task_text") or "—"
    if len(task) > 3500:
        task = task[:3499] + "…"
    return (
        f"📋 Заявка · рядок таблиці {sr}\n\n"
        f"📅 Дата: {rec.get('created_at_utc', '—')}\n"
        f"👤 Telegram: {rec.get('telegram_user_id', '—')} @{rec.get('telegram_username', '')}\n"
        f"📱 Телефон: {rec.get('phone', '—')}\n"
        f"🏷 Контакт: {rec.get('contact_name', '—')}\n"
        f"💰 Тариф: {rec.get('package', '—')} · {rec.get('price_usd', '—')} USD\n"
        f"📍 Статус дзвінка: {format_call_status_ua(rec.get('call_status', ''))}\n"
        f"📝 Нотатки: {rec.get('admin_notes') or '—'}\n\n"
        f"🎯 Завдання:\n{task}"
    )


def _admin_detail_keyboard(sheet_row: int, list_page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏳ Очікування", callback_data=f"adm:s:{sheet_row}:w"),
                InlineKeyboardButton("🔧 У роботі", callback_data=f"adm:s:{sheet_row}:i"),
                InlineKeyboardButton("✅ Готово", callback_data=f"adm:s:{sheet_row}:c"),
            ],
            [InlineKeyboardButton("📝 Змінити нотатки", callback_data=f"adm:n:{sheet_row}")],
            [InlineKeyboardButton("◀️ До списку заявок", callback_data=f"adm:l:{list_page}")],
        ]
    )





async def _admin_build_list_page(
    context: ContextTypes.DEFAULT_TYPE, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    orders = await list_all_orders()
    n = len(orders)
    if n == 0:
        return (
            "📭 Поки немає заявок у таблиці.\n\n"
            "Як тільки клієнти оформлять запити — вони з’являться тут.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 У головне меню", callback_data="main_menu")]]),
        )
    total_pages = max(1, (n + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["admin_list_page"] = page
    start = page * ADMIN_PAGE_SIZE
    chunk = orders[start : start + ADMIN_PAGE_SIZE]
    lines = [
        "📋 Усі заявки\n"
        f"Сторінка {page + 1} з {total_pages} · заявок: {n}\n"
        "Натисніть заявку, щоб змінити статус або нотатки."
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for rec in chunk:
        sr = int(rec.get("_sheet_row") or 0)
        dt = (rec.get("created_at_utc") or "")[:16].replace("T", " ")
        uid = (rec.get("telegram_user_id") or "")[:14]
        st = format_call_status_ua(rec.get("call_status", ""))
        lines.append(f"• №{sr} · {dt} · id {uid} · {st}")
        buttons.append([InlineKeyboardButton(f"📂 Відкрити №{sr}", callback_data=f"adm:v:{sr}")])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"adm:l:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"adm:l:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _admin_show_detail(q, context: ContextTypes.DEFAULT_TYPE, sheet_row: int) -> None:
    rec = await asyncio.to_thread(get_order_by_sheet_row_sync, sheet_row)
    if not rec:
        await q.answer("❌ Заявку не знайдено в таблиці.", show_alert=True)
        return
    page = int(context.user_data.get("admin_list_page", 0))
    await q.answer()
    text = _format_admin_detail(rec)
    kb = _admin_detail_keyboard(sheet_row, page)
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception:
        await q.message.reply_text(text, reply_markup=kb)


async def _admin_edit_detail_after_change(
    q, context: ContextTypes.DEFAULT_TYPE, sheet_row: int
) -> None:
    rec = await asyncio.to_thread(get_order_by_sheet_row_sync, sheet_row)
    if not rec:
        return
    page = int(context.user_data.get("admin_list_page", 0))
    text = _format_admin_detail(rec)
    kb = _admin_detail_keyboard(sheet_row, page)
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception as e:
        logger.warning("Не вдалося оновити картку заявки: %s", e)


async def cb_admin_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("🔒 Доступ лише для адміністраторів.", show_alert=True)
        return
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await q.answer()
        return
    kind = parts[1]
    try:
        if kind == "l":
            page = int(parts[2])
            text, kb = await _admin_build_list_page(context, page)
            await q.answer()
            await q.edit_message_text(text, reply_markup=kb)
            return
        if kind == "v":
            row = int(parts[2])
            await _admin_show_detail(q, context, row)
            return
        if kind == "s":
            row = int(parts[2])
            code = parts[3] if len(parts) > 3 else ""
            new_status = _ADMIN_STATUS_CB.get(code)
            if not new_status:
                await q.answer("⚠️ Невідомий статус.", show_alert=True)
                return
            rec = await asyncio.to_thread(get_order_by_sheet_row_sync, row)
            if not rec:
                await q.answer("❌ Заявку не знайдено в таблиці.", show_alert=True)
                return
            notes = (rec.get("admin_notes") or "").strip()
            result = await asyncio.to_thread(update_order_workflow_sync, row, new_status, notes)
            if not result.get("changed"):
                await q.answer("ℹ️ Змін не було — усе вже так.")
                return
            uid = result.get("telegram_user_id")
            if uid is not None:
                ok, err = await asyncio.to_thread(
                    send_order_update_notification,
                    int(uid),
                    str(result.get("created_at") or ""),
                    str(result.get("task_text") or ""),
                    new_status,
                    notes,
                )
                if not ok:
                    logger.warning("Telegram notify: %s", err)
            await q.answer("✅ Збережено! Клієнту надіслано сповіщення.")
            await _admin_edit_detail_after_change(q, context, row)
            return
    except Exception as e:
        logger.exception("Адмін callback")
        await q.answer(f"Помилка: {e}", show_alert=True)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🔒 Цей розділ доступний лише адміністраторам.")
        return
    text, kb = await _admin_build_list_page(context, 0)
    await update.message.reply_text(text, reply_markup=kb)


async def admin_notes_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("🔒 Доступ лише для адміністраторів.", show_alert=True)
        return ConversationHandler.END
    m = re.match(r"^adm:n:(\d+)$", q.data or "")
    if not m:
        return ConversationHandler.END
    await q.answer()
    sheet_row = int(m.group(1))
    context.user_data["admin_notes_row"] = sheet_row
    await q.message.reply_text(
        f"📝 Надішліть текст **нотаток** для заявки (рядок {sheet_row}).\n\n"
        "Щоб скасувати — напишіть /cancel",
        parse_mode="Markdown",
    )
    return ADMIN_NOTES_WAITING


async def admin_notes_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    row = context.user_data.get("admin_notes_row")
    if row is None:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    rec = await asyncio.to_thread(get_order_by_sheet_row_sync, row)
    if not rec:
        await update.message.reply_text("❌ Заявку не знайдено. Спробуйте оновити список у адмінці.")
        context.user_data.pop("admin_notes_row", None)
        return ConversationHandler.END
    st = _norm_call_status(rec.get("call_status", ""))
    result = await asyncio.to_thread(update_order_workflow_sync, row, st, text)
    context.user_data.pop("admin_notes_row", None)
    if result.get("changed") and result.get("telegram_user_id") is not None:
        ok, err = await asyncio.to_thread(
            send_order_update_notification,
            int(result["telegram_user_id"]),
            str(result.get("created_at") or ""),
            str(result.get("task_text") or ""),
            st,
            text,
        )
        msg = (
            "✅ Готово! Клієнт отримав сповіщення в Telegram."
            if ok
            else f"⚠️ Нотатки збережено, але не вдалося надіслати повідомлення: {err}"
        )
    else:
        msg = "ℹ️ Текст такий самий — змін не було."
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def admin_notes_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("admin_notes_row", None)
    await update.message.reply_text("👌 Добре, нотатки не змінюємо.")
    return ConversationHandler.END


def _main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📞 Прозвон сервіс", callback_data="prozvon")]
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛡️ Адмінка · заявки", callback_data="adm:l:0")])
    return InlineKeyboardMarkup(rows)

MAIN_MENU_TEXT = (
    "👋 Вітаємо!\n\n"
    "Оберіть розділ нижче — далі все підкажемо крок за кроком."
)

PROZVON_SUBMENU_TEXT = "📞 **Прозвон сервіс**\n\nОберіть дію — ми поруч, якщо виникнуть питання."

# Умови (юридичний текст без Markdown — менше помилок у Telegram)
PROZVON_RULES = (
    "📌 Умови — Call Service (прозвон)\n\n"
    "👋 Перед оформленням перегляньте текст нижче — це правила співпраці.\n\n"
    "1️⃣ Опис послуги\n"
    "Ми надаємо вихідні дзвінки від вашого імені іноземними мовами (англійська, німецька та ін.). "
    "Звінки можуть стосуватися підтвердження замовлень, комунікації з клієнтами, підтримки або "
    "взаємодії з третіми сторонами. За потреби доступний жіночий голос.\n"
    "Важливо: дзвінок має сильніший ефект, ніж переписка, але не гарантує 100% результат.\n\n"
    "2️⃣ Оплата\n"
    "При першій співпраці — передоплата $10. Подальші замовлення можуть оплачуватися після виконання "
    "(за домовленістю).\n"
    "Тарифи: 1 дзвінок — $10 · 3 дзвінки — $20 · кожен додатковий дзвінок — $5.\n"
    "Оплата не повертається після виконання дзвінка.\n\n"
    "3️⃣ Дані перед дзвінком (повний пакет)\n"
    "Перед виконанням дзвінка клієнт зобов'язаний надати (у вільних полях форми нижче — стисло все необхідне):\n"
    "сайт; номер замовлення; сума замовлення; спосіб оплати;\n"
    "трек-номер, кур'єрська служба, дата доставки;\n"
    "ім'я та прізвище отримувача; адреса доставки;\n"
    "номер телефону з замовлення; електронна пошта; назва товару;\n"
    "коротко суть дзвінка / задача; номер телефону магазину, куди телефонувати.\n"
    "Заявки без повного набору даних можуть бути відхилені або оброблені з затримкою.\n\n"
    "4️⃣ Форма в боті (після «Почати оформлення»)\n"
    "Крок 1 — ваш телефон для зв'язку.\n"
    "Крок 2 — ім'я / компанія.\n"
    "Крок 3 — задача (хто, мета, сценарій).\n"
    "Крок 4 — умови та всі додаткові дані з пункту 3.\n\n"
    "Тариф обирається після збереження заявки: 1 дзвінок — $10, 3 дзвінки — $20, кожен додатковий — $5.\n\n"
    "✅ Натискаючи «Почати оформлення», ви автоматично погоджуєтесь з цими умовами."
)


def _prozvon_submenu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Мої заявки", callback_data="prozvon_my")],
            [InlineKeyboardButton("✨ Новий запит на дзвінок", callback_data="prozvon_new")],
            [InlineKeyboardButton("🏠 У головне меню", callback_data="main_menu")],
        ]
    )


def _prozvon_back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад до прозвону", callback_data="prozvon_menu")]]
    )


def _truncate_field(text: str, limit: int = 500) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t or "—"
    return t[: limit - 1] + "…"


def _payment_rule_ua(rec: dict[str, str]) -> str:
    raw = (rec.get("payment_rule") or "").strip()
    if raw == "prepay_first":
        return "🔐 Перше співробітництво — передоплата $10 до виконання"
    if raw == "after_work":
        return "🤝 Повторне замовлення — оплата після виконання (за домовленістю)"
    return raw or "—"


def _format_order_block(index: int, rec: dict[str, str]) -> str:
    def g(key: str) -> str:
        return _truncate_field(rec.get(key, ""), 600)

    first = (rec.get("is_first_cooperation") or "").strip().lower()
    first_ua = "так" if first in ("yes", "так", "true", "1") else ("ні" if first in ("no", "ні", "false", "0") else (first or "—"))

    return (
        f"┏━━ 📋 Заявка №{index} ━━\n"
        f"📅 Дата: {g('created_at_utc')}\n"
        f"📱 Телефон: {g('phone')}\n"
        f"👤 Контакт: {g('contact_name')}\n"
        f"🎯 Завдання: {g('task_text')}\n"
        f"📎 Додатково / умови: {g('conditions_text')}\n"
        f"💰 Тариф: {g('package')} · {g('price_usd')} USD\n"
        f"🆕 Перше замовлення: {first_ua}\n"
        f"💳 Умови оплати: {_payment_rule_ua(rec)}\n"
        f"💵 Статус оплати: {g('payment_status')}\n"
        f"📍 Статус дзвінка: {format_call_status_ua(rec.get('call_status', ''))}\n"
        f"📝 Нотатки: {g('admin_notes')}"
    )


def _build_orders_messages(orders: list[dict[str, str]], max_len: int = 4000) -> list[str]:
    """Розбиває список заявок на кілька повідомлень (ліміт Telegram ~4096)."""
    if not orders:
        return [
            "📭 Поки немає заявок.\n\n"
            "✨ Натисніть «Новий запит на дзвінок» у меню прозвону — і ми все збережемо тут."
        ]
    header = f"📋 Ваші заявки ({len(orders)} шт.)\n\n"
    parts = [_format_order_block(i, rec) for i, rec in enumerate(orders, 1)]
    chunks: list[str] = []
    buf = header
    for i, part in enumerate(parts):
        sep = "" if buf.endswith(header) and i == 0 else "\n\n"
        tentative = buf + sep + part
        if len(tentative) <= max_len:
            buf = tentative
        else:
            chunks.append(buf)
            buf = f"📎 Продовження списку\n\n{part}"
    if buf:
        chunks.append(buf)
    return chunks


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    keyboard = _main_menu_keyboard(uid)
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.edit_message_text(MAIN_MENU_TEXT, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(MAIN_MENU_TEXT, reply_markup=keyboard)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Головне меню після /start (звичайний CommandHandler)."""
    context.user_data.clear()
    await show_main_menu_from_message(update, context)


async def cmd_start_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Те саме під час активного діалогу — завершує ConversationHandler."""
    await cmd_start(update, context)
    return ConversationHandler.END


async def show_main_menu_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    keyboard = _main_menu_keyboard(uid)
    await update.message.reply_text(MAIN_MENU_TEXT, reply_markup=keyboard)


async def cb_prozvon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        PROZVON_SUBMENU_TEXT,
        reply_markup=_prozvon_submenu_markup(),
        parse_mode="Markdown",
    )


async def cb_prozvon_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        PROZVON_SUBMENU_TEXT,
        reply_markup=_prozvon_submenu_markup(),
        parse_mode="Markdown",
    )


async def cb_prozvon_menu_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await cb_prozvon_menu(update, context)
    return ConversationHandler.END


async def cb_prozvon_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Почати оформлення", callback_data="prozvon_start")],
            [InlineKeyboardButton("◀️ Назад", callback_data="prozvon_menu")],
        ]
    )
    await q.edit_message_text(
        PROZVON_RULES,
        reply_markup=keyboard,
    )


async def cb_prozvon_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    chat_id = q.message.chat_id
    await q.edit_message_text("⏳ Завантажую список… Секунду.")
    try:
        orders = await list_orders_for_user(uid)
    except Exception as e:
        logger.exception("Читання заявок")
        await q.edit_message_text(
            "😕 Не вдалося завантажити заявки.\n\n"
            "Перевірте підключення до Google Таблиці та змінні на сервері.\n"
            f"Деталі: {e}",
            reply_markup=_prozvon_back_markup(),
        )
        return
    parts = _build_orders_messages(orders)
    await q.edit_message_text(parts[0], reply_markup=_prozvon_back_markup())
    for extra in parts[1:]:
        await context.bot.send_message(chat_id=chat_id, text=extra)


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await show_main_menu(update, context)
    return ConversationHandler.END


async def cb_main_menu_outer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Назад до меню» з екрана умов, коли діалог ще не розпочато."""
    context.user_data.clear()
    await show_main_menu(update, context)


async def conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нове повідомлення замість edit — швидше й без помилок Markdown на довгому попередньому тексті."""
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    if q.message:
        await q.message.reply_text(
            "📱 **Крок 1 з 4**\n\n"
            "Введіть номер телефону для звʼязку "
            "(краще міжнародний формат, наприклад +380…):",
            parse_mode="Markdown",
        )
    return PHONE


async def conv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "👤 **Крок 2 з 4**\n\nВведіть **імʼя або назву компанії** (контактна особа):",
        parse_mode="Markdown",
    )
    return NAME


async def conv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["contact_name"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "🎯 **Крок 3 з 4**\n\nОпишіть **завдання**: кого прозвонити, мета, бажаний сценарій / очікування:",
        parse_mode="Markdown",
    )
    return TASK


async def conv_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["task_text"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "📎 **Крок 4 з 4**\n\nОпишіть **умови та додаткові дані**: часові вікна, обмеження, мова, зауваження:",
        parse_mode="Markdown",
    )
    return CONDITIONS


async def conv_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["conditions_text"] = (update.message.text or "").strip()
    uid = update.effective_user.id
    prior = await count_orders_for_user(uid)
    is_first = prior == 0
    context.user_data["is_first_order"] = is_first
    context.user_data["prior_orders_count"] = prior

    if is_first:
        pay_hint = (
            "🔐 Це ваше **перше** замовлення: передоплата **$10** до початку роботи.\n\n"
        )
    else:
        pay_hint = (
            f"🤝 У вас уже **{prior}** заявок у системі. Подальші можна оплачувати **після виконання** "
            "(за домовленістю).\n\n"
        )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💵 1 дзвінок — $10", callback_data="tariff_1")],
            [InlineKeyboardButton("💵 3 дзвінки — $20", callback_data="tariff_3")],
            [InlineKeyboardButton("➕ Додатковий дзвінок — $5", callback_data="tariff_extra")],
        ]
    )
    await update.message.reply_text(
        pay_hint
        + "✅ Дані збережено в памʼяті бота.\n\n"
        "Оберіть **тариф** — після цього заявку запишемо в таблицю:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return SELECT_TARIFF


async def conv_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    mapping = {
        "tariff_1": ("1 дзвінок", 10.0),
        "tariff_3": ("3 дзвінки", 20.0),
        "tariff_extra": ("Додатковий дзвінок", 5.0),
    }
    pkg_key = q.data or ""
    if pkg_key not in mapping:
        await q.answer()
        return SELECT_TARIFF

    # Неповні дані (часто через другий інстанс бота або зламану сесію) — до answer()
    _need_str = ("phone", "contact_name", "task_text", "conditions_text")
    if any(not context.user_data.get(k) for k in _need_str) or "is_first_order" not in context.user_data:
        await q.answer(
            "⚠️ Сесію не знайдено. Натисніть /start і пройдіть кроки знову.",
            show_alert=True,
        )
        return ConversationHandler.END

    # Повторний клік, поки йде insert_order (один процес)
    if context.user_data.get("_tariff_busy"):
        await q.answer("⏳ Заявка вже зберігається…", show_alert=False)
        return SELECT_TARIFF

    package_label, price = mapping[pkg_key]
    user = q.from_user
    is_first = bool(context.user_data["is_first_order"])
    payment_rule = "prepay_first" if is_first else "after_work"
    row = OrderRecord(
        telegram_user_id=user.id,
        telegram_username=user.username,
        section="prozvon",
        phone=context.user_data["phone"],
        contact_name=context.user_data["contact_name"],
        task_text=context.user_data["task_text"],
        conditions_text=context.user_data["conditions_text"],
        package=package_label,
        price_usd=price,
        is_first_cooperation=is_first,
        payment_rule=payment_rule,
    )
    # Після перевірок — одразу відповісти на callback (знімає «годинник» у клієнті)
    await q.answer("💾 Зберігаю заявку…", show_alert=False)
    context.user_data["_tariff_busy"] = True
    try:
        # Прибрати кнопки з повідомлення з тарифами (щоб не клацали повторно)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Нове коротке повідомлення — легше й швидше за edit великого повідомлення
        status_msg = await q.message.reply_text(
            "⏳ Записуємо у Google Таблицю…\n"
            "Зазвичай це кілька секунд."
        )
        try:
            await insert_order(row)
        except Exception as e:
            logger.exception("Збереження заявки")
            await status_msg.edit_text(
                "😕 Не вдалося зберегти заявку в таблицю.\n\n"
                "Перевірте `.env`, доступ до таблиці та ключ сервісного акаунта.\n"
                f"Деталі: {e}\n\n/start — почати спочатку."
            )
            context.user_data.clear()
            return ConversationHandler.END

        pay_note = (
            "💡 Нагадування: для **першого** замовлення потрібна передоплата **$10** до виконання.\n\n"
            if is_first
            else "💡 Для **повторного** замовлення оплату можна узгодити після виконання.\n\n"
        )
        await status_msg.edit_text(
            "✅ **Готово!** Заявку збережено.\n\n"
            + pay_note
            + "💳 Оплату підключимо окремо — з’явиться посилання на оплату.\n\n"
            "Натисніть /start — повернутися в меню.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        context.user_data.pop("_tariff_busy", None)


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "👌 Оформлення скасовано.\n\nНатисніть /start — відкрити меню знову."
    )
    return ConversationHandler.END


async def conv_fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👆 Оберіть тариф кнопками під повідомленням.\n"
        "Або /cancel — скасувати оформлення."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "409 Conflict: один TELEGRAM_BOT_TOKEN не може використовуватися двома процесами. "
            "Зупини локальний `python bot.py`, другий деплой на Railway, інший сервер або webhook. "
            "Лиши лише один активний polling."
        )
        return
    tb = getattr(err, "__traceback__", None)
    if tb is not None:
        logger.error("Необроблена помилка", exc_info=(type(err), err, tb))
    else:
        logger.error("Необроблена помилка: %s", err)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Потрібен TELEGRAM_BOT_TOKEN у файлі .env (токен від @BotFather)."
        )

    app = (
        Application.builder()
        .token(token)
        .connect_timeout(20.0)
        .read_timeout(45.0)
        .write_timeout(30.0)
        .build()
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_notes_entry, pattern=r"^adm:n:\d+$")],
        states={
            ADMIN_NOTES_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_notes_save),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_notes_cancel),
            CommandHandler("start", cmd_start_fallback),
        ],
        name="admin_notes",
        allow_reentry=True,
    )

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(conv_start, pattern=r"^prozvon_start$")],
        states={
            PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_phone),
            ],
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_name),
            ],
            TASK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_task),
            ],
            CONDITIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_conditions),
            ],
            SELECT_TARIFF: [
                CallbackQueryHandler(conv_tariff, pattern=r"^tariff_(1|3|extra)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, conv_fallback_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CommandHandler("start", cmd_start_fallback),
            CallbackQueryHandler(cb_main_menu, pattern=r"^main_menu$"),
            CallbackQueryHandler(cb_prozvon_menu_fallback, pattern=r"^prozvon_menu$"),
        ],
        name="prozvon_order",
        persistent=False,
        # per_message=True тут заборонено: у станах є MessageHandler (PTB вимагає лише CallbackQuery).
    )

    app.add_error_handler(on_error)

    # /start має бути перед ConversationHandler, інакше деякі оновлення можуть «губитися».
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(cb_admin_router, pattern=r"^adm:(l|v|s):"))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_prozvon_my, pattern=r"^prozvon_my$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon_new, pattern=r"^prozvon_new$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon_menu, pattern=r"^prozvon_menu$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon, pattern=r"^prozvon$"))
    app.add_handler(CallbackQueryHandler(cb_main_menu_outer, pattern=r"^main_menu$"))

    print("Бот запущено. Ctrl+C — зупинка.")
    logger.warning(
        "Один TELEGRAM_BOT_TOKEN = один процес polling. Друга копія (ПК + Railway) дає 409 Conflict "
        "і «крутиться» кнопка, поки один інстанс не відпустить оновлення."
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
