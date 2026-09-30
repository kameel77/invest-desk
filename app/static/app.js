/* Trading Desk dashboard — vanilla JS, no build step. */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Number(n).toLocaleString("pl-PL", { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (n) => (n === null || n === undefined) ? "—" : `${n >= 0 ? "+" : ""}${fmt(n)}%`;
const sign = (n) => (n >= 0 ? "pos" : "neg");
const ts2date = (ts) => new Date(ts * 1000).toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

let chartSymbol = null;
let lastScan = null;
let chartBars = [];       // bars currently drawn
let chartGeom = null;     // {x(i), y(v), slot, n} — needed to map mouse x -> bar
let hoverIndex = -1;
let chartRange = 200;
// "intl": green = up (TradingView, US). "pl": green = down (GPW, Europe).
let candlePalette = { up: "#3fb950", down: "#f85149" };
const PALETTES = {
  intl: { up: "#3fb950", down: "#f85149" },
  pl: { up: "#f85149", down: "#3fb950" },
};

function setPalette(name) {
  candlePalette = PALETTES[name] || PALETTES.intl;
  try { localStorage.setItem("tdPalette", name); } catch (e) { /* private mode */ }
  drawPrice(chartBars);
}

/** Open the fullscreen chart modal for `sym`. */
function showChart(sym) {
  chartSymbol = sym;
  hoverIndex = -1;
  switchModalTab("price");
  $("#chartModal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#modalTitle").textContent = sym;
  $("#modalSub").textContent = "wczytywanie…";
  loadChart();
}

function closeModal() {
  $("#chartModal").hidden = true;
  document.body.style.overflow = "";
}

function switchModalTab(name) {
  $$(".mtab").forEach((t) => t.classList.toggle("active", t.dataset.mtab === name));
  $$(".mtab-pane").forEach((p) => p.classList.toggle("active", p.id === `mpane-${name}`));
  if (name === "analysis") loadModalAnalysis();
  if (name === "fund" && typeof loadFundamentals === "function") loadFundamentals();
  // The canvas was hidden while another tab was active: clientWidth was 0,
  // so it must be redrawn once visible.
  if (name === "price") drawPrice(chartBars);
}

function compRow(name, value) {
  const v = Math.max(-1, Math.min(1, value || 0));
  const w = Math.abs(v) * 50;
  const left = v >= 0 ? 50 : 50 - w;
  return `<div class="comp">
    <span class="name">${name}</span>
    <div class="bar"><i class="${v >= 0 ? "p" : "n"}" style="left:${left}%;width:${w}%"></i><span class="mid"></span></div>
    <span class="num ${v >= 0 ? "pos" : "neg"}">${fmt(v, 2)}</span>
  </div>`;
}

async function loadModalAnalysis() {
  const box = $("#analysisBody");
  if (!chartSymbol) return;
  box.innerHTML = '<p class="muted">Ładowanie…</p>';
  try {
    const a = await api(`/api/analysis/${encodeURIComponent(chartSymbol)}`);
    const ev = await api("/api/fundamentals/" + encodeURIComponent(chartSymbol)).catch(() => ({}));
    const stat = (lab, val, sub) => `<div class="stat"><div class="lab">${lab}</div>`
      + `<div class="val">${val}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
    box.innerHTML = `
      <div class="stat-row">
        ${stat("Score", `<span class="${a.score >= 0 ? "pos" : "neg"}">${fmt(a.score, 1)}</span>`,
      "próg wejścia 35 / wyjścia -25")}
        ${stat("RSI(14)", fmt(a.rsi, 1), a.rsi > 70 ? "wykupienie" : a.rsi < 30 ? "wyprzedanie" : "neutralnie")}
        ${stat("ROC 20d", `<span class="${a.roc_fast >= 0 ? "pos" : "neg"}">${pct(a.roc_fast)}</span>`, "momentum")}
        ${stat("ROC 60d", `<span class="${a.roc_slow >= 0 ? "pos" : "neg"}">${pct(a.roc_slow)}</span>`, "trend średni")}
        ${stat("ATR", fmt(a.atr, 3), `ATR% ${fmt((a.atr / a.price) * 100, 2)}`)}
        ${stat("Wol. ann.", fmt(a.vol_ann, 1) + "%", "zmienność roczna")}
      </div>
      <div class="split" style="margin-top:16px">
        <div class="fund-box">
          <h3>Komponenty score</h3>
          <div class="comps">
            ${compRow("Trend", a.components.trend)}
            ${compRow("Momentum", a.components.momentum)}
            ${compRow("Rewersja", a.components.reversion)}
          </div>
        </div>
        <div class="fund-box">
          <h3>Średnie i poziomy</h3>
    <div class="row"><span>SMA20</span><b>${fmt(a.sma20)}</b></div>
          <div class="row"><span>SMA50</span><b>${fmt(a.sma50)}</b></div>
          <div class="row"><span>SMA200</span><b>${fmt(a.sma200)}</b></div>
          <div class="row"><span>Stop (2×ATR)</span><b>${fmt(a.suggested_stop)}</b></div>
          <div class="row"><span>Cel (3×ATR)</span><b>${fmt(a.suggested_target)}</b></div>
          <div class="row"><span>Płynność (ADV)</span><b class="${a.liquidity_ok ? "pos" : "neg"}">${a.liquidity_ok ? "OK" : "niska"}</b></div>
        </div>
      </div>
      <div class="fund-box" style="margin-top:16px">
        <h3>Uzasadnienie modelu</h3>
        ${(a.reasons || []).map((r) => `<div class="row"><span>• ${r}</span><b></b></div>`).join("")}
      </div>
      ${ev.available ? `<div class="fund-box" style="margin-top:16px">
        <h3>Kontekst fundamentalny</h3>
     <div class="row"><span>C/Z trailing</span><b>${fmt(ev.trailing_pe)}</b></div>
        <div class="row"><span>C/Z forward</span><b>${fmt(ev.forward_pe)}</b></div>
        <div class="row"><span>Marża netto</span><b>${ev.profit_margin != null ? fmt(ev.profit_margin * 100, 1) + "%" : "—"}</b></div>
        <div class="row"><span>Beta</span><b>${fmt(ev.beta, 2)}</b></div>
        <div class="row"><span>Rekomendacja</span><b>${ev.recommendation || "—"}</b></div>
        <div class="row"><span>Cel analityków</span><b>${fmt(ev.target_mean)}</b></div>
      </div>` : ""}`;
  } catch (e) {
    box.innerHTML = `<p class="muted">Brak analizy: ${e.message}</p>`;
  }
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), 3800);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.message || `HTTP ${r.status}`);
  return data;
}

function table(el, cols, rows, opts = {}) {
  const head = cols.map((c) => `<th class="${c.num ? "num" : ""}">${c.label}</th>`).join("");
  const body = rows.map((r) => {
    const tds = cols.map((c) => {
      const v = c.render ? c.render(r) : (r[c.key] ?? "—");
      return `<td class="${c.num ? "num" : ""} ${c.cls ? c.cls(r) : ""}">${v}</td>`;
    }).join("");
    const attrs = opts.onRow ? ` class="clickable" data-sym="${r.symbol}"` : "";
    return `<tr${attrs}>${tds}</tr>`;
  }).join("");
  el.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length}" class="muted">brak danych</td></tr>`}</tbody>`;
  if (opts.onRow) el.querySelectorAll("tr[data-sym]").forEach((tr) => {
    tr.onclick = () => opts.onRow(tr.dataset.sym);
  });
}

function scoreBar(score) {
  const pctv = Math.max(-100, Math.min(100, score || 0));
  const w = Math.abs(pctv) / 2;
  const left = pctv >= 0 ? 50 : 50 - w;
  return `<div class="bar"><i class="${pctv >= 0 ? "p" : "n"}" style="left:${left}%;width:${w}%"></i><span class="mid"></span></div>`;
}

/* --- rendering --------------------------------------------------------- */

function renderAccount(a) {
  $("#accountStrip").innerHTML = `
    <div class="stat"><b class="${sign(a.total_return_pct)}">${fmt(a.equity)} PLN</b><span>Equity</span></div>
    <div class="stat"><b>${fmt(a.cash)}</b><span>Gotówka</span></div>
    <div class="stat"><b class="${sign(a.total_return_pct)}">${pct(a.total_return_pct)}</b><span>Zwrot</span></div>
    <div class="stat"><b>${a.open_positions}</b><span>Pozycje</span></div>`;
  if (typeof renderAccountStats === "function") renderAccountStats(a);
}

async function loadAccount() {
  const a = await api("/api/account");
  renderAccount(a);
  return a;
}

function drawEquity(curve) {
  const c = $("#equityChart");
  const dpr = window.devicePixelRatio || 1;
  c.width = c.clientWidth * dpr; c.height = 220 * dpr;
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  const W = c.clientWidth, H = 220;
  ctx.clearRect(0, 0, W, H);
  if (curve.length < 2) {
    ctx.fillStyle = "#8b98a5"; ctx.font = "13px sans-serif";
    ctx.fillText("Brak historii equity — uruchom skan, aby zasilić konto.", 16, 40);
    return;
  }
  const vals = curve.map((p) => p.equity);
  const min = Math.min(...vals), max = Math.max(...vals);
  const pad = (max - min) * 0.15 || 1;
  const lo = min - pad, hi = max + pad;
  const x = (i) => (i / (curve.length - 1)) * (W - 8) + 4;
  const y = (v) => H - 18 - ((v - lo) / (hi - lo)) * (H - 32);

  ctx.strokeStyle = "#26303d"; ctx.lineWidth = 1;
  for (let g = 0; g <= 3; g++) {
    const gy = 18 + (g * (H - 36)) / 3;
    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
  }
  const up = vals[vals.length - 1] >= vals[0];
  ctx.strokeStyle = up ? "#3fb950" : "#f85149"; ctx.lineWidth = 2;
  ctx.beginPath();
  vals.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.stroke();
  ctx.lineTo(x(vals.length - 1), H); ctx.lineTo(x(0), H); ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, up ? "rgba(63,185,80,.22)" : "rgba(248,81,73,.22)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = grad; ctx.fill();
  ctx.fillStyle = "#8b98a5"; ctx.font = "11px sans-serif";
  ctx.fillText(hi.toFixed(0), 4, 14); ctx.fillText(lo.toFixed(0), 4, H - 4);
}

async function loadEquity() {
  const { curve } = await api("/api/equity?limit=500");
  drawEquity(curve);
  $("#equityHint").textContent = curve.length
    ? `${curve.length} punktów · ${ts2date(curve[0].ts)} → ${ts2date(curve[curve.length - 1].ts)}`
    : "—";
}

async function loadSignals() {
  const side = $("#sigSideFilter") ? $("#sigSideFilter").value : "";
  const { signals: all } = await api("/api/signals?limit=200");
  const sigs = side ? all.filter((s) => s.side === side) : all;
  table($("#signalsTable"), [
    { label: "Czas", render: (s) => ts2date(s.ts) },
    { label: "Symbol", render: (s) => `<b>${s.symbol}</b>` },
    { label: "Hipoteza", render: (s) => {
        const h = (s.meta || {}).hypothesis || "trend";
        return h === "reversion"
          ? '<span class="pill" style="color:#7ee787;border-color:#2ea043">rewersja</span>'
          : '<span class="pill">trend</span>';
      } },
    { label: "Kierunek", render: (s) => `<span class="badge ${s.side === "BUY" ? "buy" : "sell"}">${s.side}</span>` },
    { label: "Score", num: true, render: (s) => `<span class="scorenum ${sign(s.score)}">${fmt(s.score, 1)}</span>` },
    { label: "Cena", num: true, render: (s) => fmt(s.price) },
    { label: "Strategia", render: (s) => `<span class="pill">${s.strategy || "—"}</span>` },
    { label: "Powody", render: (s) => `<span class="muted">${(s.reasons || []).slice(0, 2).join(" · ")}</span>` },
    { label: "Wykonany", render: (s) => (s.acted ? "✓" : "—") },
  ], sigs, {
    onRow: showChart,
  });

  const recent = sigs.slice(0, 8);
  $("#topSignalsMeta").textContent = recent.length ? `ostatnie ${recent.length}` : "";
  table($("#topSignals"), [
    { label: "Symbol", render: (s) => `<b>${s.symbol}</b>` },
    { label: "Kierunek", render: (s) => `<span class="badge ${s.side === "BUY" ? "buy" : "sell"}">${s.side}</span>` },
    { label: "Score", num: true, render: (s) => `<span class="scorenum ${sign(s.score)}">${fmt(s.score, 1)}</span>` },
    { label: "Wizualnie", render: (s) => scoreBar(s.score) },
    { label: "Czas", render: (s) => `<span class="muted">${ts2date(s.ts)}</span>` },
  ], recent);
}

async function loadPositions() {
  const a = await api("/api/account");
  renderAccount(a);
  // Lots are rendered first and separately: the aggregate position below is a
  // sum of these, so reading the position first hides which single call was
  // right — averaging a +11% tranche against two losers reads as "flat".
  const { lots, stats } = await api("/api/lots");
  const openLots = (a && a.lots) || [];
  const allLots = [...openLots, ...(lots || [])].sort((x, y) =>
    (y.opened_ts || 0) - (x.opened_ts || 0));
  table($("#lotsTable"), [
    { label: "#", render: (l) => `<span class="pill">${l.lot_id ?? "—"}</span>` },
    { label: "Symbol", render: (l) => `<b>${l.symbol}</b>` },
    { label: "Hipoteza", render: (l) => {
        const h = l.hypothesis || "trend";
        return h === "reversion"
          ? '<span class="pill" style="color:#7ee787;border-color:#2ea043">rewersja</span>'
          : '<span class="pill">trend</span>';
      } },
    { label: "Wejście", num: true, render: (l) => fmt(l.entry_price) },
    { label: "Wyjście", num: true, render: (l) => l.exit_price ? fmt(l.exit_price) : "—" },
    { label: "PnL", num: true, render: (l) => {
        const closed = l.closed_ts || l.exit_price;
        const v = closed ? l.realized_pnl : l.unrealized_pnl;
        const p = closed ? l.hold_pct : l.unrealized_pct;
        return `<span class="${sign(v)}">${fmt(v)} (${pct(p)})</span>`;
      } },
    { label: "Stop / Cel", num: true, render: (l) => `${fmt(l.stop)} / ${fmt(l.target)}` },
    { label: "Status", render: (l) => l.closed_ts
        ? `<span class="pill">${l.exit_reason || "zamknieta"}</span>`
        : `<span class="hint">${l.bars_held ?? 0}/${l.max_hold ?? 20} dni</span>` },
    { label: "Akcja", render: (l) => l.closed_ts
        ? "—"
        : `<button class="btn sm" onclick="closeLot(${l.lot_id})">Zamknij transzę</button>` },
  ], allLots, { onRow: showChart });

  const byHyp = (stats && stats.by_hypothesis) || [];
  const hint = $("#lotsHint");
  if (hint) {
    hint.textContent = byHyp.length
      ? byHyp.map(s => `${s.hypothesis}: ${s.trades} transzy, hit ${s.hit_rate_pct}%, razem ${fmt(s.total_pnl)}`).join("  |  ")
      : "wynik liczony osobno dla każdego wejścia";
  }
  table($("#positionsTable"), [
    { label: "Symbol", render: (p) => `<b>${p.symbol}</b>` },
    { label: "Ilość", num: true, render: (p) => fmt(p.qty, 4) },
    { label: "Średnia", num: true, render: (p) => fmt(p.avg_price) },
    { label: "Ostatnia", num: true, render: (p) => fmt(p.last_price) },
    { label: "Wartość", num: true, render: (p) => fmt(p.value) },
    { label: "PnL", num: true, render: (p) => `<span class="${sign(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)} (${pct(p.unrealized_pct)})</span>` },
    { label: "Stop", num: true, render: (p) => fmt(p.stop) },
    { label: "Cel", num: true, render: (p) => fmt(p.target) },
    { label: "Akcja", render: (p) => `<button class="btn sm" onclick="closePos('${p.symbol}')">Zamknij</button>` },
  ], a.positions, { onRow: showChart });

  const { orders } = await api("/api/orders?limit=50");
  table($("#ordersTable"), [
    { label: "Czas", render: (o) => ts2date(o.ts) },
    { label: "Symbol", render: (o) => `<b>${o.symbol}</b>` },
    { label: "Strona", render: (o) => `<span class="badge ${o.side === "BUY" ? "buy" : "sell"}">${o.side}</span>` },
    { label: "Ilość", num: true, render: (o) => fmt(o.qty, 4) },
    { label: "Status", render: (o) => (o.status === "FILLED" ? "Wypełnione" : o.status) },
    { label: "PnL", num: true, render: (o) => `<span class="${sign(o.realized_pnl)}">${fmt(o.realized_pnl)}</span>` },
    { label: "Powód", render: (o) => `<span class="muted">${o.reason || "—"}</span>` },
  ], orders);
}

window.closePos = async (sym) => {
  if (!confirm(`Zamknij pozycję ${sym}?`)) return;
  const r = await api("/api/orders", { method: "POST", body: JSON.stringify({ symbol: sym, side: "SELL" }) });
  toast(r.status === "FILLED" ? `Sprzedano ${sym}, PnL ${fmt(r.realized_pnl)}` : `Odrzucone: ${r.reason}`, r.status !== "FILLED");
  loadPositions(); loadAccount();
};

// Close exactly one transaction. The other lots of the same symbol stay open —
// each was a separate decision with its own stop and target, so closing one
// must not silently close the rest.
window.closeLot = async (lotId) => {
  if (!confirm(`Zamknac transze #${lotId}? Pozostale transze tego waloru zostana otwarte.`)) return;
  const r = await api("/api/orders", { method: "POST", body: JSON.stringify({ symbol: "", side: "SELL", lot_id: lotId, reason: "manual" }) });
  if (r.status === "FILLED") {
    toast(`Transza #${lotId} zamknieta: ${fmt(r.realized_pnl)} (${pct(r.hold_pct)})`);
  } else {
    toast(`Odrzucone: ${r.reason}`, true);
  }
  loadPositions(); loadAccount();
};

// Decision journal. Append-only and never reset: a declined call is as much a
// record of the strategy's thinking as one that became an order, and losing
// it would make the system impossible to audit after the fact.
const ACTION_LABEL = { SIGNAL: "sygnał", FILLED: "wykonane", REJECTED: "odrzucone",
                       EXIT: "wyjście", SKIPPED: "pominięte" };

async function loadDecisions() {
  const { decisions, total, counts } = await api("/api/decisions?limit=300");
  const hint = $("#decisionsHint");
  if (hint) {
    hint.textContent = `${total} wpisow  |  ` +
      Object.entries(counts).map(([k, v]) => `${ACTION_LABEL[k] || k}: ${v}`).join(", ");
  }
  table($("#decisionsTable"), [
    { label: "Czas", render: (d) => ts2date(d.ts) },
    { label: "Akcja", render: (d) => `<span class="pill">${ACTION_LABEL[d.action] || d.action}</span>` },
    { label: "Symbol", render: (d) => d.symbol ? `<b>${d.symbol}</b>` : "—" },
    { label: "Hipoteza", render: (d) => d.hypothesis === "reversion" ? "rewersja" : "trend" },
    { label: "Kierunek", render: (d) => d.side ? `<span class="badge ${d.side === "BUY" ? "buy" : "sell"}">${d.side}</span>` : "—" },
    { label: "Cena", num: true, render: (d) => fmt(d.price) },
    { label: "Ilość", num: true, render: (d) => fmt(d.qty, 4) },
    { label: "PnL", num: true, render: (d) => d.realized_pnl
        ? `<span class="${sign(d.realized_pnl)}">${fmt(d.realized_pnl)}</span>` : "—" },
    { label: "Uzasadnienie", render: (d) => {
        const r = (d.reasons && d.reasons.length) ? d.reasons.join("; ") : (d.reason || "");
        const z = d.state && d.state.z !== undefined && d.state.z !== null ? ` z=${d.state.z}` : "";
        return `<span class="muted">${(r + z).slice(0, 70)}</span>`;
      } },
  ], decisions, { onRow: showChart });
}

async function loadMarket() {
  const { quotes } = await api("/api/quotes");
  table($("#quotesTable"), [
    { label: "Symbol", render: (q) => `<b>${q.symbol}</b>` },
    { label: "Rynek", render: (q) => `<span class="pill">${q.currency || "—"}</span>` },
    { label: "Giełda", render: (q) => `<span class="muted">${q.exchange || "—"}</span>` },
    { label: "Cena", num: true, render: (q) => fmt(q.price) },
    { label: "Zmiana", num: true, render: (q) => `<span class="${sign(q.change_pct)}">${pct(q.change_pct)}</span>` },
    { label: "Źródło", render: (q) => `<span class="muted">${q.source || "—"}</span>` },
    { label: "Aktualizacja", render: (q) => `<span class="muted">${ts2date(q.ts)}</span>` },
  ], quotes, { onRow: showChart });
  if (!chartSymbol && quotes.length) chartSymbol = quotes[0].symbol;
}

async function loadChart() {
  if (!chartSymbol) return;
  $("#modalSub").textContent = "wczytywanie…";
  try {
    const d = await api(`/api/candles/${encodeURIComponent(chartSymbol)}?limit=${chartRange}`);
    chartBars = d.bars;
    drawPrice(chartBars);
    setReadout(chartBars.length ? chartBars.length - 1 : -1);
    const q = (await api("/api/quotes")).quotes.find((x) => x.symbol === chartSymbol);
    const bits = [];
    if (q) bits.push(`${fmt(q.price)} ${q.currency || ""} · ${pct(q.change_pct)} · ${q.exchange || ""} · ${ts2date(q.ts)}`);
    bits.push(`${d.count} świec dziennych`);
    $("#modalSub").textContent = bits.join("  ·  ");
  } catch (e) {
    $("#modalSub").textContent = "błąd wczytywania";
    drawPrice([]);
    toast(`Wykres ${chartSymbol}: ${e.message}`, true);
  }
}

function drawPrice(bars) {
  const c = $("#priceChart");
  const dpr = window.devicePixelRatio || 1;
  const H = Math.max(300, Math.min(560, window.innerHeight - 250));
  c.width = c.clientWidth * dpr; c.height = H * dpr;
  const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const W = c.clientWidth;
  ctx.clearRect(0, 0, W, H);
  chartGeom = null;
  if (!bars || bars.length < 2) {
    ctx.fillStyle = "#8b98a5"; ctx.font = "13px sans-serif";
    ctx.fillText("Brak danych historycznych dla tego instrumentu.", 16, 30);
    return;
  }

  const VOL_H = Math.round(H * 0.17);
  const PRICE_H = H - VOL_H - 34;
  const VOL_TOP = PRICE_H + 18;
  const n = bars.length;
  const highs = bars.map((b) => b.h), lows = bars.map((b) => b.l);
  const maxVol = Math.max(...bars.map((b) => b.v || 0)) || 1;
  let hi = Math.max(...highs), lo = Math.min(...lows);
  const pad = (hi - lo) * 0.06 || hi * 0.02 || 1;
  hi += pad; lo -= pad;

  const slot = W / n;
  const bw = Math.max(1, Math.min(9, slot * 0.62));
  const x = (i) => 4 + i * slot + slot / 2;
  const y = (v) => PRICE_H - 6 - ((v - lo) / (hi - lo)) * (PRICE_H - 26);
  const vy = (v) => VOL_TOP + VOL_H - (v / maxVol) * VOL_H;
  chartGeom = { x, y, vy, slot, n, lo, hi, PRICE_H, VOL_TOP, VOL_H, W, bw };

  // grid + price axis
  ctx.strokeStyle = "#1e2530"; ctx.lineWidth = 1;
  ctx.fillStyle = "#8b98a5"; ctx.font = "10px sans-serif";
  for (let g = 0; g <= 4; g++) {
    const gy = 14 + (g * (PRICE_H - 30)) / 4;
    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
    const val = hi - (g * (hi - lo)) / 4;
    ctx.fillText(val.toFixed(2), 3, gy - 3);
  }

  // Candles are coloured by their own open→close direction, which is what
  // a green/red convention means. The palette decides which colour is "up":
  // green=up internationally, green=down on GPW/Warsaw.
  bars.forEach((b, i) => {
    const col = b.c >= b.o ? candlePalette.up : candlePalette.down;
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(i), y(b.h)); ctx.lineTo(x(i), y(b.l));
    ctx.stroke();
    const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
    ctx.fillRect(x(i) - bw / 2, top, bw, Math.max(1, bot - top));
  });

  // volume
  bars.forEach((b, i) => {
    const base = b.c >= b.o ? candlePalette.up : candlePalette.down;
    ctx.fillStyle = base + "59";  // ~35% alpha
    const vh = (b.v / maxVol) * VOL_H;
    ctx.fillRect(x(i) - bw / 2, VOL_TOP + VOL_H - vh, bw, vh);
  });
  ctx.strokeStyle = "#1e2530";
  ctx.beginPath(); ctx.moveTo(0, VOL_TOP + VOL_H + 1); ctx.lineTo(W, VOL_TOP + VOL_H + 1); ctx.stroke();
  ctx.fillStyle = "#8b98a5"; ctx.font = "10px sans-serif";
  ctx.fillText("Wolumen", 4, VOL_TOP + 10);

  // date axis: first, middle, last
  ctx.fillStyle = "#8b98a5"; ctx.font = "10px sans-serif";
  const label = (i, anchor) => {
    const d = new Date(bars[i].ts * 1000);
    const t = `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.${d.getFullYear()}`;
    ctx.textAlign = anchor;
    ctx.fillText(t, i === 0 ? 4 : (i === n - 1 ? W - 4 : W / 2), H - 6);
    ctx.textAlign = "left";
  };
  label(0, "left"); label(Math.floor(n / 2), "center"); label(n - 1, "right");
  if (hoverIndex >= 0 && hoverIndex < n) drawCrosshair(ctx, hoverIndex);
}

/** Vertical crosshair + price tag on the hovered bar. */
function drawCrosshair(ctx, i) {
  const g = chartGeom;
  if (!g) return;
  const b = chartBars[i];
  if (!b) return;
  const cx = g.x(i);
  ctx.save();
  ctx.strokeStyle = "#4c9aff";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(cx, 6); ctx.lineTo(cx, g.VOL_TOP + g.VOL_H); ctx.stroke();
  ctx.setLineDash([]);

  const cy = g.y(b.c);
  ctx.beginPath(); ctx.arc(cx, cy, 3.5, 0, Math.PI * 2);
  ctx.fillStyle = b.c >= b.o ? candlePalette.up : candlePalette.down; ctx.fill();
  // price tag pinned to the right edge
  const tag = fmt(b.c);
  ctx.font = "10px sans-serif";
  const w = ctx.measureText(tag).width + 10;
  ctx.fillStyle = "#1f2732";
  ctx.fillRect(g.W - w - 2, cy - 8, w, 16);
  ctx.strokeStyle = "#26303d";
  ctx.strokeRect(g.W - w - 2, cy - 8, w, 16);
  ctx.fillStyle = "#e6edf3";
  ctx.textAlign = "center";
  ctx.fillText(tag, g.W - w / 2 - 2, cy + 3.5);
  ctx.textAlign = "left";
  ctx.restore();
}

/** Fill the OHLCV readout strip for bar `i` (or clear it with -1). */
function setReadout(i) {
  const b = chartBars[i];
  if (!b) {
    ["rdDate", "rdO", "rdH", "rdL", "rdC", "rdV", "rdChg", "rdChgPct", "rdChgPrev"]
      .forEach((id) => ($("#" + id).textContent = "—"));
    $("#rdChg").className = "rd-chg";
    return;
  }
  const d = new Date(b.ts * 1000);
  const dd = `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}.${d.getFullYear()}`;
  const chg = b.c - b.o;
  const chgPct = b.o ? (chg / b.o) * 100 : 0;
  const up = chg >= 0;
  // The header % is vs the previous close, the session % is vs this bar's
  // open — two different numbers that read as a bug unless both are labelled.
  const prev = chartBars[i - 1];
  const chgPrev = prev ? (b.c - prev.c) / prev.c * 100 : null;
  $("#rdDate").textContent = dd;
  $("#rdO").textContent = fmt(b.o);
  $("#rdH").textContent = fmt(b.h);
  $("#rdL").textContent = fmt(b.l);
  $("#rdC").textContent = fmt(b.c);
  $("#rdV").textContent = fmt(b.v, 0);
  const chgEl = $("#rdChg");
  chgEl.textContent = `${up ? "+" : ""}${fmt(chg)}`;
  chgEl.className = "rd-chg " + (up ? "up" : "down");
  const pctEl = $("#rdChgPct");
  pctEl.textContent = `${up ? "+" : ""}${fmt(chgPct)}%`;
  pctEl.className = up ? "up" : "down";
  const prevEl = $("#rdChgPrev");
  if (chgPrev === null) {
    prevEl.textContent = "—";
    prevEl.className = "";
  } else {
    prevEl.textContent = `${chgPrev >= 0 ? "+" : ""}${fmt(chgPrev)}%`;
    prevEl.className = chgPrev >= 0 ? "pos" : "neg";
  }
}

function barIndexAt(clientX) {
  const g = chartGeom;
  if (!g || !chartBars.length) return -1;
  const rect = $("#priceChart").getBoundingClientRect();
  return Math.max(0, Math.min(g.n - 1, Math.floor((clientX - rect.left - 4) / g.slot)));
}

function initChartHover() {
  const c = $("#priceChart");
  c.addEventListener("mousemove", (e) => {
    const i = barIndexAt(e.clientX);
    if (i === hoverIndex) return;
    hoverIndex = i;
    setReadout(i);
    drawPrice(chartBars);
  });
  c.addEventListener("mouseleave", () => {
    hoverIndex = chartBars.length ? chartBars.length - 1 : -1;
    setReadout(hoverIndex);
    drawPrice(chartBars);
  });
}

async function loadAnalysis() {
  const scan = lastScan || (await api("/api/scan"));
  if (scan.status === "busy") { toast("Skan w toku…", true); return; }
  lastScan = scan;
  const rows = scan.results || [];
  table($("#analysisTable"), [
    { label: "Symbol", render: (a) => `<b>${a.symbol}</b> <span class="pill">${a.market}</span>` },
    { label: "Cena", num: true, render: (a) => fmt(a.price) },
    { label: "Score", num: true, render: (a) => `<span class="scorenum ${sign(a.score)}">${fmt(a.score, 1)}</span>` },
    { label: "", render: (a) => scoreBar(a.score) },
    { label: "Trend", num: true, render: (a) => fmt(a.components.trend, 2) },
    { label: "Mom.", num: true, render: (a) => fmt(a.components.momentum, 2) },
    { label: "Rev.", num: true, render: (a) => fmt(a.components.reversion, 2) },
    { label: "RSI", num: true, render: (a) => fmt(a.rsi, 0) },
    { label: "20d ROC", num: true, cls: (a) => sign(a.roc_fast), render: (a) => pct(a.roc_fast) },
    { label: "ADR", num: true, render: (a) => fmt(a.adv, 0) },
    { label: "Płynność", render: (a) => (a.liquidity_ok ? "✓" : '<span class="neg">niska</span>') },
    { label: "Stop", num: true, render: (a) => fmt(a.suggested_stop) },
    { label: "Cel", num: true, render: (a) => fmt(a.suggested_target) },
    { label: "Akcja", render: (a) => `<button class="btn sm" onclick="buySym('${a.symbol}')">Kup</button>` },
  ], rows, { onRow: showChart });
  toast(`Skan: ${scan.analyzed} instrumentów, ${(scan.signals || []).length} sygnałów`);
}

window.buySym = async (sym) => {
  const r = await api("/api/orders", { method: "POST", body: JSON.stringify({ symbol: sym, side: "BUY" }) });
  toast(r.status === "FILLED" ? `Kup ${r.qty} ${sym} @ ${fmt(r.price)}` : `Odrzucone: ${r.reason}`, r.status !== "FILLED");
  loadAccount();
};

/* --- nav / wiring ------------------------------------------------------ */

function switchView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "decisions") loadDecisions();
}

$$(".tab").forEach((t) => (t.onclick = () => switchView(t.dataset.view)));

// --- modal wiring ---
$$("[data-close-modal]").forEach((b) => (b.onclick = closeModal));
$$(".mtab").forEach((t) => (t.onclick = () => switchModalTab(t.dataset.mtab)));
$$("#rangeSeg .seg-btn").forEach((b) => (b.onclick = () => {
  $$("#rangeSeg .seg-btn").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  chartRange = parseInt(b.dataset.range, 10);
  loadChart();
}));
$("#modalFundBtn").onclick = () => switchModalTab("fund");
$$("#colorSeg .seg-btn").forEach((b) => (b.onclick = () => {
  $$("#colorSeg .seg-btn").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  setPalette(b.dataset.color);
}));
$("#sigSideFilter").onchange = () => loadSignals();
$("#btnBacktest").onclick = () => runBacktest();
$("#btnSweep").onclick = () => runSweep();
$("#btnWalkforward").onclick = () => runWalkforward();
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#chartModal").hidden) closeModal();
});
window.addEventListener("resize", () => {
  if (!$("#chartModal").hidden) drawPrice(chartBars);
});

$("#btnScanNow").onclick = async () => {
  const b = $("#btnScanNow"); b.disabled = true; b.textContent = "Skanuję…";
  try { lastScan = await api("/api/scan"); await refreshAll(); }
  catch (e) { toast(e.message, true); }
  finally { b.disabled = false; b.textContent = "Uruchom skan"; }
};

$("#btnRunFullScan").onclick = async () => {
  const b = $("#btnRunFullScan"); b.disabled = true; b.textContent = "Skanuję…";
  try { lastScan = await api(`/api/scan?execute=${$("#autoExecute").checked}`); await refreshAll(); }
  catch (e) { toast(e.message, true); }
  finally { b.disabled = false; b.textContent = "Pełny skan"; }
};

$("#btnRefreshQuotes").onclick = async () => {
  const b = $("#btnRefreshQuotes"); b.disabled = true;
  try { await api("/api/quotes?refresh=true"); await loadMarket(); toast("Kursy odświeżone"); }
  catch (e) { toast(e.message, true); }
  finally { b.disabled = false; }
};

$("#btnAddSymbol").onclick = async () => {
  const v = $("#newSymbol").value.trim();
  if (!v) return;
  try {
    await api("/api/watchlist", { method: "POST", body: JSON.stringify({ symbol: v }) });
    $("#newSymbol").value = "";
    await api("/api/scan?symbols=" + encodeURIComponent(v));
    await loadMarket();
    toast(`${v.toUpperCase()} dodany`);
  } catch (e) { toast(e.message, true); }
};

async function refreshAll() {
  await Promise.allSettled([loadAccount(), loadEquity(), loadSignals(), loadPositions(), loadMarket(), loadAnalytics()]);
}

(async function init() {
  try {
    initChartHover();
    // Restore the saved candle-colour convention before the first draw.
    try {
      const saved = localStorage.getItem("tdPalette") || "intl";
      setPalette(saved);
      $$("#colorSeg .seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.color === saved));
    } catch (e) { /* localStorage unavailable */ }
    await loadAccount();
    await Promise.allSettled([loadEquity(), loadSignals(), loadPositions(), loadMarket(), loadAnalytics()]);
  } catch (e) { toast("Błąd startu: " + e.message, true); }
  setInterval(() => { loadAccount().catch(() => {}); }, 30000);
  setInterval(() => { if ($("#view-market").classList.contains("active")) loadMarket().catch(() => {}); }, 60000);
})();
