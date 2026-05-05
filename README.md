# OLX Flip Bot — Dashboard Edition

Private resale-deal hunter that scrapes OLX (and CustoJusto for cross-reference
pricing), scores each listing against a robust median, alerts you on Telegram
when the deal is good enough, and surfaces every detected deal in a private
web dashboard you can deploy to **Render**.

The Python scraper from earlier versions still runs underneath, unchanged in
its scoring logic — the new layer adds a database, an HTTP API, and a web UI.

---

## What you get

- **Background scraper** (every `SCRAPER_INTERVAL_MINUTES`) running 24/7 in
  the same process as the web app.
- **Telegram alerts** (unchanged) with the dashboard URL appended.
- **PostgreSQL** persistence on Render (`DATABASE_URL`); SQLite for local dev.
- **Dedup across restarts** — same OLX listing won't spam Telegram or insert
  twice; updates `last_seen_at` and price fields instead.
- **Dashboard** with stats, filters, sorting, dark theme, mobile-friendly.
- **Per-deal page** with notes, favorites, contacted/ignored/archived flags,
  CSV export and one-click Telegram resend.
- **Health endpoints** at `/health` and `/api/health` for Render probes.

---

## Quick start (local)

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Configure secrets
cp .env.example .env
$EDITOR .env       # set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DASHBOARD_PASSWORD

# 3. Run the web app + scraper together
python app.py

# Open http://localhost:3000 and login with DASHBOARD_USER / DASHBOARD_PASSWORD.
```

Local dev uses SQLite at `./data/deals.db` — no PostgreSQL needed unless you
set `DATABASE_URL`.

### Equivalent npm-style scripts

There's no Node here, but the spiritual equivalents are:

| Need                          | Command                                                |
|-------------------------------|--------------------------------------------------------|
| `npm start`                   | `python app.py` *(dev)* / `gunicorn -w 1 app:app` *(prod)* |
| `npm run dev`                 | `FLASK_DEBUG=1 python app.py`                          |
| `npm run db:init`             | `python -c "import db; db.init_db()"`                  |
| `npm run scraper` *(scraper alone)* | `RUN_SCRAPER=true python -c "from app import _maybe_start_scraper; _maybe_start_scraper(); import time; time.sleep(99999999)"` |
| Old CLI scraper *(no web)*    | `python bot.py`                                        |

The original `python bot.py` flow still works — it ignores the database when
`DATABASE_URL` is unset and behaves exactly like before.

---

## Deploy to Render

### Option A — Blueprint (one click)

1. Push this repo to GitHub.
2. Render → **New** → **Blueprint** → pick the repo.
3. Render reads `render.yaml` and creates:
   - PostgreSQL database (`olx-flip-db`)
   - Web service (`olx-flip-bot`)
4. In the service's **Environment** tab set the masked secrets:
   - `DASHBOARD_PASSWORD`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `BASE_URL` (e.g. `https://olx-flip-bot.onrender.com` — used for the
     dashboard link inside Telegram alerts)
5. Deploy. The first build will create tables automatically and the scraper
   starts within a few seconds of boot.

### Option B — Manual

1. Create a PostgreSQL instance, copy its **Internal Database URL**.
2. Create a **Web Service**:
   - Runtime: Python (Render auto-detects from `runtime.txt`)
   - Build command: `pip install --upgrade pip && pip install -r requirements.txt`
   - Start command: `gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT app:app`
   - Health check path: `/health`
3. Set environment variables (see [`.env.example`](.env.example)). The
   minimum for a working deploy:

   ```
   DATABASE_URL=<from step 1>
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   DASHBOARD_USER=admin
   DASHBOARD_PASSWORD=<your password>
   SESSION_SECRET=<random 32+ chars>
   BASE_URL=https://<your-service>.onrender.com
   NODE_ENV=production
   RUN_SCRAPER=true
   SCRAPER_INTERVAL_MINUTES=10
   ```

> **Free plan caveat:** Render's free web services *sleep* after ~15 minutes
> of inactivity, which kills the scraper thread. For real 24/7 monitoring,
> use the **Starter** plan ($7/mo) or hit `/health` from an external uptime
> pinger every 10 min.

---

## Environment variables

Full reference in [`.env.example`](.env.example).

| Variable                       | Purpose                                                  |
|--------------------------------|----------------------------------------------------------|
| `DATABASE_URL`                 | PostgreSQL connection string. Trumps `SQLITE_PATH`.      |
| `SQLITE_PATH`                  | Local SQLite file (dev only). Default `./data/deals.db`. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Required for alerts.                          |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD`   | Single-user auth.                             |
| `SESSION_SECRET`               | Cookie signing secret. Use 32+ random bytes.             |
| `PORT`                         | Render injects this. Local default 3000.                 |
| `BASE_URL`                     | Public origin used in Telegram dashboard links.          |
| `RUN_SCRAPER`                  | `false` to run dashboard only.                           |
| `SCRAPER_INTERVAL_MINUTES`     | Loop period. Default 10.                                 |
| `SCRAPER_RUN_ON_STARTUP`       | First scan immediately. Default true.                    |
| `CONFIG_PATH`                  | Watchlists YAML. Default `config.yml`.                   |
| `NODE_ENV`                     | Set to `production` to enable secure cookies.            |
| **Concurrency / rate limiting** |                                                         |
| `SCRAPER_GLOBAL_CONCURRENCY`   | Max in-flight HTTP requests across all hosts. Default 4. |
| `SCRAPER_PER_HOST_CONCURRENCY` | Max in-flight per host (OLX, CustoJusto). Default 2.     |
| `SCRAPER_MIN_HOST_INTERVAL`    | Floor seconds between same-host requests. Default 0.8.   |
| `WATCH_WORKER_COUNT`           | Per-watchlist thread pool size. Default 4.               |
| **Browser extension API**      |                                                         |
| `EXTENSION_API_TOKEN`          | Required to enable `/api/evaluate`. Long random string.  |
| `EXTENSION_ALLOWED_ORIGIN`     | Comma-separated CORS origins (e.g. `chrome-extension://abc…`). |
| `EXTENSION_LIVE_FETCH`         | `true` to fetch refs on cache miss; default `false`.     |

---

## How the dashboard works

### Auth
- Single user/password from env vars; sessions are signed with
  `SESSION_SECRET`.
- All routes except `/health` and `/api/health` require auth.
- Login throttled to 5 attempts per 5 minutes per IP.

### Routes (all return JSON unless noted)
| Method | Path                                | Purpose                                  |
|--------|-------------------------------------|------------------------------------------|
| GET    | `/`                                 | Redirect to dashboard or login           |
| GET    | `/login`                            | Login form                               |
| GET    | `/dashboard`                        | Dashboard (HTML)                         |
| GET    | `/deals/<id>`                       | Deal detail page (HTML)                  |
| GET    | `/health`, `/api/health`            | Public health & uptime                   |
| GET    | `/api/deals`                        | Filtered/sorted list                     |
| GET    | `/api/deals/<id>`                   | Single deal                              |
| POST   | `/api/deals`                        | Manual upsert                            |
| PATCH  | `/api/deals/<id>`                   | Toggle favorite/contacted/ignored/etc.   |
| DELETE | `/api/deals/<id>`                   | Soft-archive                             |
| POST   | `/api/deals/<id>/send-telegram`     | Re-send to Telegram                      |
| GET    | `/api/stats`                        | Aggregate stats                          |
| GET    | `/api/scraper/status`               | Scraper state                            |
| POST   | `/api/scraper/run-now`              | Trigger scan now                         |
| GET    | `/api/deals.csv`                    | CSV export                               |

### Deal saving and dedup
1. Scraper detects an alertable listing inside `bot.process_watch`.
2. Before sending Telegram, it calls `on_deal_callback` (defined in
   `scraper.py`) which `db.upsert_deal(...)` writes to the `deals` table.
3. The dedup key is `url_hash` (SHA-1 of the canonical URL minus query
   string). Matching rows are **updated** with the new price/last_seen_at
   instead of duplicating.
4. The Telegram message is appended with the dashboard link
   (`<BASE_URL>/deals/<id>`).
5. After the Telegram send succeeds, `telegram_sent` flips to true.
6. Future scans of the same URL hit the existing row; the bot won't re-alert
   unless price drops by `price_drop_threshold_percent` (default 10%).

### Scraper survival on Render
- Single gunicorn worker (`-w 1`), one daemon thread = one scraper.
- All state (deals, seen, alerted_price) lives in PostgreSQL — surviving
  restarts and redeploys cleanly.
- `seen.json` and `market_cache.json` are still used as a local-dev fallback
  when `DATABASE_URL` is empty.
- SIGINT/SIGTERM are handled to cleanly stop the scan loop.

---

## Tests / sanity checks

```bash
# 1. Run all sanity tests (pricing + config + API). Network-free.
python tests/run_all.py

# 2. Static check
python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read())"
python -c "import ast; ast.parse(open('pricing.py', encoding='utf-8').read())"
python -c "import ast; ast.parse(open('app.py', encoding='utf-8').read())"

# 3. Boot the app locally (uses SQLite fallback)
python app.py
# - Visit http://localhost:3000 and login.
# - Check /health returns {"status": "ok"}.
# - Click "Run scan" — within a few seconds new deals appear on the dashboard.
# - Check Telegram receives an alert with a /deals/<id> link.

# 4. The original CLI bot still works
python bot.py
```

If you want to verify dedup, run two consecutive scans without changing
anything: the dashboard counts and Telegram alerts shouldn't grow.

---

## Files at a glance

| File                   | What it is                                                     |
|------------------------|----------------------------------------------------------------|
| `bot.py`               | Scraper, scoring, Telegram, market estimation. CLI entry.      |
| `pricing.py`           | Pure pricing/comparison utilities (median, IQR/MAD, reliability, verdict). |
| `db.py`                | SQLAlchemy models, dedup, list/stats helpers.                  |
| `scraper.py`           | Background loop (`ScraperRunner`).                             |
| `app.py`               | Flask app, routes, auth, `/api/evaluate`.                      |
| `templates/*`          | Server-rendered HTML.                                          |
| `static/*`             | CSS + vanilla JS for the dashboard.                            |
| `config.yml`           | Watchlists, blacklist, location filter, scraper concurrency.   |
| `render.yaml`          | Render Blueprint (DB + web service).                           |
| `Procfile`             | Heroku-style start command (also accepted by Render).          |
| `requirements.txt`     | Python deps.                                                   |
| `runtime.txt`          | Python version pin for Render.                                 |
| `browser-extension/`   | MV3 browser extension that calls `/api/evaluate` on OLX pages. |
| `tests/`               | Sanity tests (pricing, config, API).                           |

---

## Compliance

The scraper hits **public** OLX search pages with reasonable delays
(`request_delay_seconds` per host) and one worker process. It does **not**
bypass CAPTCHA, login walls, or anti-bot protections. It does not log into
OLX. Use it for personal monitoring of public listings only.

---

## Scraper concurrency & rate limiting

`MarketplaceScraper` (in `bot.py`) enforces multiple guardrails so adding
watchlists doesn't trip OLX's anti-bot heuristics:

- **Global semaphore** caps total in-flight HTTP requests
  (`SCRAPER_GLOBAL_CONCURRENCY`, default **4**).
- **Per-host semaphore** caps in-flight requests to each host
  (`SCRAPER_PER_HOST_CONCURRENCY`, default **2**).
- **Minimum interval** between same-host requests
  (`SCRAPER_MIN_HOST_INTERVAL`, default **0.8s**).
- **Randomised jitter** of 0.5–2.5s (configurable in `settings.scraper`)
  between requests.
- **Retries with exponential backoff** for 429/403/5xx and connection errors
  (`retry_max_attempts`, `retry_backoff_base_seconds`).
- **Soft-ban detection** — three consecutive 429/403 on the same host
  triggers a 90s pause for that host before resuming.
- **Honors `Retry-After`** headers when longer than the computed backoff.

If you grow watchlist count further, **don't** simply raise concurrency —
prefer raising `SCRAPER_INTERVAL_MINUTES` so each cycle doesn't pile up.

> **Rate-limit caveat:** OLX has been observed to soft-ban IP addresses that
> issue >1 request/sec sustained. The defaults above are conservative on
> purpose. If you see `[SOFTBAN]` lines in the logs, lower
> `SCRAPER_PER_HOST_CONCURRENCY` to 1 and bump `SCRAPER_MIN_HOST_INTERVAL`
> to 2.0s.

---

## Pricing reliability

The market median is now wrapped in reliability gates so a watchlist with
3 noisy comparables doesn't spam alerts:

- `min_sample_size` (default **8**) — raw comparables required.
- `min_filtered_sample_size` (default **5**) — comparables left after IQR/MAD
  trim, blacklist filtering, and bundle exclusion.
- `min_reliability_score` (default **0.55**) — combines sample size, match
  precision (exact / partial / global), retention, and IQR width into 0..1.
- `min_match_type_for_alert` (default `partial`) — refuse alerts that fall
  back to global-pool comparison.
- Built-in `DAMAGE_KEYWORDS` (`avariado`, `partido`, `peças`, `bloqueado`,
  `icloud`, `bateria inchada`, `not working`, etc.) are filtered from both
  the candidate listings and the market reference pool.
- `outlier_method: iqr | mad` — IQR is default (multiplier 1.0); MAD with
  threshold 3.5 handles bimodal markets better.
- Listings are deduped by canonical URL **and** by `(token-set title, price
  bucket)` so cross-posts collapse into one entry.

Each scored listing now carries `verdict`, `reliability_score`,
`filtered_sample_size`, `match_type`, and a structured `reasons` list — both
in Telegram alerts and the dashboard.

---

## Browser extension (`browser-extension/`)

A small Manifest V3 extension that decorates OLX listing pages with a
verdict overlay sourced from your backend's `/api/evaluate` endpoint.

### Setup

1. Backend: set `EXTENSION_API_TOKEN` to a long random string. Optionally
   set `EXTENSION_ALLOWED_ORIGIN` to your extension's
   `chrome-extension://<id>` origin.
2. Browser: `chrome://extensions` → enable Developer Mode → **Load
   unpacked** → pick `browser-extension/`.
3. Click the extension icon → set **Backend URL** and **API Token** → save.
4. Open any OLX listing — the overlay appears next to the price.

The token is **never** stored in the repo. See
[`browser-extension/README.md`](browser-extension/README.md) for the full
guide.

---

## API: `/api/evaluate`

Used by the browser extension; usable by any HTTP client.

**Request**

```http
POST /api/evaluate
Authorization: Bearer <EXTENSION_API_TOKEN>
Content-Type: application/json

{
  "title": "RTX 3060 12GB como nova",
  "price": 140,
  "url": "https://www.olx.pt/d/anuncio/rtx-3060-...",
  "condition": "like_new",
  "category": "GPU",
  "location": "Braga"
}
```

**Response (200)**

```json
{
  "verdict": "good_deal",
  "listing_price": 140,
  "estimated_market_price": 218.5,
  "profit_margin_percent": 56.1,
  "sample_size": 12,
  "filtered_sample_size": 11,
  "reliability_score": 0.87,
  "match_type": "exact",
  "watch_name": "RTX 3060",
  "condition": "like_new",
  "reasons": [
    "Preço 140€ vs mediana 219€ (margem 56.1%)",
    "11 comparáveis após filtragem (de 12 brutos, match exact)",
    "Fiabilidade 0.87"
  ]
}
```

**Errors**

- `400` — missing `title`/`url`, or `price` not a number.
- `401` — missing or wrong token.
- `503` — `EXTENSION_API_TOKEN` not set on the backend (endpoint disabled).

The endpoint reads market data from the same per-watchlist cache that the
scraper warms (`market_cache.json`). It does **not** trigger live OLX
scrapes by default. Set `EXTENSION_LIVE_FETCH=true` to allow fallback
scraping on cache miss (slower; respects all the rate-limit guardrails).

**Curl example**

```bash
curl -sS -X POST "$BASE_URL/api/evaluate" \
  -H "Authorization: Bearer $EXTENSION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"RTX 3060","price":150,"url":"https://www.olx.pt/d/anuncio/x.html"}' \
  | python -m json.tool
```

---

## Render deployment recommendation

For up to ~150 watchlists at 10-minute intervals, the single-process model
(one gunicorn worker, scraper thread inside) on **Starter** plan handles
fine. Above that, split into:

- **Web service** (`olx-flip-bot`) — serves the dashboard + `/api/evaluate`.
  Set `RUN_SCRAPER=false`.
- **Background worker** — runs `python bot.py` directly. Both share the
  same Postgres DB and `config.yml`.

This decouples API latency from scrape cycles. The current `render.yaml`
ships the single-process setup; the worker split is a manual change.

---

## Tests

```bash
python tests/run_all.py          # 31 tests across pricing/config/api
python tests/test_pricing.py     # pricing module only
```

The API test seeds `data/market_cache.json` with synthetic data so it never
touches the network. Don't commit `data/market_cache.json` — it's a local
cache, not a fixture.
