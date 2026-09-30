/* Fundamentals panel — profile, valuation, balance sheet, dividend, analysts.
   Split out of app.js purely for size; uses the helpers app.js defines. */

function bigNum(n) {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  if (a >= 1e12) return `${fmt(n / 1e12, 2)} bln`;
  if (a >= 1e9) return `${fmt(n / 1e9, 2)} mld`;
  if (a >= 1e6) return `${fmt(n / 1e6, 1)} mln`;
  return fmt(n, 0);
}

const pctRaw = (n) => (n === null || n === undefined) ? "—" : `${fmt(n * 100, 1)}%`;

function fundBox(title, rows) {
  const body = rows
    .filter(([, v]) => v !== null && v !== undefined && v !== "—")
    .map(([k, v]) => `<div class="row"><span>${k}</span><b>${v}</b></div>`).join("");
  return `<div class="fund-box"><h3>${title}</h3>${body || '<div class="row muted">brak danych</div>'}</div>`;
}

function fundDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString("pl-PL") : "—";
}

async function loadFundamentals() {
  if (!chartSymbol) return;
  const body = $("#fundamentalsBody");
  if (!body) return;
  const sub = $("#modalSub");
  if (sub) sub.textContent = "wczytywanie danych spółki…";
  try {
    const d = await api(`/api/fundamentals/${encodeURIComponent(chartSymbol)}`);
    if (!d.available) {
      if (sub) sub.textContent = "brak danych fundamentalnych";
      body.innerHTML = `<p class="muted">Brak danych fundamentalnych dla ${chartSymbol}.`
        + (d.stale ? " (pokazuję ostatnie zapisane)" : "") + "</p>";
      return;
    }
    if (sub) sub.textContent = (d.stale ? "cache · " : "")
      + `${d.data_source || "Yahoo"} · ${ts2date(d.fetched_ts)}`;

    const profile = d.name ? fundBox("Profil", [
      ["Nazwa", d.name],
      ["Sektor", d.sector],
      ["Branża", d.industry],
      ["Kraj", d.country],
      ["Giełda", d.exchange],
      ["Pracownicy", d.employees],
    ]) : "";

    const boxes = [
      fundBox("Sesja", [
        ["Kurs", fmt(d.price)],
        ["Otwarcie", fmt(d.open)],
        ["Dzienny min", fmt(d.day_low)],
        ["Dzienny max", fmt(d.day_high)],
        ["Wolumen", bigNum(d.volume)],
        ["Śr. wolumen", bigNum(d.avg_volume)],
        ["52W niski", fmt(d.fifty_two_week_low)],
        ["52W wysoki", fmt(d.fifty_two_week_high)],
        ["Od 52W wysokiego", pct(d.pct_from_52w_high)],
      ]),
      fundBox("Wycena", [
        ["Kapitalizacja", bigNum(d.market_cap)],
        ["C/Z trailing", fmt(d.trailing_pe)],
        ["C/Z forward", fmt(d.forward_pe)],
        ["C/WK", fmt(d.price_to_book)],
        ["C/S", fmt(d.price_to_sales)],
        ["EV/EBITDA", fmt(d.ev_to_ebitda)],
        ["Beta", fmt(d.beta, 2)],
        ["Akcje w obiegu", bigNum(d.shares_outstanding)],
      ]),
      fundBox("Rentowność", [
        ["Marża brutto", pctRaw(d.gross_margin)],
        ["Marża operacyjna", pctRaw(d.operating_margin)],
        ["Marża netto", pctRaw(d.profit_margin)],
        ["ROE", pctRaw(d.return_on_equity)],
        ["Przychód TTM", bigNum(d.revenue_ttm)],
        ["Zysk netto TTM", bigNum(d.net_income_ttm)],
        ["EBITDA", bigNum(d.ebitda)],
        ["Wzrost przychodów", pctRaw(d.revenue_growth)],
      ]),
      fundBox("Bilans", [
        ["Gotówka", bigNum(d.total_cash)],
        ["Dług", bigNum(d.total_debt)],
        ["Dług/kapitał", fmt(d.debt_to_equity)],
        ["Wskaźnik bieżący", fmt(d.current_ratio)],
        ["FCF", bigNum(d.free_cashflow)],
        ["Operacyjny CF", bigNum(d.operating_cashflow)],
      ]),
      fundBox("Dywidenda", [
        ["Dywidenda roczna", fmt(d.dividend_rate)],
        ["Stopa dywidendy", pct(d.dividend_yield)],
        ["Wypłacalność", pct(d.payout_ratio)],
        ["Data odcięcia", fundDate(d.ex_dividend_date)],
      ]),
      fundBox("Analitycy", [
        ["Cena średnia", fmt(d.target_mean)],
        ["Cena wysoka", fmt(d.target_high)],
        ["Cena niska", fmt(d.target_low)],
        ["Do celu śr.", pct(d.pct_to_target)],
        ["Rekomendacja", d.recommendation],
        ["Liczba ocen", d.analyst_count],
        ["Data wyników", fundDate(d.earnings_date)],
      ]),
    ];

    body.innerHTML = profile + boxes.join("")
      + (d.description ? `<p class="fund-desc">${d.description}</p>` : "");
  } catch (e) {
    if (sub) sub.textContent = "błąd wczytywania danych";
    body.innerHTML = `<p class="muted">Błąd wczytywania danych: ${e.message}</p>`;
  }
}
