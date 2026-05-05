import os
import re
import sys
import time
import json
import hashlib
import statistics
import threading
import unicodedata
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Iterable, Tuple
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv


DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
MARKET_CACHE_FILE = DATA_DIR / "market_cache.json"

# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Listing:
    title: str
    price: Optional[float]
    url: str
    source: str
    location: str = ""
    raw_text: str = ""
    image_url: str = ""
    description: str = ""


@dataclass
class WatchStats:
    name: str
    market_value: Optional[float] = None
    sample_size: int = 0
    listings_seen: int = 0
    skipped_blacklist: int = 0
    skipped_location: int = 0
    skipped_price: int = 0
    scored: int = 0
    alerts: int = 0
    price_drops: int = 0
    errors: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Text utilities
# ──────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    text = text or ""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_price(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.replace("\xa0", " ")
    matches = re.findall(r"(\d{1,3}(?:[.\s]\d{3})+|\d+)(,\d{1,2})?\s*€", text)
    if not matches:
        return None
    int_part, dec_part = matches[0]
    raw = int_part.replace(".", "").replace(" ", "")
    if dec_part:
        raw = raw + dec_part.replace(",", ".")
    try:
        price = float(raw)
        return price if price > 0 else None
    except ValueError:
        return None


# GPU/hardware variant suffixes that turn one product into a different one.
_VARIANT_SUFFIXES = r"(?:ti|super|xt|xtx|gre|max-q)"


# ──────────────────────────────────────────────────────────────────────────────
# Model fingerprinting
# Extracts a canonical model key from a listing title so we can group listings
# by specific model and compare apples to apples.
# ──────────────────────────────────────────────────────────────────────────────

# Rules are tried in order; first match wins.
# Each rule is (category_tag, pattern_that_must_match, extractor_function).
# The extractor receives the normalized title and returns a model key string,
# or None if it can't fingerprint confidently.

def _extract_ipad_model(t: str) -> Optional[str]:
    """
    Handles: iPad mini, iPad Air, iPad Pro + size + generation/chip variants.
    Examples:
      "ipad pro 11 m4"       -> "ipad pro 11 m4"
      "ipad air 13 m2"       -> "ipad air 13 m2"
      "ipad mini 6"          -> "ipad mini 6"
      "ipad 10 geracao"      -> "ipad 10"
      "ipad pro 12.9 2022"   -> "ipad pro 12.9"
    """
    # iPad Pro / Air / mini with optional size and chip/gen
    m = re.search(r"ipad (pro|air|mini)\s*(\d+[\.,]?\d*)?(?:\s+(m\d|a\d{2}\w*|\d{1,2}(?:th|st|nd|rd)?\s*gen(?:eracao|eration)?))?", t)
    if m:
        variant = m.group(1)
        size    = (m.group(2) or "").replace(",", ".").strip()
        chip    = (m.group(3) or "").strip()
        key = f"ipad {variant}"
        if size:
            key += f" {size}"
        if chip:
            chip = re.sub(r"\s*(th|st|nd|rd)?\s*gen(eracao|eration)?", "gen", chip)
            key += f" {chip}"
        return key
    # Base iPad with generation number
    m = re.search(r"ipad\s+(\d{1,2})(?:\s*(?:th|st|nd|rd|a|o)?\s*(?:gen|geracao|generation))?", t)
    if m:
        return f"ipad gen{m.group(1)}"
    # Just "ipad" with year
    m = re.search(r"ipad\s+(20\d\d)", t)
    if m:
        return f"ipad {m.group(1)}"
    return "ipad"  # generic fallback — still better than nothing


def _extract_iphone_model(t: str) -> Optional[str]:
    """
    "iphone 13 pro max 256" -> "iphone 13 pro max"
    "iphone 15 128gb"       -> "iphone 15"
    """
    m = re.search(r"iphone\s+(\d{1,2})\s*(pro\s*max|pro\s*plus|pro|plus|mini)?", t)
    if not m:
        return None
    number  = m.group(1)
    variant = (m.group(2) or "").strip()
    key = f"iphone {number}"
    if variant:
        key += f" {variant}"
    return key


def _extract_samsung_model(t: str) -> Optional[str]:
    """
    "samsung galaxy s22 ultra" -> "samsung s22 ultra"
    "samsung a54"              -> "samsung a54"
    """
    m = re.search(r"(?:samsung\s+)?(?:galaxy\s+)?(s\d{1,2}|a\d{2}|m\d{2}|z\s*fold\s*\d?|z\s*flip\s*\d?)\s*(ultra|plus|\+|fe)?", t)
    if not m:
        return None
    model   = m.group(1).replace(" ", "")
    variant = (m.group(2) or "").replace("+", "plus").strip()
    key = f"samsung {model}"
    if variant:
        key += f" {variant}"
    return key


def _extract_macbook_model(t: str) -> Optional[str]:
    """
    "macbook air m2 13" -> "macbook air m2"
    "macbook pro 14 m3" -> "macbook pro 14 m3"
    """
    m = re.search(r"macbook\s+(air|pro)\s*(\d{2})?\s*(m\d)?", t)
    if not m:
        return None
    variant = m.group(1)
    size    = m.group(2) or ""
    chip    = m.group(3) or ""
    key = f"macbook {variant}"
    if chip:
        key += f" {chip}"
    if size:
        key += f" {size}"
    return key


def _extract_gpu_model(t: str) -> Optional[str]:
    """
    "rtx 3060 ti 12gb msi" -> "rtx 3060 ti"
    "rx 6700 xt"           -> "rx 6700 xt"
    "gtx 1080 ti"          -> "gtx 1080 ti"
    """
    m = re.search(r"(rtx|gtx|rx)\s*(\d{3,4})\s*(ti|super|xt|xtx|gre)?", t)
    if not m:
        return None
    family  = m.group(1)
    number  = m.group(2)
    suffix  = (m.group(3) or "").strip()
    key = f"{family} {number}"
    if suffix:
        key += f" {suffix}"
    return key


def _extract_console_model(t: str) -> Optional[str]:
    patterns = [
        (r"ps\s*5", "ps5"),
        (r"ps\s*4\s*(pro|slim)?", lambda m: "ps4 " + (m.group(1) or "").strip() if m.group(1) else "ps4"),
        (r"xbox\s+series\s+(x|s)", lambda m: f"xbox series {m.group(1)}"),
        (r"(?:nintendo\s+)?switch\s*(oled|lite)?", lambda m: "switch " + (m.group(1) or "v1").strip()),
    ]
    for pat, result in patterns:
        m = re.search(pat, t)
        if m:
            return result(m) if callable(result) else result
    return None


# Registry: maps a trigger keyword → extractor function
_FINGERPRINT_EXTRACTORS = [
    ("ipad",     re.compile(r"\bipad\b"),                    _extract_ipad_model),
    ("iphone",   re.compile(r"\biphone\b"),                  _extract_iphone_model),
    ("samsung",  re.compile(r"\bsamsung\b|\bgalaxy\b"),       _extract_samsung_model),
    ("macbook",  re.compile(r"\bmacbook\b"),                  _extract_macbook_model),
    ("gpu",      re.compile(r"\b(rtx|gtx|rx)\s*\d{3,4}\b"),  _extract_gpu_model),
    ("console",  re.compile(r"\b(ps[45]|xbox|nintendo|switch)\b"), _extract_console_model),
]


def extract_model_key(title: str) -> Optional[str]:
    """
    Returns a canonical model key for the listing, e.g. 'ipad pro 11 m4'.
    Returns None if no specific model can be detected (use generic comparison).
    """
    t = normalize(title)
    for _tag, trigger, extractor in _FINGERPRINT_EXTRACTORS:
        if trigger.search(t):
            result = extractor(t)
            if result:
                return normalize(result)
    return None


def titles_same_model(title_a: str, title_b: str) -> bool:
    """True if both listings fingerprint to the same model key."""
    key_a = extract_model_key(title_a)
    key_b = extract_model_key(title_b)
    if key_a is None or key_b is None:
        return False
    return key_a == key_b


def title_matches(title: str, keywords: List[str]) -> bool:
    """
    Returns True only if ALL keywords appear in the title.
    Also rejects titles where a keyword is immediately followed by a variant
    suffix that would make it a different product (e.g. searching "rtx 3060"
    should not match "rtx 3060 ti").
    """
    t = normalize(title)
    for k in keywords:
        nk = re.escape(normalize(k))
        if not re.search(r"(?<![a-z0-9])" + nk + r"(?![a-z0-9])", t):
            return False
        # Reject if the keyword is immediately followed by a variant suffix
        # that turns it into a different SKU (e.g. "rtx 3060 ti", "rx 6700 xt")
        if re.search(r"(?<![a-z0-9])" + nk + r"\s*(?:ti|super|xt|xtx|gre|max-q)(?![a-z0-9])", t):
            return False
    return True


def _word_boundary_search(needle: str, haystack: str) -> bool:
    """Match `needle` in `haystack` only at word boundaries (both already normalized)."""
    if not needle:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None


def find_blacklist(text: str, blacklist: List[str]) -> Optional[str]:
    t = normalize(text)
    for word in blacklist:
        if _word_boundary_search(normalize(word), t):
            return word
    return None


def matches_location(text: str, allowed_locations: List[str]) -> Optional[str]:
    t = normalize(text)
    for loc in allowed_locations:
        if _word_boundary_search(normalize(loc), t):
            return loc
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _use_db_seen() -> bool:
    """Use the database for seen-tracking when DATABASE_URL is set (Render)."""
    return bool(os.getenv("DATABASE_URL", "").strip())


def load_seen() -> Dict[str, Dict]:
    """
    Returns a dict {url: {"first": iso, "price": float, "alerted_price": float,
    "alerted_at": iso}}. On Render the canonical store is the DB; locally we
    keep using seen.json for zero-config dev.
    """
    if _use_db_seen():
        try:
            from db import load_seen_dict
            return load_seen_dict()
        except Exception as e:
            print(f"[WARN] DB seen load failed, falling back to file: {e}")
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, list):
        now = _now_iso()
        return {url: {"first": now} for url in data}
    if isinstance(data, dict):
        return data
    return {}


def save_seen(seen: Dict[str, Dict], max_age_days: int = 60) -> None:
    """Prune entries older than max_age_days, then persist (DB or file)."""
    if _use_db_seen():
        try:
            from db import save_seen_dict
            save_seen_dict(seen, max_age_days=max_age_days)
            return
        except Exception as e:
            print(f"[WARN] DB seen save failed, falling back to file: {e}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pruned = {url: meta for url, meta in seen.items()
              if meta.get("first", "9999") >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned, indent=2, sort_keys=True), encoding="utf-8")


def load_market_cache() -> Dict[str, Dict]:
    if not MARKET_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(MARKET_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_market_cache(cache: Dict[str, Dict]) -> None:
    MARKET_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def market_cache_key(watch: Dict) -> str:
    """Stable hash over the inputs that determine a watchlist's market value.
    Changes to keywords/blacklist/URLs invalidate the cache automatically."""
    payload = json.dumps({
        "k": sorted(watch.get("keywords", [])),
        "s": sorted(watch.get("search_urls", [])),
        "r": sorted(watch.get("reference_urls", [])),
        "b": sorted(watch.get("blacklist", [])),
    }, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def get_cached_market(cache: Dict, key: str, ttl_minutes: int) -> Optional[Dict]:
    entry = cache.get(key)
    if not entry:
        return None
    try:
        computed = datetime.strptime(entry["computed_at"], "%Y-%m-%dT%H:%M:%SZ")
        computed = computed.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    age_min = (datetime.now(timezone.utc) - computed).total_seconds() / 60
    if age_min > ttl_minutes:
        return None
    return entry.get("market")


# ──────────────────────────────────────────────────────────────────────────────
# Scraper
# ──────────────────────────────────────────────────────────────────────────────

class MarketplaceScraper:
    def __init__(self, user_agent: str, delay_seconds: float = 2.5):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.delay_seconds = delay_seconds
        # Locks: one for the cache (so two threads don't fetch the same URL
        # twice) and one per host (so we rate-limit OLX and CustoJusto
        # independently — different hosts can be hit in parallel).
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, List[Listing]] = {}
        self._inflight: Dict[str, threading.Event] = {}
        self._host_locks: Dict[str, threading.Lock] = {}
        self._host_locks_lock = threading.Lock()

    def _host_lock(self, url: str) -> threading.Lock:
        host = urlparse(url).netloc
        with self._host_locks_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = threading.Lock()
                self._host_locks[host] = lock
            return lock

    def fetch_html(self, url: str) -> str:
        # Per-host serialization: requests to the same host honour the delay,
        # but different hosts run in parallel (OLX + CustoJusto concurrently).
        with self._host_lock(url):
            time.sleep(self.delay_seconds)
            r = self.session.get(url, timeout=25)
            # On 404 try category-stripping fallbacks before giving up. OLX
            # renames category slugs over time, and the user's URLs may be
            # slightly stale ("/audio/" no longer exists, etc.). The fallback
            # walks up the path and finally hits the host-wide search.
            if r.status_code == 404:
                for fallback in self._fallback_urls(url):
                    fr = self.session.get(fallback, timeout=25)
                    if fr.ok:
                        print(f"  [INFO] 404 em {url} — usando fallback {fallback}")
                        return fr.text
            r.raise_for_status()
            return r.text

    @staticmethod
    def _fallback_urls(url: str) -> List[str]:
        """For an OLX URL like /a/b/c/q-foo/ produce /a/b/q-foo/, /a/q-foo/, /q-foo/."""
        p = urlparse(url)
        if "olx.pt" not in p.netloc:
            return []
        m = re.match(r"^(.*?)/(q-[^/]+)/?$", p.path)
        if not m:
            return []
        category, query = m.group(1), m.group(2)
        segments = [s for s in category.split("/") if s]
        out = []
        # Strip last segment progressively
        while segments:
            segments.pop()
            new_path = "/" + "/".join(segments + [query]) + "/"
            out.append(f"{p.scheme}://{p.netloc}{new_path}")
        return out

    # ── OLX parser ────────────────────────────────────────────────────────────

    def scrape_olx(self, url: str) -> List["Listing"]:
        html = self.fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        listings: List[Listing] = []

        # OLX wraps each ad in a <div data-cy="l-card"> or similar
        cards = soup.select("[data-cy='l-card']")
        if not cards:
            # fallback: any <li> with a link and price
            cards = soup.select("li:has(a[href])")

        for card in cards:
            a = card.find("a", href=True)
            if not a:
                continue

            href = a.get("href", "")
            full_url = href if href.startswith("http") else urljoin("https://www.olx.pt", href)

            # Title: prefer dedicated title element
            title_el = (
                card.select_one("[data-testid='ad-title']")
                or card.select_one("h3")
                or card.select_one("h4")
                or card.select_one("h6")
                or card.select_one(".css-1s3qyje")   # OLX title class (may change)
            )
            title = clean_text(title_el.get_text(" ") if title_el else a.get_text(" "))
            if len(title) < 5:
                continue

            # Price
            price_el = (
                card.select_one("[data-testid='ad-price']")
                or card.select_one("p[class*='price']")
                or card.select_one("strong")
            )
            price_text = price_el.get_text(" ") if price_el else card.get_text(" ")
            price = parse_price(price_text)
            if price is None:
                continue

            # ── Location extraction (the key fix) ─────────────────────────────
            # OLX puts location in a <p> near the bottom of the card,
            # sometimes with data-testid="location-date" or class containing "location"
            location = ""
            loc_el = (
                card.select_one("[data-testid='location-date']")
                or card.select_one("p[class*='location']")
                or card.select_one("span[class*='location']")
                # generic: small text nodes that look like "Braga, hoje"
            )
            if loc_el:
                location = clean_text(loc_el.get_text(" "))
            else:
                # Fallback: scan all <p> and <span> for location patterns
                # OLX usually shows "Cidade, data" e.g. "Braga, Hoje às 14:32"
                for el in card.find_all(["p", "span"]):
                    txt = clean_text(el.get_text(" "))
                    # Short text containing a comma likely "Location, date"
                    if "," in txt and 3 < len(txt) < 80:
                        location = txt
                        break

            raw_text = clean_text(card.get_text(" "))[:1000]

            # Image: OLX cards put the thumbnail in <img src="..."> or
            # data-src for lazy-loaded ones.
            image_url = ""
            img_el = card.select_one("img[src]") or card.select_one("img[data-src]")
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = urljoin("https://www.olx.pt", image_url)

            listings.append(Listing(
                title=title[:160],
                price=price,
                url=full_url.split("?")[0],
                source="OLX",
                location=location,
                raw_text=raw_text,
                image_url=image_url,
            ))

        return dedupe_listings(listings)

    # ── CustoJusto parser ─────────────────────────────────────────────────────
    # CustoJusto is a Next.js app — listings are embedded as JSON inside
    # <script id="__NEXT_DATA__">. CSS-based scraping returns garbage because
    # the visible DOM is hydrated client-side.

    def scrape_custojusto(self, url: str) -> List["Listing"]:
        html = self.fetch_html(url)
        listings: List[Listing] = []

        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, flags=re.DOTALL,
        )
        if not m:
            # Page structure changed — fall back to generic scrape so we still
            # try (and at least log nothing-found rather than crash).
            soup = BeautifulSoup(html, "html.parser")
            return self._scrape_generic_links(url, soup, "CustoJusto")

        try:
            data = json.loads(m.group(1))
            items = data["props"]["pageProps"]["listItems"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

        for it in items:
            title = clean_text(it.get("title", ""))
            if len(title) < 5:
                continue
            price = it.get("price")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            if price is None or price <= 0:
                continue
            href = it.get("url", "")
            full_url = href if href.startswith("http") else urljoin("https://www.custojusto.pt", href)

            loc = it.get("locationNames") or {}
            location = ", ".join(
                v for v in (loc.get("county"), loc.get("district")) if v
            )

            body = clean_text(it.get("body", ""))
            raw_text = clean_text(f"{title} {body} {location}")[:1000]
            image_url = it.get("imageFullURL") or ""

            listings.append(Listing(
                title=title[:160],
                price=price,
                url=full_url.split("?")[0],
                source="CustoJusto",
                description=body[:2000],
                image_url=image_url,
                location=location,
                raw_text=raw_text,
            ))

        return dedupe_listings(listings)

    # ── Generic fallback ──────────────────────────────────────────────────────

    def _scrape_generic_links(self, url: str, soup: BeautifulSoup, source: str) -> List["Listing"]:
        listings: List[Listing] = []
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            text = clean_text(a.get_text(" "))
            parent = a.find_parent()
            parent_text = clean_text(parent.get_text(" ")) if parent else text
            combined = clean_text(text + " " + parent_text)
            price = parse_price(combined)
            if price is None:
                continue
            title = re.sub(r"\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?\s*€", "", text)
            title = clean_text(title)
            if len(title) < 5:
                title = clean_text(combined[:160])
            if len(title) < 5:
                continue
            listings.append(Listing(
                title=title[:160],
                price=price,
                url=urljoin(url, href).split("?")[0],
                source=source,
                location="",
                raw_text=combined[:1000],
            ))
        return dedupe_listings(listings)

    def scrape_url(self, url: str) -> List["Listing"]:
        # Coalesce concurrent requests for the same URL: the first thread
        # fetches, the others wait on the Event and reuse the cached result.
        with self._cache_lock:
            cached = self._cache.get(url)
            if cached is not None:
                return cached
            event = self._inflight.get(url)
            if event is None:
                event = threading.Event()
                self._inflight[url] = event
                owner = True
            else:
                owner = False
        if not owner:
            event.wait()
            return self._cache.get(url, [])

        result: List[Listing] = []
        try:
            if "olx.pt" in url:
                result = self.scrape_olx(url)
            elif "custojusto.pt" in url:
                result = self.scrape_custojusto(url)
            else:
                html = self.fetch_html(url)
                soup = BeautifulSoup(html, "html.parser")
                result = self._scrape_generic_links(url, soup, "Marketplace")
            return result
        finally:
            with self._cache_lock:
                self._cache[url] = result
                self._inflight.pop(url, None)
                event.set()


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def dedupe_listings(items: Iterable[Listing]) -> List[Listing]:
    seen = set()
    out = []
    for item in items:
        key = item.url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def remove_outliers(prices: List[float], iqr_multiplier: float = 1.0) -> List[float]:
    """
    Trim Tukey-style outliers. Default multiplier is 1.0 (tighter than the
    classic 1.5) because second-hand prices have a long right tail of bundle
    listings (e.g. "PC + RTX 3060" lumped into a GPU search) and a short
    left tail of bait/scam listings — we want both pruned aggressively.
    """
    prices = sorted(p for p in prices if p and p > 0)
    if len(prices) < 5:
        return prices
    q1 = statistics.quantiles(prices, n=4)[0]
    q3 = statistics.quantiles(prices, n=4)[2]
    iqr = q3 - q1
    low = max(0, q1 - iqr_multiplier * iqr)
    high = q3 + iqr_multiplier * iqr
    return [p for p in prices if low <= p <= high]


# Hardware terms grouped by category. A title matching ≥2 different categories
# almost always describes a bundle (PC build, laptop bundle, etc.) and should
# be excluded from market reference data — including such listings drags the
# median up by 3-5x for component watchlists.
_BUNDLE_CATEGORIES = {
    "gpu":    re.compile(r"\b(rtx|gtx|radeon|geforce|rx\s*\d{3,4})\b"),
    "cpu":    re.compile(r"\b(ryzen|i[3579]\b|i[3579]-\d|core\s*i\d|threadripper|xeon)\b"),
    "ram":    re.compile(r"\b(ddr[345]|\d+\s*gb\s*(?:ram|de\s*ram))\b"),
    "ssd":    re.compile(r"\b(ssd|nvme|hdd|m\.?2)\b"),
    "mobo":   re.compile(r"\b(motherboard|placa[\s-]m[ãae]e|b550|b650|x570|x670|z690|z790)\b"),
    "build":  re.compile(r"\b(torre\s+gaming|setup\s+completo|pc\s+(?:completo|gaming|gamer)|build\s+gaming)\b"),
}


def looks_like_bundle(title: str) -> bool:
    """True if the title mentions hardware from ≥2 distinct categories — a
    strong signal that the listing is a multi-item bundle (full PC, kit, etc.)
    rather than the single component the watchlist is targeting."""
    t = normalize(title)
    hits = sum(1 for pat in _BUNDLE_CATEGORIES.values() if pat.search(t))
    return hits >= 2


def combined_blacklist(config: Dict, watch: Dict) -> List[str]:
    return list(config.get("global_blacklist", [])) + list(watch.get("blacklist", []))


# ──────────────────────────────────────────────────────────────────────────────
# Market value estimation
# ──────────────────────────────────────────────────────────────────────────────

def _build_market_stats(prices: List[float], iqr_mult: float = 1.0) -> Dict:
    prices = remove_outliers(prices, iqr_multiplier=iqr_mult)
    if not prices:
        return {"market_value": None, "sample_size": 0, "min": None, "max": None}
    return {
        "market_value": round(statistics.median(prices), 2),
        "sample_size": len(prices),
        "min": min(prices),
        "max": max(prices),
    }


def estimate_market_value(
    scraper: MarketplaceScraper,
    reference_urls: List[str],
    search_urls: List[str],
    keywords: List[str],
    blacklist: List[str],
    config: Optional[Dict] = None,
) -> Dict:
    """
    Returns a market dict with two levels:
      - "by_model": {model_key -> stats_dict}  ← per-model price comparison
      - "global":   stats_dict                  ← fallback when model unknown

    Reference data is filtered three ways for accuracy:
      1. keyword match (must contain the watched terms)
      2. blacklist (no "para peças", "avariada", etc.)
      3. bundle detection (no full PCs in a GPU watchlist, etc.)
    Then Tukey IQR outlier trimming is applied.
    """
    cfg_settings = (config or {}).get("settings", {})
    iqr_mult = cfg_settings.get("outlier_iqr_multiplier", 1.0)
    exclude_bundles = cfg_settings.get("exclude_bundle_listings", True)

    unique_refs = [u for u in reference_urls if u not in search_urls]
    all_ref_urls = search_urls + unique_refs

    refs: List[Listing] = []
    for url in all_ref_urls:
        try:
            refs.extend(scraper.scrape_url(url))
        except Exception as e:
            print(f"  [WARN] reference scrape failed {url}: {e}")

    # Filter: keyword + blacklist + bundle detection (the last one only for
    # market-reference purposes; we still process bundles as candidate flips
    # if they pass the listing-side filters).
    filtered = []
    bundles_excluded = 0
    for item in refs:
        if item.price is None:
            continue
        if not title_matches(item.title, keywords):
            continue
        combined_text = " ".join([item.title, item.raw_text])
        if find_blacklist(combined_text, blacklist):
            continue
        if exclude_bundles and looks_like_bundle(item.title):
            bundles_excluded += 1
            continue
        filtered.append(item)

    by_model: Dict[str, List[float]] = {}
    global_prices: List[float] = []
    for item in filtered:
        key = extract_model_key(item.title)
        global_prices.append(item.price)
        if key:
            by_model.setdefault(key, []).append(item.price)

    model_stats = {k: _build_market_stats(v, iqr_mult) for k, v in by_model.items()}
    global_stats = _build_market_stats(global_prices, iqr_mult)

    if model_stats:
        sample_info = ", ".join(f"{k}({v['sample_size']})" for k, v in sorted(model_stats.items()))
        print(f"  [MARKET-MODELS] {sample_info}")
    if bundles_excluded:
        print(f"  [MARKET-CLEAN] {bundles_excluded} bundles excluídos do cálculo")
    if not filtered and refs:
        print(f"  [WARN] {len(refs)} anúncios encontrados mas todos filtrados (keywords/blacklist/bundle)")
    elif not refs:
        print(f"  [WARN] scrape devolveu 0 anúncios — verificar URLs de referência")

    return {
        "by_model": model_stats,
        "global":   global_stats,
    }


def get_market_for_listing(market: Dict, listing: "Listing") -> Dict:
    """
    Pick the tightest applicable market stats for this specific listing.
    Priority: exact model match > global fallback.
    """
    model_key = extract_model_key(listing.title)
    if model_key:
        by_model = market.get("by_model", {})
        if model_key in by_model:
            stats = by_model[model_key]
            if stats.get("market_value") is not None:
                return {**stats, "_match": f"exact:{model_key}"}
        # Try partial match (e.g. "ipad pro 11" matches "ipad pro 11 m2" pool)
        for k, stats in by_model.items():
            if (model_key in k or k in model_key) and stats.get("market_value") is not None:
                return {**stats, "_match": f"partial:{k}"}
    # Fallback to global
    return {**market.get("global", {}), "_match": "global"}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────

def score_listing(listing: Listing, market_stats: Dict, cfg: Dict) -> Dict:
    min_margin = cfg["settings"].get("min_margin_percent", 20)
    min_profit = cfg["settings"].get("min_profit_eur", 25)
    min_sample = cfg["settings"].get("min_sample_size", 8)

    if listing.price is None:
        return {"alert": False, "reason": "No seller price"}

    market_value = market_stats.get("market_value")
    if market_value is None:
        return {"alert": False, "reason": "No market reference"}

    sample_size = market_stats.get("sample_size", 0)
    if sample_size < min_sample:
        # Not enough data to trust — score it but don't alert.
        profit = market_value - listing.price
        margin_percent = ((market_value / listing.price) - 1) * 100
        return {
            "alert": False,
            "profit": round(profit, 2),
            "margin_percent": round(margin_percent, 1),
            "market_value": market_value,
            "market_sample_size": sample_size,
            "match_type": market_stats.get("_match", "global"),
            "reason": f"Amostra insuficiente ({sample_size}<{min_sample})",
        }

    profit = market_value - listing.price
    margin_percent = ((market_value / listing.price) - 1) * 100
    alert = margin_percent >= min_margin and profit >= min_profit
    match_type = market_stats.get("_match", "global")

    return {
        "alert": alert,
        "profit": round(profit, 2),
        "margin_percent": round(margin_percent, 1),
        "market_value": market_value,
        "market_sample_size": market_stats.get("sample_size", 0),
        "match_type": match_type,
        "reason": f"Mediana de mercado {round(margin_percent, 1)}% acima do preço pedido [{match_type}]",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }, timeout=20)
    if not r.ok:
        # Surface the Telegram error description instead of a bare HTTPError.
        try:
            desc = r.json().get("description", r.text)
        except Exception:
            desc = r.text
        raise RuntimeError(f"Telegram {r.status_code}: {desc}")


def format_alert(
    watch_name: str,
    listing: Listing,
    score: Dict,
    location_hit: str,
    previous_price: Optional[float] = None,
) -> str:
    loc_display  = listing.location or location_hit or "desconhecida"
    model_key    = extract_model_key(listing.title)
    model_line   = f"Modelo detectado: {model_key}\n" if model_key else ""
    match_type   = score.get("match_type", "global")
    compare_note = "comparação por modelo exacto" if "exact" in match_type else \
                   "comparação por modelo parcial" if "partial" in match_type else \
                   "comparação global (modelo não detectado)"
    if previous_price is not None and listing.price is not None:
        drop_pct = round((1 - listing.price / previous_price) * 100, 1)
        header = (
            f"💸 Preço baixou {drop_pct}% (de {previous_price:.0f}€ → {listing.price:.0f}€)\n\n"
        )
    else:
        header = "🔥 Possível flip lucrativo\n\n"
    return (
        f"{header}"
        f"Categoria: {watch_name}\n"
        f"Artigo: {listing.title}\n"
        f"{model_line}"
        f"Preço do vendedor: {listing.price:.0f}€\n"
        f"Mediana de mercado: {score['market_value']:.0f}€  ({compare_note})\n"
        f"Lucro estimado: {score['profit']:.0f}€\n"
        f"Margem: {score['margin_percent']}%\n"
        f"Amostra de referência: {score['market_sample_size']} anúncios\n"
        f"Localização: {loc_display}\n\n"
        f"Fonte: {listing.source}\n"
        f"{listing.url}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-watchlist worker (runs in thread pool)
# ──────────────────────────────────────────────────────────────────────────────

def process_watch(
    watch: Dict,
    config: Dict,
    scraper: MarketplaceScraper,
    seen: Dict[str, Dict],
    token: str,
    chat_id: str,
    allowed_locations: List[str],
    require_location: bool,
    alerted_this_cycle: Optional[set] = None,
    alerted_lock: Optional[threading.Lock] = None,
    market_cache: Optional[Dict] = None,
    market_cache_lock: Optional[threading.Lock] = None,
    on_deal_callback=None,
) -> Tuple[Dict[str, Dict], WatchStats]:
    """
    Process one watchlist entry.
    Returns (new_seen_entries, stats) — new_seen_entries is a dict suitable
    for `seen.update(...)`.

    `alerted_this_cycle` (if provided) is a shared set guarded by `alerted_lock`
    that prevents the same listing URL being alerted by two watchlists in the
    same cycle (e.g. when one listing matches multiple keyword sets).
    """
    new_seen: Dict[str, Dict] = {}
    stats = WatchStats(name=watch["name"])
    blacklist = combined_blacklist(config, watch)
    drop_threshold = config["settings"].get("price_drop_threshold_percent", 10) / 100

    print(f"\n[WATCH] {watch['name']}")

    # Cross-cycle market cache: reference data doesn't change every 10 min,
    # so reuse it for `market_cache_ttl_minutes` (default 60). The first cycle
    # warms the cache; subsequent cycles within the TTL skip all reference
    # scraping for this watchlist (huge throughput win).
    ttl = config["settings"].get("market_cache_ttl_minutes", 60)
    cache_key = market_cache_key(watch)
    market = None
    if market_cache is not None and market_cache_lock is not None:
        with market_cache_lock:
            market = get_cached_market(market_cache, cache_key, ttl)
    if market is not None:
        print(f"  [MARKET-CACHED] reused (TTL {ttl} min)")
    else:
        market = estimate_market_value(
            scraper,
            watch.get("reference_urls", []),
            watch.get("search_urls", []),
            watch["keywords"],
            blacklist,
            config=config,
        )
        if market_cache is not None and market_cache_lock is not None:
            with market_cache_lock:
                market_cache[cache_key] = {
                    "market": market,
                    "computed_at": _now_iso(),
                    "watch_name": watch["name"],
                }
    global_stats = market.get("global", {})
    stats.market_value = global_stats.get("market_value")
    stats.sample_size = global_stats.get("sample_size", 0)
    g_val_str = f"{stats.market_value}€" if stats.market_value is not None else "None€"
    print(f"  [MARKET-GLOBAL] valor={g_val_str}  amostra={stats.sample_size}  intervalo=[{global_stats.get('min')}, {global_stats.get('max')}]")

    for search_url in watch["search_urls"]:
        try:
            listings = scraper.scrape_url(search_url)
        except Exception as e:
            stats.errors += 1
            print(f"  [WARN] falhou scrape {search_url}: {e}")
            continue

        for listing in listings:
            stats.listings_seen += 1
            key = listing.url.split("?")[0]
            if key in new_seen:
                continue

            # ── Price-drop short-circuit ──────────────────────────────────────
            # If we've seen this listing before and the price dropped enough,
            # treat it like a fresh opportunity. Otherwise skip cleanly.
            previous = seen.get(key)
            is_price_drop = False
            if previous is not None:
                prev_price = previous.get("price")
                if (prev_price and listing.price
                        and listing.price < prev_price * (1 - drop_threshold)):
                    is_price_drop = True
                    print(f"  [PRICE-DROP] {prev_price}EUR -> {listing.price}EUR: {listing.title[:50]}")
                else:
                    continue

            text_for_filter = " ".join([
                listing.title, listing.raw_text, listing.location, listing.url,
            ])

            bad_word = find_blacklist(text_for_filter, blacklist)
            if bad_word:
                stats.skipped_blacklist += 1
                print(f"  [SKIP-BL] '{bad_word}': {listing.title[:70]}")
                new_seen[key] = {"first": _now_iso(), "price": listing.price}
                continue

            # Keyword match — don't memoize failures to `new_seen` (would hide
            # legitimate matches from sibling watchlists).
            if not title_matches(listing.title, watch["keywords"]):
                continue

            location_hit = matches_location(text_for_filter, allowed_locations)
            if require_location and not location_hit:
                has_location_info = bool(listing.location.strip())
                if has_location_info:
                    stats.skipped_location += 1
                    print(f"  [SKIP-LOC] fora da área: {listing.title[:70]} | loc={listing.location}")
                    new_seen[key] = {"first": _now_iso(), "price": listing.price}
                    continue
                print(f"  [WARN-LOC] sem localização detectada, a incluir: {listing.title[:70]}")

            max_buy = watch.get("max_buy_price")
            if max_buy and listing.price and listing.price > max_buy:
                stats.skipped_price += 1
                new_seen[key] = {"first": _now_iso(), "price": listing.price}
                continue

            market_stats = get_market_for_listing(market, listing)
            score = score_listing(listing, market_stats, config)
            loc_display = listing.location or location_hit or "—"
            model_key = extract_model_key(listing.title) or "?"
            stats.scored += 1
            print(f"  [SCORE] {listing.price}€ | margem={score.get('margin_percent','?')}% | modelo={model_key} | match={score.get('match_type','?')} | {listing.title[:45]}")

            # ── Alert gating ──────────────────────────────────────────────────
            # Two layers prevent duplicate alerts:
            #
            #  1. Cross-cycle: refuse to alert if we've already alerted on this
            #     listing AND the price hasn't dropped at least `drop_threshold`
            #     below the last alerted price. The previous price-drop check
            #     compares against `price` (latest observation), this one
            #     compares against `alerted_price` (most recent alerted price)
            #     — the stricter of the two wins, so a slow price slide that
            #     never crosses the threshold won't re-alert.
            #
            #  2. Cross-watchlist within one cycle: a shared set guards against
            #     the same URL being alerted by two watchlists in parallel.
            prev_alerted_price = (previous or {}).get("alerted_price")
            should_alert = bool(score.get("alert"))
            if should_alert and prev_alerted_price is not None:
                if listing.price >= prev_alerted_price * (1 - drop_threshold):
                    should_alert = False  # already alerted at this (or higher) price

            if should_alert and alerted_this_cycle is not None and alerted_lock is not None:
                with alerted_lock:
                    if key in alerted_this_cycle:
                        should_alert = False
                    else:
                        alerted_this_cycle.add(key)

            new_alerted_price = prev_alerted_price
            if should_alert:
                # Save the deal first so the dashboard reflects it even if
                # Telegram is down. The callback returns a dashboard URL that
                # we append to the Telegram message.
                deal_dashboard_url = None
                if on_deal_callback is not None:
                    try:
                        risk_words = []
                        bw = find_blacklist(text_for_filter, blacklist)
                        if bw:
                            risk_words.append(bw)
                        deal_payload = {
                            "url": listing.url,
                            "title": listing.title,
                            "price": listing.price,
                            "estimated_value": score.get("market_value"),
                            "estimated_low": (market_stats or {}).get("min"),
                            "estimated_high": (market_stats or {}).get("max"),
                            "profit": score.get("profit"),
                            "profit_percent": score.get("margin_percent"),
                            "location": listing.location or location_hit,
                            "image_url": listing.image_url,
                            "source": listing.source,
                            "category": watch["name"],
                            "search_term": ", ".join(watch.get("keywords", [])),
                            "description": listing.description or listing.raw_text[:500],
                            "reason": score.get("reason"),
                            "risk_flags": risk_words,
                            "telegram_sent": False,
                        }
                        deal_dashboard_url = on_deal_callback(deal_payload)
                    except Exception as e:
                        print(f"  [WARN] DB save failed: {e}")

                try:
                    prev_price_for_alert = previous.get("price") if (previous and is_price_drop) else None
                    msg = format_alert(
                        watch["name"], listing, score, location_hit or "",
                        previous_price=prev_price_for_alert,
                    )
                    if deal_dashboard_url:
                        msg += f"\nDashboard: {deal_dashboard_url}"
                    send_telegram(token, chat_id, msg)
                    new_alerted_price = listing.price
                    if is_price_drop:
                        stats.price_drops += 1
                    else:
                        stats.alerts += 1
                    print(f"  [ALERT] Telegram enviado!")

                    # Mark the deal as telegram_sent now that it succeeded.
                    if on_deal_callback is not None:
                        try:
                            from db import hash_url, mark_telegram_sent
                            mark_telegram_sent(hash_url(listing.url))
                        except Exception:
                            pass
                except Exception as e:
                    stats.errors += 1
                    # Roll back the cycle-dedup so a retry next cycle is allowed
                    if alerted_this_cycle is not None and alerted_lock is not None:
                        with alerted_lock:
                            alerted_this_cycle.discard(key)
                    print(f"  [WARN] Telegram falhou: {e}")

            entry = {
                "first": (previous or {}).get("first") or _now_iso(),
                "price": listing.price,
            }
            if new_alerted_price is not None:
                entry["alerted_price"] = new_alerted_price
                entry["alerted_at"] = (previous or {}).get("alerted_at") or _now_iso()
                if should_alert:
                    entry["alerted_at"] = _now_iso()
            new_seen[key] = entry

    return new_seen, stats


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def run_once(config: Dict, token: str, chat_id: str, on_deal_callback=None) -> List[WatchStats]:
    t0 = time.time()
    seen = load_seen()
    scraper = MarketplaceScraper(
        user_agent=config["settings"]["user_agent"],
        delay_seconds=config["settings"].get("request_delay_seconds", 2.5),
    )

    allowed_locations = config.get("location_filter", {}).get("allowed_locations", [])
    require_location = config["settings"].get("require_location_match", True)

    max_workers = min(3, len(config["watchlists"]))
    all_new_seen: Dict[str, Dict] = {}
    all_stats: List[WatchStats] = []
    alerted_this_cycle: set = set()
    alerted_lock = threading.Lock()
    market_cache = load_market_cache()
    market_cache_lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_watch,
                watch, config, scraper, seen,
                token, chat_id,
                allowed_locations, require_location,
                alerted_this_cycle, alerted_lock,
                market_cache, market_cache_lock,
                on_deal_callback,
            ): watch["name"]
            for watch in config["watchlists"]
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                new_entries, stats = future.result()
                all_new_seen.update(new_entries)
                all_stats.append(stats)
            except Exception as e:
                print(f"[ERROR] watchlist '{name}': {e}")
                all_stats.append(WatchStats(name=name, errors=1))

    seen.update(all_new_seen)
    max_age = config["settings"].get("seen_max_age_days", 60)
    save_seen(seen, max_age_days=max_age)
    save_market_cache(market_cache)

    elapsed = time.time() - t0
    _print_cycle_summary(all_stats, elapsed, len(seen))
    return all_stats


def _print_cycle_summary(stats: List[WatchStats], elapsed: float, seen_count: int) -> None:
    total_alerts = sum(s.alerts for s in stats)
    total_drops = sum(s.price_drops for s in stats)
    total_scored = sum(s.scored for s in stats)
    total_skipped = sum(s.skipped_blacklist + s.skipped_location + s.skipped_price for s in stats)
    total_errors = sum(s.errors for s in stats)
    no_market = [s.name for s in stats if s.market_value is None]
    weak_market = [s.name for s in stats if s.market_value is not None and s.sample_size < 5]
    print()
    print("-" * 70)
    print(f"[SUMMARY] {len(stats)} watchlists em {elapsed:.1f}s | "
          f"alertas={total_alerts} | descidas={total_drops} | scored={total_scored} | "
          f"skipped={total_skipped} | erros={total_errors} | seen.json={seen_count}")
    if no_market:
        print(f"  Sem dados de mercado: {', '.join(no_market[:6])}{' …' if len(no_market) > 6 else ''}")
    if weak_market:
        print(f"  Amostra fraca (<5): {', '.join(weak_market[:6])}{' …' if len(weak_market) > 6 else ''}")
    print("-" * 70)


def main() -> None:
    # On Windows, the default console encoding (cp1252) can't print é/ç/€ —
    # force stdout/stderr to UTF-8 so the bot never crashes on log output.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID em falta no .env")

    with open("config.yml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    interval = int(config["settings"].get("check_interval_minutes", 10)) * 60

    print("OLX Flip Bot v3 iniciado.")
    print(f"A verificar de {interval // 60} em {interval // 60} minutos.")
    print("Carrega CTRL+C para parar.\n")

    # Heartbeat: confirm Telegram works before going into the loop. If this
    # fails the user finds out immediately instead of after the first alert.
    try:
        send_telegram(
            token, chat_id,
            f"OLX Flip Bot iniciado — {len(config['watchlists'])} watchlists, "
            f"check a cada {interval // 60} min.",
        )
        print("[INFO] Heartbeat Telegram enviado.")
    except Exception as e:
        print(f"[WARN] Heartbeat Telegram falhou: {e}")

    while True:
        try:
            run_once(config, token, chat_id)
        except KeyboardInterrupt:
            print("\nBot parado.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")

        print(f"\n--- A aguardar {interval // 60} min ---\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
