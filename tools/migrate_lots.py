"""One-off migration: fold the old merged ``positions`` rows into lots.

The pre-lot model stored one row per symbol with an averaged cost, so the
old positions cannot be split back into their original transactions. Each is
therefore migrated as a SINGLE lot carrying the averaged entry — the P&L
attribution for anything opened before the migration stays approximate, and
this script says so rather than pretending otherwise.

Run once:  .venv/bin/python -m tools.migrate_lots
"""
from __future__ import annotations

import json

from app import db, execution


def main() -> None:
    db.init_db()
    conn = db.get_conn()

    existing = list(conn.execute("SELECT * FROM lots"))
    if existing:
        print(f"Tabela lots zawiera juz {len(existing)} pozycji — nic do migracji.")
        return

    old = list(conn.execute("SELECT * FROM positions WHERE qty > 0"))
    if not old:
        print("Brak pozycji w starym modelu — migracja niepotrzebna.")
        return

    print(f"Migruje {len(old)} pozycji z modelu scalonego do transz...\n")
    for row in old:
        sym = row["symbol"]
        qty = float(row["qty"])
        # The old avg_price had the entry fee folded into it; the lot model
        # keeps the fee separate, so pull it back out for a clean P&L.
        avg = float(row["avg_price"])
        orders = conn.execute(
            "SELECT side, qty, realized_pnl FROM orders WHERE symbol=? ORDER BY ts",
            (sym,)).fetchall()
        buys = [o for o in orders if o["side"] == "BUY"]
        hyp = "trend"
        if buys:
            # infer the hypothesis from whichever signal opened it
            sig = conn.execute(
                "SELECT meta FROM signals WHERE symbol=? ORDER BY ts ASC LIMIT 1",
                (sym,)).fetchone()
            if sig:
                try:
                    hyp = (json.loads(sig["meta"] or "{}")).get("hypothesis", "trend")
                except Exception:
                    hyp = "trend"

        lot_id = db.open_lot(sym, qty, avg, 0.0, row["stop_price"], row["take_price"],
                             hyp, json.dumps({"migrated": True}))
        print(f"  lot#{lot_id:<4} {sym:9} qty={qty:<12.4f} avg={avg:>9.4f} "
              f"stop={row['stop_price']} cel={row['take_price']} hyp={hyp}")

    # Keep the legacy table in sync for anything still reading it.
    with db.tx() as c:
        c.execute("DELETE FROM positions")

    print(f"\nGotowe. Otwarte transzy: {len(db.open_lots())}")
    eq = execution.equity()
    print(f"Equity po migracji: {eq['equity']} (gotowka {eq['cash']}, "
          f"pozycje {eq['open_positions']}, transzy {eq['open_lots']})")


if __name__ == "__main__":
    main()
