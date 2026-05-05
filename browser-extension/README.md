# OLX Flip Evaluator — Browser Extension (Manifest V3)

A small overlay that decorates OLX listing pages with a verdict from your
private `OLX Flip Bot` backend (`/api/evaluate`).

This is a **personal-use** extension. It is **not** packaged for the Chrome
Web Store and the API token must be configured locally — never check secrets
into the repo.

## What it does

1. On every OLX listing detail page (URL contains `/d/anuncio/`):
   - Reads the visible title, price, condition, and location.
2. Calls `POST <BACKEND_URL>/api/evaluate` with `Authorization: Bearer <TOKEN>`.
3. Renders an overlay card next to the price with:
   - Verdict (`good_deal` / `neutral` / `bad_deal` / `unreliable`).
   - Estimated market price (median).
   - Profit margin (%).
   - Reliability score (0..1).
   - Sample size after filtering.
   - Top reasons (max 5).

## Installation (Chrome / Brave / Edge — Developer Mode)

1. Open `chrome://extensions` (or `brave://extensions`, `edge://extensions`).
2. Enable **Developer mode** (top right toggle).
3. Click **Load unpacked**.
4. Pick the `browser-extension/` directory in this repository.
5. The extension's icon appears in the toolbar.

## Configure

1. Click the extension icon → set:
   - **Backend URL** — e.g. `https://olx-flip-bot.onrender.com` (no trailing slash needed).
   - **API Token** — the value of `EXTENSION_API_TOKEN` you set on the backend
     (Render → Environment → Add).
2. Click **Guardar**.
3. Open any OLX listing — the overlay appears next to the price.

The values live in `chrome.storage.sync`. Wipe them by clicking *Remove
extension* if you ever lose your laptop.

## Backend setup checklist

The backend will return `503 extension_disabled` until you set
`EXTENSION_API_TOKEN`. Recommended env vars:

| Variable                    | Purpose                                                                       |
|-----------------------------|-------------------------------------------------------------------------------|
| `EXTENSION_API_TOKEN`       | Required. Long random string. Used as `Authorization: Bearer <token>`.        |
| `EXTENSION_ALLOWED_ORIGIN`  | Optional. Comma-separated list of allowed origins for CORS preflight (e.g. `chrome-extension://abcdef…`). Leave empty to disable CORS entirely. |
| `EXTENSION_LIVE_FETCH`      | `false` (default) — only use cached market data; `true` — fetch fresh refs on cache miss (slower, may trip rate limits). |

To find your extension's `chrome-extension://` origin, open
`chrome://extensions`, enable Developer mode, and copy the ID under the
extension's name. The origin is `chrome-extension://<ID>`.

## Privacy & security notes

- The token is stored in `chrome.storage.sync`. Browsers protect this from
  other websites, but it is **not** safe to publish the unpacked extension
  with the token baked in.
- The extension only sends data to the **single** backend URL you configure.
  It does not phone home, does not log telemetry, does not call OLX APIs.
- The backend's `/api/evaluate` endpoint authenticates *every* request with a
  constant-time token compare and rejects requests without
  `Authorization: Bearer …` or `X-API-Token: …`.

## Development tips

- Reload the extension after every JS edit (Chrome → Extensions → reload icon).
- DevTools for the content script: open the OLX listing tab, F12, *Console*.
  Logs prefixed by the extension live in the page console because content
  scripts run there.
- DevTools for the popup: right-click the extension icon → *Inspect popup*.

## File structure

```
browser-extension/
├── manifest.json      # MV3 manifest, host permissions, content_scripts wiring
├── content.js         # Reads OLX page → calls /api/evaluate → renders overlay
├── styles.css         # Overlay card styles
├── popup.html         # Backend URL + token configuration UI
├── popup.js           # chrome.storage.sync handlers
└── README.md          # This file
```

## Limitations / TODOs

- Selectors for OLX (`title`, `price`, `condition`, `location`) drift over
  time. Update them in `content.js` (the `readListing()` function) when OLX
  redesigns the listing page.
- No icons (`icons: {}` in `manifest.json`). Add 16/32/48/128 PNGs in
  `icons/` if you want a polished build.
- Mobile browsers (Kiwi, etc.) accept MV3 unpacked but the overlay positioning
  is desktop-tuned.
