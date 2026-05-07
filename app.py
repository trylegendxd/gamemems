# ──────────────────────────────────────────────────────────────────────────────
# PATCH: adicionar ao ficheiro app.py, dentro da função create_app(),
#        logo após o bloco do endpoint  POST /api/scraper/run-now
# ──────────────────────────────────────────────────────────────────────────────

    @app.route("/api/admin/limpar-anuncios", methods=["POST"])
    @require_auth
    def api_limpar_anuncios():
        """
        Admin action: reset all listing data so the next scan starts fresh.

        Steps
        -----
        1. Reject the request if a scan is currently running (race-condition
           guard).
        2. Hard-delete every row in `deals` and `seen_listings` (DB).
        3. Reset `data/seen.json` and `data/market_cache.json` to `{}`.
        4. Return a JSON summary with counts and a timestamp.

        Authentication: same session-cookie auth as all other API endpoints.
        Additional guard: requires the header  X-Confirm-Action: limpar
        to prevent accidents from mis-clicks.
        """
        # ── CSRF-style confirmation header ───────────────────────────────
        confirm = request.headers.get("X-Confirm-Action", "")
        if confirm != "limpar":
            return jsonify({
                "error": "missing_confirmation",
                "detail": "Enviar header X-Confirm-Action: limpar para confirmar."
            }), 400

        # ── Refuse while a scan is in progress ───────────────────────────
        current_runner = scraper_mod.get_runner()
        if current_runner is not None:
            status = current_runner.get_status()
            if status.get("is_scraping"):
                return jsonify({
                    "error": "scan_in_progress",
                    "detail": "Aguarda o fim do scan antes de limpar."
                }), 409

        started_at = datetime.now(timezone.utc).isoformat()
        log.warning(
            "[LIMPAR-ANUNCIOS] início — utilizador=%s ip=%s",
            session.get("user", "?"),
            request.remote_addr,
        )

        # ── 1. DB: hard-delete deals + seen_listings ──────────────────────
        try:
            db_counts = db.clear_all_listings()
            log.info(
                "[LIMPAR-ANUNCIOS] DB limpo — deals=%d  seen_listings=%d",
                db_counts["deals"],
                db_counts["seen_listings"],
            )
        except Exception as exc:
            log.exception("[LIMPAR-ANUNCIOS] ERRO ao limpar DB: %s", exc)
            return jsonify({"error": "db_error", "detail": str(exc)}), 500

        # ── 2. Ficheiros locais: seen.json + market_cache.json ────────────
        try:
            file_result = bot_module.clear_local_caches()
            log.info("[LIMPAR-ANUNCIOS] caches locais: %s", file_result)
        except Exception as exc:
            log.warning("[LIMPAR-ANUNCIOS] AVISO ao limpar caches: %s", exc)
            file_result = {"error": str(exc)}

        # ── 3. Reset contador de total_alerts/total_scans no runner ──────
        #      (opcional mas melhora a legibilidade do status após limpeza)
        if current_runner is not None:
            with current_runner._state_lock:
                current_runner.state["total_scans"]  = 0
                current_runner.state["total_alerts"] = 0
                current_runner.state["total_deals_found"] = 0

        finished_at = datetime.now(timezone.utc).isoformat()
        log.warning(
            "[LIMPAR-ANUNCIOS] concluído — deals_removidos=%d  seen_removidos=%d",
            db_counts["deals"],
            db_counts["seen_listings"],
        )

        return jsonify({
            "ok": True,
            "started_at":  started_at,
            "finished_at": finished_at,
            "db": db_counts,
            "local_caches": file_result,
        })
