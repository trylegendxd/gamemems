"""Sanity tests for the Flask /api/evaluate endpoint."""
import os
import sys
import unittest

# Make repo root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Run with a SQLite DB (the default fallback) and an extension token so the
# endpoint isn't disabled. These env vars must be set BEFORE importing app.
os.environ.setdefault("DASHBOARD_PASSWORD", "test-password")
os.environ.setdefault("EXTENSION_API_TOKEN", "test-token-xyz")
os.environ.setdefault("EXTENSION_ALLOWED_ORIGIN", "http://localhost:9999")
os.environ.setdefault("RUN_SCRAPER", "false")

# Avoid sharing the dev seen.json — point at a temp file.
import tempfile
_tmpdir = tempfile.mkdtemp(prefix="olxbot-test-")
os.environ["SQLITE_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["DATABASE_URL"] = ""

# Pre-seed the market cache with synthetic data so /api/evaluate has something
# to evaluate against. This avoids any HTTP calls during the test.
import json
import bot
import pricing
import yaml

with open(os.path.join(os.path.dirname(__file__), "..", "config.yml"),
          encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
_target_watch = next(w for w in _cfg["watchlists"] if w["name"] == "RTX 3060")
_prices = [200, 205, 208, 210, 212, 215, 220, 222, 224, 228, 232, 240]
_sources = ["OLX"] * 8 + ["CustoJusto"] * 4
_market_data = {
    "global": pricing.build_market_stats(_prices, {}, sources=_sources),
    "by_model": {
        "rtx 3060": pricing.build_market_stats(_prices, {}, sources=_sources),
    },
    "raw_count": 12,
    "filter_dropped": {"keyword": 0, "blacklist": 0, "damage": 0, "bundle": 0, "no_price": 0},
}
_cache_key = bot.market_cache_key(_target_watch)
bot.MARKET_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
bot.MARKET_CACHE_FILE.write_text(
    json.dumps({_cache_key: {"market": _market_data,
                             "computed_at": "2999-01-01T00:00:00Z",
                             "watch_name": _target_watch["name"]}}),
    encoding="utf-8",
)

import app as app_module


class TestApiEvaluate(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_missing_token_returns_401(self):
        r = self.client.post("/api/evaluate", json={"title": "x", "url": "x"})
        self.assertEqual(r.status_code, 401)

    def test_invalid_token_returns_401(self):
        r = self.client.post(
            "/api/evaluate",
            json={"title": "RTX 3060", "url": "https://www.olx.pt/d/anuncio/x.html", "price": 140},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(r.status_code, 401)

    def test_missing_fields_returns_400(self):
        r = self.client.post(
            "/api/evaluate",
            json={"title": "RTX 3060"},  # missing url
            headers={"Authorization": "Bearer test-token-xyz"},
        )
        self.assertEqual(r.status_code, 400)

    def test_valid_request_returns_verdict(self):
        r = self.client.post(
            "/api/evaluate",
            json={
                "title": "RTX 3060 12GB como nova",
                "url": "https://www.olx.pt/d/anuncio/test.html",
                "price": 140,
                "condition": "like_new",
                "location": "Braga",
                "brand": "nvidia",
            },
            headers={"Authorization": "Bearer test-token-xyz"},
        )
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertIn("verdict", data)
        self.assertIn(data["verdict"], ("good_deal", "neutral", "bad_deal", "unreliable"))
        self.assertIn("reliability_score", data)
        self.assertIn("filtered_sample_size", data)
        self.assertIsInstance(data["reasons"], list)
        # New source-breakdown fields
        self.assertIn("source_counts", data)
        self.assertIn("source_diversity", data)
        self.assertEqual(data["source_diversity"], 2)

    def test_overpriced_marked_bad(self):
        r = self.client.post(
            "/api/evaluate",
            json={
                "title": "RTX 3060",
                "url": "https://www.olx.pt/d/anuncio/test2.html",
                "price": 350,
            },
            headers={"Authorization": "Bearer test-token-xyz"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.get_json()["verdict"], ("bad_deal", "unreliable"))

    def test_cors_preflight(self):
        r = self.client.open(
            "/api/evaluate", method="OPTIONS",
            headers={"Origin": "http://localhost:9999"},
        )
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "http://localhost:9999")


if __name__ == "__main__":
    unittest.main()
