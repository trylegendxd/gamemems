# ──────────────────────────────────────────────────────────────────────────────
# PATCH: adicionar ao ficheiro bot.py, após a função `save_market_cache`
# ──────────────────────────────────────────────────────────────────────────────

def clear_local_caches() -> dict:
    """
    Reset the local-filesystem dedup and market-cache files to empty dicts.

    Called by the "Limpar Anúncios" admin action to ensure that even when
    running with SQLite (no DATABASE_URL) the next scan truly starts fresh.

    Returns a summary of which files were touched:
        {
          "seen_file_cleared":         True | False,
          "market_cache_file_cleared": True | False,
        }
    """
    result: dict = {}

    # ── seen.json ─────────────────────────────────────────────────────────
    try:
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text("{}", encoding="utf-8")
        result["seen_file_cleared"] = True
    except Exception as exc:
        result["seen_file_cleared"] = False
        result["seen_file_error"] = str(exc)

    # ── market_cache.json ─────────────────────────────────────────────────
    try:
        MARKET_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MARKET_CACHE_FILE.write_text("{}", encoding="utf-8")
        result["market_cache_file_cleared"] = True
    except Exception as exc:
        result["market_cache_file_cleared"] = False
        result["market_cache_file_error"] = str(exc)

    return result
