# Bug & Risk Analysis (May 6, 2026)

## Scope
- Static review of core modules (`app.py`, `bot.py`, `pricing.py`) and current automated tests.
- Verification run: `pytest -q` (56 passing).

## Potential Bugs / Errors

### 1) Open redirect risk on login `next` parameter
**Where:** `app.py` login handler.

The app redirects to `request.args.get("next")` after successful auth without validating that destination is a local path. If an attacker crafts a login URL with an external `next` value (for example `https://evil.example/...`) and a user logs in, they can be bounced to an attacker-controlled site.

**Why this matters:** phishing/session-trust abuse via trusted domain login flow.

**Suggested fix:** only allow relative paths starting with `/` and reject absolute URLs/schemes.

### 2) In-memory login rate-limit store can grow unbounded
**Where:** `app.py` (`LOGIN_RATELIMIT` dictionary).

Rate-limit entries are retained by IP key and only old *timestamps* are pruned when that exact IP hits login again. IP keys that never return are never removed, so long-lived processes under spray traffic can accumulate stale keys.

**Why this matters:** memory growth over time under hostile or noisy traffic.

**Suggested fix:** periodically purge keys with empty/really old attempt lists, or move to an external TTL-backed store.

### 3) Global scraper data directory may break under alternate working directories
**Where:** `bot.py` (`DATA_DIR = Path("data")`).

Paths are relative to process working directory, not module location. Running the bot from a different cwd (cron/service wrappers/tests) can create/read unintended `data/` folders and appear as missing state.

**Why this matters:** inconsistent cache/seen tracking and hard-to-debug behavior across environments.

**Suggested fix:** anchor data paths to repository/module path (`Path(__file__).resolve().parent / "data"`) or configurable absolute env path.

### 4) Over-broad damage keywords may produce false negatives in valid listings
**Where:** `pricing.py` (`DAMAGE_KEYWORDS` includes `"peças"`, `"pecas"`).

Single-word tokens like `peças/pecas` can appear in benign contexts (e.g., accessories bundles), causing listings to be filtered as damaged even when they are valid deals.

**Why this matters:** lower recall; missed opportunities.

**Suggested fix:** tighten to explicit multi-word risk phrases (e.g., `"para pecas"`) or require co-occurrence logic for ambiguous single terms.

## Verification Summary
- Automated tests currently pass and do not catch the above risks.
- These are potential correctness/security/operational issues identified by code review.
