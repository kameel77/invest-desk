"""Explore Yahoo's quoteSummary: which fields are actually populated.

The endpoint needs a cookie + crumb pair, and coverage differs per symbol
(a GPW small cap has no analyst estimates; an ETF has no fundamentals at all).
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

from app.market import _http  # reuse the app's client for identical headers

CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CONSENT_URL = "https://fc.yahoo.com/"
SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"

MODULES = ("price,summaryDetail,defaultKeyStatistics,financialData,"
           "assetProfile,calendarEvents,earnings,recommendationTrend")


async def get_crumb(client: httpx.AsyncClient) -> str:
    await client.get(CONSENT_URL)
    r = await client.get(CRUMB_URL)
    return r.text.strip()


def _flat(v):
    if isinstance(v, dict):
        return v.get("fmt", v.get("raw"))
    return v


def _session() -> httpx.AsyncClient:
    """A dedicated client: quoteSummary needs its own cookie jar (the consent
    cookie + crumb pair), which the shared market client never holds."""
    return httpx.AsyncClient(
        timeout=25.0,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        follow_redirects=True,
    )


async def dump(symbol: str) -> dict:
    async with _session() as client:
        crumb = await get_crumb(client)
        r = await client.get(SUMMARY_URL + symbol, params={"modules": MODULES, "crumb": crumb})
        result = ((r.json() or {}).get("quoteSummary") or {}).get("result")
        # `result` comes back as a one-element list, or null on an unknown symbol.
        if isinstance(result, list):
            result = result[0] if result else None
        if not isinstance(result, dict):
            return {}
        return {mod: {k: _flat(v) for k, v in (data or {}).items()}
                for mod, data in result.items()}


async def main() -> None:
    for sym in (sys.argv[1:] or ["CDR.WA", "AAPL", "SPY"]):
        data = await dump(sym)
        print(f"\n{'='*70}\n{sym}  ({len(data)} modules)")
        for mod, fields in data.items():
            populated = {k: v for k, v in fields.items() if v not in (None, {}, [])}
            print(f"  {mod}: {len(populated)}/{len(fields)} populated")
            if mod in ("price", "summaryDetail", "financialData", "calendarEvents"):
                for k, v in list(populated.items())[:22]:
                    print(f"     {k} = {str(v)[:52]}")


if __name__ == "__main__":
    asyncio.run(main())
