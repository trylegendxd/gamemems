/**
 * OLX Flip Evaluator — content script.
 *
 * On any OLX listing detail page:
 *   1. Reads the visible title, price, condition, and location.
 *   2. Calls POST <BACKEND>/api/evaluate with an EXTENSION_API_TOKEN.
 *   3. Renders an overlay card next to the price with the verdict and reasons.
 *
 * Configuration is in chrome.storage.sync (see popup.html / popup.js).
 *
 * Heuristic rule of thumb: this only runs on pages that look like a single
 * listing (path contains "/d/anuncio/"). Search-result pages are skipped to
 * avoid spamming the backend.
 */
(function () {
  "use strict";

  const LISTING_PATH_RE = /\/d\/anuncio\//;
  if (!LISTING_PATH_RE.test(window.location.pathname)) return;

  const OVERLAY_ID = "olx-flip-evaluator-overlay";
  if (document.getElementById(OVERLAY_ID)) return;

  function readListing() {
    const titleEl =
      document.querySelector('[data-cy="ad_title"]') ||
      document.querySelector("h1") ||
      document.querySelector("h4");
    const priceEl =
      document.querySelector('[data-testid="ad-price-container"]') ||
      document.querySelector('[data-cy="ad-price"]') ||
      document.querySelector('h3 strong, .css-12vqlj3, [aria-label*="Preço"]');
    const locationEl =
      document.querySelector('[data-testid="map-aside-section"] p') ||
      document.querySelector('a[href*="cidade"]') ||
      document.querySelector('p[class*="locationDate"]');
    const conditionLabel = Array.from(document.querySelectorAll("p, li, span"))
      .find((el) => /(estado|condição|condicao)/i.test(el.textContent || ""));

    const rawTitle = (titleEl && titleEl.textContent || "").trim();
    const rawPrice = (priceEl && priceEl.textContent || "").trim();
    const rawLoc = (locationEl && locationEl.textContent || "").trim();
    const rawCondition = (conditionLabel && conditionLabel.textContent || "").trim();

    // Parse a "1.234,56 €" or "1234 €" string into a float
    let price = null;
    const m = rawPrice.match(/([\d.\s ]+),?(\d{1,2})?\s*€/);
    if (m) {
      const ints = m[1].replace(/[\s. ]/g, "");
      const decs = m[2] ? `.${m[2]}` : "";
      const n = parseFloat(ints + decs);
      if (Number.isFinite(n) && n > 0) price = n;
    }

    let condition = "unknown";
    const cl = rawCondition.toLowerCase();
    if (/(novo|selado|nunca usado)/.test(cl)) condition = "new";
    else if (/(como novo|seminovo|quase novo)/.test(cl)) condition = "like_new";
    else if (/(usado|usada)/.test(cl)) condition = "used";

    return {
      title: rawTitle,
      price,
      url: window.location.href.split("?")[0],
      condition,
      location: rawLoc,
      priceAnchor: priceEl,
    };
  }

  async function getSettings() {
    return new Promise((resolve) => {
      try {
        chrome.storage.sync.get(
          { backendUrl: "", apiToken: "" },
          (cfg) => resolve(cfg || {})
        );
      } catch (_) {
        resolve({});
      }
    });
  }

  function renderOverlay(anchor, content) {
    const wrap = document.createElement("div");
    wrap.id = OVERLAY_ID;
    wrap.className = "olx-flip-overlay";
    wrap.innerHTML = content;
    if (anchor && anchor.parentElement) {
      anchor.parentElement.insertBefore(wrap, anchor.nextSibling);
    } else {
      document.body.appendChild(wrap);
    }
    return wrap;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderResult(anchor, listing, result) {
    const verdict = result.verdict || "unreliable";
    const verdictClass =
      verdict === "good_deal" ? "ok" :
      verdict === "bad_deal" ? "bad" :
      verdict === "neutral" ? "neutral" : "unreliable";
    const margin = (typeof result.profit_margin_percent === "number")
      ? `${result.profit_margin_percent.toFixed(1)}%`
      : "—";
    const market = (typeof result.estimated_market_price === "number")
      ? `${result.estimated_market_price.toFixed(0)}€`
      : "—";
    const reliability = (typeof result.reliability_score === "number")
      ? result.reliability_score.toFixed(2)
      : "—";
    const reasons = (result.reasons || [])
      .slice(0, 5)
      .map((r) => `<li>${escapeHtml(r)}</li>`)
      .join("");
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
        ${reasons ? `<ul class="olx-flip-reasons">${reasons}</ul>` : ""}
        <div class="olx-flip-footer">OLX Flip Evaluator · ${escapeHtml(result.match_type || "?")}</div>
      </div>`;
    renderOverlay(listing.priceAnchor, html);
  }

  function renderError(anchor, message) {
    renderOverlay(anchor, `
      <div class="olx-flip-card olx-flip-error">
        <div class="olx-flip-header">
          <span class="olx-flip-verdict">⚠️ Não disponível</span>
        </div>
        <div class="olx-flip-error-msg">${escapeHtml(message)}</div>
        <div class="olx-flip-footer">Configura o backend no popup da extensão.</div>
      </div>
    `);
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

  async function main() {
    const listing = readListing();
    if (!listing.title || listing.price === null) {
      // Not enough info — bail quietly.
      return;
    }
    const settings = await getSettings();
    if (!settings.backendUrl || !settings.apiToken) {
      renderError(listing.priceAnchor, "Falta backend URL / API token.");
      return;
    }

    const url = settings.backendUrl.replace(/\/+$/, "") + "/api/evaluate";
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
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
        }),
      });
    } catch (e) {
      renderError(listing.priceAnchor, `Erro de rede: ${e.message || e}`);
      return;
    }

    if (response.status === 401) {
      renderError(listing.priceAnchor, "Token inválido (401). Verifica o popup.");
      return;
    }
    if (response.status === 503) {
      renderError(listing.priceAnchor, "Backend desligou /api/evaluate (sem token).");
      return;
    }
    if (!response.ok) {
      renderError(listing.priceAnchor, `HTTP ${response.status}`);
      return;
    }
    let data;
    try {
      data = await response.json();
    } catch (e) {
      renderError(listing.priceAnchor, "Resposta inválida do backend.");
      return;
    }
    renderResult(listing.priceAnchor, listing, data);
  }

  // Wait for OLX hydration to settle. The selectors come and go for ~1s.
  setTimeout(main, 1200);
})();
