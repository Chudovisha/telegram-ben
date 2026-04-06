"""Запуск Telegram-бота + легкий HTTP /health для Railway (PORT)."""
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


def run_health_app() -> None:
    from flask import Flask

    app = Flask(__name__)

    @app.route("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/")
    def root():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Потрібен TELEGRAM_BOT_TOKEN у змінних середовища або .env")

    if not os.environ.get("ADMIN_TELEGRAM_USER_IDS"):
        logger.warning(
            "ADMIN_TELEGRAM_USER_IDS не задано — кнопка «Адмінка» у боті буде прихована."
        )

    threading.Thread(target=run_bot_thread, daemon=True, name="telegram-bot").start()
    run_health_app()
