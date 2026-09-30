"""Fundamentals, profile and analyst data per instrument.

Yahoo's ``quoteSummary`` endpoint needs a consent cookie plus a matching
crumb, unlike the chart API which needs neither. The crumb is fetched once and
reused until Yahoo rejects it (the pair expires on its own schedule), and a
failure here must never take down the price path — every caller degrades to
"no fundamentals" rather than an error.

Coverage is uneven by design: a GPW small cap has no analyst estimates, an ETF
has no fundamentals at all. Absent fields stay ``None`` instead of being
invented, so the UI can distinguish "not reported" from "zero".
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from . import config, db

log = logging.getLogger("trading.fundamentals")

CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CONSENT_URL = "https://fc.yahoo.com/"
SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"

MODULES = ("price,summaryDetail,defaultKeyStatistics,financialData,"
           "assetProfile,calendarEvents,earnings,recommendationTrend")

# Yahoo's crumb stays valid for hours; re-fetching it per request would double
# the call count and get us rate-limited.
CRUMB_TTL = 3600.0
CRUMB_TTL_BACKOFF = 900.0  # after a rejection, wait longer before retrying

_crumb: str = ""
_crumb_fetched_at: float = 0.0
_client: Optional[httpx.AsyncClient] = None
_fund_cache: dict[str, tuple[float, dict[str, Any]]] = {}
FUND_TTL = 6 * 3600.0  # fundamentals move on a quarterly cadence


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # A dedicated client: quoteSummary needs its own cookie jar, which the
        # shared market client never holds.
        _client = httpx.AsyncClient(
            timeout=25.0,
            headers={"User-Agent": config.USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def get_crumb(force: bool = False) -> str:
    global _crumb, _crumb_fetched_at
    now = time.time()
    if not force and _crumb and (now - _crumb_fetched_at) < CRUMB_TTL:
        return _crumb
    c = _http()
    try:
        # fc.yahoo.com sets the consent cookie the crumb is bound to.
        await c.get(CONSENT_URL)
        r = await c.get(CRUMB_URL)
        text = r.text.strip()
        if r.status_code == 200 and text and len(text) < 40:
            _crumb = text
            _crumb_fetched_at = now
            return _crumb
        log.warning("crumb fetch failed: HTTP %s %r", r.status_code, text[:40])
    except Exception as exc:
        log.warning("crumb fetch error: %s", exc)
    return ""


def _invalidate_crumb() -> None:
    global _crumb, _crumb_fetched_at
    _crumb = ""
    _crumb_fetched_at = time.time() - (CRUMB_TTL - CRUMB_TTL_BACKOFF)


def _flat(v: Any) -> Any:
    """Yahoo returns most numerics as ``{raw, fmt}``; prefer the formatted one."""
    if isinstance(v, dict):
        if "fmt" in v:
            return v["fmt"]
        return v.get("raw")
    if isinstance(v, list):
        return [_flat(x) for x in v]
    return v


def _num(v: Any) -> Optional[float]:
    """Coerce Yahoo's ``'1.13'`` / ``'906.41M'`` / ``{raw: 1.13}`` to a float."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return _num(v.get("raw"))
    if isinstance(v, str):
        s = v.replace("%", "").replace(" ", "").replace(" ", "").strip()
        mult = 1.0
        if s.endswith(("T", "B", "M", "K")):
            mult = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3}[s[-1]]
            s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None
    return None


def _ts(v: Any) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None and n > 0 else None


def _first(data: dict, *keys: str) -> Any:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", {}, []):
            return _flat(v)
    return None


async def fetch_fundamentals(symbol: str, force: bool = False) -> dict[str, Any]:
    """Profile, valuation, dividend and analyst data for one instrument.

    Never raises: an unreachable Yahoo returns ``{"available": False}``.
    """
    symbol = symbol.upper()
    cached = _fund_cache.get(symbol)
    if not force and cached and (time.time() - cached[0]) < FUND_TTL:
        return cached[1]

    out: dict[str, Any] = {"symbol": symbol, "available": False}

    crumb = await get_crumb()
    if not crumb:
        out["error"] = "brak crumb Yahoo"
        return out

    try:
        r = await _http().get(SUMMARY_URL + symbol, params={"modules": MODULES, "crumb": crumb})
    except Exception as exc:
        log.warning("quoteSummary %s failed: %s", symbol, exc)
        out["error"] = str(exc)
        return out

    if r.status_code in (401, 403):
        # The crumb expired (or was invalidated) — drop it and retry once.
        _invalidate_crumb()
        crumb = await get_crumb(force=True)
        if crumb:
            try:
                r = await _http().get(SUMMARY_URL + symbol,
                                      params={"modules": MODULES, "crumb": crumb})
            except Exception as exc:
                out["error"] = str(exc)
                return out

    try:
        result = ((r.json() or {}).get("quoteSummary") or {}).get("result")
        if isinstance(result, list):
            result = result[0] if result else None
    except Exception as exc:
        out["error"] = f"zły JSON: {exc}"
        return out

    if not isinstance(result, dict) or r.status_code != 200:
        out["error"] = f"brak danych (HTTP {r.status_code})"
        return out

    price = result.get("price") or {}
    summary = result.get("summaryDetail") or {}
    stats = result.get("defaultKeyStatistics") or {}
    fin = result.get("financialData") or {}
    prof = result.get("assetProfile") or {}
    cal = result.get("calendarEvents") or {}
    earn = result.get("earnings") or {}

    earnings_date = None
    ed = ((cal.get("earnings") or {}).get("earningsDate") or [])
    if ed and isinstance(ed, list):
        earnings_date = _ts(ed[0])

    out.update({
        "available": True,
        "fetched_ts": int(time.time()),
        # --- identity ---
        "name": _first(price, "longName", "shortName") or _first(prof, "longBusinessName"),
        "sector": _first(prof, "sector"),
        "industry": _first(prof, "industry"),
        "country": _first(prof, "country"),
        "website": _first(prof, "website"),
        "employees": _num(_first(prof, "fullTimeEmployees")),
        "description": _first(prof, "longBusinessSummary"),
        "quote_type": _first(price, "quoteType"),
        # --- session ---
        "price": _num(_first(price, "regularMarketPrice")),
        "previous_close": _num(_first(price, "regularMarketPreviousClose", "previousClose")),
        "open": _num(_first(price, "regularMarketOpen", "open")),
        "day_low": _num(_first(price, "regularMarketDayLow", "dayLow")),
        "day_high": _num(_first(price, "regularMarketDayHigh", "dayHigh")),
        "volume": _num(_first(price, "regularMarketVolume", "volume")),
        "avg_volume": _num(_first(summary, "averageVolume", "averageDailyVolume3Month")),
        "market_cap": _num(_first(price, "marketCap") or _first(summary, "marketCap")),
        "market_state": _first(price, "marketState"),
        "data_source": _first(price, "regularMarketSource"),
        "exchange": _first(price, "exchangeName", "exchange"),
        "currency": _first(price, "currency"),
        # --- range ---
        "fifty_two_week_low": _num(_first(summary, "fiftyTwoWeekLow")),
        "fifty_two_week_high": _num(_first(summary, "fiftyTwoWeekHigh")),
        "year_change_pct": _num(_first(summary, "52WeekChange", "ytdReturnPercent")),
        # --- valuation ---
        "trailing_pe": _num(_first(summary, "trailingPE")),
        "forward_pe": _num(_first(summary, "forwardPE")),
        "peg": _num(_first(summary, "pegRatio")),
        "price_to_book": _num(_first(summary, "priceToBook")),
        "price_to_sales": _num(_first(summary, "priceToSalesTrailing12Months")),
        "ev_to_ebitda": _num(_first(stats, "enterpriseToEbitda")),
        "beta": _num(_first(summary, "beta")),
        # --- size / profitability ---
        "profit_margin": _num(_first(fin, "profitMargins")),
        "operating_margin": _num(_first(fin, "operatingMargins")),
        "gross_margin": _num(_first(fin, "grossMargins")),
        "return_on_equity": _num(_first(fin, "returnOnEquity")),
        "revenue_ttm": _num(_first(fin, "totalRevenue")),
        "gross_profit_ttm": _num(_first(fin, "grossProfits")),
        # netIncomeToCommon is often null for GPW names; netIncome is the
        # broader (pre-preference-shares) line and is populated more reliably.
        "net_income_ttm": _num(_first(fin, "netIncomeToCommon", "netIncome")),
        "net_income_common_ttm": _num(_first(fin, "netIncomeToCommon")),
        "ebitda": _num(_first(fin, "ebitda")),
        "total_cash": _num(_first(fin, "totalCash")),
        "total_debt": _num(_first(fin, "totalDebt")),
        "debt_to_equity": _num(_first(fin, "debtToEquity")),
        "current_ratio": _num(_first(fin, "currentRatio")),
        "quick_ratio": _num(_first(fin, "quickRatio")),
        "free_cashflow": _num(_first(fin, "freeCashflow")),
        "operating_cashflow": _num(_first(fin, "operatingCashflow")),
        "revenue_growth": _num(_first(fin, "revenueGrowth")),
        "earnings_growth": _num(_first(fin, "earningsGrowth")),
        "shares_outstanding": _num(_first(stats, "sharesOutstanding")),
        "float_shares": _num(_first(stats, "floatShares")),
        # --- dividend ---
        "dividend_rate": _num(_first(summary, "dividendRate")),
        "dividend_yield": _num(_first(summary, "dividendYield")),
        "payout_ratio": _num(_first(summary, "payoutRatio")),
        "ex_dividend_date": _ts(_first(cal, "exDividendDate")),
        # --- analysts ---
        "target_mean": _num(_first(fin, "targetMeanPrice")),
        "target_high": _num(_first(fin, "targetHighPrice")),
        "target_low": _num(_first(fin, "targetLowPrice")),
        "recommendation": _first(fin, "recommendationKey"),
        "analyst_count": _num(_first(fin, "numberOfAnalystOpinions")),
        "earnings_date": earnings_date,
    })

    # Derived: distance to the 52-week high, and % under/over the mean target.
    lo52, hi52 = out["fifty_two_week_low"], out["fifty_two_week_high"]
    p = out["price"]
    out["pct_from_52w_high"] = round(((p / hi52) - 1) * 100, 1) if p and hi52 else None
    out["pct_above_52w_low"] = round(((p / lo52) - 1) * 100, 1) if p and lo52 else None
    tgt = out["target_mean"]
    out["pct_to_target"] = round(((tgt / p) - 1) * 100, 1) if p and tgt else None
    _ = earn  # quarterly trend, not surfaced in v1

    _fund_cache[symbol] = (time.time(), out)
    return out


async def fetch_many(symbols: list[str], force: bool = False) -> dict[str, dict]:
    """Fundamentals for several symbols, rate-limited to 3 concurrent calls."""
    sem = asyncio.Semaphore(3)
    out: dict[str, dict] = {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                out[sym.upper()] = await fetch_fundamentals(sym, force=force)
            except Exception as exc:
                out[sym.upper()] = {"symbol": sym.upper(), "available": False, "error": str(exc)}

    await asyncio.gather(*(one(s) for s in symbols))
    return out


async def save_fundamentals(data: dict[str, Any]) -> None:
    """Persist the flat, JSON-friendly part of a fundamentals payload."""
    with db.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS fundamentals ("
            "symbol TEXT PRIMARY KEY, fetched_ts INTEGER, payload TEXT)")
        c.execute(
            "INSERT INTO fundamentals(symbol, fetched_ts, payload) VALUES (?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET fetched_ts=excluded.fetched_ts, "
            "payload=excluded.payload",
            (data["symbol"], int(data.get("fetched_ts") or time.time()),
             json.dumps(data, ensure_ascii=False)),
        )


def get_cached_fundamentals(symbol: str) -> Optional[dict]:
    row = db.get_conn().execute(
        "SELECT payload FROM fundamentals WHERE symbol=?", (symbol.upper(),)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None
