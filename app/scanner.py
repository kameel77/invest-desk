"""Scanner — walks the watchlist, evaluates every symbol, emits signals.

Runs on a timer in the background (see ``app/main.py``) and on demand from the
dashboard's "Run scan" button.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from . import config, db, execution, market, reversion, signals

log = logging.getLogger("trading.scanner")

_last_scan: dict[str, Any] = {"ts": 0, "count": 0, "duration": 0.0}

# The context gate is OFF for live signals by default. Walk-forward is what
# decides whether regime/RS/VIX earn their keep; until that says so, blocking
# live alerts on them ships an untested rule to the one place it can cost
# real money. The code path is exercised by the backtester either way.
USE_CONTEXT = False


def _context_gate(symbol: str) -> tuple[bool, list[str]]:
    """Would the context filter allow a long entry in ``symbol``?"""
    from . import context as ctx_mod
    from . import signals as sig
    try:
        df = sig.compute_indicators(sig.load_frame(symbol, limit=1000))
        if df is None or len(df) < 60:
            return True, []
        c = ctx_mod.context_at(ctx_mod.build_context(df, symbol), len(df) - 1)
        allowed, reasons, _size = ctx_mod.score_context(c, min_rs=0.0)
        return allowed, reasons
    except Exception as exc:
        log.debug("context gate %s failed: %s", symbol, exc)
        return True, []


def _generate_signal(analysis: dict[str, Any], holding: bool) -> dict[str, Any] | None:
    """Turn a score into a BUY/SELL signal, or None when nothing qualifies."""
    score = float(analysis["score"])
    sym = analysis["symbol"]
    price = float(analysis["price"])

    if holding:
        # Exits are mechanical: score drops, or the stop already fired.
        if score <= signals.SELL_THRESHOLD:
            return {"ts": int(time.time()), "symbol": sym, "side": "SELL", "score": score,
                    "price": price, "strategy": "trend-momentum-exit",
                    "reasons": analysis["reasons"] + ["exit: composite score below threshold"],
                    "meta": {"components": analysis["components"], "holding": True}}
        return None

    if not analysis["liquidity_ok"]:
        return None
    if score >= signals.BUY_THRESHOLD:
        sizing = signals.size_position(analysis, db.get_cash())
        if sizing["qty"] <= 0:
            return None
        return {"ts": int(time.time()), "symbol": sym, "side": "BUY", "score": score,
                "price": price, "strategy": "trend-momentum-long",
                "reasons": analysis["reasons"],
                "meta": {"components": analysis["components"], "sizing": sizing,
                         "rsi": analysis["rsi"], "adv": analysis["adv"]}}
    return None


async def scan(symbols: list[str] | None = None, auto_execute: bool = False) -> dict[str, Any]:
    started = time.time()
    universe = symbols or db.get_watchlist()
    if not universe:
        return {"error": "empty watchlist"}

    # 1. History, 2. quotes, 3. analyse.
    await market.refresh_candles(universe, rng="2y")
    await market.fetch_quotes(universe)

    # Symbols with at least one open lot. The reversion engine treats a symbol
    # it already holds as "manage the exit", not "look for another entry".
    held = {r["symbol"] for r in db.open_lots()}
    results: list[dict[str, Any]] = []
    new_signals: list[dict[str, Any]] = []

    def _work(sym: str) -> dict[str, Any] | None:
        try:
            return signals.evaluate(sym)
        except Exception as exc:
            log.warning("analysis %s failed: %s", sym, exc)
            return None

    analyses = await asyncio.to_thread(_run_analyses, universe, _work)
    # Each symbol is evaluated by BOTH hypotheses. A name can legitimately be a
    # trend long and a reversion short at the same time, so the two are kept
    # separate rather than merged into one verdict.
    rv = _run_reversions(universe, held)

    def _record(sig: dict[str, Any], analysis: dict[str, Any]) -> None:
        """Persist a candidate signal, honouring the context gate and dedupe."""
        hyp = (sig.get("meta") or {}).get("hypothesis", "trend")
        if sig["side"] == "BUY" and USE_CONTEXT and hyp != reversion.HYPOTHESIS:
            allowed, ctx_reasons = _context_gate(analysis["symbol"])
            if not allowed:
                log.info("sygnał %s odrzucony przez kontekst: %s",
                         analysis["symbol"], "; ".join(ctx_reasons))
                analysis["context_blocked"] = ctx_reasons
                return
            analysis["context_notes"] = ctx_reasons
        # One signal per symbol+side+hypothesis per 12h. Keying on the
        # hypothesis matters: the two systems can legitimately want opposite
        # things on the same name.
        recent = db.get_conn().execute(
            "SELECT ts, meta FROM signals WHERE symbol=? AND side=? "
            "ORDER BY ts DESC LIMIT 8", (analysis["symbol"], sig["side"]),
        ).fetchall()
        for row in recent:
            try:
                prev = (json.loads(row["meta"] or "{}")).get("hypothesis", "trend")
            except Exception:
                prev = "trend"
            if prev == hyp and (sig["ts"] - int(row["ts"])) < 12 * 3600:
                return
        sig["id"] = db.add_signal(sig)
        new_signals.append(sig)
        # The journal records the judgement together with the state that
        # justified it, so a call can be reviewed later even if it never
        # became an order.
        db.record_decision(
            ts=sig.get("ts"), symbol=sig["symbol"], side=sig["side"],
            hypothesis=hyp, action="SIGNAL", price=sig.get("price"),
            score=sig.get("score"),
            reason=sig.get("strategy"),
            reasons=sig.get("reasons") or [],
            state={"cash": round(db.get_cash(), 2),
                   "open_lots": len(db.open_lots()),
                   **{k: v for k, v in (sig.get("meta") or {}).items()
                      if k not in ("sizing",)}},
            ref_type="signal", ref_id=sig["id"])

    for analysis in analyses:
        results.append(analysis)
        for sig in (_generate_signal(analysis, analysis["symbol"] in held),
                    rv.get(analysis["symbol"])):
            if sig is None:
                continue
            _record(sig, analysis)

    executed: list[dict[str, Any]] = []
    if auto_execute and new_signals:
        executed = execute_signals(new_signals)

    triggers = execution.check_stops()
    snapshot = equity_snapshot()
    _last_scan.update({"ts": int(started), "count": len(results),
                       "duration": round(time.time() - started, 2)})

    results.sort(key=lambda a: a["score"], reverse=True)
    return {
        "ts": int(started),
        "universe_size": len(universe),
        "analyzed": len(results),
        "signals": sorted(new_signals, key=lambda s: -abs(float(s["score"]))),
        "executed": executed,
        "stop_triggers": triggers,
        "equity": snapshot,
        "results": results,
        "duration": _last_scan["duration"],
    }


def _run_analyses(universe: list[str], worker) -> list[dict[str, Any]]:
    out = []
    for sym in universe:
        analysis = worker(sym)
        if analysis:
            out.append(analysis)
    return out


def _reversion_signal(symbol: str, holding: bool) -> dict[str, Any] | None:
    """Buy a price stretched below its own mean; sell when the mean returns.

    Mirrors the walk-forward run exactly: 20-day mean, 2.0 sigma entry, exit
    at z >= 0, 10-bar time stop. Runs beside the trend engine rather than
    replacing it — both feed the same paper account, each tagged with its
    hypothesis, so the live comparison is the real test of the backtest.
    """
    a = reversion.evaluate(symbol)
    if a is None:
        return None
    now = int(time.time())
    if holding:
        z = a["z"]
        if z is not None and z >= 0:
            return {"ts": now, "symbol": symbol.upper(), "side": "SELL",
                    "score": round(a["strength"] * 100, 1), "price": a["price"],
                    "strategy": "reversion-exit-mean",
                    "reasons": [f"cena wróciła do średniej ({z:+.2f}σ)"],
                    "meta": {"hypothesis": reversion.HYPOTHESIS, "z": z}}
        return None
    if not a["eligible"]:
        return None
    sizing = reversion.size(a, db.get_cash())
    if sizing["qty"] <= 0:
        return None
    return {"ts": now, "symbol": symbol.upper(), "side": "BUY",
            "score": round(a["strength"] * 100, 1), "price": a["price"],
            "strategy": "reversion-long", "reasons": a["reasons"],
            "meta": {"hypothesis": reversion.HYPOTHESIS, "sizing": sizing,
                     "z": a["z"], "target_mean": a["target_mean"],
                     "max_hold_days": reversion.HOLD_DAYS}}


def _run_reversions(universe: list[str], held: set[str]) -> dict[str, dict[str, Any]]:
    """{symbol: signal} for the reversion hypothesis across the universe."""
    out: dict[str, dict[str, Any]] = {}
    for sym in universe:
        try:
            sig = _reversion_signal(sym, sym in held)
        except Exception as exc:
            log.debug("reversion %s failed: %s", sym, exc)
            continue
        if sig is not None:
            out[sym] = sig
    return out


def execute_signals(signal_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn signals into paper orders (size capped at 15% of equity).

    Each BUY opens its own lot tagged with the hypothesis that produced it,
    carrying that hypothesis's stop and target. A second BUY on the same symbol
    opens a second lot rather than averaging into the first — the two calls
    stay separately attributable.
    """
    out: list[dict[str, Any]] = []
    for sig in signal_list:
        meta = sig.get("meta") or {}
        hyp = meta.get("hypothesis", "trend")
        if sig["side"] == "BUY":
            sizing = meta.get("sizing") or {}
            qty = float(sizing.get("qty") or 0)
            if qty <= 0:
                out.append({"symbol": sig["symbol"], "status": "SKIPPED", "reason": "size below min ticket"})
                continue
            res = execution.buy(sig["symbol"], qty, signal_id=sig.get("id"),
                                reason=sig.get("strategy", "signal"),
                                hypothesis=hyp,
                                # The reversion sizing dict carries the stop it
                                # derived from the live ATR; the target is left
                                # to execution, which asks the same hypothesis
                                # for its levels. Passing the stop as a target
                                # would silently cap the trade at the loss.
                                stop=sizing.get("stop"), target=None,
                                entry_meta={k: v for k, v in meta.items() if k != "sizing"})
        else:
            res = execution.sell(sig["symbol"], signal_id=sig.get("id"),
                                 reason=sig.get("strategy", "signal"),
                                 hypothesis=hyp if hyp != "trend" else None)
        out.append({"symbol": sig["symbol"], "side": sig["side"], **res})
    return out


def equity_snapshot() -> dict[str, Any]:
    snap = execution.equity()
    curve = db.equity_curve(2)
    prev = curve[-2]["equity"] if len(curve) >= 2 else config.STARTING_CASH
    snap["day_pnl"] = round(snap["equity"] - float(prev), 2)
    db.snapshot_equity(snap["equity"], snap["day_pnl"])
    return snap


def last_scan() -> dict[str, Any]:
    return dict(_last_scan)
