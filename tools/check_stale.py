"""Diagnostic: is the quote's ts actually the market timestamp?

Yahoo's chart meta carries ``regularMarketTime``, but for a stale or
illiquid listing it can be months old while ``regularMarketPrice`` still
returns a number. The UI then shows a change computed against an ancient
prev_close, which reads as a wild move that never happened.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from app import config, db, market


async def main() -> None:
    db.init_db()
    quotes = await market.fetch_quotes(db.get_watchlist(), force=True)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    print(f"{'symbol':9} {'price':>10} {'chg%':>8} {'age_days':>9}  verdict")
    stale = []
    for q in sorted(quotes, key=lambda x: x["ts"]):
        age_days = (now - q["ts"]) / 86400
        bad = age_days > 5
        if bad:
            stale.append(q["symbol"])
        print(f"{q['symbol']:9} {q['price']:>10.2f} {q['change_pct'] or 0:>8.2f} "
              f"{age_days:>9.1f}  {'STALE' if bad else 'ok'}")
    print(f"\nstale quotes: {stale or 'NONE'}")


if __name__ == "__main__":
    asyncio.run(main())
