"""Запуск Telegram-бота (polling) і веб-адмінки на одному процесі (Railway: один контейнер, PORT для HTTP)."""
from __future__ import annotations

import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def run_bot_thread() -> None:
    from bot import main as bot_main

    bot_main()


if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Потрібен TELEGRAM_BOT_TOKEN у змінних середовища або .env")

    threading.Thread(target=run_bot_thread, daemon=True, name="telegram-bot").start()

    from admin_app import create_app

    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    if not os.environ.get("ADMIN_PASSWORD"):
        logger.warning(
            "ADMIN_PASSWORD не задано — вхід в /admin/login буде недоступний (503)."
        )
    if not (os.environ.get("FLASK_SECRET_KEY") or os.environ.get("FLASK_SECRET")):
        logger.warning(
            "FLASK_SECRET_KEY не задано — для продакшену задайте випадковий рядок (сесії адмінки)."
        )
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
