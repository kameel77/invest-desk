"""Resolve a GPW company name to its Yahoo ``.WA`` ticker.

Yahoo's Warsaw coverage is partial and the suffixes are not guessable
(``KGHM`` -> ``KGH.WA``, not ``KGHM.WA``), so resolve through the search
endpoint. Run directly: ``python -m tools.resolve_gpw "KGHM" "mBank"``.
"""
from __future__ import annotations

import sys

import httpx

UA = {"User-Agent": "Mozilla/5.0"}


def resolve(name: str) -> str | None:
    with httpx.Client(timeout=15, headers=UA) as c:
        d = c.get("https://query1.finance.yahoo.com/v1/finance/search",
                  params={"q": name, "quotesCount": 12}).json()
    # Yahoo reports the Warsaw exchange as "WSE" (older payloads used "WAR",
    # and ex-regime listings show the MIC's "XWAR") — accept all three.
    hits = [q for q in d.get("quotes", [])
            if str(q.get("exchange")) in {"WSE", "WAR", "XWAR"}
            and str(q.get("symbol", "")).endswith(".WA")]
    if not hits:
        return None
    hits.sort(key=lambda q: -(q.get("score") or 0))
    return hits[0]["symbol"]


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        sym = resolve(arg)
        print(f"{arg:26} -> {sym or 'NIE ZNALEZIONO'}")
