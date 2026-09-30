"""Market data providers.

Primary source is the Yahoo Finance chart API (no key required, covers Warsaw
via the ``.WA`` suffix and US equities/ETFs). When ``ALPACA_API_KEY`` is
configured, US symbols get true real-time IEX quotes layered on top.

Design note: every provider returns the same ``Bar``/``Quote`` shapes so the
strategy engine never knows which feed produced a bar.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from . import config, db

log = logging.getLogger("trading.market")

_UA = config.USER_AGENT
_client: Optional[httpx.AsyncClient] = None

# symbol -> (fetched_at, payload) in-memory cache to stay inside rate limits
_quote_cache: dict[str, tuple[float, dict]] = {}
_candle_cache: dict[str, tuple[float, int]] = {}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# --- Yahoo Finance --------------------------------------------------------

YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")


def is_gpw(symbol: str) -> bool:
    s = symbol.upper()
    return s.endswith(".WA") or s.endswith(".PL")


def market_hint(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith((".WA", ".PL")):
        return "GPW"
    if s.startswith(("BTC", "ETH", "SOL")):
        return "CRYPTO"
    return "US"


async def _yahoo_chart(symbol: str, rng: str, interval: str) -> Optional[dict]:
    last_exc: Optional[Exception] = None
    for host in YAHOO_HOSTS:
        url = f"{host}/v8/finance/chart/{symbol}"
        params = {"range": rng, "interval": interval, "includePrePost": "false"}
        try:
            resp = await _http().get(url, params=params)
            if resp.status_code != 200:
                last_exc = RuntimeError(f"HTTP {resp.status_code} {symbol}")
                continue
            data = resp.json()
            err = (data.get("chart") or {}).get("error")
            if err:
                last_exc = RuntimeError(f"yahoo error {err}")
                continue
            results = (data.get("chart") or {}).get("result") or []
            if not results:
                last_exc = RuntimeError(f"empty result {symbol}")
                continue
            return results[0]
        except Exception as exc:  # network flake -> try the other host
            last_exc = exc
    log.warning("yahoo chart failed for %s: %s", symbol, last_exc)
    return None


def _parse_yahoo(result: dict) -> tuple[list[tuple], dict]:
    meta = result.get("meta", {}) or {}
    ts_list = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[tuple] = []
    for i, ts in enumerate(ts_list):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        o = opens[i] if i < len(opens) else c
        h = highs[i] if i < len(highs) else c
        l = lows[i] if i < len(lows) else c
        v = volumes[i] if i < len(volumes) else 0
        # Yahoo occasionally returns sentinel zeros on illiquid bars
        if not h or h <= 0:
            h = max(o or c, c)
        if not l or l <= 0:
            l = min(o or c, c)
        bars.append((int(ts), float(o or c), float(h), float(l), float(c), float(v or 0)))

    info = {
        "currency": meta.get("currency", ""),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
        "price": meta.get("regularMarketPrice"),
        "prev_close": (meta.get("chartPreviousClose") or meta.get("previousClose")),
        "market_ts": meta.get("regularMarketTime"),
        "name": meta.get("longName") or meta.get("shortName") or "",
    }
    return bars, info


# --- Alpaca (optional real-time US) --------------------------------------

def _alpaca_enabled() -> bool:
    return config.ALPACA_REALTIME


async def _alpaca_quote(symbol: str) -> Optional[dict]:
    if not _alpaca_enabled():
        return None
    url = f"{config.ALPACA_BASE}/v2/stocks/{symbol.upper()}/trades/latest"
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
    }
    try:
        resp = await _http().get(url, headers=headers)
        if resp.status_code != 200:
            return None
        trade = (resp.json() or {}).get("trade") or {}
        p = trade.get("p")
        if not p:
            return None
        return {"price": float(p), "ts": trade.get("t") or int(time.time())}
    except Exception as exc:
        log.debug("alpaca quote failed %s: %s", symbol, exc)
        return None


# --- public API -----------------------------------------------------------

async def fetch_candles(symbol: str, rng: str = "2y", interval: str = "1d",
                        force: bool = False) -> int:
    """Download OHLCV bars into SQLite. Returns the number of rows written."""
    symbol = symbol.upper()
    cached = _candle_cache.get(symbol)
    if not force and cached and (time.time() - cached[0]) < config.CANDLE_TTL:
        return 0

    result = await _yahoo_chart(symbol, rng, interval)
    if not result:
        return 0
    bars, _info = _parse_yahoo(result)
    if not bars:
        return 0
    written = db.upsert_candles(symbol, bars)
    _candle_cache[symbol] = (time.time(), len(bars))
    return written


async def fetch_quote(symbol: str, force: bool = False) -> Optional[dict]:
    """Latest price for one symbol, cached for ``config.QUOTE_TTL`` seconds."""
    symbol = symbol.upper()
    cached = _quote_cache.get(symbol)
    if not force and cached and (time.time() - cached[0]) < config.QUOTE_TTL:
        return cached[1]

    result = await _yahoo_chart(symbol, "5d", "1d" if is_gpw(symbol) else "1d")
    if not result:
        return None
    _bars, info = _parse_yahoo(result)
    price = info.get("price")
    prev = info.get("prev_close")
    # Yahoo's meta `prev_close` is the close BEFORE the requested range, which
    # for a "5d" window is the session before those five days — so it is not
    # the previous session and the header % comes out wrong (ABS.WA showed
    # -1.40% against a real -1.17%). The stored candles are authoritative:
    # the last bar is today, the one before it is the previous close.
    prev = _previous_close(symbol) or prev
    if price is None:
        # fall back to the last bar close
        row = db.get_conn().execute(
            "SELECT close FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
        if not row:
            return None
        price = float(row["close"])

    source = "yahoo"
    market_ts = int(info.get("market_ts") or time.time())
    if market_hint(symbol) == "US":
        live = await _alpaca_quote(symbol)
        if live and live.get("price"):
            price = live["price"]
            market_ts = int(live["ts"] or market_ts)
            source = "alpaca-iex"

    # Yahoo happily returns a regularMarketPrice whose regularMarketTime is
    # months stale (thin listings halt or get delisted), and the change_pct
    # computed from the stale prev_close then reads as a move that never
    # happened. Drop the quote and let the UI say "brak danych" instead of
    # printing a fictional +78%.
    if (time.time() - market_ts) > config.STALE_QUOTE_SECONDS:
        log.warning("dropping stale quote for %s: %.0f days old",
                    symbol, (time.time() - market_ts) / 86400)
        return None

    change_pct = None
    if prev:
        try:
            change_pct = (float(price) / float(prev) - 1.0) * 100.0
        except Exception:
            change_pct = None

    quote = {
        "symbol": symbol,
        "ts": market_ts,
        "price": float(price),
        "prev_close": float(prev) if prev else None,
        "change_pct": change_pct,
        "currency": info.get("currency", ""),
        "exchange": info.get("exchange", ""),
        "market": market_hint(symbol),
        "source": source,
        "name": info.get("name", ""),
    }
    db.save_quote(symbol, quote)
    _quote_cache[symbol] = (time.time(), quote)
    return quote


async def fetch_quotes(symbols: list[str], force: bool = False) -> list[dict]:
    """Batch quotes with bounded concurrency (5 at a time keeps us polite)."""
    sem = asyncio.Semaphore(5)
    out: list[dict] = []

    async def one(sym: str) -> None:
        async with sem:
            try:
                q = await fetch_quote(sym, force=force)
                if q:
                    out.append(q)
            except Exception as exc:
                log.warning("quote %s failed: %s", sym, exc)

    await asyncio.gather(*(one(s) for s in symbols))
    return out


async def refresh_candles(symbols: list[str], rng: str = "2y") -> dict[str, int]:
    sem = asyncio.Semaphore(4)
    result: dict[str, int] = {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                result[sym.upper()] = await fetch_candles(sym, rng=rng)
            except Exception as exc:
                log.warning("candles %s failed: %s", sym, exc)
                result[sym.upper()] = 0

    await asyncio.gather(*(one(s) for s in symbols))
    return result


def cached_price(symbol: str) -> Optional[float]:
    """Last known price from the local DB — no network."""
    row = db.get_conn().execute(
        "SELECT price FROM quotes WHERE symbol=?", (symbol.upper(),)).fetchone()
    if row and row["price"]:
        return float(row["price"])
    row = db.get_conn().execute(
        "SELECT close FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol.upper(),)
    ).fetchone()
    return float(row["close"]) if row and row["close"] else None


def candle_count(symbol: str) -> int:
    row = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM candles WHERE symbol=?", (symbol.upper(),)).fetchone()
    return int(row["n"]) if row else 0


def _previous_close(symbol: str) -> Optional[float]:
    """Close of the session BEFORE the newest stored bar.

    The newest bar is the session being quoted, so the previous close is the
    one behind it. Taking the newest bar instead makes every intraday change
    read 0.00% (price == that bar's close).
    """
    rows = db.get_conn().execute(
        "SELECT close FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 2",
        (symbol.upper(),)).fetchall()
    if len(rows) < 2:
        return None
    return float(rows[1]["close"]) if rows[1]["close"] else None


# --- symbol resolution ----------------------------------------------------

# Yahoo labels the Warsaw exchange "WSE"; older payloads used "WAR" and
# ex-regime listings report the MIC's "XWAR".
_WARSAW_EXCHANGES = {"WSE", "WAR", "XWAR"}


async def resolve_symbol(query: str) -> Optional[dict[str, Any]]:
    """Resolve a free-text query (company name or ticker) to a Yahoo symbol.

    Needed because a GPW ticker is not derivable from the company name —
    KGHM -> KGH.WA, mBank -> MBK.WA, Budimex -> BDX.WA — and Yahoo's Warsaw
    coverage is only partial. Returns ``None`` when nothing matches.
    """
    q = query.strip()
    if not q:
        return None

    # An exact symbol costs one request, so try it before searching.
    direct = await _yahoo_chart(q, "5d", "1d")
    if direct:
        _bars, info = _parse_yahoo(direct)
        return {"symbol": q.upper(), "name": info.get("name", ""),
                "exchange": info.get("exchange", ""),
                "market": market_hint(q), "match": "direct"}

    for host in YAHOO_HOSTS:
        try:
            resp = await _http().get(f"{host}/v1/finance/search",
                                     params={"q": q, "quotesCount": 12})
            if resp.status_code != 200:
                continue
            quotes = (resp.json() or {}).get("quotes") or []
        except Exception as exc:
            log.debug("search failed for %s: %s", q, exc)
            continue

        equities = [x for x in quotes
                    if x.get("quoteType") == "EQUITY" and x.get("symbol")]
        equities.sort(key=lambda x: -(x.get("score") or 0))
        # Prefer a Warsaw listing: the query is usually a Polish company name,
        # and the top-scored hit is often an OTC or German line instead.
        for want_warsaw in (True, False):
            for x in equities:
                sym = str(x["symbol"])
                exch = str(x.get("exchange", ""))
                is_warsaw = exch in _WARSAW_EXCHANGES or sym.upper().endswith((".WA", ".PL"))
                if is_warsaw != want_warsaw:
                    continue
                if not await _yahoo_chart(sym, "5d", "1d"):
                    continue
                return {"symbol": sym.upper(),
                        "name": x.get("longname") or x.get("shortname") or "",
                        "exchange": exch, "market": market_hint(sym), "match": "search"}
    return None
