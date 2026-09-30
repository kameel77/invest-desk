"""Probe which extra Yahoo modules are actually populated for GPW vs US names.

The point is to rank candidate data sources by what really arrives, not by
what the API documentation promises — the GPW coverage differs sharply from
the US, and a proposed signal is worthless if the field is empty.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app import config

CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CONSENT_URL = "https://fc.yahoo.com/"
SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"

MODULES = ("calendarEvents,earnings,earningsTrend,upgradeDowngradeHistory,"
           "majorHoldersBreakdown,recommendationTrend,indexTrend,"
           "incomeStatementHistory,balanceSheetHistory,cashflowStatementHistory")


def _session() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=25.0, headers={"User-Agent": config.USER_AGENT},
                             follow_redirects=True)


def _flat(v):
    if isinstance(v, dict):
        return v.get("fmt", v.get("raw"))
    return v


def _populated(data: dict) -> dict:
    return {k: _flat(v) for k, v in (data or {}).items() if v not in (None, "", {}, [])}


async def probe(symbol: str) -> dict:
    async with _session() as c:
        await c.get(CONSENT_URL)
        crumb = (await c.get(CRUMB_URL)).text.strip()
        r = await c.get(SUMMARY_URL + symbol, params={"modules": MODULES, "crumb": crumb})
        result = ((r.json() or {}).get("quoteSummary") or {}).get("result")
        if isinstance(result, list):
            result = result[0] if result else None
        return result if isinstance(result, dict) else {}


async def main() -> None:
    for sym in (sys.argv[1:] or ["CDR.WA", "PKN.WA", "AAPL", "NVDA"]):
        res = await probe(sym)
        print(f"\n{'='*66}\n{sym}  ({len(res)} modułów)")
        for mod, data in res.items():
            fields = _populated(data)
            if not fields:
                print(f"  {mod:30} PUSTE")
                continue
            if mod == "earningsTrend":
                rows = data.get("trend") or []
                est = rows[0].get("earningsEstimate", {}) if rows else {}
                print(f"  {mod:30} {len(fields)} pól | okresy={len(rows)} "
                      f"| eps est={_flat(est.get('avg'))} | wzrost yoy={_flat(est.get('growth'))}")
            elif mod == "upgradeDowngradeHistory":
                hist = (data.get("history") or [])
                print(f"  {mod:30} {len(hist)} zmian rekomendacji")
                for h in hist[:3]:
                    d = h.get("epochGradeDate")
                    ds = _flat(d) if not isinstance(d, dict) else d.get("fmt", "?")
                    print(f"      {ds} {_flat(h.get('firm'))}: "
                          f"{h.get('fromGrade')} -> {h.get('toGrade')} ({h.get('action')})")
            elif mod == "majorHoldersBreakdown":
                print(f"  {mod:30} insiders={fields.get('insidersPercentHeld')} "
                      f"instytucje={fields.get('institutionsPercentHeld')}")
            elif mod == "calendarEvents":
                ed = (data.get("earnings") or {}).get("earningsDate") or []
                div = data.get("exDividendDate")
                print(f"  {mod:30} wyniki={[ _flat(x.get('raw')) for x in ed][:2]} "
                      f"| dywidenda={_flat(div)}")
            elif mod == "recommendationTrend":
                rows = data.get("trend") or []
                print(f"  {mod:30} {len(rows)} okresów")
            elif mod.endswith("History"):
                rows = data.get(list(data.keys())[0]) or []
                print(f"  {mod:30} {len(rows)} okresów")
            else:
                print(f"  {mod:30} {len(fields)} pól")


if __name__ == "__main__":
    asyncio.run(main())
