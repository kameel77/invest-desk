/* Backtest view — walks the stored candles and renders the metrics.
   The runs are CPU-bound on the server, so the buttons disable while running. */

function renderBacktestPortfolio(p) {
  if (!p || p.error) {
    $("#btStats").innerHTML = `<p class="muted">${p && p.error ? p.error : "brak danych"}</p>`;
    return;
  }
  $("#btStats").innerHTML = [
    statBox("Transakcje", p.trades, `${p.wins} wygranych / ${p.losses} przegranych`),
    statBox("Zwrot", pct(p.total_return_pct), `kapitał ${fmt(p.final_equity)}`,
      p.total_return_pct >= 0 ? "pos" : "neg"),
    statBox("Trajność", fmt(p.hit_rate_pct, 1) + "%", `wygrana ${fmt(p.avg_win_pct, 2)}% / strata ${fmt(p.avg_loss_pct, 2)}%`),
    statBox("Profit factor", p.profit_factor ?? "—", p.profit_factor >= 1 ? "zyskowny" : "stratny",
      (p.profit_factor ?? 0) >= 1 ? "pos" : "neg"),
    statBox("Max drawdown", fmt(p.max_drawdown_pct, 2) + "%", "spadek szczytu"),
    statBox("Sharpe", fmt(p.sharpe, 2), `hold ${fmt(p.avg_bars_held, 1)} dni`, p.sharpe >= 0 ? "pos" : "neg"),
  ].join("");
}

function renderBacktestSymbols(bySymbol) {
  table($("#btSymbols"), [
    { label: "Symbol", render: (r) => `<b>${r.symbol}</b>` },
    { label: "Transakcje", num: true, render: (r) => r.trades },
    { label: "Zwrot", num: true, cls: (r) => sign(r.total_return_pct), render: (r) => pct(r.total_return_pct) },
    { label: "Trajność", num: true, render: (r) => fmt(r.hit_rate_pct, 1) + "%" },
    { label: "PF", num: true, cls: (r) => sign((r.profit_factor ?? 1) - 1), render: (r) => r.profit_factor ?? "—" },
    { label: "Max DD", num: true, render: (r) => fmt(r.max_drawdown_pct, 2) + "%" },
    { label: "Sharpe", num: true, cls: (r) => sign(r.sharpe), render: (r) => fmt(r.sharpe, 2) },
    { label: "Wyjścia", render: (r) => `<span class="muted">${Object.entries(r.exit_reasons || {}).map(([k, v]) => `${k}:${v}`).join(" ")}</span>` },
  ], bySymbol, { onRow: showChart });
}

function renderSweep(s) {
  $("#sweepMeta").textContent = `${s.variants.length} wariantów · ${s.symbols} instrumentów`;
  table($("#btSweep"), [
    { label: "#", num: true, render: (r, i) => (r._i ?? "") },
    { label: "Konfiguracja", render: (r) => `<b>${r.label}</b>` },
    { label: "Transakcje", num: true, render: (r) => r.trades },
    { label: "Zwrot", num: true, cls: (r) => sign(r.total_return_pct), render: (r) => pct(r.total_return_pct) },
    { label: "Trajność", num: true, render: (r) => fmt(r.hit_rate_pct, 1) + "%" },
    { label: "PF", num: true, cls: (r) => sign((r.profit_factor ?? 1) - 1), render: (r) => r.profit_factor ?? "—" },
    { label: "Max DD", num: true, render: (r) => fmt(r.max_drawdown_pct, 2) + "%" },
    { label: "Sharpe", num: true, cls: (r) => sign(r.sharpe), render: (r) => fmt(r.sharpe, 2) },
  ], s.variants);
}

async function runBacktest() {
  const b = $("#btnBacktest");
  b.disabled = true; b.textContent = "Symuluję…";
  toast("Backtest: symulacja krok po kroku, to chwilę potrwa");
  try {
    const r = await api("/api/backtest");
    renderBacktestPortfolio(r.portfolio);
    renderBacktestSymbols(r.by_symbol);
    toast(`Backtest: ${r.symbols_with_trades} instrumentów, `
      + `${(r.portfolio && r.portfolio.trades) || 0} transakcji, `
      + `zwrot ${(r.portfolio && r.portfolio.total_return_pct) ?? "—"}%`);
  } catch (e) {
    toast("Backtest: " + e.message, true);
  } finally {
    b.disabled = false; b.textContent = "Uruchom backtest";
  }
}

async function runSweep() {
  const b = $("#btnSweep");
  b.disabled = true; b.textContent = "Sweep…";
  toast("Sweep: 19 konfiguracji na całym uniwersum — może potrwać kilka minut");
  try {
    const s = await api("/api/backtest/sweep");
    s.variants.forEach((v, i) => (v._i = i + 1));
    renderSweep(s);
    const best = s.variants[0];
    toast(`Najlepsza: ${best.label} → ${best.total_return_pct}% (Sharpe ${best.sharpe})`);
  } catch (e) {
    toast("Sweep: " + e.message, true);
  } finally {
    b.disabled = false; b.textContent = "Pełny sweep";
  }
}

async function runWalkforward() {
  const b = $("#btnWalkforward");
  b.disabled = true; b.textContent = "Walk-forward…";
  toast("Walk-forward: 5 fałdów × 9 konfiguracji — kilka minut");
  try {
    const r = await api("/api/walkforward?folds=5");
    if (r.error) { toast("Walk-forward: " + r.error, true); return; }
    renderWalkforward(r);
  } catch (e) {
    toast("Walk-forward: " + e.message, true);
  } finally {
    b.disabled = false; b.textContent = "Walk-forward";
  }
}

function renderWalkforward(r) {
  const oos = r.oos || {};
  const base = r.baseline_oos || {};
  $("#wfMeta").textContent = `${r.folds} fałdów · ${r.candidates} konfiguracji · ${r.symbols} instrumentów`;
  $("#wfStats").innerHTML = [
    statBox("Zwrot OOS", pct(oos.avg_return_pct), "średnia z okien testowych",
      (oos.avg_return_pct || 0) >= 0 ? "pos" : "neg"),
    statBox("Fałdy zyskowne", `${oos.profitable_folds}/${oos.folds}`,
      "spośród okien testowych", (oos.profitable_folds || 0) > (oos.folds || 0) / 2 ? "pos" : "neg"),
    statBox("Sharpe OOS", fmt(oos.avg_sharpe, 2), "poza próbką", (oos.avg_sharpe || 0) >= 0 ? "pos" : "neg"),
    statBox("Transakcje OOS", oos.trades, "łącznie"),
    statBox("Bazowo (domyślne)", pct(base.avg_return_pct), "bez dostrajania",
      (base.avg_return_pct || 0) >= 0 ? "pos" : "neg"),
    statBox("Bazowo zyskowne", `${base.profitable_folds}/${r.folds}`, "ta sama metoda"),
  ].join("");

  table($("#wfFolds"), [
    { label: "Fałd", num: true, render: (f) => f.fold },
    { label: "Wybrana konfiguracja", render: (f) => `<b>${f.chosen}</b>` },
    { label: "Trening n", num: true, render: (f) => f.train.trades },
    { label: "Trening %", num: true, cls: (f) => sign(f.train.total_return_pct), render: (f) => pct(f.train.total_return_pct) },
    { label: "Trening Sharpe", num: true, cls: (f) => sign(f.train.sharpe), render: (f) => fmt(f.train.sharpe, 2) },
    { label: "Test n", num: true, render: (f) => f.test.trades },
    { label: "Test %", num: true, cls: (f) => sign(f.test.total_return_pct), render: (f) => pct(f.test.total_return_pct) },
    { label: "Test Sharpe", num: true, cls: (f) => sign(f.test.sharpe), render: (f) => fmt(f.test.sharpe, 2) },
    { label: "Bazowo %", num: true, cls: (f) => sign(f.test_default.total_return_pct), render: (f) => pct(f.test_default.total_return_pct) },
  ], r.results);
}
