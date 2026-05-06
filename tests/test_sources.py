"""Tests for the multi-source adapter registry on MarketplaceScraper."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class TestSourceRegistry(unittest.TestCase):
    def test_default_adapters_registered(self):
        labels = [t[0] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS]
        self.assertIn("OLX", labels)
        self.assertIn("CustoJusto", labels)

    def test_predicates_disjoint(self):
        olx_pred = next(t[1] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS if t[0] == "OLX")
        cj_pred = next(t[1] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS if t[0] == "CustoJusto")
        self.assertTrue(olx_pred("https://www.olx.pt/q-rtx-3060/"))
        self.assertFalse(olx_pred("https://www.custojusto.pt/q/rtx"))
        self.assertTrue(cj_pred("https://www.custojusto.pt/q/rtx"))
        self.assertFalse(cj_pred("https://www.olx.pt/q-rtx-3060/"))

    def test_register_source_adds_adapter(self):
        before = len(bot.MarketplaceScraper.SOURCE_ADAPTERS)

        def my_predicate(url):
            return "example.test" in url

        try:
            bot.MarketplaceScraper.register_source(
                "Example", my_predicate, "_scrape_generic_links",
            )
            self.assertEqual(len(bot.MarketplaceScraper.SOURCE_ADAPTERS), before + 1)
            self.assertEqual(bot.MarketplaceScraper.SOURCE_ADAPTERS[-1][0], "Example")
        finally:
            # Roll back the registration so we don't leak state into other tests.
            bot.MarketplaceScraper.SOURCE_ADAPTERS.pop()


class TestBrandTiebreaker(unittest.TestCase):
    def test_brand_breaks_tie(self):
        cfg = {"watchlists": [
            {"name": "RTX 3060", "keywords": ["rtx 3060"], "search_urls": ["x"]},
            {"name": "RTX 3060 Ti", "keywords": ["rtx 3060 ti"], "search_urls": ["x"]},
        ]}
        # No brand → matches RTX 3060 (more specific keywords loses on suffix)
        w = bot.find_best_watch_for_title("RTX 3060 12GB", cfg)
        self.assertEqual(w["name"], "RTX 3060")

    def test_brand_returns_none_when_no_overlap(self):
        cfg = {"watchlists": [
            {"name": "RTX 3060", "keywords": ["rtx 3060"], "search_urls": ["x"]},
        ]}
        self.assertIsNone(bot.find_best_watch_for_title("Bicicleta de montanha", cfg))


if __name__ == "__main__":
    unittest.main()
