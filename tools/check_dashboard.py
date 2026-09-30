"""Static consistency check: every id app.js touches must exist in index.html.

A dashboard that renders blank is the classic failure here — FastAPI serves
the files happily, so the wiring is only checked at runtime in a browser.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
html = (ROOT / "app" / "static" / "index.html").read_text()
js = "\n".join((ROOT / "app" / "static" / f).read_text()
        for f in ("app.js", "fundamentals.js", "analytics.js", "backtest.js"))

ids_html = set(re.findall(r'id="([^"]+)"', html))
ids_js = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', js))

missing = sorted(ids_js - ids_html)
unused = sorted(ids_html - ids_js)

tabs = set(re.findall(r'data-view="([^"]+)"', html))
views = set(re.findall(r'id="view-([^"]+)"', html))

print(f"ids in index.html : {len(ids_html)}")
print(f"ids used by js   : {len(ids_js)}")
print(f"missing ids       : {missing or 'NONE'}")
print(f"unused ids        : {unused or 'NONE'}")
print(f"tabs == views     : {tabs == views}  ({sorted(tabs)})")

ok = not missing and tabs == views
print("RESULT:", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
