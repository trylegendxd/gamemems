"""
Flask web dashboard for the OLX Flip Bot.

Endpoints:
  GET  /                       Login or redirect to dashboard.
  GET  /login                  Login form.
  POST /login                  Submit credentials.
  GET  /logout                 Clear session.
  GET  /dashboard              Server-rendered dashboard.
  GET  /deals/<id>             Server-rendered deal detail page.

  GET  /health                 Public health probe (Render).
  GET  /api/health             Public detailed health probe.
  GET  /api/deals              List deals with filters/sorting.
  GET  /api/deals/<id>         Single deal.
  POST /api/deals              Manual insert (rare; used for tests).
  PATCH /api/deals/<id>        Update favorite/contacted/ignored/archived/seen/notes.
  DELETE /api/deals/<id>       Soft-delete (archive).
  POST /api/deals/<id>/send-telegram   Re-send a deal to Telegram.
  GET  /api/stats              Aggregate dashboard stats.
  GET  /api/scraper/status     Scraper state (last run, errors, counters).
  POST /api/scraper/run-now    Trigger a scan immediately.
  GET  /api/deals.csv          CSV export.

Authentication is a simple username/password against env vars
DASHBOARD_USER / DASHBOARD_PASSWORD with sessions signed by SESSION_SECRET.
All routes except /health and /api/health require auth.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import (
    Flask, Response, abort, g, jsonify, make_response, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

# Make stdout UTF-8 (Render logs are fine, but Windows dev consoles aren't).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

load_dotenv()

import db
import scraper as scraper_mod
import bot as bot_module
import yaml


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    app.config["SECRET_KEY"] = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    if os.getenv("NODE_ENV", os.getenv("FLASK_ENV", "")) == "production":
        app.config["SESSION_COOKIE_SECURE"] = True

    # ── Initialize DB ────────────────────────────────────────────────────────
    log.info("Database URL: %s", db.db_url_summary())
    db.init_db()
    log.info("Database tables ready (postgres=%s)", db.is_postgres())

    # ── Auth helpers ─────────────────────────────────────────────────────────
    DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
    DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
    LOGIN_RATELIMIT: Dict[str, List[float]] = {}
    LOGIN_RL_LOCK = threading.Lock()

    def is_authenticated() -> bool:
        return session.get("user") == DASHBOARD_USER

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not is_authenticated():
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def is_rate_limited(ip: str, max_attempts: int = 5, window_seconds: int = 300) -> bool:
        now = time.time()
        with LOGIN_RL_LOCK:
            attempts = [t for t in LOGIN_RATELIMIT.get(ip, []) if now - t < window_seconds]
            LOGIN_RATELIMIT[ip] = attempts
            return len(attempts) >= max_attempts

    def record_login_attempt(ip: str):
        with LOGIN_RL_LOCK:
            LOGIN_RATELIMIT.setdefault(ip, []).append(time.time())

    # ── Health (public) ──────────────────────────────────────────────────────
    started_at = time.time()

    @app.route("/health")
    @app.route("/api/health")
    def health():
        runner = scraper_mod.get_runner()
        sstatus = runner.get_status() if runner else {"status": "disabled"}
        # DB ping
        try:
            db.stats_summary()
            db_ok = True
        except Exception as e:
            db_ok = False
            log.warning("DB health check failed: %s", e)
        body = {
            "status": "ok" if db_ok else "degraded",
            "uptime_seconds": int(time.time() - started_at),
            "database": "ok" if db_ok else "error",
            "database_kind": "postgres" if db.is_postgres() else "sqlite",
            "scraper": sstatus,
            "version": "2.0",
        }
        return jsonify(body), (200 if db_ok else 503)

    # ── Auth pages ───────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        if is_authenticated():
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
        error = None
        if request.method == "POST":
            if is_rate_limited(ip):
                error = "Demasiadas tentativas. Tenta novamente em alguns minutos."
            else:
                user = request.form.get("username", "").strip()
                pw   = request.form.get("password", "")
                if not DASHBOARD_PASSWORD:
                    error = "DASHBOARD_PASSWORD não está configurada no servidor."
                elif user == DASHBOARD_USER and pw == DASHBOARD_PASSWORD:
                    session.clear()
                    session["user"] = user
                    session.permanent = True
                    app.permanent_session_lifetime = timedelta(days=30)
                    nxt = request.args.get("next") or url_for("dashboard")
                    return redirect(nxt)
                else:
                    record_login_attempt(ip)
                    error = "Credenciais inválidas."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ── Dashboard pages ──────────────────────────────────────────────────────
    @app.route("/dashboard")
    @require_auth
    def dashboard():
        categories = db.distinct_categories()
        return render_template("dashboard.html", categories=categories)

    @app.route("/deals/<int:deal_id>")
    @require_auth
    def deal_detail(deal_id: int):
        deal = db.get_deal(deal_id)
        if deal is None:
            abort(404)
        # Mark seen on view
        db.update_deal(deal_id, {"seen": True})
        deal["seen"] = True
        return render_template("deal_detail.html", deal=deal)

    # ── API: deals ───────────────────────────────────────────────────────────
    def _bool_arg(name: str, default: bool = False) -> bool:
        v = request.args.get(name)
        if v is None:
            return default
        return v.lower() in ("1", "true", "yes", "on")

    def _float_arg(name: str) -> Optional[float]:
        v = request.args.get(name)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except ValueError:
            return None

    @app.route("/api/deals")
    @require_auth
    def api_list_deals():
        limit = min(int(request.args.get("limit", 200)), 500)
        offset = int(request.args.get("offset", 0))
        deals = db.list_deals(
            search=request.args.get("search") or None,
            category=request.args.get("category") or None,
            min_profit_percent=_float_arg("min_profit_percent"),
            min_profit=_float_arg("min_profit"),
            location=request.args.get("location") or None,
            only_unseen=_bool_arg("only_unseen"),
            only_favorites=_bool_arg("only_favorites"),
            hide_risky=_bool_arg("hide_risky"),
            hide_ignored=_bool_arg("hide_ignored", default=True),
            hide_archived=_bool_arg("hide_archived", default=True),
            sort=request.args.get("sort", "newest"),
            limit=limit,
            offset=offset,
        )
        return jsonify({"deals": deals, "count": len(deals), "limit": limit, "offset": offset})

    @app.route("/api/deals/<int:deal_id>")
    @require_auth
    def api_get_deal(deal_id: int):
        deal = db.get_deal(deal_id)
        if deal is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify(deal)

    @app.route("/api/deals", methods=["POST"])
    @require_auth
    def api_create_deal():
        body = request.get_json(silent=True) or {}
        if not body.get("url") or not body.get("title"):
            return jsonify({"error": "url and title are required"}), 400
        saved = db.upsert_deal(body)
        return jsonify(saved), (201 if saved.get("_was_inserted") else 200)

    @app.route("/api/deals/<int:deal_id>", methods=["PATCH"])
    @require_auth
    def api_update_deal(deal_id: int):
        body = request.get_json(silent=True) or {}
        updated = db.update_deal(deal_id, body)
        if updated is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify(updated)

    @app.route("/api/deals/<int:deal_id>", methods=["DELETE"])
    @require_auth
    def api_delete_deal(deal_id: int):
        ok = db.delete_deal(deal_id)
        if not ok:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"archived": True})

    @app.route("/api/deals/<int:deal_id>/send-telegram", methods=["POST"])
    @require_auth
    def api_send_telegram(deal_id: int):
        deal = db.get_deal(deal_id)
        if deal is None:
            return jsonify({"error": "not_found"}), 404
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return jsonify({"error": "telegram_not_configured"}), 400
        base_url = os.getenv("BASE_URL", "").rstrip("/")
        msg = (
            f"🔁 Re-enviado do dashboard\n\n"
            f"Categoria: {deal.get('category') or '-'}\n"
            f"Artigo: {deal.get('title')}\n"
            f"Preço do vendedor: {deal.get('price'):.0f}€\n"
            if deal.get('price') is not None else
            f"🔁 Re-enviado do dashboard\n\nArtigo: {deal.get('title')}\n"
        )
        if deal.get('estimated_value') is not None:
            msg += f"Mediana de mercado: {deal['estimated_value']:.0f}€\n"
        if deal.get('profit') is not None:
            msg += f"Lucro estimado: {deal['profit']:.0f}€\n"
        if deal.get('profit_percent') is not None:
            msg += f"Margem: {deal['profit_percent']}%\n"
        if deal.get('location'):
            msg += f"Localização: {deal['location']}\n"
        msg += f"\n{deal.get('url')}"
        if base_url:
            msg += f"\nDashboard: {base_url}/deals/{deal_id}"
        try:
            bot_module.send_telegram(token, chat_id, msg)
            db.mark_telegram_sent(deal["url_hash"])
            return jsonify({"sent": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    # ── API: stats / scraper ─────────────────────────────────────────────────
    @app.route("/api/stats")
    @require_auth
    def api_stats():
        s = db.stats_summary()
        runner = scraper_mod.get_runner()
        s["scraper"] = runner.get_status() if runner else {"status": "disabled"}
        return jsonify(s)

    @app.route("/api/scraper/status")
    @require_auth
    def api_scraper_status():
        runner = scraper_mod.get_runner()
        if runner is None:
            return jsonify({"status": "disabled"}), 200
        return jsonify(runner.get_status())

    @app.route("/api/scraper/run-now", methods=["POST"])
    @require_auth
    def api_scraper_run_now():
        runner = scraper_mod.get_runner()
        if runner is None:
            return jsonify({"error": "scraper_disabled"}), 400
        triggered = runner.trigger_now()
        return jsonify({"triggered": triggered, "status": runner.get_status()})

    # ── CSV export ───────────────────────────────────────────────────────────
    @app.route("/api/deals.csv")
    @require_auth
    def api_deals_csv():
        deals = db.list_deals(
            hide_ignored=_bool_arg("hide_ignored", default=False),
            hide_archived=_bool_arg("hide_archived", default=False),
            limit=10000,
        )
        out = io.StringIO()
        cols = [
            "id", "title", "category", "price", "estimated_value",
            "profit", "profit_percent", "location", "source",
            "telegram_sent", "favorite", "contacted", "ignored", "archived",
            "url", "image_url", "first_seen_at",
        ]
        w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for d in deals:
            w.writerow(d)
        resp = make_response(out.getvalue())
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = "attachment; filename=olx_deals.csv"
        return resp

    # ── Errors ───────────────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not_found"}), 404
        return render_template("error.html", code=404, message="Página não encontrada"), 404

    @app.errorhandler(500)
    def server_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "server_error"}), 500
        return render_template("error.html", code=500, message="Erro interno"), 500

    return app


# ── Entry point ─────────────────────────────────────────────────────────────

app = create_app()


def _maybe_start_scraper():
    """Start the scraper thread unless RUN_SCRAPER=false."""
    if os.getenv("RUN_SCRAPER", "true").lower() in ("0", "false", "no"):
        log.info("RUN_SCRAPER disabled — dashboard only")
        return
    interval = float(os.getenv("SCRAPER_INTERVAL_MINUTES", "10"))
    scraper_mod.start_runner(
        config_path=os.getenv("CONFIG_PATH", "config.yml"),
        interval_minutes=interval,
        run_on_startup=os.getenv("SCRAPER_RUN_ON_STARTUP", "true").lower() not in ("0", "false", "no"),
    )


def _install_signal_handlers():
    def graceful_shutdown(signum, _frame):
        log.info("signal %s received — shutting down", signum)
        runner = scraper_mod.get_runner()
        if runner:
            runner.stop()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, graceful_shutdown)
        except (ValueError, OSError):
            # signal handlers can't be installed in non-main threads (e.g.
            # when gunicorn forks workers). Gunicorn handles SIGTERM itself
            # and the thread is daemon, so the OS will reap it on shutdown.
            pass


# Start scraper as soon as the module is imported (gunicorn's preload-style).
_install_signal_handlers()
_maybe_start_scraper()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    log.info("starting Flask dev server on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)
