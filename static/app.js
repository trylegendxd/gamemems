// Dashboard front-end. Vanilla JS, no build step.

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

const filterEls = {
  search:    $("#f-search"),
  category:  $("#f-category"),
  minpct:    $("#f-minpct"),
  minprofit: $("#f-minprofit"),
  location:  $("#f-location"),
  sort:      $("#f-sort"),
  unseen:    $("#f-unseen"),
  favs:      $("#f-favs"),
  norisk:    $("#f-norisk"),
  noign:     $("#f-noignored"),
  noarc:     $("#f-noarchived"),
};

// ── helpers ─────────────────────────────────────────────────────────────────

function debounce(fn, ms = 250) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function fmtMoney(v) {
  if (v == null) return "—";
  return Math.round(v).toLocaleString("pt-PT") + "€";
}

function fmtPct(v) {
  if (v == null) return "—";
  return `${v}%`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "agora";
  if (diff < 3600) return `${Math.floor(diff/60)}min`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h`;
  return d.toLocaleDateString("pt-PT");
}

function buildQS() {
  const p = new URLSearchParams();
  if (filterEls.search.value)    p.set("search", filterEls.search.value.trim());
  if (filterEls.category.value)  p.set("category", filterEls.category.value);
  if (filterEls.minpct.value)    p.set("min_profit_percent", filterEls.minpct.value);
  if (filterEls.minprofit.value) p.set("min_profit", filterEls.minprofit.value);
  if (filterEls.location.value)  p.set("location", filterEls.location.value.trim());
  p.set("sort", filterEls.sort.value);
  if (filterEls.unseen.checked)  p.set("only_unseen", "1");
  if (filterEls.favs.checked)    p.set("only_favorites", "1");
  if (filterEls.norisk.checked)  p.set("hide_risky", "1");
  p.set("hide_ignored",  filterEls.noign.checked ? "1" : "0");
  p.set("hide_archived", filterEls.noarc.checked ? "1" : "0");
  return p;
}

// ── render ──────────────────────────────────────────────────────────────────

function renderDeals(deals) {
  const grid = $("#deals");
  grid.innerHTML = "";
  if (!deals.length) {
    grid.innerHTML = '<div class="empty">Sem deals que combinem com os filtros.</div>';
    return;
  }
  const tpl = $("#card-tpl");
  for (const d of deals) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = d.id;
    if (!d.seen) node.classList.add("unseen");

    const img = node.querySelector("img");
    if (d.image_url) {
      img.src = d.image_url; img.alt = d.title;
    } else {
      img.remove();
    }

    const pct = d.profit_percent;
    const pctEl = node.querySelector(".badge-pct");
    if (pct != null) {
      pctEl.textContent = `${pct}%`;
      // ≥30% keeps the default green styling (.badge-pct alone).
      // 20-30% gets the yellow modifier; <20% gets the muted modifier.
      if      (pct >= 30) { /* default green, no extra class */ }
      else if (pct >= 20) pctEl.classList.add("medium");
      else                pctEl.classList.add("low");
    } else {
      pctEl.classList.add("hidden");
    }

    if (d.risk_flags && d.risk_flags.length) {
      node.querySelector(".badge-risk").classList.remove("hidden");
      node.querySelector(".badge-risk").title = "Risco: " + d.risk_flags.join(", ");
    }

    node.querySelector(".title").textContent = d.title;
    node.querySelector(".cat").textContent   = d.category || "—";
    node.querySelector(".loc").textContent   = d.location || "—";
    node.querySelector(".price").textContent = fmtMoney(d.price);
    node.querySelector(".median").textContent = "med " + fmtMoney(d.estimated_value);
    node.querySelector(".profit").textContent =
      d.profit != null ? `+${fmtMoney(d.profit)} (${fmtPct(d.profit_percent)})` : "";

    const olx = node.querySelector('[data-action="olx"]');
    olx.href = d.url;

    const fav = node.querySelector('[data-action="favorite"]');
    if (d.favorite) fav.classList.add("active");

    node.querySelectorAll("[data-action]").forEach(btn => {
      btn.addEventListener("click", e => onCardAction(e, d));
    });

    grid.appendChild(node);
  }
}

async function onCardAction(e, deal) {
  e.preventDefault(); e.stopPropagation();
  const action = e.currentTarget.dataset.action;
  switch (action) {
    case "olx":      window.open(deal.url, "_blank"); break;
    case "detail":   window.location.href = `/deals/${deal.id}`; break;
    case "favorite": await togglePatch(deal.id, "favorite", !deal.favorite); break;
    case "ignore":   await togglePatch(deal.id, "ignored", !deal.ignored); break;
    case "archive":  await togglePatch(deal.id, "archived", true); break;
  }
}

async function togglePatch(id, key, value) {
  await fetch(`/api/deals/${id}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({[key]: value}),
  });
  loadDeals();
}

// ── data fetching ───────────────────────────────────────────────────────────

async function loadDeals() {
  try {
    const r = await fetch(`/api/deals?${buildQS()}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    renderDeals(j.deals);
  } catch (e) {
    console.error("loadDeals", e);
    $("#deals").innerHTML = `<div class="empty">Erro: ${e.message}</div>`;
  }
}

async function loadStats() {
  try {
    const r = await fetch("/api/stats");
    if (!r.ok) return;
    const s = await r.json();
    $("#s-total").textContent    = s.total_deals;
    $("#s-today").textContent    = s.deals_today;
    $("#s-avgpct").textContent   = `${s.avg_profit_percent || 0}%`;
    $("#s-active").textContent   = fmtMoney(s.estimated_active_profit);
    $("#s-favs").textContent     = s.favorites;
    $("#s-ignored").textContent  = s.ignored;
    $("#s-archived").textContent = s.archived;
    $("#s-lastscan").textContent = fmtTime(s.scraper.last_finished);
    updateScraperBadge(s.scraper);
  } catch (e) { console.error("loadStats", e); }
}

function updateScraperBadge(s) {
  const dot = $("#scraper-status .dot");
  const txt = $("#scraper-status .status-text");
  dot.className = "dot dot-" + (s.status || "idle");
  let label = s.status || "—";
  if (s.is_scraping) label = "scan a correr…";
  else if (s.last_finished) label = "última scan " + fmtTime(s.last_finished);
  if (s.last_error) label = "erro: " + s.last_error;
  txt.textContent = label;
}

// ── events ──────────────────────────────────────────────────────────────────

function bindFilters() {
  const debLoad = debounce(loadDeals, 300);
  for (const k in filterEls) {
    const el = filterEls[k];
    el.addEventListener("input", debLoad);
    el.addEventListener("change", debLoad);
  }
  $("#btn-clear").addEventListener("click", () => {
    filterEls.search.value = ""; filterEls.category.value = "";
    filterEls.minpct.value = ""; filterEls.minprofit.value = "";
    filterEls.location.value = ""; filterEls.sort.value = "newest";
    filterEls.unseen.checked = false; filterEls.favs.checked = false;
    filterEls.norisk.checked = false;
    filterEls.noign.checked = true; filterEls.noarc.checked = true;
    loadDeals();
  });
  $("#btn-run-now").addEventListener("click", async () => {
    const r = await fetch("/api/scraper/run-now", {method: "POST"});
    const j = await r.json();
    if (!j.triggered) alert("Já há um scan em curso.");
    setTimeout(() => { loadDeals(); loadStats(); }, 2000);
  });
}

function tickFooter() {
  $("#last-refresh").textContent = new Date().toLocaleTimeString("pt-PT");
}

async function refreshAll() {
  await Promise.all([loadDeals(), loadStats()]);
  tickFooter();
}

// ── boot ────────────────────────────────────────────────────────────────────

bindFilters();
refreshAll();
setInterval(refreshAll, 30_000);
