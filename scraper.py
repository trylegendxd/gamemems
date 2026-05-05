"""
Background scraper scheduler.

Wraps `bot.run_once` in a daemon thread that:
  - Runs every SCRAPER_INTERVAL_MINUTES (default 10).
  - Refuses to overlap (single-flight via threading.Lock).
  - Records last_started, last_finished, last_success, last_error, total_scans,
    total_deals_found, current_status — exposed via /api/scraper/status.
  - Persists every alerted deal into the DB before Telegram fires
    (via the on_deal_callback hook in bot.process_watch).
  - Survives errors without killing the Flask process.
  - Exposes trigger_now() for manual scans from the dashboard.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml

import bot as bot_module
import db as db_module


log = logging.getLogger("scraper")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ScraperRunner:
    """One-process background scraper. Instantiated once by app.py."""

    def __init__(
        self,
        config_path: str = "config.yml",
        interval_minutes: Optional[float] = None,
        run_on_startup: bool = True,
    ):
        self.config_path = config_path
        env_interval = os.getenv("SCRAPER_INTERVAL_MINUTES")
        self.interval_minutes = float(
            interval_minutes if interval_minutes is not None
            else (env_interval if env_interval else 10)
        )
        self.run_on_startup = run_on_startup

        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.base_url = os.getenv("BASE_URL", "").rstrip("/")

        # State (read by /api/scraper/status)
        self._lock = threading.Lock()        # serialize scans
        self._state_lock = threading.Lock()  # protect state dict
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self.state: Dict[str, Any] = {
            "status": "idle",          # idle | running | sleeping | error | stopped
            "started_at": None,
            "last_started":  None,
            "last_finished": None,
            "last_success":  None,
            "last_error":    None,
            "total_scans":   0,
            "total_alerts":  0,
            "total_deals_found": 0,
            "interval_minutes":  self.interval_minutes,
            "is_scraping":   False,
        }
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            log.info("scraper already running")
            return
        self._stop_event.clear()
        self._set_state(status="sleeping", started_at=_now_iso())
        self._thread = threading.Thread(
            target=self._loop, name="scraper-loop", daemon=True
        )
        self._thread.start()
        log.info("scraper thread started (interval=%.1fmin)", self.interval_minutes)

    def stop(self, timeout: float = 5.0) -> None:
        log.info("scraper stop requested")
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._set_state(status="stopped", is_scraping=False)

    def trigger_now(self) -> bool:
        """Wake the loop to run immediately. Returns False if already running."""
        if self._lock.locked():
            return False
        self._wake_event.set()
        return True

    # ── core loop ───────────────────────────────────────────────────────────

    def _loop(self) -> None:
        if self.run_on_startup:
            self._wake_event.set()  # don't sleep before the first scan

        while not self._stop_event.is_set():
            # Wait for either the interval to elapse or trigger_now to fire.
            woke_early = self._wake_event.wait(timeout=self.interval_minutes * 60)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            if woke_early:
                log.info("scraper woken early (manual trigger)")
            self._run_one_safely()

    def _run_one_safely(self) -> None:
        # Single-flight: skip if a scan is already in flight.
        if not self._lock.acquire(blocking=False):
            log.warning("scan already in progress, skipping this tick")
            return
        try:
            self._set_state(status="running", is_scraping=True, last_started=_now_iso())
            t0 = time.time()
            stats = self._run_one()
            elapsed = time.time() - t0

            total_alerts = sum(s.alerts + s.price_drops for s in (stats or []))
            with self._state_lock:
                self.state["total_scans"] += 1
                self.state["total_alerts"] += total_alerts
                # Refresh deals_found from the DB so manual inserts are counted too.
                try:
                    self.state["total_deals_found"] = db_module.stats_summary().get("total_deals", 0)
                except Exception:
                    pass
                self.state["last_finished"] = _now_iso()
                self.state["last_success"] = _now_iso()
                self.state["last_error"] = None
                self.state["status"] = "sleeping"
                self.state["is_scraping"] = False
                self.state["last_elapsed_seconds"] = round(elapsed, 1)
            log.info("scan complete in %.1fs, alerts=%d", elapsed, total_alerts)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("scan failed: %s\n%s", e, tb)
            with self._state_lock:
                self.state["last_finished"] = _now_iso()
                self.state["last_error"] = f"{type(e).__name__}: {e}"
                self.state["status"] = "error"
                self.state["is_scraping"] = False
        finally:
            self._lock.release()

    def _run_one(self):
        with open(self.config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return bot_module.run_once(
            config, self.token, self.chat_id,
            on_deal_callback=self._on_deal,
        )

    # ── deal callback used by bot.process_watch ─────────────────────────────

    def _on_deal(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Persist a deal and return its dashboard URL (if BASE_URL is set) so
        the Telegram message can include a clickable link.
        """
        try:
            saved = db_module.upsert_deal(payload)
        except Exception as e:
            log.exception("failed to save deal: %s", e)
            return None
        if not self.base_url:
            return None
        return f"{self.base_url}/deals/{saved['id']}"

    # ── status read API ─────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self.state)

    def _set_state(self, **kwargs):
        with self._state_lock:
            self.state.update(kwargs)


# Module-level singleton (created by app.py at startup)
runner: Optional[ScraperRunner] = None


def get_runner() -> Optional[ScraperRunner]:
    return runner


def start_runner(*args, **kwargs) -> ScraperRunner:
    global runner
    if runner is None:
        runner = ScraperRunner(*args, **kwargs)
    runner.start()
    return runner
