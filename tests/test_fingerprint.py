"""Tests for model fingerprinting + title_matches false-positive fix +
hierarchical market lookup."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import pricing


class TestStorageInModelKey(unittest.TestCase):
    def test_iphone_keeps_storage(self):
        self.assertEqual(
            bot.extract_model_key("iPhone 16 Pro 128GB azul titânio"),
            "iphone 16 pro 128gb",
        )
        self.assertEqual(
            bot.extract_model_key("iPhone 15 Pro Max 512GB"),
            "iphone 15 pro max 512gb",
        )

    def test_iphone_separates_storage_tiers(self):
        a = bot.extract_model_key("iPhone 16 Pro 128GB")
        b = bot.extract_model_key("iPhone 16 Pro 256GB")
        c = bot.extract_model_key("iPhone 16 Pro 512GB")
        self.assertNotEqual(a, b)
        self.assertNotEqual(b, c)

    def test_iphone_no_storage_in_title(self):
        # Falls back to plain model key when storage isn't visible.
        self.assertEqual(
            bot.extract_model_key("iPhone 15 Pro como nova"),
            "iphone 15 pro",
        )

    def test_samsung_storage(self):
        self.assertEqual(
            bot.extract_model_key("Samsung Galaxy S24 Ultra 512GB titânio"),
            "samsung s24 ultra 512gb",
        )

    def test_macbook_storage(self):
        # Storage detected from "256gb"
        self.assertEqual(
            bot.extract_model_key("MacBook Air M2 13 256GB cinzento"),
            "macbook air m2 13 256gb",
        )
        # 1TB recognised
        self.assertEqual(
            bot.extract_model_key("MacBook Pro M3 14 1TB"),
            "macbook pro m3 14 1tb",
        )

    def test_ipad_storage(self):
        self.assertEqual(
            bot.extract_model_key("iPad Pro 11 M4 256GB"),
            "ipad pro 11 m4 256gb",
        )

    def test_gpu_vram(self):
        # 8GB and 12GB RTX 3060 are different SKUs.
        a = bot.extract_model_key("RTX 3060 8GB MSI Ventus")
        b = bot.extract_model_key("RTX 3060 12GB Asus")
        self.assertEqual(a, "rtx 3060 8gb")
        self.assertEqual(b, "rtx 3060 12gb")
        self.assertNotEqual(a, b)

    def test_storage_only_known_tiers(self):
        # "32gb ram" should NOT pollute a phone fingerprint with "32gb"
        # because 32 is not a known phone storage tier.
        key = bot.extract_model_key("iPhone 15 32GB ram")
        self.assertEqual(key, "iphone 15")  # 32 isn't in _PHONE_STORAGE_TIERS


class TestTitleMatchesVariantFix(unittest.TestCase):
    def test_super_after_short_keyword_no_longer_rejects(self):
        # The bug: keyword "m1" + listing "MacBook Air M1 super estado"
        # was rejected because of the GPU "super" suffix rule applying
        # to non-GPU keywords.
        self.assertTrue(
            bot.title_matches("MacBook Air M1 super estado", ["macbook air", "m1"])
        )

    def test_super_after_i5_keyword_no_longer_rejects(self):
        self.assertTrue(
            bot.title_matches("PC Gaming i5 super, SSD 256GB", ["i5", "ssd"])
        )

    def test_gpu_variant_rejection_still_works(self):
        # The actual GPU case that the variant rule was designed for.
        self.assertFalse(bot.title_matches("RTX 3060 Ti 8GB", ["rtx 3060"]))
        self.assertFalse(bot.title_matches("RX 6800 XT", ["rx 6800"]))
        self.assertTrue(bot.title_matches("RTX 3060 8GB MSI", ["rtx 3060"]))


class TestHierarchicalMarketLookup(unittest.TestCase):
    def setUp(self):
        # Build a market dict where the storage-specific bucket is thin
        # (1 sample) but the parent bucket without storage is rich (10).
        thin = pricing.build_market_stats([950], {})
        rich = pricing.build_market_stats(
            [800, 820, 830, 850, 870, 880, 900, 920, 950, 980], {},
        )
        self.market = {
            "by_model": {
                "iphone 15 pro 1tb": thin,
                "iphone 15 pro": rich,
            },
            "global": pricing.build_market_stats(
                [400, 500, 600, 700, 800, 900, 1000, 1100, 1200], {},
            ),
        }

    def test_thin_specific_falls_back_to_rich_parent(self):
        # iPhone 15 Pro 1TB has only 1 sample → should walk up to
        # "iphone 15 pro" (10 samples).
        listing = bot.Listing(
            title="iPhone 15 Pro 1TB",
            price=850, url="x", source="OLX",
        )
        stats = bot.get_market_for_listing(self.market, listing)
        self.assertTrue(stats["_match"].startswith("partial:"))
        self.assertEqual(stats["filtered_sample_size"], 10)

    def test_thick_exact_used_when_available(self):
        listing = bot.Listing(
            title="iPhone 15 Pro 256GB",   # not in by_model → walks up
            price=850, url="x", source="OLX",
        )
        stats = bot.get_market_for_listing(self.market, listing)
        # Falls back to "iphone 15 pro" parent which has 10 samples.
        self.assertEqual(stats["filtered_sample_size"], 10)


if __name__ == "__main__":
    unittest.main()
