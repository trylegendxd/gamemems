"""
Pricing & comparison utilities.

Pure functions — no HTTP, no global state. Reused by:
  - bot.py / scraper      → per-cycle market estimation.
  - app.py /api/evaluate  → on-demand listing evaluation for the browser
                            extension.

Lazy imports of bot.py text helpers (normalize, find_blacklist, extract_model_key,
title_matches, looks_like_bundle) keep this module free of circular imports.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ──────────────────────────────────────────────────────────────────────────────
# Keywords
# ──────────────────────────────────────────────────────────────────────────────

# Suspicious wording that strongly suggests a damaged / unusable / stolen unit.
# Used to filter both candidate listings AND the market reference pool.
DAMAGE_KEYWORDS: Tuple[str, ...] = (
    # English
    "damaged", "broken", "faulty", "not working", "doesnt work", "doesn't work",
    "for parts", "spare parts", "no warranty",
    # Portuguese
    "avariado", "avariada", "avarias", "avaria",
    "partido", "partida", "quebrado", "quebrada",
    "defeito", "defeituoso", "defeituosa",
    "estragado", "estragada", "danificado", "danificada",
    "para peças", "para pecas", "peças", "pecas",
    "reparar", "reparação", "reparacao", "para reparação",
    "não funciona", "nao funciona", "não liga", "nao liga",
    "sem garantia", "garantia não", "garantia nao",
    # Locked / stolen indicators
    "bloqueado", "bloqueada", "icloud", "conta bloqueada",
    "blacklisted", "imei bloqueado",
    # Battery / hardware concerns
    "bateria inchada", "bateria viciada", "bateria fraca", "swollen battery",
    "sobreaquece", "overheating", "overheat",
)

# Condition vocabulary (Portuguese + English, normalized — no accents).
_CONDITION_NEW = (
    "novo", "nova", "novos", "novas", "selado", "selada",
    "na caixa", "lacrado", "lacrada", "sem uso", "nunca usado", "nunca usada",
    "brand new", "new sealed", "sealed",
)
_CONDITION_LIKE_NEW = (
    "como novo", "como nova", "seminovo", "seminova",
    "praticamente novo", "praticamente nova",
    "quase novo", "quase nova", "estado de novo",
    "like new", "mint", "mint condition", "perfect condition",
)
_CONDITION_USED = (
    "usado", "usada", "usados", "usadas", "second hand", "ja usado", "já usado",
    "com sinais de uso",
)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers (kept tiny + duplicated to avoid bot.py import cycle)
# ──────────────────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def _word_in(needle: str, haystack_norm: str) -> bool:
    if not needle:
        return False
    return re.search(
        r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
        haystack_norm,
    ) is not None


# ──────────────────────────────────────────────────────────────────────────────
# Damage / condition detection
# ──────────────────────────────────────────────────────────────────────────────

def find_damage_keyword(text: str, extra: Iterable[str] = ()) -> Optional[str]:
    """Return the first damage/risk keyword found in text, or None."""
    norm = _normalize(text)
    for kw in tuple(DAMAGE_KEYWORDS) + tuple(extra):
        if _word_in(_normalize(kw), norm):
            return kw
    return None


def detect_condition(text: str) -> str:
    """Return one of: 'new', 'like_new', 'used', 'unknown'.

    Phrase order matters — 'like new' beats 'new' so 'como novo' isn't
    miscategorised.
    """
    norm = _normalize(text)
    for kw in _CONDITION_LIKE_NEW:
        if _word_in(_normalize(kw), norm):
            return "like_new"
    for kw in _CONDITION_NEW:
        if _word_in(_normalize(kw), norm):
            return "new"
    for kw in _CONDITION_USED:
        if _word_in(_normalize(kw), norm):
            return "used"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Outlier trimming
# ──────────────────────────────────────────────────────────────────────────────

def trim_outliers_iqr(prices: List[float], multiplier: float = 1.0) -> List[float]:
    """Tukey IQR trim. Multiplier 1.0 is tighter than the classic 1.5 because
    second-hand pricing has fat tails on both ends (bundles, scams)."""
    prices = sorted(p for p in prices if p and p > 0)
    if len(prices) < 5:
        return prices
    q1, _, q3 = statistics.quantiles(prices, n=4)
    iqr = q3 - q1
    low = max(0.0, q1 - multiplier * iqr)
    high = q3 + multiplier * iqr
    return [p for p in prices if low <= p <= high]


def trim_outliers_mad(prices: List[float], threshold: float = 3.5) -> List[float]:
    """Median Absolute Deviation trim. More robust than IQR on bimodal pools."""
    prices = sorted(p for p in prices if p and p > 0)
    if len(prices) < 5:
        return prices
    med = statistics.median(prices)
    abs_dev = [abs(p - med) for p in prices]
    mad = statistics.median(abs_dev)
    if mad == 0:
        # Fall back to a small fraction of the median when prices are clustered
        mad = max(med * 0.02, 0.5)
    # 0.6745 = scaling factor that makes MAD ≈ stdev for normal distributions
    keep = [p for p in prices if abs(p - med) / (mad / 0.6745) <= threshold]
    return keep or prices


def trim_outliers(prices: List[float], method: str = "iqr", **kwargs) -> List[float]:
    method = (method or "iqr").lower()
    if method == "mad":
        return trim_outliers_mad(prices, threshold=kwargs.get("mad_threshold", 3.5))
    return trim_outliers_iqr(prices, multiplier=kwargs.get("iqr_multiplier", 1.0))


# ──────────────────────────────────────────────────────────────────────────────
# Deduplication
# ──────────────────────────────────────────────────────────────────────────────

def dedupe_by_url(items: Iterable) -> list:
    """URL-keyed dedup (URLs already canonicalised by the scraper)."""
    seen = set()
    out = []
    for it in items:
        url = getattr(it, "url", None) or (it.get("url") if isinstance(it, dict) else None)
        if not url:
            out.append(it)
            continue
        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _signature(title: str, price: Optional[float], price_tolerance_pct: float) -> Tuple[str, int]:
    norm = _normalize(title)
    # Token-set ignores word order / repeated words.
    tokens = sorted(set(norm.split()))
    sig = " ".join(tokens)[:120]
    bucket_size = max(1.0, (price or 0) * (price_tolerance_pct / 100.0))
    bucket = int((price or 0) / bucket_size) if price and bucket_size else 0
    return (sig, bucket)


def dedupe_by_signature(items: Iterable, price_tolerance_pct: float = 3.0) -> list:
    """Dedup by (token-set title, price bucket).

    Catches the common case where the same seller re-posts the same ad with a
    slightly different URL or where two cross-posts of the same listing land
    in our pool (OLX mirroring CustoJusto, etc.).
    """
    seen_sigs = set()
    seen_urls = set()
    out = []
    for it in items:
        url = getattr(it, "url", None) or (it.get("url") if isinstance(it, dict) else "")
        title = getattr(it, "title", None) or (it.get("title") if isinstance(it, dict) else "")
        price = getattr(it, "price", None) if hasattr(it, "price") else (
            it.get("price") if isinstance(it, dict) else None
        )
        url_key = (url or "").split("?")[0]
        if url_key and url_key in seen_urls:
            continue
        sig = _signature(title or "", price, price_tolerance_pct)
        if sig in seen_sigs and sig != ("", 0):
            continue
        seen_sigs.add(sig)
        if url_key:
            seen_urls.add(url_key)
        out.append(it)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Reliability scoring
# ──────────────────────────────────────────────────────────────────────────────

_MATCH_TYPE_WEIGHTS = {"exact": 1.0, "partial": 0.7, "global": 0.4}


def compute_reliability(
    *,
    filtered_sample: int,
    raw_sample: int,
    match_type: str,
    iqr_relative: Optional[float] = None,
    target_sample: int = 15,
) -> float:
    """Reliability ∈ [0, 1].

    Combines:
      - sample size (saturating at `target_sample`)
      - match precision (exact > partial > global)
      - retention rate (how much survived filtering — low retention means the
        market signal is weak relative to noise)
      - IQR width relative to median (penalty for dispersed markets)
    """
    if filtered_sample <= 0 or raw_sample <= 0:
        return 0.0
    size_factor = min(1.0, filtered_sample / float(max(1, target_sample)))
    match_factor = _MATCH_TYPE_WEIGHTS.get(match_type, 0.4)
    # Retention rate: floor at 0.3 because some queries naturally produce a
    # noisy raw pool where most rows are filtered out (mostly bundles).
    retention = max(0.3, filtered_sample / max(1, raw_sample))
    score = 0.5 * size_factor + 0.35 * match_factor + 0.15 * retention
    if iqr_relative is not None and iqr_relative > 0.5:
        # IQR > 50% of median = very dispersed → cap the score.
        score *= max(0.5, 1.0 - (iqr_relative - 0.5))
    return round(min(1.0, max(0.0, score)), 2)


# ──────────────────────────────────────────────────────────────────────────────
# Market stats
# ──────────────────────────────────────────────────────────────────────────────

def build_market_stats(
    prices: List[float],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute median + dispersion stats for a price list.

    Returns:
      {
        "market_value": float | None,
        "sample_size": int,         # raw, before outlier trim
        "filtered_sample_size": int,# after outlier trim
        "min": float | None,
        "max": float | None,
        "iqr_relative": float | None,
      }
    """
    settings = settings or {}
    raw = [p for p in prices if p and p > 0]
    if not raw:
        return {
            "market_value": None, "sample_size": 0, "filtered_sample_size": 0,
            "min": None, "max": None, "iqr_relative": None,
        }
    method = settings.get("outlier_method", "iqr")
    trimmed = trim_outliers(
        raw,
        method=method,
        iqr_multiplier=settings.get("outlier_iqr_multiplier", 1.0),
        mad_threshold=settings.get("outlier_mad_threshold", 3.5),
    )
    if not trimmed:
        return {
            "market_value": None, "sample_size": len(raw), "filtered_sample_size": 0,
            "min": None, "max": None, "iqr_relative": None,
        }
    med = statistics.median(trimmed)
    iqr_rel: Optional[float] = None
    if len(trimmed) >= 5 and med > 0:
        q1, _, q3 = statistics.quantiles(trimmed, n=4)
        iqr_rel = round((q3 - q1) / med, 3)
    return {
        "market_value": round(med, 2),
        "sample_size": len(raw),
        "filtered_sample_size": len(trimmed),
        "min": round(min(trimmed), 2),
        "max": round(max(trimmed), 2),
        "iqr_relative": iqr_rel,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Verdict for /api/evaluate
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_listing(
    *,
    listing: Dict[str, Any],
    market: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score a single listing against pre-computed market stats.

    `listing` shape:  {title, price, url, condition?, category?, location?}
    `market`  shape: output of build_market_stats() plus optional "_match" key
                     for match precision (exact/partial/global).
    """
    settings = settings or {}
    min_margin = float(settings.get("min_margin_percent", 20))
    min_profit = float(settings.get("min_profit_eur", 25))
    min_sample = int(settings.get("min_sample_size", 8))
    min_filtered = int(settings.get("min_filtered_sample_size", 5))
    min_reliability = float(settings.get("min_reliability_score", 0.5))

    title = listing.get("title", "") or ""
    price = listing.get("price")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    reasons: List[str] = []
    market_value = market.get("market_value")
    sample_size = market.get("sample_size", 0)
    filtered_sample = market.get("filtered_sample_size", sample_size)
    match_type = market.get("_match", "global").split(":")[0] if market.get("_match") else "global"
    reliability = compute_reliability(
        filtered_sample=filtered_sample,
        raw_sample=sample_size,
        match_type=match_type,
        iqr_relative=market.get("iqr_relative"),
    )

    # Damage / risk keywords
    damage = find_damage_keyword(title)
    if damage:
        reasons.append(f"Palavra-chave de risco no título: '{damage}'")
    else:
        reasons.append("Sem palavras-chave de dano detectadas")

    # Condition (informational unless explicitly required)
    condition = listing.get("condition") or detect_condition(title)
    if condition and condition != "unknown":
        reasons.append(f"Condição detectada: {condition}")

    # Reliability gate
    if (
        market_value is None
        or sample_size < min_sample
        or filtered_sample < min_filtered
        or reliability < min_reliability
    ):
        if market_value is None:
            reasons.append("Sem dados de mercado para esta categoria")
        else:
            reasons.append(
                f"{filtered_sample} anúncios comparáveis (mín. {min_filtered}, "
                f"reliability {reliability:.2f}/{min_reliability:.2f})"
            )
        return {
            "verdict": "unreliable",
            "listing_price": price,
            "estimated_market_price": market_value,
            "profit_margin_percent": None,
            "sample_size": int(sample_size),
            "filtered_sample_size": int(filtered_sample),
            "reliability_score": reliability,
            "match_type": match_type,
            "reasons": reasons,
            "condition": condition,
        }

    if price is None or price <= 0:
        return {
            "verdict": "unreliable",
            "listing_price": price,
            "estimated_market_price": market_value,
            "profit_margin_percent": None,
            "sample_size": int(sample_size),
            "filtered_sample_size": int(filtered_sample),
            "reliability_score": reliability,
            "match_type": match_type,
            "reasons": reasons + ["Preço do anúncio inválido ou em falta"],
            "condition": condition,
        }

    # Margin = how much the median exceeds the listing price.
    profit = market_value - price
    margin_pct = ((market_value / price) - 1) * 100 if price else 0.0

    if damage:
        verdict = "bad_deal"
        reasons.insert(
            0,
            "Sinalizado como mau negócio devido a palavras-chave de risco no título",
        )
    elif margin_pct >= min_margin and profit >= min_profit:
        verdict = "good_deal"
        reasons.insert(
            0,
            f"Anúncio {round(margin_pct, 1)}% abaixo da mediana ({market_value:.0f}€)",
        )
    elif price > market_value * 1.05:
        verdict = "bad_deal"
        reasons.insert(
            0,
            f"Preço {round(((price/market_value)-1)*100, 1)}% acima da mediana — caro",
        )
    else:
        verdict = "neutral"
        reasons.insert(
            0,
            f"Próximo da mediana ({market_value:.0f}€, margem {round(margin_pct, 1)}%)",
        )

    reasons.append(
        f"{filtered_sample} comparáveis após filtragem (de {sample_size} brutos, "
        f"match {match_type})"
    )

    return {
        "verdict": verdict,
        "listing_price": round(price, 2),
        "estimated_market_price": round(market_value, 2),
        "profit_margin_percent": round(margin_pct, 1),
        "sample_size": int(sample_size),
        "filtered_sample_size": int(filtered_sample),
        "reliability_score": reliability,
        "match_type": match_type,
        "reasons": reasons,
        "condition": condition,
    }
