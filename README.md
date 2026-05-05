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

| Variable                  | Purpose                                                  |
|---------------------------|----------------------------------------------------------|
| `DATABASE_URL`            | PostgreSQL connection string. Trumps `SQLITE_PATH`.      |
| `SQLITE_PATH`             | Local SQLite file (dev only). Default `./data/deals.db`. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Required for alerts.                     |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD`   | Single-user auth.                        |
| `SESSION_SECRET`          | Cookie signing secret. Use 32+ random bytes.             |
| `PORT`                    | Render injects this. Local default 3000.                 |
| `BASE_URL`                | Public origin used in Telegram dashboard links.          |
| `RUN_SCRAPER`             | `false` to run dashboard only.                           |
| `SCRAPER_INTERVAL_MINUTES`| Loop period. Default 10.                                 |
| `SCRAPER_RUN_ON_STARTUP`  | First scan immediately. Default true.                    |
| `CONFIG_PATH`             | Watchlists YAML. Default `config.yml`.                   |
| `NODE_ENV`                | Set to `production` to enable secure cookies.            |

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
# 1. Static check
python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read())"
python -c "import ast; ast.parse(open('app.py', encoding='utf-8').read())"
python -c "import ast; ast.parse(open('db.py', encoding='utf-8').read())"
python -c "import ast; ast.parse(open('scraper.py', encoding='utf-8').read())"

# 2. Boot the app locally (uses SQLite fallback)
python app.py
# - Visit http://localhost:3000 and login.
# - Check /health returns {"status": "ok"}.
# - Click "Run scan" — within a few seconds new deals appear on the dashboard.
# - Check Telegram receives an alert with a /deals/<id> link.
# - Click the link, write a note, mark favorite — refresh, the state stuck.

# 3. The original CLI bot still works
python bot.py
```

If you want to verify dedup, run two consecutive scans without changing
anything: the dashboard counts and Telegram alerts shouldn't grow.

---

## Files at a glance

| File              | What it is                                                     |
|-------------------|----------------------------------------------------------------|
| `bot.py`          | Original scraper (unchanged behaviour + tiny hooks).           |
| `db.py`           | SQLAlchemy models, dedup, list/stats helpers.                  |
| `scraper.py`      | Background loop (`ScraperRunner`).                             |
| `app.py`          | Flask app, routes, auth, signal handlers.                      |
| `templates/*`     | Server-rendered HTML.                                          |
| `static/*`        | CSS + vanilla JS for the dashboard.                            |
| `config.yml`      | Watchlists, blacklist, location filter (existing).             |
| `render.yaml`     | Render Blueprint (DB + web service).                           |
| `Procfile`        | Heroku-style start command (also accepted by Render).          |
| `requirements.txt`| Python deps.                                                   |
| `runtime.txt`     | Python version pin for Render.                                 |

---

## Compliance

The scraper hits **public** OLX search pages with reasonable delays
(`request_delay_seconds` per host) and one worker process. It does **not**
bypass CAPTCHA, login walls, or anti-bot protections. It does not log into
OLX. Use it for personal monitoring of public listings only.
