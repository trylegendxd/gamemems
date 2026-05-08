"""
tests/test_vinted_fb_adapters.py
─────────────────────────────────
Testes para os novos adaptadores Vinted e Facebook Marketplace.

Cobre:
  1. Vinted
     1a. URL parsing (catalog → API URL)
     1b. Item parser com payload realista da API
     1c. End-to-end com fetch mockado (multi-page + dedupe)
     1d. Filtros: items sem preço, moeda errada, título curto
  2. Facebook
     2a. Sem cookies → silently skip
     2b. Com cookies → injecta no session
     2c. Parse do HTML (regex de título+preço)
     2d. Detecta redirect para login

Network-free. Não corre nenhuma chamada HTTP real.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_scraper():
    """Cria uma instância de MarketplaceScraper sem fazer requests reais."""
    import bot
    return bot.MarketplaceScraper(
        user_agent="test-agent/1.0",
        global_concurrency=1,
        per_host_concurrency=1,
        min_host_interval_seconds=0,
        jitter_min_seconds=0,
        jitter_max_seconds=0,
        request_timeout_seconds=5,
        retry_max_attempts=1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Vinted
# ─────────────────────────────────────────────────────────────────────────────

class TestVintedUrlParsing(unittest.TestCase):

    def setUp(self):
        self.scraper = _make_scraper()

    def test_catalog_url_converts_to_api(self):
        url = "https://www.vinted.pt/catalog?search_text=iphone+15"
        api = self.scraper._vinted_to_api_url(url)
        self.assertIsNotNone(api)
        self.assertIn("/api/v2/catalog/items", api)
        self.assertIn("search_text=iphone+15", api)
        self.assertIn("per_page=", api)

    def test_api_url_passes_through(self):
        url = "https://www.vinted.pt/api/v2/catalog/items?search_text=ps5&per_page=20"
        result = self.scraper._vinted_to_api_url(url)
        self.assertEqual(result, url)

    def test_non_vinted_url_returns_none(self):
        self.assertIsNone(self.scraper._vinted_to_api_url("https://www.olx.pt/q-rtx/"))

    def test_vinted_url_without_catalog_returns_none(self):
        self.assertIsNone(self.scraper._vinted_to_api_url("https://www.vinted.pt/help"))

    def test_page_url_appends_page_param(self):
        api = "https://www.vinted.pt/api/v2/catalog/items?search_text=x&per_page=20"
        self.assertEqual(self.scraper._vinted_page_url(api, 1), api)
        p2 = self.scraper._vinted_page_url(api, 2)
        self.assertIn("page=2", p2)
        self.assertIn("search_text=x", p2)


class TestVintedItemParsing(unittest.TestCase):

    def setUp(self):
        self.scraper = _make_scraper()

    def _sample_item(self, **overrides):
        item = {
            "id": 12345,
            "title": "iPhone 15 Pro 256GB",
            "price": {"amount": "650.00", "currency_code": "EUR"},
            "url": "https://www.vinted.pt/items/12345-iphone-15-pro",
            "photo": {
                "url": "https://images.vinted.net/abc/thumb.jpg",
                "full_size_url": "https://images.vinted.net/abc/full.jpg",
            },
            "brand_title": "Apple",
            "size_title": "256GB",
            "user": {"id": 1, "city": "Lisboa", "country_title_local": "Portugal"},
            "description": "Como novo, com caixa.",
        }
        item.update(overrides)
        return item

    def test_parse_valid_item(self):
        listing = self.scraper._parse_vinted_item(self._sample_item())
        self.assertIsNotNone(listing)
        self.assertEqual(listing.title, "iPhone 15 Pro 256GB")
        self.assertEqual(listing.price, 650.0)
        self.assertEqual(listing.source, "Vinted")
        self.assertEqual(listing.url, "https://www.vinted.pt/items/12345-iphone-15-pro")
        self.assertEqual(listing.location, "Lisboa, Portugal")
        self.assertIn("Apple", listing.description)
        self.assertEqual(listing.image_url, "https://images.vinted.net/abc/full.jpg")

    def test_reject_non_eur_currency(self):
        item = self._sample_item(price={"amount": "650.00", "currency_code": "USD"})
        self.assertIsNone(self.scraper._parse_vinted_item(item))

    def test_reject_missing_price(self):
        item = self._sample_item(price=None)
        self.assertIsNone(self.scraper._parse_vinted_item(item))

    def test_reject_zero_price(self):
        item = self._sample_item(price={"amount": "0", "currency_code": "EUR"})
        self.assertIsNone(self.scraper._parse_vinted_item(item))

    def test_reject_short_title(self):
        item = self._sample_item(title="ab")
        self.assertIsNone(self.scraper._parse_vinted_item(item))

    def test_falls_back_to_thumb_when_no_full(self):
        item = self._sample_item(photo={"url": "https://x.test/thumb.jpg"})
        listing = self.scraper._parse_vinted_item(item)
        self.assertEqual(listing.image_url, "https://x.test/thumb.jpg")

    def test_constructs_url_from_id_when_url_missing(self):
        item = self._sample_item(url="")
        listing = self.scraper._parse_vinted_item(item)
        self.assertIsNotNone(listing)
        self.assertIn("/items/12345", listing.url)

    def test_handles_data_uri_image(self):
        item = self._sample_item(photo={"full_size_url": "data:image/png;base64,xyz"})
        listing = self.scraper._parse_vinted_item(item)
        self.assertEqual(listing.image_url, "")


class TestVintedScrapeEndToEnd(unittest.TestCase):

    def setUp(self):
        self.scraper = _make_scraper()
        self.scraper._vinted_warmed = {"www.vinted.pt"}  # skip homepage warm-up

    def test_scrape_paginates_and_dedupes(self):
        page1_items = [
            {"id": 1, "title": "iPhone 15 Pro",
             "price": {"amount": "650", "currency_code": "EUR"},
             "url": "https://www.vinted.pt/items/1"},
            {"id": 2, "title": "iPhone 15 Plus",
             "price": {"amount": "550", "currency_code": "EUR"},
             "url": "https://www.vinted.pt/items/2"},
        ]
        page2_items = [
            {"id": 1, "title": "iPhone 15 Pro",  # duplicado
             "price": {"amount": "650", "currency_code": "EUR"},
             "url": "https://www.vinted.pt/items/1"},
            {"id": 3, "title": "iPhone 15",
             "price": {"amount": "500", "currency_code": "EUR"},
             "url": "https://www.vinted.pt/items/3"},
        ]
        page3_items = []  # vazio → para a paginação

        responses = [
            json.dumps({"items": page1_items}),
            json.dumps({"items": page2_items}),
            json.dumps({"items": page3_items}),
        ]
        call_count = {"n": 0}

        def fake_fetch(url):
            i = call_count["n"]
            call_count["n"] += 1
            if i >= len(responses):
                raise RuntimeError("mais chamadas que páginas mockadas")
            return responses[i]

        with patch.object(self.scraper, "fetch_html", side_effect=fake_fetch):
            url = "https://www.vinted.pt/catalog?search_text=iphone"
            listings = self.scraper.scrape_vinted(url)

        self.assertEqual(len(listings), 3, "deviam ficar 3 únicos após dedupe")
        titles = sorted(l.title for l in listings)
        self.assertEqual(titles, ["iPhone 15", "iPhone 15 Plus", "iPhone 15 Pro"])
        self.assertTrue(all(l.source == "Vinted" for l in listings))

    def test_scrape_returns_empty_on_invalid_url(self):
        with patch.object(self.scraper, "fetch_html") as fake:
            result = self.scraper.scrape_vinted("https://www.olx.pt/q-rtx/")
        self.assertEqual(result, [])
        fake.assert_not_called()

    def test_scrape_handles_non_json_response(self):
        with patch.object(self.scraper, "fetch_html", return_value="<html>blocked</html>"):
            url = "https://www.vinted.pt/catalog?search_text=ps5"
            listings = self.scraper.scrape_vinted(url)
        self.assertEqual(listings, [])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Facebook Marketplace
# ─────────────────────────────────────────────────────────────────────────────

class TestFacebookCookies(unittest.TestCase):

    def setUp(self):
        # Reset class-level state entre testes
        import bot
        bot.MarketplaceScraper.FB_COOKIES_INJECTED = False
        bot.MarketplaceScraper.FB_WARNED_NO_COOKIES = False
        self.scraper = _make_scraper()

    def test_no_cookies_returns_false_and_skips(self):
        with patch.dict(os.environ, {"FB_C_USER": "", "FB_XS": ""}, clear=False):
            os.environ.pop("FB_C_USER", None)
            os.environ.pop("FB_XS", None)
            ok = self.scraper._ensure_fb_cookies()
        self.assertFalse(ok)

    def test_with_cookies_injects_into_session(self):
        with patch.dict(os.environ, {
            "FB_C_USER": "1234567890",
            "FB_XS": "abc:def:ghi",
            "FB_DATR": "datrvalue",
        }):
            ok = self.scraper._ensure_fb_cookies()

        self.assertTrue(ok)
        # Verificar cookies no session
        cookies = {c.name: c.value for c in self.scraper.session.cookies}
        self.assertEqual(cookies.get("c_user"), "1234567890")
        self.assertEqual(cookies.get("xs"), "abc:def:ghi")
        self.assertEqual(cookies.get("datr"), "datrvalue")

    def test_scrape_returns_empty_without_cookies(self):
        os.environ.pop("FB_C_USER", None)
        os.environ.pop("FB_XS", None)
        with patch.object(self.scraper, "fetch_html") as fake:
            result = self.scraper.scrape_facebook_marketplace(
                "https://www.facebook.com/marketplace/lisbon/search?query=iphone"
            )
        self.assertEqual(result, [])
        fake.assert_not_called()


class TestFacebookHtmlParsing(unittest.TestCase):

    def setUp(self):
        import bot
        bot.MarketplaceScraper.FB_COOKIES_INJECTED = True  # bypass cookie check
        self.scraper = _make_scraper()

    def test_extracts_listings_from_html(self):
        # HTML simulado com 2 listings
        fake_html = (
            'random html stuff '
            '<a href="/marketplace/item/111111/">item 1</a>'
            'more stuff '
            '<a href="/marketplace/item/222222/">item 2</a>'
            'inline data: '
            '{"marketplace_listing_title":"iPhone 14 128GB",'
            '"foo":"bar","listing_price":{"amount":"450.00","currency":"EUR"}}'
            ' more padding xxx '
            '{"marketplace_listing_title":"PS5 Slim",'
            '"foo":"bar","listing_price":{"amount":"380.50","currency":"EUR"}}'
        )
        result = self.scraper._parse_fb_marketplace_page(
            fake_html,
            "https://www.facebook.com/marketplace/lisbon/search?query=iphone",
        )
        self.assertEqual(len(result), 2)
        titles = sorted(l.title for l in result)
        self.assertEqual(titles, ["PS5 Slim", "iPhone 14 128GB"])
        prices = sorted(l.price for l in result)
        self.assertEqual(prices, [380.5, 450.0])
        # Os IDs devem ser distintos e estar nas URLs
        urls = {l.url for l in result}
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("marketplace/item/" in u for u in urls))

    def test_skips_non_eur_currency(self):
        fake_html = (
            '<a href="/marketplace/item/333/">x</a>'
            '{"marketplace_listing_title":"Something",'
            '"listing_price":{"amount":"100","currency":"USD"}}'
        )
        result = self.scraper._parse_fb_marketplace_page(fake_html, "x")
        self.assertEqual(result, [])

    def test_returns_empty_when_markup_unrecognized(self):
        result = self.scraper._parse_fb_marketplace_page(
            "<html>nothing here</html>", "x",
        )
        self.assertEqual(result, [])

    def test_login_redirect_returns_empty(self):
        login_html = "<html><head><title>Log in to Facebook</title></head>" + \
                     "<body>Please login to continue</body></html>"
        with patch.object(self.scraper, "fetch_html", return_value=login_html):
            result = self.scraper.scrape_facebook_marketplace(
                "https://www.facebook.com/marketplace/lisbon/search?query=ps5"
            )
        self.assertEqual(result, [])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Registry
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceRegistration(unittest.TestCase):

    def test_vinted_and_facebook_are_registered(self):
        import bot
        labels = [t[0] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS]
        self.assertIn("Vinted", labels)
        self.assertIn("Facebook", labels)

    def test_vinted_predicate_matches(self):
        import bot
        pred = next(
            t[1] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS if t[0] == "Vinted"
        )
        self.assertTrue(pred("https://www.vinted.pt/catalog?search_text=x"))
        self.assertTrue(pred("https://www.vinted.fr/catalog?search_text=x"))
        self.assertFalse(pred("https://www.olx.pt/q-x/"))

    def test_facebook_predicate_matches(self):
        import bot
        pred = next(
            t[1] for t in bot.MarketplaceScraper.SOURCE_ADAPTERS if t[0] == "Facebook"
        )
        self.assertTrue(pred("https://www.facebook.com/marketplace/lisbon/search?query=x"))
        self.assertFalse(pred("https://www.facebook.com/profile.php?id=1"))
        self.assertFalse(pred("https://www.olx.pt/q-x/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
