"""Paper trading on a lot (transaction) basis.

Every buy opens its own lot with its own entry price, stop, target and
hypothesis. A position is the SUM of its open lots — never a merged average.
Two things fall out of that:

* **Attribution.** The P&L of a decision belongs to that decision. Averaging
  two entries into one cost basis makes a winning lot look like a loser
  whenever the second entry was worse, which is exactly how a strategy gets
  blamed for a decision it did not make.
* **Per-lot risk.** Each lot's stop and target are set at ITS entry. A
  position opened across three calls does not share one stop that no single
  call justified.

Disposal is FIFO by default, which is also the order Polish PIT expects for
computing taxable gains.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from . import config, db, market, signals

log = logging.getLogger("trading.execution")

# Time stops, in trading days. The trend engine used 20 bars and the
# reversion engine 10 (the value its walk-forward run settled on). Without a
# time stop a losing lot waits forever for a mean reversion that never comes.
MAX_HOLD_BARS = {"trend": 20, "reversion": 10}
DEFAULT_MAX_HOLD = 20


def _commission(notional: float) -> float:
    return max(notional * config.COMMISSION_PCT, config.COMMISSION_MIN) if notional > 0 else 0.0


def _fill_price(price: float, side: str) -> float:
    """Slippage: buys pay a little more, sells receive a little less."""
    slip = price * config.SLIPPAGE_PCT
    return price + slip if side == "BUY" else max(price - slip, 0.0)


def bars_held(opened_ts: int) -> float:
    """Approximate age in trading days (252 days per 365 calendar days)."""
    return (time.time() - int(opened_ts)) / 86400.0 / 365.0 * 252


def lot_view(row: Any) -> dict[str, Any]:
    """One open lot, marked to market."""
    qty = float(row["qty"])
    entry = float(row["entry_price"])
    fee = float(row["entry_fee"] or 0)
    price = market.cached_price(row["symbol"]) or entry
    try:
        meta = json.loads(row["entry_meta"] or "{}")
    except Exception:
        meta = {}
    return {
        "lot_id": row["id"],
        "symbol": row["symbol"],
        "hypothesis": row["hypothesis"],
        "qty": qty,
        "entry_price": round(entry, 4),
        "entry_fee": round(fee, 2),
        "last_price": round(price, 4),
        "value": round(qty * price, 2),
        "unrealized_pnl": round((price - entry) * qty - fee, 2),
        "unrealized_pct": round(((price / entry) - 1) * 100, 2) if entry else 0.0,
        "stop": row["stop_price"],
        "target": row["take_price"],
        "opened_ts": row["opened_ts"],
        "bars_held": round(bars_held(row["opened_ts"]), 1),
        "max_hold": MAX_HOLD_BARS.get(row["hypothesis"], DEFAULT_MAX_HOLD),
        "entry_meta": meta,
    }


def equity() -> dict[str, Any]:
    """Cash plus every open lot, marked to market."""
    cash = db.get_cash()
    lots = [lot_view(r) for r in db.open_lots()]
    market_value = sum(l["value"] for l in lots)

    by_symbol: dict[str, dict[str, Any]] = {}
    for l in lots:
        acc = by_symbol.setdefault(l["symbol"], {
            "symbol": l["symbol"], "qty": 0.0, "value": 0.0, "lots": 0,
            "unrealized_pnl": 0.0, "cost": 0.0, "last_price": l["last_price"],
            "hypotheses": [],
        })
        acc["qty"] += l["qty"]
        acc["value"] += l["value"]
        acc["unrealized_pnl"] += l["unrealized_pnl"]
        acc["cost"] += l["entry_price"] * l["qty"]
        acc["lots"] += 1
        if l["hypothesis"] not in acc["hypotheses"]:
            acc["hypotheses"].append(l["hypothesis"])

    positions = []
    for acc in by_symbol.values():
        acc["avg_price"] = round(acc["cost"] / acc["qty"], 4) if acc["qty"] else 0.0
        acc["unrealized_pct"] = round(
            ((acc["last_price"] / acc["avg_price"]) - 1) * 100, 2) if acc["avg_price"] else 0.0
        sym_lots = [l for l in lots if l["symbol"] == acc["symbol"]]
        acc["stop"] = min((l["stop"] for l in sym_lots if l["stop"]), default=None)
        acc["target"] = max((l["target"] for l in sym_lots if l["target"]), default=None)
        acc["opened_ts"] = min(l["opened_ts"] for l in sym_lots)
        positions.append(acc)
    positions.sort(key=lambda a: -abs(a["unrealized_pnl"]))

    realized = db.get_conn().execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM lots "
        "WHERE closed_ts IS NOT NULL").fetchone()["total"]

    total = cash + market_value
    return {
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "equity": round(total, 2),
        "total_return_pct": round((total / config.STARTING_CASH - 1) * 100, 2)
        if config.STARTING_CASH else 0.0,
        "realized_pnl": round(float(realized or 0.0), 2),
        "open_positions": len(positions),
        "open_lots": len(lots),
        "positions": positions,
        "lots": lots,
    }


def _default_levels(symbol: str, hypothesis: str,
                    entry: Optional[float] = None
                    ) -> tuple[Optional[float], Optional[float]]:
    """Stop and target for a lot, measured from ITS entry price.

    The distance matters more than the level: levels derived from the live
    price and then applied to a different fill produce a stop that is already
    breached the moment the lot opens. Sizing a tranche off its own entry is
    what makes per-lot risk meaningful.
    """
    try:
        if hypothesis == "reversion":
            from . import reversion as rv
            a = rv.evaluate(symbol)
            if not a:
                return (None, None)
            atr, dist_stop, dist_take = a["atr"], rv.STOP_ATR, rv.TARGET_ATR
        else:
            a = signals.evaluate(symbol)
            if not a:
                return (None, None)
            atr = a.get("atr") or (a.get("suggested_stop") and abs(
                a.get("last_price", 0) - a["suggested_stop"])) or None
            dist_stop, dist_take = 2.0, 3.0
        if not atr:
            return (None, None)
        base = float(entry) if entry else None
        if base is None:
            # No entry given: fall back to the levels the analysis proposed.
            return (a.get("stop") or a.get("suggested_stop"),
                    a.get("take") or a.get("suggested_target"))
        return (round(base - dist_stop * float(atr), 4),
                round(base + dist_take * float(atr), 4))
    except Exception as exc:
        log.debug("levels for %s failed: %s", symbol, exc)
        return (None, None)


def buy(symbol: str, qty: float, signal_id: Optional[int] = None,
        reason: str = "", limit_price: Optional[float] = None,
        hypothesis: str = "trend", stop: Optional[float] = None,
        target: Optional[float] = None,
        entry_meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Open ONE lot. Buying the same symbol again opens a second lot with its
    own risk levels — it never averages into the first one."""
    symbol = symbol.upper()
    price = limit_price or market.cached_price(symbol)
    if not price:
        db.record_decision(action="REJECTED", symbol=symbol, side="BUY",
                           hypothesis=hypothesis, qty=qty,
                           reason=f"brak ceny dla {symbol}")
        return {"status": "REJECTED", "reason": f"no price for {symbol}"}
    fill = _fill_price(float(price), "BUY")
    notional = fill * qty
    fee = _commission(notional)
    cash = db.get_cash()
    if notional + fee > cash:
        db.record_decision(action="REJECTED", symbol=symbol, side="BUY",
                           hypothesis=hypothesis, qty=qty, price=fill,
                           reason=f"za mało gotówki: potrzeba "
                                  f"{notional + fee:,.2f}, jest {cash:,.2f}",
                           state={"cash": round(cash, 2),
                                  "needed": round(notional + fee, 2)})
        return {"status": "REJECTED",
                "reason": f"insufficient cash: need {notional + fee:,.2f}, have {cash:,.2f}"}

    if stop is None or target is None:
        d_stop, d_target = _default_levels(symbol, hypothesis, entry=fill)
        stop = stop if stop is not None else d_stop
        target = target if target is not None else d_target

    # A stop at or above the fill would be breached on the first tick, and a
    # target below it is not a target. Clamp rather than trust the caller.
    if stop is not None and stop >= fill:
        stop = round(fill * 0.95, 4)
    if target is not None and target <= fill:
        target = round(fill * 1.06, 4)

    lot_id = db.open_lot(symbol, qty, fill, fee, stop, target, hypothesis,
                         json.dumps(entry_meta or {}, ensure_ascii=False), signal_id)
    db.set_cash(cash - notional - fee)
    db.add_order({
        "ts": int(time.time()), "symbol": symbol, "side": "BUY", "qty": qty,
        "limit_price": limit_price, "status": "FILLED",
        "reason": reason or "manual", "signal_id": signal_id, "realized_pnl": 0.0,
    })
    if signal_id:
        db.mark_signal_acted(signal_id)
    db.record_decision(action="FILLED", symbol=symbol, side="BUY",
                       hypothesis=hypothesis, price=fill, qty=qty,
                       reason=reason or "manual",
                       state={"cash_after": round(cash - notional - fee, 2),
                              "stop": stop, "target": target,
                              "open_lots": len(db.open_lots(symbol))},
                       ref_type="lot", ref_id=lot_id)
    log.info("BUY lot#%s %s qty=%.4f @ %.4f hyp=%s", lot_id, symbol, qty, fill, hypothesis)
    return {"status": "FILLED", "lot_id": lot_id, "symbol": symbol, "qty": qty,
            "price": round(fill, 4), "fee": round(fee, 2), "hypothesis": hypothesis,
            "stop": stop, "target": target, "cash_after": round(cash - notional - fee, 2)}


def sell_lot(lot_id: int, reason: str = "",
             limit_price: Optional[float] = None) -> dict[str, Any]:
    """Close one specific lot — the unit the P&L is reported on."""
    row = db.get_conn().execute(
        "SELECT * FROM lots WHERE id=? AND closed_ts IS NULL", (lot_id,)).fetchone()
    if row is None:
        return {"status": "REJECTED", "reason": f"lot {lot_id} not open"}

    symbol = row["symbol"]
    qty = float(row["qty"])
    entry = float(row["entry_price"])
    entry_fee = float(row["entry_fee"] or 0)
    price = limit_price or market.cached_price(symbol)
    if not price:
        return {"status": "REJECTED", "reason": f"no price for {symbol}"}
    fill = _fill_price(float(price), "SELL")
    notional = fill * qty
    fee = _commission(notional)
    pnl = (fill - entry) * qty - entry_fee - fee

    db.close_lot(lot_id, fill, fee, reason, pnl)
    db.set_cash(db.get_cash() + notional - fee)
    db.add_order({
        "ts": int(time.time()), "symbol": symbol, "side": "SELL", "qty": qty,
        "limit_price": limit_price, "status": "FILLED",
        "reason": reason or "manual", "signal_id": row["signal_id"],
        "realized_pnl": round(pnl, 2),
    })
    try:
        held_meta = json.loads(row["entry_meta"] or "{}")
    except Exception:
        held_meta = {}
    db.record_decision(action="EXIT", symbol=symbol, side="SELL",
                       hypothesis=row["hypothesis"], price=fill, qty=qty,
                       reason=reason or "manual", realized_pnl=round(pnl, 2),
                       state={"entry_price": round(entry, 4),
                              "entry_z": held_meta.get("z"),
                              "held_bars": round(bars_held(row["opened_ts"]), 1),
                              "cash_after": round(db.get_cash(), 2)},
                       ref_type="lot", ref_id=lot_id)
    log.info("SELL lot#%s %s qty=%.4f @ %.4f pnl=%.2f (%s)",
             lot_id, symbol, qty, fill, pnl, reason)
    return {"status": "FILLED", "lot_id": lot_id, "symbol": symbol, "qty": qty,
            "price": round(fill, 4), "fee": round(fee, 2),
            "realized_pnl": round(pnl, 2), "hypothesis": row["hypothesis"],
            "hold_pct": round(((fill / entry) - 1) * 100, 2) if entry else 0.0,
            "reason": reason}


def sell(symbol: str, qty: Optional[float] = None, signal_id: Optional[int] = None,
         reason: str = "", limit_price: Optional[float] = None,
         hypothesis: Optional[str] = None) -> dict[str, Any]:
    """Close lots for a symbol, oldest first (FIFO).

    ``qty=None`` closes every open lot for that symbol. Splitting a single lot
    is deliberately refused: a transaction was decided as a whole, and a
    half-lot has no meaning for the decision record.
    """
    symbol = symbol.upper()
    lots = db.open_lots(symbol, hypothesis)
    if not lots:
        db.record_decision(action="REJECTED", symbol=symbol, side="SELL",
                           hypothesis=hypothesis or "trend", qty=qty,
                           reason=f"brak otwartej pozycji w {symbol}")
        return {"status": "REJECTED", "reason": f"no open position in {symbol}"}

    remaining = float(qty) if qty is not None else None
    closed: list[dict[str, Any]] = []
    for lot in lots:
        lot_qty = float(lot["qty"])
        if remaining is not None and lot_qty > remaining + 1e-9:
            db.record_decision(action="REJECTED", symbol=symbol, side="SELL",
                               hypothesis=hypothesis or "trend", qty=remaining,
                               reason=f"nie można podzielić transzy: {lot_qty:g} "
                                      f"szt. w lot#{lot['id']}, żądano {remaining:g}",
                               state={"lot_id": lot["id"], "lot_qty": lot_qty})
            return {"status": "REJECTED",
                    "reason": f"cannot split a lot: {lot_qty:g} shares open "
                              f"in lot#{lot['id']}, {remaining:g} requested"}
        res = sell_lot(int(lot["id"]), reason=reason, limit_price=limit_price)
        if res["status"] != "FILLED":
            return res
        closed.append(res)
        if remaining is not None:
            remaining -= lot_qty
            if remaining <= 1e-9:
                break

    return {"status": "FILLED", "symbol": symbol, "lots_closed": len(closed),
            "realized_pnl": round(sum(r["realized_pnl"] for r in closed), 2),
            "cash_after": db.get_cash(), "closed": closed}


def check_exits() -> list[dict[str, Any]]:
    """Apply the per-lot exit rules: stop, target, and the time stop.

    Each lot is judged on its own levels, so a second tranche bought lower
    carries its own wider stop instead of inheriting the first tranche's.
    """
    triggered: list[dict[str, Any]] = []
    for lot in list(db.open_lots()):
        symbol = lot["symbol"]
        price = market.cached_price(symbol)
        if not price:
            continue
        stop, target = lot["stop_price"], lot["take_price"]
        hyp = lot["hypothesis"]
        reason = None

        if stop and price <= float(stop):
            reason = "stop-loss"
        elif target and price >= float(target):
            reason = "take-profit"
        else:
            held = bars_held(lot["opened_ts"])
            limit = MAX_HOLD_BARS.get(hyp, DEFAULT_MAX_HOLD)
            if held >= limit:
                reason = f"time-stop {int(held)}/{limit}"

        if reason:
            res = sell_lot(int(lot["id"]), reason=reason)
            triggered.append({"lot_id": lot["id"], "symbol": symbol,
                              "hypothesis": hyp, "trigger": reason,
                              "price": price, "result": res})
    return triggered


def check_stops() -> list[dict[str, Any]]:
    """Backwards-compatible alias used by the scanner."""
    return check_exits()


def reset(archive: bool = True) -> dict[str, Any]:
    """Wipe the paper account back to starting cash.
    Archives a full copy first, always. This function is nuclear — reaching
    for it to tidy up test data once destroyed a live equity curve, and a
    paper account is the only record of whether a strategy works. The
    decision journal is never touched: what the system decided outlives what
    it currently holds.
    """
    import shutil
    from pathlib import Path

    src = config.DB_PATH
    saved = None
    if archive and Path(src).exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        # Ścieżka z DATA_DIR, nie względna do katalogu roboczego. Względna
        # oznaczała, że archiwum lądowało w CWD procesu — przy starcie
        # z innego katalogu zgubilibyśmy kopię, a testy zaścigały produkcyjne
        # data/ własnymi archiwami.
        archive_dir = Path(config.DATA_DIR)
        archive_dir.mkdir(parents=True, exist_ok=True)
        saved = str(archive_dir / f"account_{stamp}.db")
        shutil.copy2(src, saved)
    with db.tx() as c:
        c.execute("DELETE FROM lots")
        c.execute("DELETE FROM positions")
        c.execute("DELETE FROM orders")
        c.execute("DELETE FROM signals")
        c.execute("DELETE FROM equity")
    db.set_cash(config.STARTING_CASH)
    return {"status": "RESET", "archive": saved, "starting_cash": config.STARTING_CASH}
