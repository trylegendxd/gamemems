"""Sanity check for config.yml — does it load and look structurally correct?"""
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "config.yml")
        with open(path, "r", encoding="utf-8") as f:
            cls.cfg = yaml.safe_load(f)

    def test_has_required_top_level(self):
        for key in ("settings", "watchlists", "global_blacklist", "location_filter"):
            self.assertIn(key, self.cfg)

    def test_settings_values_sensible(self):
        s = self.cfg["settings"]
        self.assertGreaterEqual(s.get("min_margin_percent", 0), 1)
        self.assertGreaterEqual(s.get("min_profit_eur", 0), 1)
        self.assertGreaterEqual(s.get("min_sample_size", 0), 1)

    def test_scraper_settings_present(self):
        scr = self.cfg["settings"].get("scraper", {})
        # The new section is optional but should be present after the upgrade.
        self.assertIsInstance(scr, dict)
        if scr:
            self.assertGreaterEqual(int(scr.get("global_concurrency", 1)), 1)
            self.assertGreaterEqual(int(scr.get("per_host_concurrency", 1)), 1)

    def test_each_watchlist_has_required_fields(self):
        for w in self.cfg["watchlists"]:
            self.assertIn("name", w, f"watch missing 'name': {w}")
            self.assertIn("keywords", w, f"watch '{w.get('name')}' missing keywords")
            self.assertIn("search_urls", w, f"watch '{w.get('name')}' missing search_urls")
            self.assertGreaterEqual(len(w["keywords"]), 1, f"watch '{w['name']}' has zero keywords")
            self.assertGreaterEqual(len(w["search_urls"]), 1, f"watch '{w['name']}' has zero search_urls")
            for u in w["search_urls"]:
                self.assertTrue(u.startswith("http"), f"watch '{w['name']}' bad URL: {u}")


if __name__ == "__main__":
    unittest.main()
