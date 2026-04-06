"""Telegram-бот: розділи, «Прозвон сервіс», збір даних, запис у Google Таблицю, вибір тарифу."""
from __future__ import annotations

import logging
import os

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

from storage import OrderRecord, insert_order, list_orders_for_user

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PHONE, NAME, TASK, CONDITIONS, SELECT_TARIFF = range(5)

MAIN_MENU_TEXT = "Оберіть розділ:"

PROZVON_SUBMENU_TEXT = "📞 **Прозвон сервіс**\n\nОберіть дію:"

PROZVON_RULES = (
    "📞 **Новий запит на дзвінок**\n\n"
    "Тут ви оформлюєте заявку на прозвони. Потрібно надати чесні та повні дані — "
    "від цього залежить якість роботи.\n\n"
    "**Що потрібно буде ввести після натискання «Почати оформлення»:**\n"
    "1. Телефон для звʼязку з вами.\n"
    "2. Імʼя або назва компанії / контактна особа.\n"
    "3. Завдання: кого прозвонити, мета, короткий сценарій або очікування.\n"
    "4. Умови та додаткові дані: часові вікна, обмеження, мова, заборонені теми тощо.\n\n"
    "**Тарифи (оплата — на наступному кроці, після збереження заявки):**\n"
    "• 1 дзвінок — **$10**\n"
    "• 3 дзвінки — **$20**\n\n"
    "Натискаючи «Почати оформлення», ви погоджуєтесь надати актуальні дані для виконання послуги."
)


def _prozvon_submenu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Мої заявки", callback_data="prozvon_my")],
            [InlineKeyboardButton("➕ Новий запит на дзвінок", callback_data="prozvon_new")],
            [InlineKeyboardButton("« Назад до меню", callback_data="main_menu")],
        ]
    )


def _prozvon_back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Назад до прозвону", callback_data="prozvon_menu")]])


def _truncate_field(text: str, limit: int = 500) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t or "—"
    return t[: limit - 1] + "…"


def _format_order_block(index: int, rec: dict[str, str]) -> str:
    def g(key: str) -> str:
        return _truncate_field(rec.get(key, ""), 600)

    return (
        f"── Заявка №{index} ──\n"
        f"Дата: {g('created_at_utc')}\n"
        f"Телефон: {g('phone')}\n"
        f"Контакт: {g('contact_name')}\n"
        f"Завдання: {g('task_text')}\n"
        f"Умови: {g('conditions_text')}\n"
        f"Тариф: {g('package')} | {g('price_usd')} USD\n"
        f"Оплата: {g('payment_status')}"
    )


def _build_orders_messages(orders: list[dict[str, str]], max_len: int = 4000) -> list[str]:
    """Розбиває список заявок на кілька повідомлень (ліміт Telegram ~4096)."""
    if not orders:
        return [
            "📋 У вас поки немає збережених заявок.\n\n"
            "Натисніть «Новий запит на дзвінок» у меню прозвону, щоб створити першу."
        ]
    header = f"📋 Ваші заявки ({len(orders)}):\n\n"
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
            buf = f"📋 (продовження)\n\n{part}"
    if buf:
        chunks.append(buf)
    return chunks


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Прозвон сервіс", callback_data="prozvon")]]
    )
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
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Прозвон сервіс", callback_data="prozvon")]]
    )
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
            [InlineKeyboardButton("Почати оформлення", callback_data="prozvon_start")],
            [InlineKeyboardButton("« Назад", callback_data="prozvon_menu")],
        ]
    )
    await q.edit_message_text(
        PROZVON_RULES,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def cb_prozvon_my(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    chat_id = q.message.chat_id
    await q.edit_message_text("⏳ Завантажую ваші заявки…")
    try:
        orders = await list_orders_for_user(uid)
    except Exception as e:
        logger.exception("Читання заявок")
        await q.edit_message_text(
            "Не вдалося завантажити заявки. Перевір доступ до таблиці та `.env`.\n"
            f"Технічна причина: {e}",
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
            "Крок 1/4. Введіть номер телефону для звʼязку "
            "(у міжнародному форматі, якщо можливо):",
        )
    return PHONE


async def conv_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["phone"] = (update.message.text or "").strip()
    await update.message.reply_text("Крок 2/4. Введіть **імʼя або назву компанії** (контактна особа):", parse_mode="Markdown")
    return NAME


async def conv_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["contact_name"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "Крок 3/4. Опишіть **завдання**: кого прозвонити, мета, бажаний сценарій / очікування:",
        parse_mode="Markdown",
    )
    return TASK


async def conv_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["task_text"] = (update.message.text or "").strip()
    await update.message.reply_text(
        "Крок 4/4. Опишіть **умови та додаткові дані**: часові вікна, обмеження, мова, зауваження:",
        parse_mode="Markdown",
    )
    return CONDITIONS


async def conv_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["conditions_text"] = (update.message.text or "").strip()
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1 дзвінок — $10", callback_data="tariff_1")],
            [InlineKeyboardButton("3 дзвінки — $20", callback_data="tariff_3")],
        ]
    )
    await update.message.reply_text(
        "Дані прийнято. Оберіть **тариф** (заявку буде збережено після вибору):",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return SELECT_TARIFF


async def conv_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    mapping = {
        "tariff_1": ("1 дзвінок", 10.0),
        "tariff_3": ("3 дзвінки", 20.0),
    }
    pkg_key = q.data or ""
    if pkg_key not in mapping:
        await q.answer()
        return SELECT_TARIFF

    # Повторний клік, поки йде insert_order (один процес)
    if context.user_data.get("_tariff_busy"):
        await q.answer("Збереження вже виконується…", show_alert=False)
        return SELECT_TARIFF

    # Callback одразу (до Google) — знімає «годинник» у Telegram
    await q.answer("Зберігаю заявку…", show_alert=False)
    context.user_data["_tariff_busy"] = True

    package_label, price = mapping[pkg_key]
    user = q.from_user
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
    )
    try:
        # Прибрати кнопки з повідомлення з тарифами (щоб не клацали повторно)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Нове коротке повідомлення — легше й швидше за edit великого повідомлення
        status_msg = await q.message.reply_text(
            "⏳ Запис у Google Таблицю…\n"
            "(зазвичай кілька секунд — залежить від мережі та Google)"
        )
        try:
            await insert_order(row)
        except Exception as e:
            logger.exception("Збереження заявки")
            await status_msg.edit_text(
                "Не вдалося зберегти заявку в таблицю. Перевір `.env`, доступ до Google Таблиці та ключ сервісного акаунта.\n"
                f"Технічна причина: {e}\n\n/start — спробувати знову."
            )
            context.user_data.clear()
            return ConversationHandler.END

        await status_msg.edit_text(
            "✅ Заявку збережено в таблиці.\n\n"
            "Оплату підключимо окремо — тут буде перехід до платіжного сервісу.\n\n"
            "/start — головне меню."
        )
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        context.user_data.pop("_tariff_busy", None)


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Оформлення скасовано. Натисніть /start для меню.")
    return ConversationHandler.END


async def conv_fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Зараз потрібно обрати тариф кнопками під повідомленням або натисніть /cancel."
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
                CallbackQueryHandler(conv_tariff, pattern=r"^tariff_[13]$"),
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
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_prozvon_my, pattern=r"^prozvon_my$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon_new, pattern=r"^prozvon_new$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon_menu, pattern=r"^prozvon_menu$"))
    app.add_handler(CallbackQueryHandler(cb_prozvon, pattern=r"^prozvon$"))
    app.add_handler(CallbackQueryHandler(cb_main_menu_outer, pattern=r"^main_menu$"))

    print("Бот запущено. Ctrl+C — зупинка.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
