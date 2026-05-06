"""
Sanity tests for pricing.py — pure-function module, no network calls.

Run:    python -m unittest tests.test_pricing
Or:     python tests/run_all.py
"""
import os
import sys
import unittest

# Make repo root importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pricing


class TestOutlierTrimming(unittest.TestCase):
    def test_iqr_drops_extremes(self):
        prices = [100, 105, 110, 112, 115, 120, 125, 130, 1500]   # 1500 is outlier
        trimmed = pricing.trim_outliers_iqr(prices, multiplier=1.0)
        self.assertNotIn(1500, trimmed)
        self.assertIn(115, trimmed)

    def test_iqr_passthrough_for_tiny_samples(self):
        # Less than 5 → no trimming (insufficient signal)
        self.assertEqual(
            sorted(pricing.trim_outliers_iqr([100, 100, 100, 1000])),
            [100, 100, 100, 1000],
        )

    def test_mad_handles_clusters(self):
        prices = [100, 100, 100, 100, 100, 100, 100, 1000]
        trimmed = pricing.trim_outliers_mad(prices, threshold=3.5)
        self.assertNotIn(1000, trimmed)


class TestMarketStats(unittest.TestCase):
    def test_median_and_filtered_sample(self):
        prices = [200, 210, 215, 220, 225, 230, 1500]
        stats = pricing.build_market_stats(prices, {"outlier_method": "iqr",
                                                    "outlier_iqr_multiplier": 1.0})
        self.assertEqual(stats["sample_size"], 7)
        self.assertEqual(stats["filtered_sample_size"], 6)
        self.assertAlmostEqual(stats["market_value"], 217.5, places=1)

    def test_empty_input(self):
        stats = pricing.build_market_stats([])
        self.assertIsNone(stats["market_value"])
        self.assertEqual(stats["sample_size"], 0)
        self.assertEqual(stats["filtered_sample_size"], 0)

    def test_low_tail_guard_drops_implausible_floor(self):
        prices = [20, 30, 40, 110, 115, 120, 125, 130, 135, 140]
        stats = pricing.build_market_stats(prices, {"outlier_method": "iqr"})
        self.assertGreaterEqual(stats["min"], 40)
        self.assertEqual(stats["sample_size"], 10)


class TestDamageDetection(unittest.TestCase):
    def test_pt_damage_phrases(self):
        for phrase in [
            "iPhone 13 avariado, vidro partido",
            "Macbook para peças",
            "iPad bloqueado por icloud",
            "RTX 3060 com bateria inchada",
            "PS5 não liga",
        ]:
            self.assertIsNotNone(pricing.find_damage_keyword(phrase),
                                 f"should flag: {phrase}")


    def test_ambiguous_pecas_word_alone_is_not_flagged(self):
        self.assertIsNone(pricing.find_damage_keyword("Vendo peças e acessórios originais"))

    def test_para_pecas_phrase_is_flagged(self):
        self.assertIsNotNone(pricing.find_damage_keyword("Macbook para pecas para reparação"))

    def test_clean_phrase_passes(self):
        self.assertIsNone(pricing.find_damage_keyword("RTX 3060 12GB como nova"))
        self.assertIsNone(pricing.find_damage_keyword("MacBook M2 selado na caixa"))


class TestConditionDetection(unittest.TestCase):
    def test_like_new_beats_new(self):
        self.assertEqual(pricing.detect_condition("Como novo, sem riscos"), "like_new")

    def test_new_match(self):
        self.assertEqual(pricing.detect_condition("Selado nunca usado"), "new")

    def test_used_match(self):
        self.assertEqual(pricing.detect_condition("Usado em bom estado"), "used")

    def test_unknown(self):
        self.assertEqual(pricing.detect_condition("RTX 3060 12GB"), "unknown")


class TestReliability(unittest.TestCase):
    def test_zero_sample(self):
        self.assertEqual(
            pricing.compute_reliability(filtered_sample=0, raw_sample=0, match_type="global"),
            0.0,
        )

    def test_grows_with_sample(self):
        small = pricing.compute_reliability(filtered_sample=2, raw_sample=2, match_type="exact")
        big = pricing.compute_reliability(filtered_sample=20, raw_sample=25, match_type="exact")
        self.assertLess(small, big)

    def test_match_type_gradient(self):
        ex = pricing.compute_reliability(filtered_sample=10, raw_sample=10, match_type="exact")
        pt = pricing.compute_reliability(filtered_sample=10, raw_sample=10, match_type="partial")
        gl = pricing.compute_reliability(filtered_sample=10, raw_sample=10, match_type="global")
        self.assertGreater(ex, pt)
        self.assertGreater(pt, gl)


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        # Tight market (sample 12, narrow IQR), good for "good_deal" detection.
        prices = [200, 205, 208, 210, 212, 215, 220, 222, 224, 228, 232, 240]
        self.market = pricing.build_market_stats(prices, {})
        self.market["_match"] = "exact:rtx 3060"

    def test_good_deal_below_median(self):
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060 como nova", "price": 140, "url": "x"},
            market=self.market,
            settings={"min_margin_percent": 20, "min_profit_eur": 25,
                      "min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5},
        )
        self.assertEqual(result["verdict"], "good_deal")
        self.assertGreater(result["profit_margin_percent"], 20)

    def test_neutral_near_median(self):
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060", "price": 215, "url": "x"},
            market=self.market,
            settings={"min_margin_percent": 20, "min_profit_eur": 25,
                      "min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5},
        )
        self.assertIn(result["verdict"], ("neutral", "good_deal"))

    def test_bad_deal_overpriced(self):
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060", "price": 350, "url": "x"},
            market=self.market,
            settings={"min_margin_percent": 20, "min_profit_eur": 25,
                      "min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5},
        )
        self.assertEqual(result["verdict"], "bad_deal")

    def test_unreliable_when_sample_too_small(self):
        # Build a market with fewer than min_sample_size entries
        thin = pricing.build_market_stats([200, 205, 210], {})
        thin["_match"] = "exact:foo"
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060", "price": 140, "url": "x"},
            market=thin,
            settings={"min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5},
        )
        self.assertEqual(result["verdict"], "unreliable")

    def test_damage_keyword_flags_bad_deal(self):
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060 avariada para peças", "price": 50, "url": "x"},
            market=self.market,
            settings={"min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5,
                      "min_margin_percent": 20, "min_profit_eur": 25},
        )
        self.assertEqual(result["verdict"], "bad_deal")
        self.assertTrue(any("risco" in r.lower() for r in result["reasons"]))


class TestSourceDiversity(unittest.TestCase):
    def test_source_counts_in_stats(self):
        prices = [200, 205, 210, 215, 220, 230, 240]
        sources = ["OLX", "OLX", "OLX", "CustoJusto", "CustoJusto", "OLX", "OLX"]
        stats = pricing.build_market_stats(prices, {}, sources=sources)
        self.assertEqual(stats["source_counts"], {"OLX": 5, "CustoJusto": 2})
        self.assertEqual(stats["source_diversity"], 2)

    def test_source_diversity_boosts_reliability(self):
        single = pricing.compute_reliability(
            filtered_sample=10, raw_sample=12, match_type="exact",
            source_diversity=1,
        )
        diverse = pricing.compute_reliability(
            filtered_sample=10, raw_sample=12, match_type="exact",
            source_diversity=2,
        )
        very_diverse = pricing.compute_reliability(
            filtered_sample=10, raw_sample=12, match_type="exact",
            source_diversity=3,
        )
        self.assertGreater(diverse, single)
        self.assertGreaterEqual(very_diverse, diverse)
        self.assertLessEqual(very_diverse, 1.0)

    def test_evaluate_includes_source_breakdown(self):
        prices = [200, 205, 208, 210, 212, 215, 220, 222, 224, 228, 232, 240]
        sources = ["OLX"] * 8 + ["CustoJusto"] * 4
        market = pricing.build_market_stats(prices, {}, sources=sources)
        market["_match"] = "exact:rtx 3060"
        result = pricing.evaluate_listing(
            listing={"title": "RTX 3060", "price": 140, "url": "x"},
            market=market,
            settings={"min_sample_size": 8, "min_filtered_sample_size": 5,
                      "min_reliability_score": 0.5,
                      "min_margin_percent": 20, "min_profit_eur": 25},
        )
        self.assertIn("source_counts", result)
        self.assertIn("source_diversity", result)
        self.assertEqual(result["source_diversity"], 2)
        self.assertEqual(sum(result["source_counts"].values()), 12)


class TestDedupeBySignature(unittest.TestCase):
    def test_dedupe_by_url(self):
        items = [
            {"url": "https://www.olx.pt/d/anuncio/abc.html", "title": "RTX 3060", "price": 200},
            {"url": "https://www.olx.pt/d/anuncio/abc.html", "title": "RTX 3060", "price": 200},
            {"url": "https://www.olx.pt/d/anuncio/xyz.html", "title": "RTX 3060", "price": 199},
        ]
        out = pricing.dedupe_by_signature(items)
        # First two collapse via URL, third one would also collapse via signature
        self.assertEqual(len(out), 1)

    def test_dedupe_signature_with_different_urls_same_listing(self):
        items = [
            {"url": "https://a.com/1", "title": "RTX 3060 12GB", "price": 200},
            {"url": "https://a.com/2", "title": "12gb RTX 3060", "price": 201},  # near-dup
            {"url": "https://a.com/3", "title": "RTX 3060", "price": 600},      # very diff price
        ]
        out = pricing.dedupe_by_signature(items, price_tolerance_pct=3.0)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
