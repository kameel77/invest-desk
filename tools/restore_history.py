"""Restore history that `execution.reset()` wiped, from a pre-reset backup.

Reset is a nuclear option for a paper account, and using it to tidy up test
data is exactly how a live equity curve disappears. This rebuilds from a
snapshot by MERGING — anything already present in the current database is
left alone, so running it twice is harmless.

    .venv/bin/python -m tools.restore_history data/trading.db.bak-prelots
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from app import db, execution

# Table -> (columns to copy). Written out rather than reflected so a schema
# change on either side fails loudly instead of silently copying junk.
MERGE: dict[str, list[str]] = {
    "equity": ["ts", "cash", "equity", "day_pnl"],
    "signals": ["ts", "symbol", "side", "score", "price", "strategy", "meta", "acted"],
    "orders": ["ts", "symbol", "side", "qty", "limit_price", "status",
               "reason", "signal_id", "realized_pnl"],
    "positions": ["symbol", "qty", "avg_price", "opened_ts", "stop_price",
                  "take_price", "realized_pnl"],
}


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "data/trading.db.bak-prelots")
    if not src.exists():
        raise SystemExit(f"Nie znaleziono kopii: {src}")
    if not db.get_conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lots'"
    ).fetchone():
        db.init_db()

    tmp = Path("data/_restore_src.db")
    shutil.copy2(src, tmp)
    s = sqlite3.connect(str(tmp))
    s.row_factory = sqlite3.Row
    cur = db.get_conn()

    total = 0
    for table, cols in MERGE.items():
        try:
            rows = s.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        except sqlite3.Error as exc:
            print(f"  {table:10} pominięta: {exc}")
            continue
        existing = {r[0] for r in cur.execute(
            f"SELECT ts FROM {table}")} if table != "positions" else {
            r[0] for r in cur.execute("SELECT symbol FROM positions")}
        added = 0
        for row in rows:
            key = row[0]
            if key in existing:
                continue
            ph = ", ".join("?" * len(cols))
            try:
                with db.tx() as c:
                    c.execute(f"INSERT INTO {table} ({', '.join(cols)}) "
                              f"VALUES ({ph})", tuple(row))
                added += 1
            except sqlite3.Error:
                continue
        total += added
        print(f"  {table:10} odtworzono {added} z {len(rows)}")
    s.close()
    tmp.unlink(missing_ok=True)

    # Positions restored as merged rows become lots, so performance is still
    # attributed per transaction.
    if not db.open_lots():
        legacy = [r for r in db.get_conn().execute("SELECT * FROM positions WHERE qty > 0")]
        if legacy:
            import json
            from app import config
            for row in legacy:
                hyp = "trend"
                sig = db.get_conn().execute(
                    "SELECT meta FROM signals WHERE symbol=? ORDER BY ts ASC LIMIT 1",
                    (row["symbol"],)).fetchone()
                if sig:
                    try:
                        hyp = (json.loads(sig["meta"] or "{}")).get("hypothesis", "trend")
                    except Exception:
                        hyp = "trend"
                qty, avg = float(row["qty"]), float(row["avg_price"])
                # The old model folded the entry fee into avg_price. For a
                # symbol with exactly one BUY that fee is recoverable: solve
                # fee = max(fill*qty*rate, min) against fill = avg - fee/qty.
                # Where two buys merged into one row the split is NOT
                # recoverable, so the lot is flagged approximate rather than
                # given an invented entry price.
                bought = db.get_conn().execute(
                    "SELECT SUM(qty) q FROM orders WHERE symbol=? AND side='BUY'",
                    (row["symbol"],)).fetchone()["q"] or 0
                fee, fill, exact = 0.0, avg, True
                if abs(float(bought) - qty) > 1e-6:
                    exact = False
                else:
                    lo, hi = 0.0, avg * qty
                    for _ in range(60):
                        f = (lo + hi) / 2
                        modelled = max((avg - f / qty) * qty * config.COMMISSION_PCT,
                                       config.COMMISSION_MIN)
                        if modelled > f:
                            lo = f
                        else:
                            hi = f
                    fee = round((lo + hi) / 2, 4)
                    fill = round(avg - fee / qty, 4)
                db.open_lot(row["symbol"], qty, fill, fee, row["stop_price"],
                            row["take_price"], hyp,
                            json.dumps({"restored": True, "entry_price_exact": exact}))
                if not exact:
                    print(f"  {row['symbol']:8} uwaga: dwie transze scalone w jeden "
                          f"wiersz - cena wejscia przyblizona ({avg:.4f})")
            print(f"  przeniesiono {len(legacy)} pozycji do transz")

    # Cash was overwritten by the reset. Reconstruct it from the account's own
    # arithmetic rather than from the last equity snapshot: that snapshot is
    # stamped at scan time, and any orders filled since (the reversion entries)
    # are not reflected in it. Taking it at face value yields an account that
    # holds positions it never paid for.
    invested = 0.0
    for r in db.get_conn().execute(
            "SELECT qty, entry_price, entry_fee FROM lots WHERE closed_ts IS NULL"):
        invested += float(r["qty"]) * float(r["entry_price"]) + float(r["entry_fee"] or 0)
    realized = float(db.get_conn().execute(
        "SELECT COALESCE(SUM(realized_pnl),0) s FROM lots "
        "WHERE closed_ts IS NOT NULL").fetchone()["s"])
    for r in db.get_conn().execute(
            "SELECT realized_pnl FROM orders WHERE side='SELL'"):
        realized += float(r["realized_pnl"] or 0)
    target = config.STARTING_CASH + realized - invested
    if abs(db.get_cash() - target) > 0.01:
        old = db.get_cash()
        db.set_cash(target)
        print(f"  gotowka odtworzona z arytmetyki konta: {old:,.2f} -> {target:,.2f}")
        print(f"    (kapital {config.STARTING_CASH:,.2f} - zainwestowane {invested:,.2f} "
              f"+ zrealizowane {realized:,.2f})")
        snap = db.get_conn().execute(
            "SELECT cash, equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if snap:
            print(f"    uwaga: ostatni snapshot equity pokazuje cash "
                  f"{float(snap['cash']):,.2f} — starszy niż wejscia rewersji, "
                  f"dlatego nie byl uzyty")

    # Cash is derived from the account's own history, so reconstruct it rather
    # than trusting whatever reset left behind.
    eq = execution.equity()
    print(f"\nOdtworzono {total} rekordow.")
    print(f"Equity: {eq['equity']} | gotowka: {eq['cash']} | "
          f"pozycje: {eq['open_positions']} | transzy: {eq['open_lots']}")


if __name__ == "__main__":
    main()
