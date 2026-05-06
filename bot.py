import logging
import os
import random
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
from typing import Callable, List, Dict, Optional, Iterable, Tuple
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import pricing


DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
MARKET_CACHE_FILE = DATA_DIR / "market_cache.json"

log = logging.getLogger("bot")

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
    condition: str = "unknown"   # new | like_new | used | unknown


@dataclass
class WatchStats:
    name: str
    market_value: Optional[float] = None
    sample_size: int = 0
    filtered_sample_size: int = 0
    reliability_score: Optional[float] = None
    listings_seen: int = 0
    skipped_blacklist: int = 0
    skipped_location: int = 0
    skipped_price: int = 0
    skipped_unreliable: int = 0
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

# Known storage tiers (in GB). Anything outside this set is rejected as a
# false positive — "8gb" in "ram ddr4 8gb" would otherwise pollute phone
# fingerprints. Storage detection is intentionally conservative; we only
# stamp a value into the model key when we're confident it's the device's
# storage tier (i.e., a known phone/tablet capacity).
_PHONE_STORAGE_TIERS = (64, 128, 256, 512, 1024, 2048)
# 1-4 digits so "1tb" / "2tb" still match. The tier whitelist below filters
# out junk (e.g. "8gb ram" wouldn't be a phone storage tier).
_PHONE_STORAGE_RE = re.compile(r"\b(\d{1,4})\s*(gb|tb)\b")
# VRAM tiers actually shipped on consumer GPUs.
_GPU_VRAM_TIERS = (4, 6, 8, 10, 12, 16, 20, 24, 32)
_GPU_VRAM_RE = re.compile(r"\b(\d{1,2})\s*gb\b")


def _extract_phone_storage(t: str) -> Optional[str]:
    """Return canonical storage label like "128gb" / "1tb" if a known phone
    storage tier appears in the title; else None.

    Captures only the first valid match; titles with multiple sizes (e.g.
    "iphone 13 256gb e 512gb") are ambiguous so we'd rather not fingerprint.
    """
    found = []
    for m in _PHONE_STORAGE_RE.finditer(t):
        n = int(m.group(1))
        unit = m.group(2)
        gb = n if unit == "gb" else n * 1024
        if gb in _PHONE_STORAGE_TIERS:
            found.append(gb)
    if not found:
        return None
    # If a single tier or the smallest is matched (handles cases where a
    # listing mentions both the phone's storage and an unrelated number)
    chosen = found[0]
    if chosen >= 1024:
        return f"{chosen // 1024}tb"
    return f"{chosen}gb"


def _extract_gpu_vram(t: str) -> Optional[str]:
    """Pick the GPU VRAM (4/6/8/10/12/16/20/24/32 GB) when present in title."""
    for m in _GPU_VRAM_RE.finditer(t):
        n = int(m.group(1))
        if n in _GPU_VRAM_TIERS:
            return f"{n}gb"
    return None

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
    storage = _extract_phone_storage(t)
    storage_suffix = f" {storage}" if storage else ""

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
        return key + storage_suffix
    # Base iPad with generation number
    m = re.search(r"ipad\s+(\d{1,2})(?:\s*(?:th|st|nd|rd|a|o)?\s*(?:gen|geracao|generation))?", t)
    if m:
        return f"ipad gen{m.group(1)}" + storage_suffix
    # Just "ipad" with year
    m = re.search(r"ipad\s+(20\d\d)", t)
    if m:
        return f"ipad {m.group(1)}" + storage_suffix
    return "ipad" + storage_suffix  # generic fallback — still better than nothing


def _extract_iphone_model(t: str) -> Optional[str]:
    """
    "iphone 13 pro max 256gb" -> "iphone 13 pro max 256gb"
    "iphone 15 128gb"          -> "iphone 15 128gb"
    "iphone 15"                -> "iphone 15"   (no storage detected)
    """
    m = re.search(r"iphone\s+(\d{1,2})\s*(pro\s*max|pro\s*plus|pro|plus|mini)?", t)
    if not m:
        return None
    number  = m.group(1)
    variant = (m.group(2) or "").strip()
    key = f"iphone {number}"
    if variant:
        key += f" {variant}"
    storage = _extract_phone_storage(t)
    if storage:
        key += f" {storage}"
    return key


def _extract_samsung_model(t: str) -> Optional[str]:
    """
    "samsung galaxy s22 ultra 256gb" -> "samsung s22 ultra 256gb"
    "samsung a54 128gb"               -> "samsung a54 128gb"
    """
    m = re.search(r"(?:samsung\s+)?(?:galaxy\s+)?(s\d{1,2}|a\d{2}|m\d{2}|z\s*fold\s*\d?|z\s*flip\s*\d?)\s*(ultra|plus|\+|fe)?", t)
    if not m:
        return None
    model   = m.group(1).replace(" ", "")
    variant = (m.group(2) or "").replace("+", "plus").strip()
    key = f"samsung {model}"
    if variant:
        key += f" {variant}"
    storage = _extract_phone_storage(t)
    if storage:
        key += f" {storage}"
    return key


def _extract_macbook_model(t: str) -> Optional[str]:
    """
    "macbook air m2 13 256gb" -> "macbook air m2 13 256gb"
    "macbook pro 14 m3 1tb"   -> "macbook pro m3 14 1tb"
    """
    # Constrain size to actual MacBook screen sizes (13/14/15/16). The
    # broader `\d{2}` was previously grabbing "20" from "MacBook Air 2020"
    # and producing keys like "macbook air 20".
    m = re.search(r"macbook\s+(air|pro)\s*(13|14|15|16)?\s*(m\d)?", t)
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
    # If the regex didn't capture a size between variant/chip (common for
    # listings ordered "macbook air m2 13 256gb"), look for one after the
    # chip too — sizes are 13/14/15/16 inches.
    if not size and chip:
        m2 = re.search(r"\bmacbook\s+(?:air|pro)\s+m\d\s+(13|14|15|16)\b", t)
        if m2:
            size = m2.group(1)
            key += f" {size}"
    storage = _extract_phone_storage(t)
    if storage:
        key += f" {storage}"
    return key


def _extract_gpu_model(t: str) -> Optional[str]:
    """
    "rtx 3060 ti 12gb msi" -> "rtx 3060 ti 12gb"
    "rtx 3060 8gb"          -> "rtx 3060 8gb"   (8GB and 12GB are different SKUs!)
    "rx 6700 xt"            -> "rx 6700 xt"
    "gtx 1080 ti"           -> "gtx 1080 ti"
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
    vram = _extract_gpu_vram(t)
    if vram:
        key += f" {vram}"
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


# Keywords that look like GPU model identifiers — only these get the
# variant-suffix rejection treatment. Applying it to every keyword causes
# false positives for short tokens like "m1" / "i5" colliding with the
# Portuguese marketing word "super" ("M1 super condição").
_GPU_KEYWORD_RE = re.compile(r"\b(rtx|gtx|rx|radeon)\s*\d{3,4}\b")


def title_matches(title: str, keywords: List[str]) -> bool:
    """
    Returns True only if ALL keywords appear in the title (whole-word match).

    Variant-suffix rejection: when a keyword looks like a GPU model
    (e.g. "rtx 3060", "rx 6800"), the title is rejected if the keyword is
    immediately followed by a SKU-changing suffix (`ti`, `super`, `xt`,
    `xtx`, `gre`, `max-q`). This prevents a "RTX 3060" watchlist from
    matching "RTX 3060 Ti" listings.

    For non-GPU keywords this rejection does NOT apply, because suffixes
    like "super" are common adjectives in Portuguese listings ("M1 super
    estado", "i5 super preço") and would silently swallow legitimate
    matches.
    """
    t = normalize(title)
    for k in keywords:
        nk_norm = normalize(k)
        nk = re.escape(nk_norm)
        if not re.search(r"(?<![a-z0-9])" + nk + r"(?![a-z0-9])", t):
            return False
        if _GPU_KEYWORD_RE.search(nk_norm):
            if re.search(
                r"(?<![a-z0-9])" + nk + r"\s*(?:ti|super|xt|xtx|gre|max-q)(?![a-z0-9])",
                t,
            ):
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
    """
    HTTP fetcher with controlled concurrency, per-host rate limiting,
    request jitter, retries with exponential backoff, and soft-ban detection.

    The class is the single integration point for all OLX / CustoJusto fetches
    so all rate-limiting policy lives here. Two semaphores cap parallelism
    (global + per-host); a per-host minimum interval and randomized jitter
    spread requests in time; exponential backoff with jitter handles 429/403
    and 5xx; persistent 429/403 trips a soft-ban pause for that host.
    """

    DEFAULT_OPTS = {
        "global_concurrency": 4,
        "per_host_concurrency": 2,
        "min_host_interval_seconds": 0.5,
        "jitter_min_seconds": 0.5,
        "jitter_max_seconds": 2.5,
        "request_timeout_seconds": 20,
        "retry_max_attempts": 3,
        "retry_backoff_base_seconds": 1.5,
        "pause_on_softban_seconds": 90,
        "softban_consecutive_threshold": 3,
    }

    def __init__(
        self,
        user_agent: str,
        delay_seconds: Optional[float] = None,   # legacy compat — used as min_host_interval
        **opts,
    ):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        # Resolve options: explicit kwargs > legacy `delay_seconds` > defaults
        cfg = dict(self.DEFAULT_OPTS)
        cfg.update({k: v for k, v in opts.items() if v is not None})
        if delay_seconds is not None and "min_host_interval_seconds" not in opts:
            cfg["min_host_interval_seconds"] = float(delay_seconds)
        self.global_concurrency = int(cfg["global_concurrency"])
        self.per_host_concurrency = int(cfg["per_host_concurrency"])
        self.min_host_interval = float(cfg["min_host_interval_seconds"])
        self.jitter_min = float(cfg["jitter_min_seconds"])
        self.jitter_max = float(cfg["jitter_max_seconds"])
        self.timeout = float(cfg["request_timeout_seconds"])
        self.retry_max_attempts = max(1, int(cfg["retry_max_attempts"]))
        self.retry_backoff_base = float(cfg["retry_backoff_base_seconds"])
        self.pause_on_softban_seconds = float(cfg["pause_on_softban_seconds"])
        self.softban_consecutive_threshold = int(cfg["softban_consecutive_threshold"])
        # Legacy attribute used elsewhere in the module.
        self.delay_seconds = self.min_host_interval

        # Concurrency primitives
        self._global_sem = threading.BoundedSemaphore(self.global_concurrency)
        self._host_sems: Dict[str, threading.BoundedSemaphore] = {}
        self._host_sems_lock = threading.Lock()
        self._host_last_request: Dict[str, float] = {}
        self._host_last_lock = threading.Lock()
        self._softban_state: Dict[str, Dict[str, float]] = {}
        self._softban_lock = threading.Lock()

        # Per-URL response cache + in-flight coalescing
        self._cache_lock = threading.Lock()
        self._cache: Dict[str, List[Listing]] = {}
        self._inflight: Dict[str, threading.Event] = {}

    @classmethod
    def from_config(cls, config: Dict) -> "MarketplaceScraper":
        s = config.get("settings", {}) or {}
        scraper_cfg = s.get("scraper", {}) or {}
        # Environment overrides win over config (deploy-friendly).
        def _env(name: str, default):
            v = os.getenv(name)
            return v if v is not None else default
        return cls(
            user_agent=s.get("user_agent", "Mozilla/5.0"),
            delay_seconds=s.get("request_delay_seconds"),
            global_concurrency=int(_env("SCRAPER_GLOBAL_CONCURRENCY",
                                        scraper_cfg.get("global_concurrency", 4))),
            per_host_concurrency=int(_env("SCRAPER_PER_HOST_CONCURRENCY",
                                          scraper_cfg.get("per_host_concurrency", 2))),
            min_host_interval_seconds=float(_env("SCRAPER_MIN_HOST_INTERVAL",
                                                 scraper_cfg.get("min_host_interval_seconds",
                                                                 s.get("request_delay_seconds", 0.8)))),
            jitter_min_seconds=float(scraper_cfg.get("jitter_min_seconds", 0.5)),
            jitter_max_seconds=float(scraper_cfg.get("jitter_max_seconds", 2.5)),
            request_timeout_seconds=float(scraper_cfg.get("request_timeout_seconds", 20)),
            retry_max_attempts=int(scraper_cfg.get("retry_max_attempts", 3)),
            retry_backoff_base_seconds=float(scraper_cfg.get("retry_backoff_base_seconds", 1.5)),
            pause_on_softban_seconds=float(scraper_cfg.get("pause_on_softban_seconds", 90)),
            softban_consecutive_threshold=int(scraper_cfg.get("softban_consecutive_threshold", 3)),
        )

    # ── concurrency primitives ─────────────────────────────────────────────

    def _get_host_sem(self, host: str) -> threading.BoundedSemaphore:
        with self._host_sems_lock:
            sem = self._host_sems.get(host)
            if sem is None:
                sem = threading.BoundedSemaphore(self.per_host_concurrency)
                self._host_sems[host] = sem
            return sem

    def _respect_min_interval(self, host: str) -> None:
        with self._host_last_lock:
            last = self._host_last_request.get(host, 0.0)
        gap = time.time() - last
        if gap < self.min_host_interval:
            time.sleep(self.min_host_interval - gap)

    def _update_last_request(self, host: str) -> None:
        with self._host_last_lock:
            self._host_last_request[host] = time.time()

    def _record_softban(self, host: str, status_code: int) -> None:
        with self._softban_lock:
            st = self._softban_state.setdefault(host, {"consecutive": 0, "until": 0.0})
            st["consecutive"] = float(st.get("consecutive", 0)) + 1
            if int(st["consecutive"]) >= self.softban_consecutive_threshold:
                st["until"] = time.time() + self.pause_on_softban_seconds
                log.error(
                    "[SOFTBAN] host=%s status=%d consecutive=%d → pausing for %ds",
                    host, status_code, int(st["consecutive"]),
                    int(self.pause_on_softban_seconds),
                )

    def _reset_softban(self, host: str) -> None:
        with self._softban_lock:
            st = self._softban_state.get(host)
            if st is not None:
                st["consecutive"] = 0
                st["until"] = 0.0

    def _wait_out_softban(self, host: str) -> None:
        with self._softban_lock:
            st = self._softban_state.get(host) or {}
            until = float(st.get("until", 0.0))
        wait = until - time.time()
        if wait > 0:
            log.warning("[SOFTBAN] %s still active — sleeping %.1fs", host, wait)
            time.sleep(wait)

    def _backoff_delay(self, attempt: int) -> float:
        return self.retry_backoff_base * (2 ** attempt) + random.uniform(0, 0.75)

    # ── single-URL fetch with retries & ban handling ───────────────────────

    def fetch_html(self, url: str) -> str:
        host = urlparse(url).netloc
        with self._global_sem:
            host_sem = self._get_host_sem(host)
            with host_sem:
                self._wait_out_softban(host)
                self._respect_min_interval(host)
                if self.jitter_max > 0:
                    time.sleep(random.uniform(self.jitter_min, self.jitter_max))

                last_error: Optional[str] = None
                for attempt in range(self.retry_max_attempts):
                    try:
                        r = self.session.get(url, timeout=self.timeout)
                    except requests.RequestException as e:
                        last_error = f"{type(e).__name__}: {e}"
                        delay = self._backoff_delay(attempt)
                        log.warning(
                            "[FETCH] %s attempt=%d/%d FAIL (%s) — retry in %.1fs",
                            url, attempt + 1, self.retry_max_attempts, last_error, delay,
                        )
                        time.sleep(delay)
                        continue
                    self._update_last_request(host)

                    # Soft-ban indicators
                    if r.status_code in (403, 429):
                        self._record_softban(host, r.status_code)
                        delay = self._backoff_delay(attempt)
                        log.warning(
                            "[FETCH] %s -> %d (%s) — backing off %.1fs",
                            url, r.status_code,
                            r.headers.get("Retry-After", "no-retry-after"),
                            delay,
                        )
                        # Honor Retry-After if longer than our backoff
                        try:
                            ra = float(r.headers.get("Retry-After", "0"))
                        except (TypeError, ValueError):
                            ra = 0.0
                        time.sleep(max(delay, ra))
                        continue

                    # Transient server errors
                    if r.status_code >= 500:
                        delay = self._backoff_delay(attempt)
                        log.warning("[FETCH] %s -> %d, retry in %.1fs", url, r.status_code, delay)
                        time.sleep(delay)
                        continue

                    # 404 → category-rename fallback (kept from previous behavior)
                    if r.status_code == 404:
                        for fallback in self._fallback_urls(url):
                            try:
                                fr = self.session.get(fallback, timeout=self.timeout)
                            except requests.RequestException:
                                continue
                            self._update_last_request(host)
                            if fr.ok:
                                log.info("[FETCH] 404 %s → fallback %s OK", url, fallback)
                                self._reset_softban(host)
                                return fr.text
                        r.raise_for_status()

                    r.raise_for_status()
                    self._reset_softban(host)
                    return r.text

                raise RuntimeError(
                    f"giving up on {url} after {self.retry_max_attempts} attempts ({last_error})"
                )

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

    # How many search-result pages to walk per URL before giving up. Default is
    # generous because OLX queries for popular products (iPhone, RTX 3060)
    # routinely span >100 listings, and stopping at page 1 silently dropped
    # them. A page that returns zero new cards short-circuits the loop, so
    # niche queries don't waste requests.
    OLX_MAX_PAGES = int(os.getenv("OLX_MAX_PAGES", "5"))

    def _olx_page_url(self, url: str, page: int) -> str:
        """Append ?page=N to an OLX search URL, preserving any existing query."""
        if page <= 1:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}"

    def scrape_olx(self, url: str) -> List["Listing"]:
        listings: List[Listing] = []
        seen_urls = set()
        prev_count = -1
        for page in range(1, self.OLX_MAX_PAGES + 1):
            page_url = self._olx_page_url(url, page)
            try:
                html = self.fetch_html(page_url)
            except Exception as e:
                if page == 1:
                    raise
                log.info("[OLX-PAGE] %s page=%d stopping (%s)", url, page, e)
                break
            page_listings = self._parse_olx_page(html)
            new_on_page = 0
            for item in page_listings:
                key = item.url.split("?")[0]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                listings.append(item)
                new_on_page += 1
            log.info("[OLX-PAGE] %s page=%d cards=%d new=%d total=%d",
                     url, page, len(page_listings), new_on_page, len(listings))
            # Stop early when:
            #   - the page returned nothing (last page reached or rate-limited),
            #   - the page returned fewer cards than the previous page (often
            #     a sign that we've walked off the end onto an "explore"-style
            #     suggestion page),
            #   - all returned cards were duplicates (shouldn't happen but is
            #     defensive against OLX cyclic pagination).
            if new_on_page == 0:
                break
            if prev_count >= 0 and len(page_listings) < prev_count // 2:
                break
            prev_count = len(page_listings)
        return dedupe_listings(listings)

    def _parse_olx_page(self, html: str) -> List["Listing"]:
        soup = BeautifulSoup(html, "html.parser")
        listings: List[Listing] = []

        # OLX wraps each ad in a <div data-cy="l-card"> or similar
        cards = soup.select("[data-cy='l-card']")
        if not cards:
            # fallback: any <li> with a link and price
            cards = soup.select("li:has(a[href])")

        for card in cards:
            # Prefer the actual ad link (matches /d/anuncio/) over the first
            # <a> in the card, which is sometimes a breadcrumb / favourite
            # button. Falling back keeps the previous behaviour for cards
            # that haven't migrated to the new URL scheme.
            a = (
                card.select_one("a[href*='/d/anuncio/']")
                or card.select_one("a[href*='/anuncio/']")
                or card.find("a", href=True)
            )
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
            # 3-char floor (was 5) — was dropping legit titles like "PS5".
            if len(title) < 3:
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
            # Image extraction is fiddly: OLX lazy-loads ad thumbnails so
            # `src` is usually a 1x1 base64 placeholder. The real URL lives
            # in `data-src` or in `srcset` (multiple resolutions; pick the
            # largest). Reject data: URIs entirely so they never end up on
            # the dashboard.
            image_url = ""
            img_el = card.select_one("img")
            if img_el:
                # Try `srcset` first — picks the highest-res candidate.
                srcset = img_el.get("srcset") or img_el.get("data-srcset") or ""
                if srcset:
                    candidates = []
                    for chunk in srcset.split(","):
                        parts = chunk.strip().split()
                        if not parts:
                            continue
                        u = parts[0]
                        # Width descriptor like "640w", else 0.
                        w = 0
                        if len(parts) > 1 and parts[1].endswith("w"):
                            try:
                                w = int(parts[1][:-1])
                            except ValueError:
                                w = 0
                        if u and not u.startswith("data:"):
                            candidates.append((w, u))
                    if candidates:
                        candidates.sort()
                        image_url = candidates[-1][1]

                # Fallback chain: data-src (lazy-load real URL) → src (only
                # if not a data: URI).
                if not image_url:
                    for attr in ("data-src", "data-original", "src"):
                        v = img_el.get(attr) or ""
                        if v and not v.startswith("data:"):
                            image_url = v
                            break

                if image_url and not image_url.startswith("http"):
                    image_url = urljoin("https://www.olx.pt", image_url)

            condition = pricing.detect_condition(f"{title} {raw_text}")

            listings.append(Listing(
                title=title[:160],
                price=price,
                url=full_url.split("?")[0],
                source="OLX",
                location=location,
                raw_text=raw_text,
                image_url=image_url,
                condition=condition,
            ))

        # Dedup happens in the outer scrape_olx() across pages; this method
        # returns raw listings so per-page diagnostics stay accurate.
        return listings

    # ── CustoJusto parser ─────────────────────────────────────────────────────
    # CustoJusto is a Next.js app — listings are embedded as JSON inside
    # <script id="__NEXT_DATA__">. CSS-based scraping returns garbage because
    # the visible DOM is hydrated client-side.

    CJ_MAX_PAGES = int(os.getenv("CJ_MAX_PAGES", "3"))

    def _custojusto_page_url(self, url: str, page: int) -> str:
        """CustoJusto paginates with `?o=N` (offset/page index)."""
        if page <= 1:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}o={page}"

    def scrape_custojusto(self, url: str) -> List["Listing"]:
        listings: List[Listing] = []
        seen_urls = set()
        prev_count = -1
        for page in range(1, self.CJ_MAX_PAGES + 1):
            page_url = self._custojusto_page_url(url, page)
            try:
                html = self.fetch_html(page_url)
            except Exception as e:
                if page == 1:
                    raise
                log.info("[CJ-PAGE] %s page=%d stopping (%s)", url, page, e)
                break
            page_listings = self._parse_custojusto_page(page_url, html)
            new_on_page = 0
            for item in page_listings:
                key = item.url.split("?")[0]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                listings.append(item)
                new_on_page += 1
            log.info("[CJ-PAGE] %s page=%d items=%d new=%d total=%d",
                     url, page, len(page_listings), new_on_page, len(listings))
            if new_on_page == 0:
                break
            if prev_count >= 0 and len(page_listings) < prev_count // 2:
                break
            prev_count = len(page_listings)
        return dedupe_listings(listings)

    def _parse_custojusto_page(self, url: str, html: str) -> List["Listing"]:
        listings: List[Listing] = []
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, flags=re.DOTALL,
        )
        if not m:
            soup = BeautifulSoup(html, "html.parser")
            return self._scrape_generic_links(url, soup, "CustoJusto")

        try:
            data = json.loads(m.group(1))
            items = data["props"]["pageProps"]["listItems"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

        for it in items:
            title = clean_text(it.get("title", ""))
            if len(title) < 3:
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
            if image_url and image_url.startswith("data:"):
                image_url = ""

            condition = pricing.detect_condition(f"{title} {body}")

            listings.append(Listing(
                title=title[:160],
                price=price,
                url=full_url.split("?")[0],
                source="CustoJusto",
                description=body[:2000],
                image_url=image_url,
                location=location,
                raw_text=raw_text,
                condition=condition,
            ))

        return listings

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

    # ── Pluggable source registry ─────────────────────────────────────────
    # Each adapter is a (label, predicate, parser) triple.
    # `predicate(url)` returns True if the adapter can handle this URL.
    # `parser(self, url)` returns a List[Listing] tagged with that label as
    # `source`.
    #
    # Add a third-party marketplace by registering another tuple in
    # SOURCE_ADAPTERS — no other code needs to change.
    SOURCE_ADAPTERS: List[Tuple[str, "Callable[[str], bool]", str]] = []

    @classmethod
    def register_source(cls, label: str, predicate, parser_method_name: str):
        """Register a marketplace adapter at class-import time."""
        cls.SOURCE_ADAPTERS.append((label, predicate, parser_method_name))

    def _dispatch_source(self, url: str) -> List["Listing"]:
        for label, predicate, parser_name in self.SOURCE_ADAPTERS:
            try:
                if predicate(url):
                    parser = getattr(self, parser_name)
                    items = parser(url)
                    # Stamp the source label so multi-source aggregation can
                    # bucket results by adapter (used by reliability scoring).
                    for it in items:
                        if not it.source or it.source == "Marketplace":
                            it.source = label
                    return items
            except Exception as e:
                log.warning("[ADAPTER] %s failed on %s: %s", label, url, e)
                raise
        # Generic fallback — useful for one-off reference URLs the user adds.
        html = self.fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        return self._scrape_generic_links(url, soup, "Generic")

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
            result = self._dispatch_source(url)
            return result
        finally:
            with self._cache_lock:
                self._cache[url] = result
                self._inflight.pop(url, None)
                event.set()


# Register the built-in marketplace adapters.
# Order matters: more specific predicates first.
MarketplaceScraper.register_source(
    "OLX",        lambda u: "olx.pt" in u,        "scrape_olx",
)
MarketplaceScraper.register_source(
    "CustoJusto", lambda u: "custojusto.pt" in u, "scrape_custojusto",
)
# To add another marketplace, drop a `scrape_<name>(self, url)` method on
# MarketplaceScraper and call MarketplaceScraper.register_source(...) here.


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def dedupe_listings(items: Iterable[Listing]) -> List[Listing]:
    """Deduplicate by canonical URL first, then by (title token-set, price bucket)
    so cross-posted ads and slightly-edited reposts collapse into one entry."""
    return pricing.dedupe_by_signature(items, price_tolerance_pct=3.0)


def remove_outliers(prices: List[float], iqr_multiplier: float = 1.0) -> List[float]:
    """Backwards-compatible wrapper around pricing.trim_outliers_iqr."""
    return pricing.trim_outliers_iqr(prices, multiplier=iqr_multiplier)


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

def _build_market_stats(prices: List[float], iqr_mult: float = 1.0,
                        method: str = "iqr", mad_threshold: float = 3.5) -> Dict:
    """Compute median + dispersion for a price list (delegates to pricing)."""
    return pricing.build_market_stats(
        prices,
        {
            "outlier_method": method,
            "outlier_iqr_multiplier": iqr_mult,
            "outlier_mad_threshold": mad_threshold,
        },
    )


def _watchlist_describes_bundle(keywords: List[str]) -> bool:
    """A watchlist whose keywords span ≥2 bundle categories (e.g.
    ["i7", "rtx"]) is intentionally tracking a multi-component product like
    a gaming laptop or pre-built PC. The bundle filter would otherwise
    discard every relevant listing because the listing legitimately mentions
    both a CPU and a GPU.
    """
    blob = normalize(" ".join(keywords))
    hits = sum(1 for pat in _BUNDLE_CATEGORIES.values() if pat.search(blob))
    return hits >= 2


def estimate_market_value(
    scraper: MarketplaceScraper,
    reference_urls: List[str],
    search_urls: List[str],
    keywords: List[str],
    blacklist: List[str],
    config: Optional[Dict] = None,
    allow_bundles: bool = False,
) -> Dict:
    """
    Returns a market dict with two levels:
      - "by_model": {model_key -> stats_dict}
      - "global":   stats_dict
      - "raw_count": total references seen
      - "filter_dropped": dict of how many were dropped at each stage

    Reference data is filtered four ways for accuracy:
      1. keyword match (must contain the watched terms)
      2. user blacklist (config + watch-level)
      3. damage keyword filter (built-in DAMAGE_KEYWORDS)
      4. bundle detection (no full PCs in a GPU watchlist, etc.)
    Then outlier trimming (IQR or MAD, configurable) is applied.
    """
    cfg_settings = (config or {}).get("settings", {})
    iqr_mult = cfg_settings.get("outlier_iqr_multiplier", 1.0)
    method = cfg_settings.get("outlier_method", "iqr")
    mad_threshold = cfg_settings.get("outlier_mad_threshold", 3.5)
    exclude_bundles = cfg_settings.get("exclude_bundle_listings", True)
    apply_damage_filter = cfg_settings.get("filter_damaged_market_refs", True)

    # If the watchlist itself describes a multi-component product (e.g. a
    # gaming laptop matched on ["i7", "rtx"]) the bundle filter would
    # discard every legit listing. Auto-detect and bypass.
    if exclude_bundles and not allow_bundles and _watchlist_describes_bundle(keywords):
        log.info("[MARKET] watchlist looks multi-component (keywords=%s) — "
                 "disabling bundle filter for this run", keywords)
        exclude_bundles = False

    unique_refs = [u for u in reference_urls if u not in search_urls]
    all_ref_urls = search_urls + unique_refs

    refs: List[Listing] = []
    for url in all_ref_urls:
        try:
            refs.extend(scraper.scrape_url(url))
        except Exception as e:
            log.warning("[REF] scrape failed %s: %s", url, e)

    # Run all filters; track drop reasons for observability.
    dropped = {"no_price": 0, "keyword": 0, "blacklist": 0, "damage": 0, "bundle": 0}
    filtered: List[Listing] = []
    for item in refs:
        if item.price is None:
            dropped["no_price"] += 1
            continue
        if not title_matches(item.title, keywords):
            dropped["keyword"] += 1
            continue
        combined_text = " ".join([item.title, item.raw_text])
        if find_blacklist(combined_text, blacklist):
            dropped["blacklist"] += 1
            continue
        if apply_damage_filter and pricing.find_damage_keyword(combined_text):
            dropped["damage"] += 1
            continue
        if exclude_bundles and looks_like_bundle(item.title):
            dropped["bundle"] += 1
            continue
        filtered.append(item)

    # Group by model fingerprint for tighter comparisons.
    by_model: Dict[str, List[float]] = {}
    by_model_sources: Dict[str, List[str]] = {}
    global_prices: List[float] = []
    global_sources: List[str] = []
    for item in filtered:
        key = extract_model_key(item.title)
        global_prices.append(item.price)
        global_sources.append(item.source or "Unknown")
        if key:
            by_model.setdefault(key, []).append(item.price)
            by_model_sources.setdefault(key, []).append(item.source or "Unknown")

    stats_opts = {
        "outlier_method": method,
        "outlier_iqr_multiplier": iqr_mult,
        "outlier_mad_threshold": mad_threshold,
    }
    model_stats = {
        k: pricing.build_market_stats(by_model[k], stats_opts,
                                      sources=by_model_sources.get(k))
        for k in by_model
    }
    global_stats = pricing.build_market_stats(global_prices, stats_opts,
                                              sources=global_sources)

    if model_stats:
        sample_info = ", ".join(
            f"{k}({v['filtered_sample_size']}/{v['sample_size']})"
            for k, v in sorted(model_stats.items())
        )
        log.info("[MARKET-MODELS] %s", sample_info)
    drops_summary = ", ".join(f"{k}={v}" for k, v in dropped.items() if v) or "none"
    log.info("[MARKET-FILTER] refs=%d filtered=%d dropped(%s)",
             len(refs), len(filtered), drops_summary)
    if not filtered and refs:
        log.warning("[MARKET] %d refs but ALL filtered out — relax keywords/blacklist?", len(refs))
    elif not refs:
        log.warning("[MARKET] 0 refs — check reference URLs")

    return {
        "by_model": model_stats,
        "global": global_stats,
        "raw_count": len(refs),
        "filter_dropped": dropped,
    }


def _model_key_drop_last_token(key: str) -> Optional[str]:
    """Drop the last token of a model key — used to walk up the precision
    ladder (e.g. "iphone 15 pro 256gb" → "iphone 15 pro" → "iphone 15")."""
    parts = key.split()
    if len(parts) <= 1:
        return None
    return " ".join(parts[:-1])


# A bucket is "trusted enough" to keep when it carries this many filtered
# comparables. Below it we walk up the precision ladder to find a wider
# bucket. Lower than the global `min_filtered_sample_size` so we still
# *prefer* a tight bucket — we just refuse to live with a 1- or 2-listing
# match when a less specific bucket has many more.
_MIN_BUCKET_SIZE_FOR_EXACT = 4


def get_market_for_listing(market: Dict, listing: "Listing") -> Dict:
    """
    Pick the tightest applicable market stats for this specific listing.

    Precision ladder:
        1. exact key (e.g. "iphone 15 pro 256gb")
        2. parent keys, dropping one trailing token at a time
           ("iphone 15 pro 256gb" → "iphone 15 pro" → "iphone 15")
        3. partial substring match against any other model bucket
        4. global pool

    Each step skips buckets that don't meet `_MIN_BUCKET_SIZE_FOR_EXACT`
    so a 1-listing exact match doesn't beat a 30-listing parent. The
    `_match` label records what we ended up using ("exact:..." /
    "partial:..." / "global") so the reliability score and Telegram alert
    can describe the precision.
    """
    by_model = market.get("by_model", {})
    model_key = extract_model_key(listing.title)
    if model_key:
        # Climb the precision ladder.
        candidate = model_key
        while candidate:
            stats = by_model.get(candidate)
            if (stats and stats.get("market_value") is not None
                    and stats.get("filtered_sample_size", 0) >= _MIN_BUCKET_SIZE_FOR_EXACT):
                label = "exact" if candidate == model_key else "partial"
                return {**stats, "_match": f"{label}:{candidate}"}
            parent = _model_key_drop_last_token(candidate)
            if parent == candidate:
                break
            candidate = parent

        # As a last resort, accept a thinner exact-key bucket so we don't
        # silently fall to global when *some* exact data exists.
        stats = by_model.get(model_key)
        if stats and stats.get("market_value") is not None:
            return {**stats, "_match": f"exact:{model_key}"}

        # Substring partial match against any other bucket
        for k, stats in by_model.items():
            if (model_key in k or k in model_key) and stats.get("market_value") is not None:
                return {**stats, "_match": f"partial:{k}"}

    # Fallback to global
    return {**market.get("global", {}), "_match": "global"}


# ──────────────────────────────────────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────────────────────────────────────

def score_listing(listing: Listing, market_stats: Dict, cfg: Dict) -> Dict:
    """Score a listing against pre-computed market stats.

    Returns a dict containing the legacy keys (`alert`, `profit`, `margin_percent`,
    `market_value`, `market_sample_size`, `match_type`, `reason`) plus the new
    keys (`filtered_sample_size`, `reliability_score`, `reasons`, `verdict`,
    `condition`) used by the dashboard / browser extension.
    """
    s = cfg.get("settings", {}) or {}
    min_margin = s.get("min_margin_percent", 20)
    min_profit = s.get("min_profit_eur", 25)
    min_sample = s.get("min_sample_size", 8)
    min_filtered = s.get("min_filtered_sample_size", max(5, int(min_sample * 0.6)))
    min_reliability = s.get("min_reliability_score", 0.5)
    min_match_pref = s.get("min_match_type_for_alert", "partial")  # exact|partial|global

    match_type_raw = market_stats.get("_match", "global")
    match_type = match_type_raw.split(":")[0]

    market_value = market_stats.get("market_value")
    sample_size = market_stats.get("sample_size", 0)
    filtered_sample = market_stats.get("filtered_sample_size", sample_size)
    iqr_rel = market_stats.get("iqr_relative")
    source_counts = market_stats.get("source_counts", {}) or {}
    source_diversity = market_stats.get("source_diversity") or len(source_counts) or 1
    reliability = pricing.compute_reliability(
        filtered_sample=filtered_sample,
        raw_sample=sample_size,
        match_type=match_type,
        iqr_relative=iqr_rel,
        source_diversity=source_diversity,
    )

    # Default skeleton populated regardless of branch
    base = {
        "alert": False,
        "profit": None,
        "margin_percent": None,
        "market_value": market_value,
        "market_sample_size": int(sample_size),
        "filtered_sample_size": int(filtered_sample),
        "reliability_score": reliability,
        "match_type": match_type,
        "verdict": "unreliable",
        "condition": getattr(listing, "condition", "unknown"),
        "reasons": [],
        "reason": "",
    }

    if listing.price is None:
        base["reasons"].append("Anúncio sem preço")
        base["reason"] = base["reasons"][0]
        return base

    if market_value is None:
        base["reasons"].append("Sem referência de mercado")
        base["reason"] = base["reasons"][0]
        return base

    profit = market_value - listing.price
    margin_percent = ((market_value / listing.price) - 1) * 100
    base["profit"] = round(profit, 2)
    base["margin_percent"] = round(margin_percent, 1)

    # Reliability gate — never alert if the market sample is too thin or noisy.
    match_rank = {"global": 0, "partial": 1, "exact": 2}
    pref_rank = match_rank.get(min_match_pref, 1)
    has_match_precision = match_rank.get(match_type, 0) >= pref_rank

    if (
        sample_size < min_sample
        or filtered_sample < min_filtered
        or reliability < min_reliability
        or not has_match_precision
    ):
        base["reasons"].append(
            f"Amostra insuficiente ou pouco fiável "
            f"(filtered={filtered_sample}/{sample_size}, reliability={reliability:.2f}, "
            f"match={match_type})"
        )
        base["reason"] = base["reasons"][0]
        return base

    # Damage / risk-keyword check on the candidate (we already filtered the
    # market pool; a damage keyword on the candidate flips it to bad_deal).
    text_for_damage = " ".join([listing.title, listing.raw_text])
    damage_word = pricing.find_damage_keyword(text_for_damage)
    if damage_word:
        base["reasons"].append(
            f"Sinalizado: palavra-chave de risco '{damage_word}'"
        )
        base["verdict"] = "bad_deal"
        base["reason"] = base["reasons"][0]
        return base

    if margin_percent >= min_margin and profit >= min_profit:
        base["alert"] = True
        base["verdict"] = "good_deal"
    elif listing.price > market_value * 1.05:
        base["verdict"] = "bad_deal"
    else:
        base["verdict"] = "neutral"

    base["reasons"] = [
        f"Preço {listing.price:.0f}€ vs mediana {market_value:.0f}€ "
        f"(margem {margin_percent:.1f}%)",
        f"{filtered_sample} comparáveis após filtragem (de {sample_size} brutos, match {match_type})",
        f"Fiabilidade {reliability:.2f}",
    ]
    if iqr_rel is not None:
        base["reasons"].append(f"Dispersão IQR/mediana = {iqr_rel:.2f}")
    base["reason"] = base["reasons"][0]
    return base


# ──────────────────────────────────────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, message: str,
                  *, max_attempts: int = 3, backoff_base: float = 1.5) -> None:
    """POST to Telegram's sendMessage with bounded retry on transient failures.

    Telegram returns 429 with a `retry_after` field when we send too fast;
    transient networking failures and 5xx are also retried. 4xx (other than
    429) are surfaced immediately because retrying won't help.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last_err = "unknown"
    for attempt in range(max_attempts):
        try:
            r = requests.post(url, json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": False,
            }, timeout=20)
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            sleep_for = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("[TG] network error attempt=%d/%d: %s — retry in %.1fs",
                        attempt + 1, max_attempts, last_err, sleep_for)
            time.sleep(sleep_for)
            continue

        if r.ok:
            return

        # Try to surface Telegram's structured description.
        try:
            data = r.json()
            desc = data.get("description", r.text)
            retry_after = (data.get("parameters") or {}).get("retry_after")
        except Exception:
            desc = r.text
            retry_after = None

        # 429 → respect the retry_after header when present.
        if r.status_code == 429:
            wait = float(retry_after) if retry_after else backoff_base * (2 ** attempt)
            log.warning("[TG] 429 rate limit; retrying in %.1fs", wait)
            time.sleep(wait)
            last_err = f"429: {desc}"
            continue

        # Other 5xx are retried; non-429 4xx surfaces immediately.
        if 500 <= r.status_code < 600:
            sleep_for = backoff_base * (2 ** attempt)
            log.warning("[TG] %d on attempt=%d/%d: %s — retry in %.1fs",
                        r.status_code, attempt + 1, max_attempts, desc, sleep_for)
            time.sleep(sleep_for)
            last_err = f"{r.status_code}: {desc}"
            continue

        raise RuntimeError(f"Telegram {r.status_code}: {desc}")

    raise RuntimeError(f"Telegram failed after {max_attempts} attempts: {last_err}")


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
    filtered = score.get("filtered_sample_size", score.get("market_sample_size", "?"))
    raw_sample = score.get("market_sample_size", "?")
    reliability = score.get("reliability_score")
    rel_str = f"{reliability:.2f}" if isinstance(reliability, (int, float)) else "?"
    cond = score.get("condition", "unknown")
    cond_line = f"Condição detectada: {cond}\n" if cond and cond != "unknown" else ""
    return (
        f"{header}"
        f"Categoria: {watch_name}\n"
        f"Artigo: {listing.title}\n"
        f"{model_line}"
        f"{cond_line}"
        f"Preço do vendedor: {listing.price:.0f}€\n"
        f"Mediana de mercado: {score['market_value']:.0f}€  ({compare_note})\n"
        f"Lucro estimado: {score['profit']:.0f}€\n"
        f"Margem: {score['margin_percent']}%\n"
        f"Amostra: {filtered}/{raw_sample} comparáveis | Fiabilidade: {rel_str}\n"
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

    log.info("[WATCH-START] %s", watch["name"])

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
        log.info("[MARKET-CACHED] %s reused (TTL %d min)", watch["name"], ttl)
    else:
        market = estimate_market_value(
            scraper,
            watch.get("reference_urls", []),
            watch.get("search_urls", []),
            watch["keywords"],
            blacklist,
            config=config,
            allow_bundles=bool(watch.get("allow_bundles", False)),
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
    stats.filtered_sample_size = global_stats.get("filtered_sample_size", 0)
    g_reliability = pricing.compute_reliability(
        filtered_sample=stats.filtered_sample_size,
        raw_sample=stats.sample_size,
        match_type="global",
        iqr_relative=global_stats.get("iqr_relative"),
        source_diversity=global_stats.get("source_diversity") or 1,
    )
    stats.reliability_score = g_reliability
    g_val_str = f"{stats.market_value}€" if stats.market_value is not None else "—"
    log.info(
        "[WATCH] %s market=%s filtered=%d/%d reliability=%.2f range=[%s,%s]",
        watch["name"], g_val_str, stats.filtered_sample_size, stats.sample_size,
        g_reliability, global_stats.get("min"), global_stats.get("max"),
    )

    for search_url in watch["search_urls"]:
        try:
            listings = scraper.scrape_url(search_url)
        except Exception as e:
            stats.errors += 1
            log.warning("[SCRAPE-FAIL] %s url=%s err=%s", watch["name"], search_url, e)
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
                    log.info("[PRICE-DROP] %s %s€ → %s€: %s",
                             watch["name"], prev_price, listing.price, listing.title[:50])
                else:
                    continue

            text_for_filter = " ".join([
                listing.title, listing.raw_text, listing.location, listing.url,
            ])

            # 1) User-supplied blacklist (per-watch + global)
            bad_word = find_blacklist(text_for_filter, blacklist)
            # 2) Built-in damage/risk vocabulary (configurable: bypass when the
            #    watchlist explicitly opts in via `allow_damaged: true`).
            damage_word = None
            if not watch.get("allow_damaged", False):
                damage_word = pricing.find_damage_keyword(text_for_filter)
            if bad_word or damage_word:
                hit = bad_word or damage_word
                stats.skipped_blacklist += 1
                log.info("[SKIP-BL] %s '%s': %s", watch["name"], hit, listing.title[:70])
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
                    log.info("[SKIP-LOC] %s out-of-area loc=%s | %s",
                             watch["name"], listing.location, listing.title[:70])
                    new_seen[key] = {"first": _now_iso(), "price": listing.price}
                    continue
                log.info("[WARN-LOC] %s no location detected, including: %s",
                         watch["name"], listing.title[:70])

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
            verdict = score.get("verdict", "?")
            if verdict == "unreliable":
                stats.skipped_unreliable += 1
            log.info(
                "[SCORE] %s price=%s margin=%s%% verdict=%s reliability=%s filtered=%s/%s match=%s model=%s | %s",
                watch["name"],
                listing.price,
                score.get("margin_percent", "?"),
                verdict,
                score.get("reliability_score", "?"),
                score.get("filtered_sample_size", "?"),
                score.get("market_sample_size", "?"),
                score.get("match_type", "?"),
                model_key,
                listing.title[:60],
            )

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
                        # Compose a multi-line reason from the structured list
                        reasons_list = score.get("reasons") or [score.get("reason") or ""]
                        reason_blob = " | ".join(r for r in reasons_list if r)
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
                            "reason": reason_blob,
                            "risk_flags": risk_words,
                            "telegram_sent": False,
                        }
                        deal_dashboard_url = on_deal_callback(deal_payload)
                    except Exception as e:
                        log.warning("[DB-SAVE-FAIL] %s: %s", watch["name"], e)

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
                    log.info("[ALERT-OK] %s telegram sent | %s",
                             watch["name"], listing.title[:60])

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
                    log.warning("[ALERT-FAIL] %s telegram failed: %s", watch["name"], e)

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
    scraper = MarketplaceScraper.from_config(config)

    allowed_locations = config.get("location_filter", {}).get("allowed_locations", [])
    require_location = config["settings"].get("require_location_match", True)

    # Per-watchlist worker count. Defaults to 3 (legacy) but bumps to the
    # global concurrency if configured higher; the underlying global semaphore
    # in MarketplaceScraper is the real cap.
    s = config.get("settings", {}) or {}
    max_workers = int(
        os.getenv("WATCH_WORKER_COUNT")
        or (s.get("scraper", {}) or {}).get("watch_worker_count")
        or min(3, len(config["watchlists"]))
    )
    max_workers = max(1, min(max_workers, len(config["watchlists"])))
    log.info(
        "[CYCLE] watchlists=%d worker_pool=%d global_concurrency=%d per_host=%d",
        len(config["watchlists"]), max_workers,
        scraper.global_concurrency, scraper.per_host_concurrency,
    )
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
                log.exception("[WATCH-ERROR] '%s': %s", name, e)
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
    total_skipped_bl = sum(s.skipped_blacklist for s in stats)
    total_skipped_loc = sum(s.skipped_location for s in stats)
    total_skipped_price = sum(s.skipped_price for s in stats)
    total_skipped_unrel = sum(s.skipped_unreliable for s in stats)
    total_errors = sum(s.errors for s in stats)
    no_market = [s.name for s in stats if s.market_value is None]
    weak_market = [s.name for s in stats
                   if s.market_value is not None and s.filtered_sample_size < 5]
    log.info("=" * 70)
    log.info(
        "[SUMMARY] watchlists=%d in %.1fs | alerts=%d drops=%d scored=%d "
        "skip(bl=%d loc=%d price=%d unreliable=%d) errors=%d seen=%d",
        len(stats), elapsed, total_alerts, total_drops, total_scored,
        total_skipped_bl, total_skipped_loc, total_skipped_price, total_skipped_unrel,
        total_errors, seen_count,
    )
    if no_market:
        log.warning("[SUMMARY] no market data: %s%s",
                    ", ".join(no_market[:6]),
                    " …" if len(no_market) > 6 else "")
    if weak_market:
        log.warning("[SUMMARY] weak filtered sample (<5): %s%s",
                    ", ".join(weak_market[:6]),
                    " …" if len(weak_market) > 6 else "")
    log.info("=" * 70)


# ──────────────────────────────────────────────────────────────────────────────
# /api/evaluate helper — match an extension-supplied listing to the most
# relevant watchlist's cached market data, then score it.
# ──────────────────────────────────────────────────────────────────────────────

def _watch_match_score(title_norm: str, watch: Dict) -> int:
    """Heuristic: how well does this title match a watchlist?

    Score 100 if all keywords are word-bounded matches in the title; otherwise
    a partial overlap count. 0 means no match.
    """
    keywords = [normalize(k) for k in watch.get("keywords", [])]
    if not keywords:
        return 0
    full = title_matches(title_norm, watch["keywords"])
    if full:
        return 100 + sum(len(k) for k in keywords)  # ties broken by total length
    hits = sum(1 for k in keywords if _word_boundary_search(k, title_norm))
    return hits * 5


def find_best_watch_for_title(title: str, config: Dict, brand: Optional[str] = None) -> Optional[Dict]:
    """Return the watchlist that best matches `title`, or None if no overlap.

    `brand` (optional) is used as a tiebreaker — when two watchlists score
    the same on title alone, the one whose name/keywords mention the brand
    wins.
    """
    norm = normalize(title)
    brand_norm = normalize(brand) if brand else None
    best, best_score = None, 0
    for w in config.get("watchlists", []):
        score = _watch_match_score(norm, w)
        if brand_norm and score > 0:
            haystack = normalize(w.get("name", "") + " " + " ".join(w.get("keywords", [])))
            if brand_norm in haystack:
                score += 3   # small bump
        if score > best_score:
            best, best_score = w, score
    return best if best_score > 0 else None


def evaluate_listing_via_api(
    payload: Dict,
    config: Dict,
    market_cache: Optional[Dict] = None,
    *,
    allow_live_fetch: bool = False,
    scraper: Optional[MarketplaceScraper] = None,
) -> Dict:
    """
    Server-side helper for the /api/evaluate endpoint.

    Picks the best-matching watchlist, looks up its cached market data, and
    runs `pricing.evaluate_listing`. Falls back to a live scrape only if
    `allow_live_fetch=True` and a scraper is supplied — by default we want
    the API to be cheap and never hammer OLX from request handlers.
    """
    market_cache = market_cache if market_cache is not None else load_market_cache()
    title = (payload.get("title") or "").strip()
    if not title:
        return {
            "verdict": "unreliable",
            "listing_price": payload.get("price"),
            "estimated_market_price": None,
            "profit_margin_percent": None,
            "sample_size": 0,
            "filtered_sample_size": 0,
            "reliability_score": 0.0,
            "match_type": "global",
            "reasons": ["title em falta"],
            "condition": "unknown",
        }

    watch = find_best_watch_for_title(title, config, brand=payload.get("brand"))
    if watch is None:
        return {
            "verdict": "unreliable",
            "listing_price": payload.get("price"),
            "estimated_market_price": None,
            "profit_margin_percent": None,
            "sample_size": 0,
            "filtered_sample_size": 0,
            "reliability_score": 0.0,
            "match_type": "global",
            "reasons": ["Não foi possível associar a um watchlist conhecido"],
            "condition": pricing.detect_condition(title),
        }

    cache_key = market_cache_key(watch)
    ttl = config.get("settings", {}).get("market_cache_ttl_minutes", 60)
    market = get_cached_market(market_cache, cache_key, ttl)

    if market is None and allow_live_fetch and scraper is not None:
        log.info("[API-EVAL] cache miss for %s — performing live fetch", watch["name"])
        market = estimate_market_value(
            scraper,
            watch.get("reference_urls", []),
            watch.get("search_urls", []),
            watch["keywords"],
            combined_blacklist(config, watch),
            config=config,
            allow_bundles=bool(watch.get("allow_bundles", False)),
        )
        market_cache[cache_key] = {
            "market": market,
            "computed_at": _now_iso(),
            "watch_name": watch["name"],
        }
        save_market_cache(market_cache)

    if market is None:
        return {
            "verdict": "unreliable",
            "listing_price": payload.get("price"),
            "estimated_market_price": None,
            "profit_margin_percent": None,
            "sample_size": 0,
            "filtered_sample_size": 0,
            "reliability_score": 0.0,
            "match_type": "global",
            "reasons": [
                f"Sem dados de mercado em cache para '{watch['name']}'. "
                "Aguarde o próximo ciclo do scraper.",
            ],
            "condition": pricing.detect_condition(title),
            "watch_name": watch["name"],
        }

    # Mock a Listing-shaped object so get_market_for_listing can pick the best slice
    fake = Listing(
        title=title,
        price=payload.get("price"),
        url=payload.get("url", ""),
        source="extension",
        location=payload.get("location") or "",
        raw_text=title,
    )
    market_stats = get_market_for_listing(market, fake)

    result = pricing.evaluate_listing(
        listing={
            "title": title,
            "price": payload.get("price"),
            "url": payload.get("url"),
            "condition": payload.get("condition") or "unknown",
            "category": payload.get("category"),
            "location": payload.get("location"),
        },
        market=market_stats,
        settings=config.get("settings", {}),
    )
    result["watch_name"] = watch["name"]
    return result


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
