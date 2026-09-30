/* Dashboard + signals analytics rendering. Uses app.js helpers. */

function statBox(lab, val, sub, cls) {
  return `<div class="stat"><div class="lab">${lab}</div>`
    + `<div class="val ${cls || ""}">${val}</div>`
    + `${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function renderMarketStats(m) {
  $("#mktStats").innerHTML = [
    statBox("Instrumenty", m.analyzed, `z ${m.universe} na liście`),
    statBox("Średni score", fmt(m.avg_score, 1),
      `trend ${fmt(m.component_averages.trend, 2)} · mom ${fmt(m.component_averages.momentum, 2)} · rev ${fmt(m.component_averages.reversion, 2)}`,
      m.avg_score >= 0 ? "pos" : "neg"),
    statBox("Kupowalne", m.above_buy_threshold, "score ≥ 35", "pos"),
    statBox("Sprzedawalne", m.below_sell_threshold, "score ≤ -25", "neg"),
    statBox("Equity", fmt(m.account.equity), `gotówka ${fmt(m.account.cash)}`),
    statBox("Zwrot", pct(m.account.total_return_pct), `zrealizowany ${fmt(m.account.realized_pnl)}`,
      m.account.total_return_pct >= 0 ? "pos" : "neg"),
  ].join("");
}

function renderAccountStats(a) {
  $("#accountStats").innerHTML = [
    statBox("Equity", fmt(a.equity), `start ${fmt(10000)}`),
    statBox("Gotówka", fmt(a.cash), `${pct((a.cash / a.equity) * 100)} kapitału`),
    statBox("Wartość poz.", fmt(a.market_value), `${a.open_positions} pozycji`),
    statBox("Zrealizowany PnL", fmt(a.realized_pnl), "po zamkniętych",
      a.realized_pnl >= 0 ? "pos" : "neg"),
    statBox("Zwrot", pct(a.total_return_pct), "od kapitału startowego",
      a.total_return_pct >= 0 ? "pos" : "neg"),
  ].join("");
}

const BUCKET_COLORS = { ">=50": "#3fb950", "35-50": "#7ee787", "20-35": "#4c9aff",
  "0-20": "#8b98a5", "<0": "#f85149" };

function renderBuckets(buckets) {
  const total = Object.values(buckets).reduce((x, y) => x + y, 0) || 1;
  $("#scoreBuckets").innerHTML = Object.entries(buckets).map(([k, v]) => {
    const w = (v / total) * 100;
    return `<div class="bucket"><span>${k}</span>`
      + `<span class="track"><i style="width:${w}%;background:${BUCKET_COLORS[k] || "#8b98a5"}"></i></span>`
      + `<span class="n">${v}</span></div>`;
  }).join("");
}

function renderSignalStats(s, targetId) {
  const box = $(targetId || "#sigStats");
  if (!box) return;
  box.innerHTML = [
    statBox("Sygnały", s.total, `${s.window_days} dni`),
    statBox("Kup", s.by_side.BUY || 0, "", "pos"),
    statBox("Sprzedaj", s.by_side.SELL || 0, "", "neg"),
    statBox("Wykonane", s.executed, `realizacja ${fmt(s.execution_rate, 1)}%`),
    statBox("Próg wejścia", fmt(s.thresholds.buy, 0), `wyjście ${fmt(s.thresholds.sell, 0)}`),
  ].join("");
  const meta = $("#sigWindowMeta");
  if (meta) meta.textContent = `okno ${s.window_days} dni`;
}

function renderByDay(byDay) {
  const box = $("#sigByDay");
  if (!box) return;
  if (!byDay.length) { box.innerHTML = '<p class="muted">brak danych</p>'; return; }
  const max = Math.max(...byDay.map((d) => d.count)) || 1;
  box.innerHTML = byDay.slice(-14).map((d) => {
    const h = Math.max(3, (d.count / max) * 70);
    const label = d.date.slice(5);
    return `<div class="sb" title="${d.date}: ${d.count} sygnałów">`
      + `<b>${d.count}</b><i style="height:${h}px"></i><span>${label}</span></div>`;
  }).join("");
}

function renderTopBottom(m) {
  const cols = [
    { label: "Symbol", render: (r) => `<b>${r.symbol}</b>` },
    { label: "Mkt", render: (r) => `<span class="pill">${r.market}</span>` },
    { label: "Score", num: true, render: (r) => `<span class="scorenum ${sign(r.score)}">${fmt(r.score, 1)}</span>` },
    { label: "RSI", num: true, render: (r) => fmt(r.rsi, 0) },
    { label: "20d", num: true, cls: (r) => sign(r.roc_fast), render: (r) => pct(r.roc_fast) },
  ];
  table($("#topTable"), cols, m.top, { onRow: showChart });
  table($("#bottomTable"), cols, m.bottom, { onRow: showChart });

  const mover = (r) => [
    { label: "Symbol", render: (x) => `<b>${x.symbol}</b>` },
    { label: "Kurs", num: true, render: (x) => fmt(x.price) },
    { label: "Zmiana", num: true, render: (x) => `<span class="${sign(x.change_pct)}">${pct(x.change_pct)}</span>` },
  ];
  table($("#gainersTable"), mover, m.gainers, { onRow: showChart });
  table($("#losersTable"), mover, m.losers, { onRow: showChart });
}

function renderReasons(s) {
  table($("#reasonsTable"), [
    { label: "Uzasadnienie", render: (r) => `<span class="muted">${r.reason}</span>` },
    { label: "×", num: true, render: (r) => r.count },
  ], s.top_reasons);
}

async function loadAnalytics() {
  const [m, s] = await Promise.allSettled([
    api("/api/market/stats"),
    api("/api/signals/stats?days=30"),
  ]);
  if (m.status === "fulfilled") {
    renderMarketStats(m.value);
    renderTopBottom(m.value);
  }
  if (s.status === "fulfilled") {
    renderSignalStats(s.value, "#sigStats");
    renderSignalStats(s.value, "#sigStats2");
    renderBuckets(s.value.score_buckets);
    renderByDay(s.value.by_day);
    renderReasons(s.value);
    table($("#symSignalsTable"), [
      { label: "Symbol", render: (r) => `<b>${r.symbol}</b>` },
      { label: "Sygnały", num: true, render: (r) => r.count },
      { label: "Śr. score", num: true, cls: (r) => sign(r.avg_score), render: (r) => fmt(r.avg_score, 1) },
    ], s.value.top_symbols, { onRow: showChart });
  }
}

window.resetAccount = async () => {
  if (!confirm("Wyzerować konto papierowe? Usunie pozycje, zlecenia i sygnały.")) return;
  const a = await api("/api/account/reset", { method: "POST" });
  renderAccountStats(a);
  toast("Konto wyzerowane");
  refreshAll();
};
