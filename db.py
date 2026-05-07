# ──────────────────────────────────────────────────────────────────────────────
# PATCH: adicionar ao ficheiro db.py, após a função `purge_old_seen_listings`
# ──────────────────────────────────────────────────────────────────────────────

def clear_all_listings() -> dict:
    """
    Hard-delete ALL rows from both `deals` and `seen_listings`.

    Used exclusively by the "Limpar Anúncios" admin action so that the next
    scan starts from a clean slate and re-publishes every offer.

    Returns a dict with the counts of deleted rows:
        {"deals": <int>, "seen_listings": <int>}
    """
    with session_scope() as s:
        deals_count   = s.scalar(select(func.count(Deal.id)))        or 0
        seen_count    = s.scalar(select(func.count(SeenListing.url_hash))) or 0
        s.execute(Deal.__table__.delete())
        s.execute(SeenListing.__table__.delete())
        return {
            "deals":         int(deals_count),
            "seen_listings": int(seen_count),
        }
