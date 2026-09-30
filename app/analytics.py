"""Cross-sectional analytics for the dashboard and the signals view.

Everything here is derived from data already in SQLite — no extra network
calls — so these endpoints stay cheap enough to poll.
"""
from __future__ import annotations

import time
from typing import Any

from . import db, execution, signals


def _pct_of(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 1) if denominator else 0.0


def signal_stats(days: int = 30) -> dict[str, Any]:
    """Counts and distribution of signals over the trailing window."""
    since = int(time.time()) - days * 86400
    conn = db.get_conn()

    total = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE ts >= ?", (since,)).fetchone()["n"]
    acted = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE ts >= ? AND acted = 1", (since,)).fetchone()["n"]

    by_side = {r["side"]: r["n"] for r in conn.execute(
        "SELECT side, COUNT(*) n FROM signals WHERE ts >= ? GROUP BY side", (since,))}

    by_market = []
    for row in conn.execute(
        "SELECT UPPER(SUBSTR(symbol, INSTR(symbol, '.') + 1)) AS suffix, COUNT(*) n "
        "FROM signals WHERE ts >= ? GROUP BY suffix ORDER BY n DESC", (since,)
    ):
        suffix = row["suffix"] or ""
        by_market.append({
            "market": {"WA": "GPW", "PL": "GPW"}.get(suffix, "US/ETF"),
            "count": row["n"],
        })

    by_day = [
        {"date": time.strftime("%Y-%m-%d", time.localtime(r["ts"])), "count": r["n"]}
        for r in conn.execute(
            "SELECT ts, COUNT(*) n FROM signals WHERE ts >= ? GROUP BY DATE(ts) ORDER BY ts", (since,))
    ]

    by_symbol = [
        {"symbol": r["symbol"], "count": r["n"], "avg_score": round(r["avg"], 1)}
        for r in conn.execute(
            "SELECT symbol, COUNT(*) n, AVG(score) avg FROM signals WHERE ts >= ? "
            "GROUP BY symbol ORDER BY n DESC, avg DESC LIMIT 12", (since,))
    ]

    score_rows = [r["score"] for r in conn.execute(
        "SELECT score FROM signals WHERE ts >= ?", (since,))]
    buckets = {">=50": 0, "35-50": 0, "20-35": 0, "0-20": 0, "<0": 0}
    for s in score_rows:
        if s >= 50:
            buckets[">=50"] += 1
        elif s >= signals.BUY_THRESHOLD:
            buckets["35-50"] += 1
        elif s >= 20:
            buckets["20-35"] += 1
        elif s >= 0:
            buckets["0-20"] += 1
        else:
            buckets["<0"] += 1

    top_reasons: dict[str, int] = {}
    for row in conn.execute("SELECT reasons FROM signals WHERE ts >= ?", (since,)):
        import json
        try:
            for r in json.loads(row["reasons"] or "[]"):
                key = r.split(" ")[0].lower() + " " + (r.split(" ")[1].lower() if len(r.split(" ")) > 1 else "")
                top_reasons[key] = top_reasons.get(key, 0) + 1
        except Exception:
            continue

    return {
        "window_days": days,
        "total": total,
        "executed": acted,
        "execution_rate": _pct_of(acted, total),
        "by_side": by_side,
        "by_market": by_market,
        "by_day": by_day,
        "top_symbols": by_symbol,
        "score_buckets": buckets,
        "top_reasons": [{"reason": k, "count": v} for k, v in
                        sorted(top_reasons.items(), key=lambda x: -x[1])[:8]],
        "thresholds": {"buy": signals.BUY_THRESHOLD, "sell": signals.SELL_THRESHOLD},
    }


def market_stats() -> dict[str, Any]:
    """Where the whole watchlist stands right now."""
    universe = db.get_watchlist()
    analyses: list[dict[str, Any]] = []
    for sym in universe:
        a = signals.evaluate(sym)
        if a:
            analyses.append(a)
    analyses.sort(key=lambda a: a["score"], reverse=True)

    quote_rows = {r["symbol"]: r for r in db.get_quotes()}
    gainers, losers = [], []
    for sym, q in quote_rows.items():
        if q["change_pct"] is None:
            continue
        row = {"symbol": sym, "change_pct": q["change_pct"], "price": q["price"]}
        (gainers if q["change_pct"] > 0 else losers).append(row)
    gainers.sort(key=lambda r: -r["change_pct"])
    losers.sort(key=lambda r: r["change_pct"])

    above = [a for a in analyses if a["score"] >= signals.BUY_THRESHOLD]
    below = [a for a in analyses if a["score"] <= signals.SELL_THRESHOLD]
    avg = round(sum(a["score"] for a in analyses) / len(analyses), 1) if analyses else 0.0

    # Which component is driving the average score right now.
    comp_avg = {k: round(sum(a["components"][k] for a in analyses) / len(analyses), 3)
                for k in ("trend", "momentum", "reversion")} if analyses else {}

    account = execution.equity()
    return {
        "universe": len(universe),
        "analyzed": len(analyses),
        "avg_score": avg,
        "above_buy_threshold": len(above),
        "below_sell_threshold": len(below),
        "component_averages": comp_avg,
        "top": [{"symbol": a["symbol"], "score": a["score"], "market": a["market"],
                 "rsi": a["rsi"], "roc_fast": a["roc_fast"],
                 "components": a["components"], "price": a["price"]} for a in analyses[:10]],
        "bottom": [{"symbol": a["symbol"], "score": a["score"], "market": a["market"],
                    "rsi": a["rsi"], "roc_fast": a["roc_fast"],
                    "components": a["components"], "price": a["price"]} for a in analyses[-8:]],
        "gainers": gainers[:6],
        "losers": losers[:6],
        "account": {
            "equity": account["equity"],
            "cash": account["cash"],
            "total_return_pct": account["total_return_pct"],
            "realized_pnl": account["realized_pnl"],
            "open_positions": account["open_positions"],
        },
    }
