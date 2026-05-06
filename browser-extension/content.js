/**
 * OLX Flip Evaluator — content script (SPA-aware).
 *
 * Lifecycle:
 *   - Runs at document_idle on every olx.pt page.
 *   - Re-evaluates on SPA navigations (pushState / replaceState / popstate).
 *   - Uses a MutationObserver to retry parsing while OLX hydrates the DOM.
 *   - Per-URL dedup so the overlay never doubles up.
 *   - Backend calls are debounced and aborted on URL change.
 *
 * Backend: POST <BACKEND>/api/evaluate with Authorization: Bearer <TOKEN>.
 */
(function () {
  "use strict";

  // ── Constants ────────────────────────────────────────────────────────────
  const LISTING_PATH_RE = /\/d\/anuncio\//;
  const OVERLAY_ID = "olx-flip-evaluator-overlay";
  const REQUEST_TIMEOUT_MS = 10_000;        // hard cap for fetch
  const PARSE_RETRY_MAX = 12;               // ~12 * 500ms = 6s
  const PARSE_RETRY_INTERVAL_MS = 500;
  const DEBOUNCE_MS = 250;                  // collapse rapid mutations
  const MIN_TIME_BETWEEN_EVALS_MS = 800;    // throttle: never fire faster than this
  const LOG_PREFIX = "[OLX-Flip]";

  // Brand list used for lightweight extraction. Lowercase, no accents.
  const BRANDS = [
    "apple", "samsung", "xiaomi", "huawei", "oneplus", "sony", "lg",
    "nvidia", "amd", "asus", "msi", "gigabyte", "evga", "zotac",
    "intel", "ryzen", "macbook", "ipad", "iphone", "airpods",
    "playstation", "ps5", "ps4", "xbox", "nintendo", "steam",
    "jbl", "bose", "marshall", "logitech", "razer", "corsair",
    "lenovo", "hp", "dell", "acer", "google", "pixel", "dji",
    "gopro", "canon", "nikon", "fuji",
  ];

  // ── State ───────────────────────────────────────────────────────────────
  let currentUrl = location.href;
  let activeAbort = null;          // AbortController for the in-flight fetch
  let lastEvalAt = 0;              // ms timestamp of last eval kickoff
  let debounceTimer = null;
  let evaluatedUrls = new Set();   // per-page-load: URLs already evaluated
  let observer = null;

  // ── Logging ─────────────────────────────────────────────────────────────
  function log(...args)  { try { console.log(LOG_PREFIX, ...args); } catch (_) {} }
  function warn(...args) { try { console.warn(LOG_PREFIX, ...args); } catch (_) {} }

  // ── Listing extraction ───────────────────────────────────────────────────
  function isListingPage() {
    return LISTING_PATH_RE.test(location.pathname);
  }

  function readListing() {
    // Try multiple selectors so we keep working as OLX redesigns its DOM.
    const titleEl =
      document.querySelector('[data-cy="ad_title"]') ||
      document.querySelector('[data-testid="ad_title"]') ||
      document.querySelector("h1") ||
      document.querySelector("h4");

    const priceEl =
      document.querySelector('[data-testid="ad-price-container"]') ||
      document.querySelector('[data-cy="ad-price"]') ||
      document.querySelector('h3 strong') ||
      document.querySelector('[aria-label*="Preço"]') ||
      document.querySelector('[class*="price"]');

    const locationEl =
      document.querySelector('[data-testid="map-aside-section"] p') ||
      document.querySelector('a[href*="cidade"]') ||
      document.querySelector('p[class*="locationDate"]');

    // Heuristic for condition
    const conditionLabel = Array.from(document.querySelectorAll("p, li, span"))
      .find((el) => /(estado|condição|condicao)/i.test(el.textContent || ""));

    const rawTitle = (titleEl && titleEl.textContent || "").trim();
    const rawPrice = (priceEl && priceEl.textContent || "").trim();
    const rawLoc = (locationEl && locationEl.textContent || "").trim();
    const rawCondition = (conditionLabel && conditionLabel.textContent || "").trim();

    // Parse "1.234,56 €" or "1234 €"
    let price = null;
    const m = rawPrice.match(/([\d.\s ]+),?(\d{1,2})?\s*€/);
    if (m) {
      const ints = m[1].replace(/[\s. ]/g, "");
      const decs = m[2] ? `.${m[2]}` : "";
      const n = parseFloat(ints + decs);
      if (Number.isFinite(n) && n > 0) price = n;
    }

    let condition = "unknown";
    const cl = rawCondition.toLowerCase();
    if (/(como novo|seminovo|quase novo)/.test(cl)) condition = "like_new";
    else if (/(novo|selado|nunca usado)/.test(cl)) condition = "new";
    else if (/(usado|usada)/.test(cl)) condition = "used";

    // Cheap brand extraction from the title
    const brand = (() => {
      const t = (rawTitle || "").toLowerCase()
        .normalize("NFD")
        .replace(/[̀-ͯ]/g, "");
      for (const b of BRANDS) {
        const re = new RegExp(`(^|\\W)${b}(\\W|$)`);
        if (re.test(t)) return b;
      }
      return null;
    })();

    // Strip query string from URL — backend canonicalises this anyway, but
    // doing it client-side keeps the overlay-dedup keys stable across
    // tracking-params changes mid-session.
    const url = location.href.split("?")[0].split("#")[0];

    return {
      title: rawTitle,
      price,
      url,
      condition,
      location: rawLoc,
      brand,
      priceAnchor: priceEl,
    };
  }

  // ── Overlay rendering ────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function clearOverlay() {
    const existing = document.getElementById(OVERLAY_ID);
    if (existing && existing.parentElement) {
      existing.parentElement.removeChild(existing);
    }
  }

  function renderOverlay(anchor, html) {
    clearOverlay();
    const wrap = document.createElement("div");
    wrap.id = OVERLAY_ID;
    wrap.className = "olx-flip-overlay";
    wrap.innerHTML = html;
    if (anchor && anchor.parentElement) {
      anchor.parentElement.insertBefore(wrap, anchor.nextSibling);
    } else {
      document.body.appendChild(wrap);
    }
    return wrap;
  }

  function verdictLabel(v) {
    switch (v) {
      case "good_deal":  return "✅ Bom negócio";
      case "bad_deal":   return "❌ Mau negócio";
      case "neutral":    return "➖ Neutro";
      case "unreliable": return "❓ Sem fiabilidade";
      default:           return v;
    }
  }

  function renderResult(listing, result) {
    const verdict = result.verdict || "unreliable";
    const verdictClass =
      verdict === "good_deal" ? "ok" :
      verdict === "bad_deal" ? "bad" :
      verdict === "neutral" ? "neutral" : "unreliable";
    const margin = (typeof result.profit_margin_percent === "number")
      ? `${result.profit_margin_percent.toFixed(1)}%` : "—";
    const market = (typeof result.estimated_market_price === "number")
      ? `${result.estimated_market_price.toFixed(0)}€` : "—";
    const reliability = (typeof result.reliability_score === "number")
      ? result.reliability_score.toFixed(2) : "—";
    const reasons = (result.reasons || [])
      .slice(0, 5)
      .map((r) => `<li>${escapeHtml(r)}</li>`)
      .join("");
    const sources = result.source_counts && Object.keys(result.source_counts).length
      ? Object.entries(result.source_counts)
          .map(([s, c]) => `${escapeHtml(s)}:${c}`).join(" · ")
      : null;
    const html = `
      <div class="olx-flip-card olx-flip-${verdictClass}">
        <div class="olx-flip-header">
          <span class="olx-flip-verdict">${verdictLabel(verdict)}</span>
          <span class="olx-flip-watch">${escapeHtml(result.watch_name || "")}</span>
        </div>
        <div class="olx-flip-grid">
          <div><b>Preço</b><br>${escapeHtml((listing.price ?? "—") + "€")}</div>
          <div><b>Mediana</b><br>${market}</div>
          <div><b>Margem</b><br>${margin}</div>
          <div><b>Fiabilidade</b><br>${reliability}</div>
          <div><b>Amostra</b><br>${result.filtered_sample_size ?? 0}/${result.sample_size ?? 0}</div>
          <div><b>Condição</b><br>${escapeHtml(result.condition || "unknown")}</div>
        </div>
        ${sources ? `<div class="olx-flip-sources">Fontes: ${sources}</div>` : ""}
        ${reasons ? `<ul class="olx-flip-reasons">${reasons}</ul>` : ""}
        <div class="olx-flip-footer">OLX Flip Evaluator · ${escapeHtml(result.match_type || "?")}</div>
      </div>`;
    renderOverlay(listing.priceAnchor, html);
  }

  function renderError(anchor, message, hint) {
    const html = `
      <div class="olx-flip-card olx-flip-error">
        <div class="olx-flip-header">
          <span class="olx-flip-verdict">⚠️ Não disponível</span>
        </div>
        <div class="olx-flip-error-msg">${escapeHtml(message)}</div>
        ${hint ? `<div class="olx-flip-footer">${escapeHtml(hint)}</div>` : ""}
      </div>`;
    renderOverlay(anchor, html);
  }

  function renderUnparsed() {
    // Non-intrusive notice when DOM isn't ready / selectors miss.
    renderOverlay(document.body, `
      <div class="olx-flip-card olx-flip-unreliable">
        <div class="olx-flip-header">
          <span class="olx-flip-verdict">❓ Não consegui ler o anúncio</span>
        </div>
        <div class="olx-flip-error-msg">A página mudou de layout. Reabre ou actualiza a extensão.</div>
      </div>`);
  }

  // ── Settings ─────────────────────────────────────────────────────────────
  function getSettings() {
    return new Promise((resolve) => {
      try {
        chrome.storage.sync.get({ backendUrl: "", apiToken: "" }, (cfg) => {
          resolve(cfg || {});
        });
      } catch (_) {
        resolve({});
      }
    });
  }

  // ── Evaluation pipeline ─────────────────────────────────────────────────
  async function callBackend(listing, settings) {
    const url = settings.backendUrl.replace(/\/+$/, "") + "/api/evaluate";
    const ctrl = new AbortController();
    activeAbort = ctrl;
    const timer = setTimeout(() => ctrl.abort("timeout"), REQUEST_TIMEOUT_MS);
    try {
      const r = await fetch(url, {
        method: "POST",
        signal: ctrl.signal,
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${settings.apiToken}`,
        },
        body: JSON.stringify({
          title: listing.title,
          price: listing.price,
          url: listing.url,
          condition: listing.condition,
          location: listing.location,
          brand: listing.brand,
        }),
      });
      return r;
    } finally {
      clearTimeout(timer);
      if (activeAbort === ctrl) activeAbort = null;
    }
  }

  async function evaluateNow() {
    if (!isListingPage()) return;

    // Throttle
    const now = Date.now();
    if (now - lastEvalAt < MIN_TIME_BETWEEN_EVALS_MS) return;

    const listing = readListing();
    if (!listing.title || listing.price === null) {
      // DOM not ready; the MutationObserver / retry loop will retry.
      return;
    }

    // Per-URL dedup
    if (evaluatedUrls.has(listing.url)) {
      // Already showed an overlay for this URL — don't refetch.
      return;
    }

    const settings = await getSettings();
    if (!settings.backendUrl || !settings.apiToken) {
      renderError(listing.priceAnchor,
        "Falta backend URL ou API token.",
        "Abre o popup da extensão para configurar.");
      // Mark as evaluated so we don't loop on this URL
      evaluatedUrls.add(listing.url);
      return;
    }

    lastEvalAt = now;
    evaluatedUrls.add(listing.url);

    let response;
    try {
      response = await callBackend(listing, settings);
    } catch (e) {
      if (e && e.name === "AbortError") {
        warn("evaluate aborted (timeout or navigation)");
        // Allow retry next time the URL changes
        evaluatedUrls.delete(listing.url);
        return;
      }
      renderError(listing.priceAnchor,
        `Erro de rede: ${e && e.message ? e.message : e}`,
        "Verifica que o backend está acessível.");
      return;
    }

    if (response.status === 401) {
      renderError(listing.priceAnchor,
        "Token inválido (401).",
        "Confere o EXTENSION_API_TOKEN no popup.");
      return;
    }
    if (response.status === 503) {
      renderError(listing.priceAnchor,
        "Backend desligou /api/evaluate (sem token configurado no servidor).",
        "Define EXTENSION_API_TOKEN no Render/.env.");
      return;
    }
    if (response.status === 400) {
      let msg = "Pedido inválido (400).";
      try { msg = (await response.json()).error || msg; } catch (_) {}
      renderError(listing.priceAnchor, msg);
      return;
    }
    if (!response.ok) {
      renderError(listing.priceAnchor, `HTTP ${response.status}`,
        "Verifica os logs do backend.");
      return;
    }

    let data;
    try {
      data = await response.json();
    } catch (_) {
      renderError(listing.priceAnchor, "Resposta inválida do backend.");
      return;
    }
    renderResult(listing, data);
    log("evaluated", listing.url, data.verdict);
  }

  // ── Retry-on-hydration loop ─────────────────────────────────────────────
  async function evaluateWithRetries() {
    for (let i = 0; i < PARSE_RETRY_MAX; i++) {
      const listing = readListing();
      if (listing.title && listing.price !== null) {
        await evaluateNow();
        return;
      }
      await new Promise((r) => setTimeout(r, PARSE_RETRY_INTERVAL_MS));
      // If URL changed mid-retry, abandon — onUrlChange will start fresh.
      if (location.href.split("?")[0].split("#")[0] !== currentUrl.split("?")[0].split("#")[0]) {
        return;
      }
    }
    // After max retries: only show "unparsed" if we are still on a listing page.
    if (isListingPage() && !document.getElementById(OVERLAY_ID)) {
      renderUnparsed();
    }
  }

  // ── SPA / DOM observers ─────────────────────────────────────────────────
  function onUrlChange() {
    const newUrl = location.href;
    if (newUrl === currentUrl) return;
    log("URL change", currentUrl, "→", newUrl);
    currentUrl = newUrl;
    // Cancel any in-flight request for the previous URL.
    if (activeAbort) {
      try { activeAbort.abort("url-change"); } catch (_) {}
      activeAbort = null;
    }
    clearOverlay();
    // We DON'T clear evaluatedUrls — it acts as a session-wide dedupe.
    // But back-navigation to a same URL reuses its overlay re-render via
    // evaluateWithRetries' `evaluatedUrls.has()` early return; if you want
    // a forced re-eval on back-navigation, uncomment:
    // evaluatedUrls.delete(newUrl.split("?")[0].split("#")[0]);
    if (isListingPage()) {
      scheduleDebouncedEval();
    }
  }

  function patchHistory() {
    const fire = () => window.dispatchEvent(new Event("olx-flip-locationchange"));
    const _push = history.pushState;
    history.pushState = function () {
      const r = _push.apply(this, arguments); fire(); return r;
    };
    const _replace = history.replaceState;
    history.replaceState = function () {
      const r = _replace.apply(this, arguments); fire(); return r;
    };
    window.addEventListener("popstate", fire);
    window.addEventListener("olx-flip-locationchange", onUrlChange);
  }

  function scheduleDebouncedEval() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      evaluateWithRetries();
    }, DEBOUNCE_MS);
  }

  function startMutationObserver() {
    if (observer) return;
    observer = new MutationObserver(() => {
      if (!isListingPage()) return;
      // Only fire if we haven't already evaluated this URL — saves a lot of
      // wasted work as OLX re-renders unrelated parts of the page.
      const u = location.href.split("?")[0].split("#")[0];
      if (evaluatedUrls.has(u)) return;
      scheduleDebouncedEval();
    });
    observer.observe(document.body || document.documentElement, {
      childList: true,
      subtree: true,
    });
  }

  // ── Boot ────────────────────────────────────────────────────────────────
  function boot() {
    patchHistory();
    startMutationObserver();
    if (isListingPage()) {
      scheduleDebouncedEval();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
