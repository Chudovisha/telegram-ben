"""Веб-адмінка: список заявок, статус дзвінка, нотатки, сповіщення в Telegram."""
from __future__ import annotations

import os
import secrets
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for

from storage import (
    CALL_STATUSES,
    list_all_orders_sync,
    update_order_workflow_sync,
)
from telegram_notify import CALL_STATUS_LABELS_UA, send_order_update_notification


def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "").strip()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = (
        os.environ.get("FLASK_SECRET_KEY", "").strip()
        or os.environ.get("FLASK_SECRET", "").strip()
        or "change-me-set-FLASK_SECRET_KEY"
    )

    @app.route("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/")
    def root():
        return redirect(url_for("admin_login"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        pw = _admin_password()
        if not pw:
            return (
                "Адмінка вимкнена: додайте ADMIN_PASSWORD у змінні середовища.",
                503,
            )
        if request.method == "POST":
            guess = (request.form.get("password") or "").strip()
            if secrets.compare_digest(guess, pw):
                session["admin_ok"] = True
                return redirect(url_for("admin_orders"))
            flash("Невірний пароль.", "error")
        return render_template("admin/login.html")

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/admin/")
    @login_required
    def admin_orders():
        try:
            orders = list_all_orders_sync()
        except Exception as e:
            flash(f"Не вдалося прочитати таблицю: {e}", "error")
            orders = []
        return render_template(
            "admin/orders.html",
            orders=orders,
            status_labels=CALL_STATUS_LABELS_UA,
            statuses=CALL_STATUSES,
        )

    @app.route("/admin/order/<int:sheet_row>", methods=["POST"])
    @login_required
    def admin_order_update(sheet_row: int):
        call_status = (request.form.get("call_status") or "").strip()
        admin_notes = request.form.get("admin_notes") or ""
        if call_status not in CALL_STATUSES:
            flash("Недопустимий статус.", "error")
            return redirect(url_for("admin_orders"))
        try:
            result = update_order_workflow_sync(sheet_row, call_status, admin_notes)
        except Exception as e:
            flash(f"Помилка збереження: {e}", "error")
            return redirect(url_for("admin_orders"))
        if not result.get("changed"):
            flash("Змін не було — дані такі самі.", "info")
            return redirect(url_for("admin_orders"))
        uid = result.get("telegram_user_id")
        if uid is None:
            flash("Збережено, але не вдалося визначити користувача для сповіщення.", "error")
            return redirect(url_for("admin_orders"))
        ok, err = send_order_update_notification(
            int(uid),
            str(result.get("created_at") or ""),
            str(result.get("task_text") or ""),
            call_status,
            admin_notes,
        )
        if ok:
            flash("Збережено. Користувачу надіслано повідомлення в Telegram.", "success")
        else:
            flash(
                f"Збережено в таблиці, але Telegram не відповів: {err}",
                "error",
            )
        return redirect(url_for("admin_orders"))

    return app
