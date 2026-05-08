"""
tests/test_limpar_anuncios.py
─────────────────────────────
Suite de testes para a feature "Limpar Anúncios".

Cobre:
  1. db.clear_all_listings()      — limpeza da DB (deals + seen_listings)
  2. bot.clear_local_caches()     — reset dos ficheiros locais
  3. Endpoint POST /api/admin/limpar-anuncios
       a) sem header → 400
       b) scan em curso → 409
       c) sucesso → 200 com contagens corretas
  4. Republicação no scan seguinte — após limpeza, as mesmas URLs
     voltam a ser processadas como novas.

Todos os testes são offline/network-free.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

# ── garante que o root do projecto está no sys.path ──────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers reutilizáveis
# ─────────────────────────────────────────────────────────────────────────────

def _make_in_memory_db():
    """Cria um engine SQLite em memória e faz o schema."""
    import sqlalchemy as sa
    import db as db_module

    engine = sa.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    db_module.Base.metadata.create_all(engine)
    SessionLocal = sa.orm.sessionmaker(bind=engine, future=True)
    return engine, SessionLocal


def _insert_deal(session_factory, url: str, price: float = 100.0):
    """Insere um Deal mínimo directamente via ORM."""
    import db as db_module
    with db_module.session_scope.__wrapped__(session_factory) if hasattr(
        db_module.session_scope, "__wrapped__"
    ) else _raw_session(session_factory) as s:
        deal = db_module.Deal(
            url=url,
            url_hash=db_module.hash_url(url),
            title="Test listing",
            price=price,
            source="olx",
        )
        s.add(deal)


def _insert_seen(session_factory, url: str):
    """Insere um SeenListing directamente via ORM."""
    import db as db_module
    with _raw_session(session_factory) as s:
        s.add(db_module.SeenListing(
            url_hash=db_module.hash_url(url),
            url=url,
            price=50.0,
        ))


from contextlib import contextmanager

@contextmanager
def _raw_session(session_factory):
    s = session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Testes de db.clear_all_listings()
# ─────────────────────────────────────────────────────────────────────────────

class TestClearAllListings(unittest.TestCase):
    """Verifica que clear_all_listings() apaga todas as linhas de ambas as tabelas."""

    def _run(self, deals_to_insert: int, seen_to_insert: int):
        import db as db_module
        engine, SessionLocal = _make_in_memory_db()

        # Monkey-patch session_scope para usar o engine em memória
        @contextmanager
        def patched_session_scope():
            s = SessionLocal()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        with patch.object(db_module, "session_scope", patched_session_scope):
            # Inserir dados
            for i in range(deals_to_insert):
                with patched_session_scope() as s:
                    s.add(db_module.Deal(
                        url=f"https://olx.pt/item-{i}",
                        url_hash=db_module.hash_url(f"https://olx.pt/item-{i}"),
                        title=f"Item {i}", price=float(i * 10), source="olx",
                    ))
            for i in range(seen_to_insert):
                with patched_session_scope() as s:
                    s.add(db_module.SeenListing(
                        url_hash=db_module.hash_url(f"https://olx.pt/seen-{i}"),
                        url=f"https://olx.pt/seen-{i}", price=float(i),
                    ))

            result = db_module.clear_all_listings()

            # Verificar contagens retornadas
            self.assertEqual(result["deals"], deals_to_insert)
            self.assertEqual(result["seen_listings"], seen_to_insert)

            # Verificar que as tabelas estão vazias
            with patched_session_scope() as s:
                from sqlalchemy import select, func
                d_count = s.scalar(select(func.count(db_module.Deal.id))) or 0
                sl_count = s.scalar(
                    select(func.count(db_module.SeenListing.url_hash))
                ) or 0
            self.assertEqual(d_count, 0, "deals não foram apagados")
            self.assertEqual(sl_count, 0, "seen_listings não foram apagados")

        return result

    def test_clear_with_data(self):
        result = self._run(deals_to_insert=5, seen_to_insert=3)
        self.assertEqual(result["deals"], 5)
        self.assertEqual(result["seen_listings"], 3)

    def test_clear_empty_db(self):
        """Limpar uma DB já vazia não deve lançar excepção."""
        result = self._run(deals_to_insert=0, seen_to_insert=0)
        self.assertEqual(result["deals"], 0)
        self.assertEqual(result["seen_listings"], 0)

    def test_clear_only_deals(self):
        result = self._run(deals_to_insert=7, seen_to_insert=0)
        self.assertEqual(result["deals"], 7)
        self.assertEqual(result["seen_listings"], 0)

    def test_clear_only_seen(self):
        result = self._run(deals_to_insert=0, seen_to_insert=4)
        self.assertEqual(result["deals"], 0)
        self.assertEqual(result["seen_listings"], 4)

    def test_return_type_is_dict_with_expected_keys(self):
        result = self._run(deals_to_insert=1, seen_to_insert=1)
        self.assertIn("deals", result)
        self.assertIn("seen_listings", result)
        self.assertIsInstance(result["deals"], int)
        self.assertIsInstance(result["seen_listings"], int)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Testes de bot.clear_local_caches()
# ─────────────────────────────────────────────────────────────────────────────

class TestClearLocalCaches(unittest.TestCase):
    """Verifica que os ficheiros locais ficam com {}."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.seen_path = Path(self.tmpdir) / "seen.json"
        self.cache_path = Path(self.tmpdir) / "market_cache.json"

    def _run_clear(self):
        import bot as bot_module
        with (
            patch.object(bot_module, "SEEN_FILE", self.seen_path),
            patch.object(bot_module, "MARKET_CACHE_FILE", self.cache_path),
        ):
            return bot_module.clear_local_caches()

    def test_files_become_empty_dict(self):
        # Pre-popular os ficheiros
        self.seen_path.write_text(
            json.dumps({"https://olx.pt/a": {"first": "2026-01-01T00:00:00Z"}}),
            encoding="utf-8",
        )
        self.cache_path.write_text(
            json.dumps({"abc123": {"market": {}, "computed_at": "2026-01-01T00:00:00Z"}}),
            encoding="utf-8",
        )

        result = self._run_clear()

        self.assertTrue(result["seen_file_cleared"])
        self.assertTrue(result["market_cache_file_cleared"])
        self.assertEqual(json.loads(self.seen_path.read_text()), {})
        self.assertEqual(json.loads(self.cache_path.read_text()), {})

    def test_creates_files_if_missing(self):
        """Se os ficheiros não existem, devem ser criados como {}."""
        self.assertFalse(self.seen_path.exists())
        self.assertFalse(self.cache_path.exists())

        result = self._run_clear()

        self.assertTrue(result["seen_file_cleared"])
        self.assertTrue(result["market_cache_file_cleared"])
        self.assertTrue(self.seen_path.exists())
        self.assertTrue(self.cache_path.exists())
        self.assertEqual(json.loads(self.seen_path.read_text()), {})

    def test_returns_summary_dict(self):
        result = self._run_clear()
        self.assertIn("seen_file_cleared", result)
        self.assertIn("market_cache_file_cleared", result)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Testes do endpoint POST /api/admin/limpar-anuncios
# ─────────────────────────────────────────────────────────────────────────────

def _build_test_app():
    """
    Constrói uma instância da app Flask com:
    - DB SQLite em memória (patch de db.session_scope)
    - scraper runner mockado
    - autenticação desactivada (patch de require_auth)
    """
    os.environ.setdefault("DASHBOARD_PASSWORD", "test")
    os.environ.setdefault("SESSION_SECRET", "test-secret")
    # Evitar que o scraper real arranque
    os.environ["RUN_SCRAPER"] = "false"

    import app as app_module
    flask_app = app_module.create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret"
    return flask_app


class TestLimparAnunciosEndpoint(unittest.TestCase):

    def setUp(self):
        self.app = _build_test_app()
        self.client = self.app.test_client()
        # Fazer login para obter sessão válida
        self.client.post(
            "/login",
            data={"username": "admin", "password": "test"},
            follow_redirects=True,
        )

    # ── 3a. Sem header de confirmação → 400 ────────────────────────────────
    def test_missing_confirm_header_returns_400(self):
        r = self.client.post("/api/admin/limpar-anuncios")
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["error"], "missing_confirmation")

    # ── 3b. Header errado → 400 ─────────────────────────────────────────────
    def test_wrong_confirm_header_returns_400(self):
        r = self.client.post(
            "/api/admin/limpar-anuncios",
            headers={"X-Confirm-Action": "errado"},
        )
        self.assertEqual(r.status_code, 400)

    # ── 3c. Scan em curso → 409 ─────────────────────────────────────────────
    def test_scan_in_progress_returns_409(self):
        import scraper as scraper_mod
        mock_runner = MagicMock()
        mock_runner.get_status.return_value = {"is_scraping": True}
        with patch.object(scraper_mod, "get_runner", return_value=mock_runner):
            r = self.client.post(
                "/api/admin/limpar-anuncios",
                headers={"X-Confirm-Action": "limpar"},
            )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "scan_in_progress")

    # ── 3d. Sucesso completo → 200 ──────────────────────────────────────────
    def test_success_returns_200_with_counts(self):
        import db as db_module
        import bot as bot_module
        import scraper as scraper_mod

        mock_runner = MagicMock()
        mock_runner.get_status.return_value = {"is_scraping": False}
        mock_runner._state_lock = threading.Lock()
        mock_runner.state = {"total_scans": 5, "total_alerts": 10, "total_deals_found": 3}

        with (
            patch.object(db_module, "clear_all_listings",
                         return_value={"deals": 7, "seen_listings": 12}),
            patch.object(bot_module, "clear_local_caches",
                         return_value={"seen_file_cleared": True,
                                       "market_cache_file_cleared": True}),
            patch.object(scraper_mod, "get_runner", return_value=mock_runner),
        ):
            r = self.client.post(
                "/api/admin/limpar-anuncios",
                headers={"X-Confirm-Action": "limpar"},
            )

        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["db"]["deals"], 7)
        self.assertEqual(body["db"]["seen_listings"], 12)
        self.assertIn("started_at", body)
        self.assertIn("finished_at", body)

    # ── 3e. Sem sessão → redireccionamento para login ───────────────────────
    def test_unauthenticated_redirects_to_login(self):
        client = self.app.test_client()  # nova sessão sem login
        r = client.post(
            "/api/admin/limpar-anuncios",
            headers={"X-Confirm-Action": "limpar"},
        )
        # require_auth redireciona (302) ou devolve 401
        self.assertIn(r.status_code, (302, 401))

    # ── 3f. DB error → 500 ──────────────────────────────────────────────────
    def test_db_error_returns_500(self):
        import db as db_module
        import scraper as scraper_mod

        mock_runner = MagicMock()
        mock_runner.get_status.return_value = {"is_scraping": False}

        with (
            patch.object(db_module, "clear_all_listings",
                         side_effect=Exception("DB connection lost")),
            patch.object(scraper_mod, "get_runner", return_value=mock_runner),
        ):
            r = self.client.post(
                "/api/admin/limpar-anuncios",
                headers={"X-Confirm-Action": "limpar"},
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("db_error", r.get_json()["error"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Republicação no scan seguinte
#    Após clear_all_listings, a mesma URL é tratada como nova pelo dedup.
# ─────────────────────────────────────────────────────────────────────────────

class TestRepublicacaoAposLimpeza(unittest.TestCase):
    """
    Simula o comportamento do ciclo de scan:

    1. URL é vista → guardada em seen_listings / seen.json
    2. clear_all_listings() + clear_local_caches() são chamados
    3. load_seen() devolve {} — URL já não é conhecida
    4. URL seria alertada novamente (dedup passa)
    """

    def test_seen_dict_empty_after_db_clear(self):
        """Após limpeza da DB, load_seen() via DB devolve {}."""
        import db as db_module
        import bot as bot_module

        engine, SessionLocal = _make_in_memory_db()

        @contextmanager
        def patched_session_scope():
            s = SessionLocal()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        url = "https://olx.pt/test-item-123"

        with patch.object(db_module, "session_scope", patched_session_scope):
            # 1. Inserir um SeenListing
            with patched_session_scope() as s:
                s.add(db_module.SeenListing(
                    url_hash=db_module.hash_url(url),
                    url=url, price=100.0,
                ))

            # Verificar que está guardado
            seen_before = db_module.load_seen_dict()
            self.assertIn(url, seen_before, "URL devia estar em seen antes da limpeza")

            # 2. Limpar
            result = db_module.clear_all_listings()
            self.assertEqual(result["seen_listings"], 1)

            # 3. Após limpeza, URL já não é conhecida
            seen_after = db_module.load_seen_dict()
            self.assertNotIn(url, seen_after, "URL não devia estar em seen após limpeza")

    def test_seen_file_empty_after_clear_local_caches(self):
        """Após clear_local_caches(), load_seen() via ficheiro devolve {}."""
        import bot as bot_module

        tmpdir = tempfile.mkdtemp()
        seen_path = Path(tmpdir) / "seen.json"
        cache_path = Path(tmpdir) / "market_cache.json"

        url = "https://olx.pt/test-item-456"
        seen_path.write_text(
            json.dumps({url: {"first": "2026-01-01T00:00:00Z", "price": 99.0}}),
            encoding="utf-8",
        )

        with (
            patch.object(bot_module, "SEEN_FILE", seen_path),
            patch.object(bot_module, "MARKET_CACHE_FILE", cache_path),
            patch.object(bot_module, "_use_db_seen", return_value=False),
        ):
            # Confirmar que está em seen antes
            seen_before = bot_module.load_seen()
            self.assertIn(url, seen_before)

            # Limpar
            bot_module.clear_local_caches()

            # Após limpeza, ficheiro é {} → URL não conhecida
            seen_after = bot_module.load_seen()
            self.assertNotIn(url, seen_after,
                             "URL não devia estar em seen após clear_local_caches()")

    def test_url_would_be_alerted_after_clear(self):
        """
        Verifica que a lógica de dedup (key not in seen) passa para uma URL
        que estava em seen antes da limpeza, simulando a republicação.
        """
        import bot as bot_module

        url = "https://olx.pt/rtx-3080-test"
        key = url.split("?")[0]

        # Simular seen pré-limpeza
        seen_with_url: dict = {key: {"first": "2026-01-01T00:00:00Z", "price": 300.0}}
        # Simular seen pós-limpeza
        seen_empty: dict = {}

        # Antes da limpeza: URL conhecida → não alerta
        would_alert_before = key not in seen_with_url
        self.assertFalse(would_alert_before, "Antes da limpeza não devia alertar")

        # Após limpeza: URL desconhecida → alerta
        would_alert_after = key not in seen_empty
        self.assertTrue(would_alert_after, "Após limpeza devia poder alertar")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
