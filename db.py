"""
Database layer for OLX Flip Bot dashboard.

- PostgreSQL in production (Render) when DATABASE_URL is set.
- SQLite fallback for local development.
- Two tables:
    deals          - alerted/notable listings shown on the dashboard
    seen_listings  - lightweight dedup index used by the scraper across cycles
                     (replaces the old seen.json file when running on Render).

Tables are created automatically on first start. No external migration tool
needed for this scale.
"""
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, event, func, select, update,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


# ──────────────────────────────────────────────────────────────────────────────
# Engine setup
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_db_url() -> str:
    """
    Decide the database connection URL.

    Priority:
      1. DATABASE_URL (Render's PostgreSQL convention).
      2. SQLITE_PATH + sqlite:/// prefix for local dev.
      3. Default to ./data/deals.db.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # Render gives us 'postgres://...' but SQLAlchemy 2.x wants
        # 'postgresql+psycopg://' (we use the psycopg 3 driver, which has
        # prebuilt wheels for Python 3.12+ and is the modern replacement
        # for psycopg2).
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    sqlite_path = os.getenv("SQLITE_PATH", "./data/deals.db")
    os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
    return f"sqlite:///{sqlite_path}"


_DB_URL = _resolve_db_url()
_engine_kwargs: Dict[str, Any] = {"future": True, "pool_pre_ping": True}
if _DB_URL.startswith("sqlite"):
    # SQLite needs check_same_thread=False because the scraper thread and
    # Flask request threads share a single engine pool.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(_DB_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Enable WAL for SQLite so reads/writes don't block each other (dev convenience).
@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if _DB_URL.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def is_postgres() -> bool:
    return _DB_URL.startswith("postgresql")


def db_url_summary() -> str:
    """Safe-to-log summary of the connection URL (no password)."""
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", _DB_URL)


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Deal(Base):
    """A listing that the scraper considered worth alerting on."""
    __tablename__ = "deals"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    url_hash        = Column(String(40), unique=True, nullable=False, index=True)
    listing_id      = Column(String(64), nullable=True, index=True)
    url             = Column(Text, nullable=False)

    title           = Column(Text, nullable=False)
    price           = Column(Float, nullable=True)
    estimated_value = Column(Float, nullable=True)
    estimated_low   = Column(Float, nullable=True)
    estimated_high  = Column(Float, nullable=True)
    profit          = Column(Float, nullable=True)
    profit_percent  = Column(Float, nullable=True)

    location        = Column(Text, nullable=True)
    image_url       = Column(Text, nullable=True)
    source          = Column(String(32), nullable=True)   # OLX / CustoJusto
    category        = Column(Text, nullable=True)         # watchlist name
    search_term     = Column(Text, nullable=True)
    description     = Column(Text, nullable=True)
    reason          = Column(Text, nullable=True)
    risk_flags      = Column(Text, nullable=True)         # comma-separated

    telegram_sent     = Column(Boolean, default=False, nullable=False)
    telegram_sent_at  = Column(DateTime(timezone=True), nullable=True)

    favorite  = Column(Boolean, default=False, nullable=False, index=True)
    contacted = Column(Boolean, default=False, nullable=False)
    ignored   = Column(Boolean, default=False, nullable=False, index=True)
    archived  = Column(Boolean, default=False, nullable=False, index=True)
    seen      = Column(Boolean, default=False, nullable=False, index=True)
    notes     = Column(Text, nullable=True)

    created_at     = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at     = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    first_seen_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at   = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url_hash": self.url_hash,
            "listing_id": self.listing_id,
            "url": self.url,
            "title": self.title,
            "price": self.price,
            "estimated_value": self.estimated_value,
            "estimated_low": self.estimated_low,
            "estimated_high": self.estimated_high,
            "profit": self.profit,
            "profit_percent": self.profit_percent,
            "location": self.location,
            "image_url": self.image_url,
            "source": self.source,
            "category": self.category,
            "search_term": self.search_term,
            "description": self.description,
            "reason": self.reason,
            "risk_flags": self.risk_flags.split(",") if self.risk_flags else [],
            "telegram_sent": self.telegram_sent,
            "telegram_sent_at": self.telegram_sent_at.isoformat() if self.telegram_sent_at else None,
            "favorite": self.favorite,
            "contacted": self.contacted,
            "ignored": self.ignored,
            "archived": self.archived,
            "seen": self.seen,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


class SeenListing(Base):
    """
    Lightweight dedup record for every observed listing (alerted or not).
    Replaces seen.json on Render where the filesystem is ephemeral.
    """
    __tablename__ = "seen_listings"

    url_hash       = Column(String(40), primary_key=True)
    url            = Column(Text, nullable=False)
    price          = Column(Float, nullable=True)
    alerted_price  = Column(Float, nullable=True)
    alerted_at     = Column(DateTime(timezone=True), nullable=True)
    first_seen_at  = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at   = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


# ──────────────────────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def hash_url(url: str) -> str:
    """Stable 40-char hash for a listing URL (used as dedup key)."""
    if not url:
        return ""
    canonical = url.split("?")[0].rstrip("/").lower()
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


_OLX_LISTING_ID_RE = re.compile(r"-ID([A-Za-z0-9]+)\.html")


def extract_olx_listing_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = _OLX_LISTING_ID_RE.search(url)
    return m.group(1) if m else None


# ── Deal upsert (the integration point used by the scraper) ──────────────────

def upsert_deal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Insert a new deal or update last_seen_at + price-related fields if the URL
    is already known. Returns the saved deal as a dict and a bool flag set on
    the dict under "_was_inserted".

    Required keys in payload: url, title.
    """
    url = payload["url"]
    payload["url_hash"] = hash_url(url)
    if not payload.get("listing_id"):
        payload["listing_id"] = extract_olx_listing_id(url)

    now = _utcnow()
    with session_scope() as s:
        existing = s.execute(
            select(Deal).where(Deal.url_hash == payload["url_hash"])
        ).scalar_one_or_none()

        if existing is None:
            deal = Deal(
                url_hash       = payload["url_hash"],
                listing_id     = payload.get("listing_id"),
                url            = url,
                title          = payload.get("title", "")[:1000],
                price          = payload.get("price"),
                estimated_value= payload.get("estimated_value"),
                estimated_low  = payload.get("estimated_low"),
                estimated_high = payload.get("estimated_high"),
                profit         = payload.get("profit"),
                profit_percent = payload.get("profit_percent"),
                location       = payload.get("location"),
                image_url      = payload.get("image_url"),
                source         = payload.get("source"),
                category       = payload.get("category"),
                search_term    = payload.get("search_term"),
                description    = payload.get("description"),
                reason         = payload.get("reason"),
                risk_flags     = ",".join(payload["risk_flags"]) if payload.get("risk_flags") else None,
                telegram_sent  = bool(payload.get("telegram_sent", False)),
                telegram_sent_at = payload.get("telegram_sent_at"),
                first_seen_at  = now,
                last_seen_at   = now,
            )
            s.add(deal)
            s.flush()
            result = deal.to_dict()
            result["_was_inserted"] = True
            return result

        # Update path — refresh price, last seen, and any non-empty fields.
        existing.last_seen_at = now
        if payload.get("price") is not None:
            existing.price = payload["price"]
        if payload.get("estimated_value") is not None:
            existing.estimated_value = payload["estimated_value"]
            existing.estimated_low  = payload.get("estimated_low",  existing.estimated_low)
            existing.estimated_high = payload.get("estimated_high", existing.estimated_high)
            existing.profit         = payload.get("profit",         existing.profit)
            existing.profit_percent = payload.get("profit_percent", existing.profit_percent)
        if payload.get("image_url"):
            existing.image_url = payload["image_url"]
        if payload.get("location"):
            existing.location = payload["location"]
        if payload.get("description"):
            existing.description = payload["description"]
        s.flush()
        result = existing.to_dict()
        result["_was_inserted"] = False
        return result


def mark_telegram_sent(url_hash: str) -> None:
    with session_scope() as s:
        s.execute(
            update(Deal)
            .where(Deal.url_hash == url_hash)
            .values(telegram_sent=True, telegram_sent_at=_utcnow())
        )


# ── Seen-listings (scraper dedup index) ──────────────────────────────────────

def load_seen_dict() -> Dict[str, Dict[str, Any]]:
    """Load all seen rows into the dict shape bot.py expects."""
    out: Dict[str, Dict[str, Any]] = {}
    with session_scope() as s:
        for row in s.execute(select(SeenListing)).scalars():
            out[row.url] = {
                "first": row.first_seen_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.first_seen_at else None,
                "price": row.price,
                "alerted_price": row.alerted_price,
                "alerted_at": row.alerted_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.alerted_at else None,
            }
    return out


def save_seen_dict(seen: Dict[str, Dict[str, Any]], max_age_days: int = 60) -> None:
    """Bulk write the seen dict back. Prunes rows older than max_age_days."""
    if not seen:
        return
    cutoff = _utcnow().timestamp() - max_age_days * 86400
    now = _utcnow()

    with session_scope() as s:
        # Pull existing keys in one query to decide insert vs update.
        existing_hashes = {
            row[0] for row in s.execute(
                select(SeenListing.url_hash).where(
                    SeenListing.url_hash.in_([hash_url(u) for u in seen.keys()])
                )
            )
        }
        for url, meta in seen.items():
            uh = hash_url(url)
            first_str = meta.get("first")
            try:
                first_dt = datetime.strptime(first_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if first_str else now
            except (TypeError, ValueError):
                first_dt = now
            if first_dt.timestamp() < cutoff:
                continue  # too old, will be pruned by skipping
            alerted_str = meta.get("alerted_at")
            try:
                alerted_dt = datetime.strptime(alerted_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if alerted_str else None
            except (TypeError, ValueError):
                alerted_dt = None

            if uh in existing_hashes:
                s.execute(
                    update(SeenListing)
                    .where(SeenListing.url_hash == uh)
                    .values(
                        price=meta.get("price"),
                        alerted_price=meta.get("alerted_price"),
                        alerted_at=alerted_dt,
                        last_seen_at=now,
                    )
                )
            else:
                s.add(SeenListing(
                    url_hash=uh,
                    url=url,
                    price=meta.get("price"),
                    alerted_price=meta.get("alerted_price"),
                    alerted_at=alerted_dt,
                    first_seen_at=first_dt,
                    last_seen_at=now,
                ))
        # Prune ancient rows (run after upserts so we don't fight ourselves).
        s.execute(
            SeenListing.__table__.delete().where(
                SeenListing.first_seen_at < datetime.fromtimestamp(cutoff, tz=timezone.utc)
            )
        )


# ── Stats ────────────────────────────────────────────────────────────────────

def stats_summary() -> Dict[str, Any]:
    with session_scope() as s:
        total = s.scalar(select(func.count(Deal.id))) or 0
        today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today = s.scalar(
            select(func.count(Deal.id)).where(Deal.first_seen_at >= today_start)
        ) or 0
        avg_profit_pct = s.scalar(
            select(func.avg(Deal.profit_percent)).where(
                Deal.archived == False, Deal.profit_percent.isnot(None)
            )
        ) or 0
        highest = s.execute(
            select(Deal).where(Deal.archived == False).order_by(Deal.profit.desc().nullslast()).limit(1)
        ).scalar_one_or_none()
        favorites = s.scalar(select(func.count(Deal.id)).where(Deal.favorite == True)) or 0
        contacted = s.scalar(select(func.count(Deal.id)).where(Deal.contacted == True)) or 0
        ignored   = s.scalar(select(func.count(Deal.id)).where(Deal.ignored == True))   or 0
        archived  = s.scalar(select(func.count(Deal.id)).where(Deal.archived == True))  or 0
        active_profit = s.scalar(
            select(func.sum(Deal.profit)).where(
                Deal.archived == False, Deal.ignored == False, Deal.profit.isnot(None)
            )
        ) or 0
        return {
            "total_deals": int(total),
            "deals_today": int(today),
            "avg_profit_percent": round(float(avg_profit_pct or 0), 1),
            "highest_profit_deal": highest.to_dict() if highest else None,
            "favorites": int(favorites),
            "contacted": int(contacted),
            "ignored": int(ignored),
            "archived": int(archived),
            "estimated_active_profit": round(float(active_profit or 0), 2),
        }


# ── Query helpers used by the API ────────────────────────────────────────────

def list_deals(
    *,
    search: Optional[str] = None,
    category: Optional[str] = None,
    min_profit_percent: Optional[float] = None,
    min_profit: Optional[float] = None,
    location: Optional[str] = None,
    only_unseen: bool = False,
    only_favorites: bool = False,
    hide_risky: bool = False,
    hide_ignored: bool = True,
    hide_archived: bool = True,
    sort: str = "newest",
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    q = select(Deal)
    if search:
        like = f"%{search.lower()}%"
        q = q.where(func.lower(Deal.title).like(like))
    if category:
        q = q.where(Deal.category == category)
    if min_profit_percent is not None:
        q = q.where(Deal.profit_percent >= min_profit_percent)
    if min_profit is not None:
        q = q.where(Deal.profit >= min_profit)
    if location:
        like = f"%{location.lower()}%"
        q = q.where(func.lower(Deal.location).like(like))
    if only_unseen:
        q = q.where(Deal.seen == False)
    if only_favorites:
        q = q.where(Deal.favorite == True)
    if hide_risky:
        # No risk_flags column data means safe; flagged ones are non-empty.
        q = q.where((Deal.risk_flags == None) | (Deal.risk_flags == ""))
    if hide_ignored:
        q = q.where(Deal.ignored == False)
    if hide_archived:
        q = q.where(Deal.archived == False)

    sort_map = {
        "newest":         Deal.first_seen_at.desc(),
        "highest_profit": Deal.profit.desc().nullslast(),
        "highest_pct":    Deal.profit_percent.desc().nullslast(),
        "lowest_price":   Deal.price.asc().nullslast(),
        "highest_value":  Deal.estimated_value.desc().nullslast(),
    }
    q = q.order_by(sort_map.get(sort, sort_map["newest"]))
    q = q.limit(limit).offset(offset)
    with session_scope() as s:
        return [d.to_dict() for d in s.execute(q).scalars()]


def get_deal(deal_id: int) -> Optional[Dict[str, Any]]:
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        return d.to_dict() if d else None


def update_deal(deal_id: int, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    allowed = {"favorite", "contacted", "ignored", "archived", "seen", "notes"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return get_deal(deal_id)
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        if d is None:
            return None
        for k, v in clean.items():
            setattr(d, k, v)
        s.flush()
        return d.to_dict()


def delete_deal(deal_id: int) -> bool:
    """Soft-delete via archive. Returns True if a row was changed."""
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        if d is None:
            return False
        d.archived = True
        return True


def distinct_categories() -> List[str]:
    with session_scope() as s:
        rows = s.execute(
            select(Deal.category).where(Deal.category.isnot(None)).distinct()
        ).scalars()
        return sorted(set(r for r in rows if r))
