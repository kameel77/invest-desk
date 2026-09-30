"""FastAPI application: REST API + static dashboard + background scanner."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, execution, fundamentals, market, scanner, signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("trading")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Trading Desk",
    description="Signal generation and paper trading across GPW and US markets.",
    version="1.0.0",
)

_scan_lock = asyncio.Lock()
_scan_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    for sym in db.get_watchlist():
        db.add_to_watchlist(sym)
    log.info("paper account: cash=%.2f equity=%.2f", db.get_cash(), execution.equity()["equity"])
    global _scan_task
    _scan_task = asyncio.create_task(_scan_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _scan_task:
        _scan_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _scan_task
    await market.close_client()
    await fundamentals.close_client()


async def _scan_loop() -> None:
    """Warm the data, scan, then keep scanning on the configured interval."""
    await asyncio.sleep(2)
    while True:
        try:
            if _scan_lock.locked():
                await asyncio.sleep(config.SCAN_INTERVAL)
                continue
            async with _scan_lock:
                res = await scanner.scan(auto_execute=False)
            log.info("scan: %s symbols, %s new signals, %.1fs",
                     res.get("analyzed"), len(res.get("signals", [])),
                     res.get("duration", 0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("scan loop error: %s", exc)
        await asyncio.sleep(config.SCAN_INTERVAL)


# --- models ---------------------------------------------------------------

class OrderIn(BaseModel):
    symbol: str
    qty: Optional[float] = None
    side: str = Field(pattern="^(?i)(BUY|SELL)$")
    signal_id: Optional[int] = None
    reason: str = "manual"
    # Sell exactly one transaction instead of closing the whole position.
    lot_id: Optional[int] = None
    # Which engine is asking — decides sizing and the risk levels.
    hypothesis: str = "trend"


class WatchIn(BaseModel):
    symbol: str


# --- health / account -----------------------------------------------------

@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "time": int(time.time()),
        "alpaca_realtime": config.ALPACA_REALTIME,
        "starting_cash": config.STARTING_CASH,
        "commission_pct": config.COMMISSION_PCT,
        "watchlist_size": len(db.get_watchlist()),
        "last_scan": scanner.last_scan(),
    }


@app.get("/api/account")
async def account() -> dict[str, Any]:
    return execution.equity()


@app.post("/api/account/reset")
async def account_reset() -> dict[str, Any]:
    execution.reset()
    return execution.equity()


# --- market data ----------------------------------------------------------

@app.get("/api/watchlist")
async def watchlist() -> dict[str, Any]:
    return {"symbols": db.get_watchlist()}


@app.post("/api/watchlist")
async def watchlist_add(item: WatchIn) -> dict[str, Any]:
    """Add by ticker or by company name (``"KGHM"`` resolves to ``KGH.WA``)."""
    raw = item.symbol.strip()
    if not raw:
        raise HTTPException(400, "symbol required")
    resolved = await market.resolve_symbol(raw)
    if not resolved:
        raise HTTPException(404, f"nie znaleziono instrumentu: {raw}")
    db.add_to_watchlist(resolved["symbol"], resolved.get("name", ""))
    await market.fetch_candles(resolved["symbol"])
    return {"added": resolved, "symbols": db.get_watchlist()}


@app.get("/api/resolve")
async def resolve(q: str) -> dict[str, Any]:
    """Symbol lookup without adding — useful for tooling and autocomplete."""
    resolved = await market.resolve_symbol(q)
    if not resolved:
        raise HTTPException(404, f"nie znaleziono instrumentu: {q}")
    return resolved


@app.delete("/api/watchlist/{symbol}")
async def watchlist_remove(symbol: str) -> dict[str, Any]:
    db.remove_from_watchlist(symbol)
    return {"symbols": db.get_watchlist()}


@app.get("/api/quotes")
async def quotes(refresh: bool = False) -> dict[str, Any]:
    universe = db.get_watchlist()
    if refresh:
        await market.fetch_quotes(universe, force=True)
    rows = db.get_quotes()
    return {"quotes": [dict(r) for r in rows], "count": len(rows)}


@app.get("/api/candles/{symbol}")
async def candles(symbol: str, limit: int = 250, refresh: bool = False) -> dict[str, Any]:
    symbol = symbol.upper()
    if refresh or market.candle_count(symbol) == 0:
        await market.fetch_candles(symbol, force=refresh)
    rows = db.get_candles(symbol, limit=min(limit, 1000))
    return {
        "symbol": symbol,
        "count": len(rows),
        "bars": [
            {"ts": r["ts"], "o": r["open"], "h": r["high"], "l": r["low"],
             "c": r["close"], "v": r["volume"]} for r in rows
        ],
    }


@app.get("/api/analysis/{symbol}")
async def analysis(symbol: str) -> dict[str, Any]:
    symbol = symbol.upper()
    if market.candle_count(symbol) == 0:
        await market.fetch_candles(symbol)
    res = signals.evaluate(symbol)
    if not res:
        raise HTTPException(404, f"not enough history for {symbol}")
    return res


@app.get("/api/reversion/{symbol}")
async def reversion_for(symbol: str) -> dict[str, Any]:
    """Mean-reversion read: z-score vs the 20-day mean, stretch in ATR, and
    whether the instrument currently qualifies for entry."""
    from . import reversion as rv
    a = rv.evaluate(symbol)
    if not a:
        raise HTTPException(404, f"brak historii dla {symbol}")
    a["bars_since_extreme"] = rv.bars_since_extreme(symbol)
    a["sizing"] = rv.size(a, db.get_cash())
    return a


@app.get("/api/reversion")
async def reversion_all() -> dict[str, Any]:
    """The reversion screen for the whole watchlist, most stretched first."""
    from . import reversion as rv
    rows = []
    for sym in db.get_watchlist():
        a = rv.evaluate(sym)
        if a:
            rows.append(a)
    rows.sort(key=lambda r: r["z"] if r["z"] is not None else 0.0)
    return {"eligible": [r for r in rows if r["eligible"]], "all": rows,
            "count": len(rows)}


@app.get("/api/lots")
async def lots(open_only: bool = False, limit: int = 200) -> dict[str, Any]:
    """Individual transactions — the unit performance is reported on.
    A position is the sum of its lots; this endpoint is where you see the
    entries separately, with the level each one was given at its own entry.
    """
    if open_only:
        return {"lots": [execution.lot_view(r) for r in db.open_lots()],
                "count": len(db.open_lots())}
    return {"lots": db.closed_lots(limit), "stats": db.lot_stats(),
            "open": [execution.lot_view(r) for r in db.open_lots()]}


@app.get("/api/decisions")
async def decisions(limit: int = 200, symbol: str = "",
                    action: str = "") -> dict[str, Any]:
    """The decision journal: what the system judged, and the state it saw.

    Append-only and never reset — a signal that was declined is as much a
    record of the strategy's thinking as one that became an order.
    """
    rows = db.recent_decisions(limit=limit, symbol=symbol or None,
                               action=action or None)
    total = db.get_conn().execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    counts = {r["action"]: r["n"] for r in db.get_conn().execute(
        "SELECT action, COUNT(*) n FROM decisions GROUP BY action")}
    return {"decisions": rows, "total": total, "counts": counts}


@app.get("/api/fundamentals/{symbol}")
async def fundamentals_for(symbol: str, refresh: bool = False) -> dict[str, Any]:
    """Profile, valuation, dividend and analyst data for one instrument.

    Served from the local cache when fresh; Yahoo is only hit on a miss, since
    this endpoint needs a cookie+crumb handshake and is easy to rate-limit.
    """
    symbol = symbol.upper()
    if not refresh:
        cached = fundamentals.get_cached_fundamentals(symbol)
        if cached and (time.time() - int(cached.get("fetched_ts") or 0)) < fundamentals.FUND_TTL:
            return cached
    data = await fundamentals.fetch_fundamentals(symbol, force=refresh)
    if data.get("available"):
        await fundamentals.save_fundamentals(data)
    else:
        cached = fundamentals.get_cached_fundamentals(symbol)
        if cached:
            cached = dict(cached)
            cached["stale"] = True
            cached["error"] = data.get("error")
            return cached
    return data


@app.get("/api/fundamentals")
async def fundamentals_all(symbols: Optional[str] = None) -> dict[str, Any]:
    """Fundamentals for the whole watchlist (or a comma-separated subset)."""
    universe = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
                if symbols else db.get_watchlist())
    return {"fundamentals": await fundamentals.fetch_many(universe)}


@app.get("/api/scan")
async def scan_now(symbols: Optional[str] = None, execute: bool = False) -> JSONResponse:
    if _scan_lock.locked():
        return JSONResponse({"status": "busy", "message": "a scan is already running"}, status_code=409)
    universe = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    async with _scan_lock:
        result = await scanner.scan(universe, auto_execute=execute)
    return JSONResponse(result)


# --- signals --------------------------------------------------------------

@app.get("/api/signals")
async def list_signals(limit: int = 50) -> dict[str, Any]:
    return {"signals": db.recent_signals(limit=limit)}


@app.get("/api/signals/stats")
async def signal_stats(days: int = 30) -> dict[str, Any]:
    """Aggregates the dashboard and the signals view need: counts, score
    distribution, which markets produce signals, and how much of the universe
    currently clears the entry threshold."""
    from . import analytics
    return analytics.signal_stats(days=days)


@app.get("/api/market/stats")
async def market_stats() -> dict[str, Any]:
    """Cross-sectional snapshot of the watchlist: score distribution, best and
    worst movers, and how many names sit above the entry threshold."""
    from . import analytics
    return analytics.market_stats()


# --- backtesting ----------------------------------------------------------

@app.get("/api/backtest/{symbol}")
async def backtest_symbol(symbol: str, buy: Optional[float] = None,
                          stop_atr: Optional[float] = None,
                          target_atr: Optional[float] = None,
                          max_hold: Optional[int] = None) -> dict[str, Any]:
    """Walk one symbol's stored candles and report what the strategy would do."""
    from . import backtest as bt
    p = bt.BacktestParams()
    if buy is not None:
        p.buy_threshold = buy
    if stop_atr is not None:
        p.stop_atr = stop_atr
    if target_atr is not None:
        p.target_atr = target_atr
    if max_hold is not None:
        p.max_hold_days = max_hold
    # The walk is CPU-bound (indicators per bar per symbol) — keep it off the loop.
    return await asyncio.to_thread(bt.backtest_symbol, symbol.upper(), p)


@app.get("/api/backtest")
async def backtest_all(buy: Optional[float] = None) -> dict[str, Any]:
    """Backtest the whole watchlist. ``buy`` overrides the entry threshold."""
    from . import backtest as bt
    p = bt.BacktestParams()
    if buy is not None:
        p.buy_threshold = buy
    return await asyncio.to_thread(bt.backtest_many, db.get_watchlist(), p)


@app.get("/api/backtest/sweep")
async def backtest_sweep() -> dict[str, Any]:
    """Run the full parameter grid. Slow (minutes) — the dashboard shows a
    warning, and the daily job runs the same grid headless."""
    from . import backtest as bt
    return await asyncio.to_thread(bt.sweep, db.get_watchlist())


@app.get("/api/walkforward")
async def walkforward(folds: int = 5, train_years: float = 2.0,
                      test_months: float = 6.0,
                      symbols: Optional[str] = None) -> dict[str, Any]:
    """Anchored walk-forward: tune on train, score the winner on unseen bars."""
    from . import walkforward as wf
    universe = ([s.strip().upper() for s in symbols.split(",") if s.strip()]
                if symbols else db.get_watchlist())
    return await asyncio.to_thread(wf.run, universe, folds, train_years, test_months)


@app.get("/api/context/{symbol}")
async def context_for(symbol: str) -> dict[str, Any]:
    """Regime / relative-strength / macro read for one instrument."""
    from . import context as ctx_mod
    from . import signals as sig
    symbol = symbol.upper()
    df = sig.load_frame(symbol, limit=1000)
    if df is None:
        raise HTTPException(404, f"brak historii dla {symbol}")
    df = sig.compute_indicators(df)
    ctx = ctx_mod.build_context(df, symbol)
    c = ctx_mod.context_at(ctx, len(df) - 1)
    allowed, reasons, size = ctx_mod.score_context(c, min_rs=0.0)
    return {
        "symbol": symbol,
        "benchmark": ctx.get("benchmark"),
        "available": bool(ctx.get("available")),
        **c,
        "would_trade": allowed,
        "size_factor": size,
        "reasons": reasons,
    }


# --- orders / positions ---------------------------------------------------

@app.get("/api/orders")
async def orders(limit: int = 100) -> dict[str, Any]:
    return {"orders": db.recent_orders(limit=limit)}


@app.post("/api/orders")
async def place_order(inp: OrderIn) -> dict[str, Any]:
    sym = inp.symbol.strip().upper()
    if market.cached_price(sym) is None:
        await market.fetch_quote(sym, force=True)
    if inp.side.upper() == "BUY":
        if inp.qty is None or inp.qty <= 0:
            # Size from the hypothesis that is asking, not always the trend
            # engine: a reversion entry carries its own risk budget.
            if inp.hypothesis == "reversion":
                from . import reversion as rv
                rva = rv.evaluate(sym)
                if not rva:
                    raise HTTPException(400, f"qty required (no analysis for {sym})")
                inp.qty = rv.size(rva, db.get_cash())["qty"]
            else:
                analysis = signals.evaluate(sym)
                if not analysis:
                    raise HTTPException(400, f"qty required (no analysis for {sym})")
                inp.qty = signals.size_position(analysis, db.get_cash())["qty"]
        return execution.buy(sym, float(inp.qty), signal_id=inp.signal_id,
                             reason=inp.reason, hypothesis=inp.hypothesis)
    # lot_id sells exactly one transaction and leaves the others untouched.
    if inp.lot_id is not None:
        return execution.sell_lot(int(inp.lot_id), reason=inp.reason or "manual")
    return execution.sell(sym, inp.qty, signal_id=inp.signal_id, reason=inp.reason,
                          hypothesis=inp.hypothesis if inp.hypothesis != "trend" else None)


@app.get("/api/positions")
async def positions() -> dict[str, Any]:
    return execution.equity()


@app.get("/api/equity")
async def equity_curve(limit: int = 500) -> dict[str, Any]:
    return {"curve": db.equity_curve(limit=limit)}


# --- static dashboard -----------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def dashboard() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(404, "dashboard not built")
    return FileResponse(str(index))
