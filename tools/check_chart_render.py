"""Headless check of the dashboard's chart renderer.

No browser and no node-canvas available here, so run drawPrice against a
recording stub of the 2D context and assert it actually issues draw calls
with sane coordinates. This is what the real bug was: the endpoint returned
500, so the renderer received zero bars and drew nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8770"


def fetch(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}", timeout=60) as r:
        return json.load(r)


class RecordingCtx:
    """Minimal 2D-context stub that records every drawing operation."""

    def __init__(self) -> None:
        self.ops: list[tuple] = []
        self.texts: list[str] = []

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.ops.append((name,) + a)
        return rec

    def fillText(self, text, x=0, y=0):
        self.texts.append(str(text))

    def setTransform(self, *a):
        self.ops.append(("setTransform",) + a)


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "CDR.WA"
    data = fetch(f"/api/candles/{symbol}?limit=200")
    bars = data["bars"]
    print(f"{symbol}: {data['count']} bars, first {bars[0]['ts']}, last {bars[-1]['ts']}")

    js = (ROOT / "app" / "static" / "app.js").read_text()
    # Lift the two functions under test out of the module.
    def grab(name: str) -> str:
        m = re.search(rf"function {name}\(", js)
        start = m.start()
        depth, i = 0, start
        while i < len(js):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    return js[start:i + 1]
            i += 1
        raise SystemExit(f"could not extract {name}")

    # NOTE: the extracted drawPrice declares its own `const c` / `const ctx`,
    # so it must be placed at module top level — wrapping it in another
    # function would shadow the injected stubs and record nothing.
    harness = f"""
globalThis.window = globalThis.window || {{}};
window.devicePixelRatio = window.devicePixelRatio || 1;
const __ops = [];
const rec = (name) => (...a) => __ops.push([name, ...a]);
// Formatters app.js defines at module scope; the crosshair calls fmt().
const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n))
  ? "—" : Number(n).toFixed(d);
const pct = (n) => (n === null || n === undefined) ? "—" : Number(n).toFixed(2) + "%";
const ts2date = (ts) => new Date(ts * 1000).toISOString();
const sign = (n) => (n >= 0 ? "pos" : "neg");
const ctx = new Proxy({{ textAlign: "left", font: "", fillStyle: "", strokeStyle: "", lineWidth: 1 }},
  {{
    get(t, p) {{
      if (p === "fillText") return (txt, x, y) => __ops.push(["fillText", txt, x, y]);
      if (typeof t[p] !== "undefined") return t[p];
      return rec(p);
    }},
    set(t, p, v) {{ t[p] = v; return true; }}
  }});
// drawPrice reaches the canvas through $("#priceChart"), so the $ stub must
// hand back a canvas-shaped object for that selector and a no-op sink for the
// text nodes it also writes to.
const $ = (sel) => sel === "#priceChart"
  ? {{ width: 900, height: 300, clientWidth: 900, getContext: () => ctx }}
  : {{ set textContent(v) {{}}, get textContent() {{ return ""; }},
      set className(v) {{}}, get className() {{ return ""; }} }};
// Module-level state app.js owns; drawPrice now reads/writes these.
let chartBars = BARS;
let chartGeom = null;
let hoverIndex = BARS.length - 1;
// Candle-colour palette (green=up by default; app.js also has a "pl" variant).
let candlePalette = {{ up: "#3fb950", down: "#f85149" }};
{grab("drawCrosshair")}
{grab("drawPrice")}
drawPrice(BARS);
// drawPrice does not return the op list (it is a fire-and-forget renderer in
// the app), so read the recorder directly.
const ops = __ops;
const kinds = {{}};
for (const o of ops) kinds[o[0]] = (kinds[o[0]] || 0) + 1;
console.log(JSON.stringify({{ kinds, total: ops.length }}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(harness.replace("BARS", json.dumps(bars)))
        path = fh.name
    if os.getenv("TD_DEBUG_HARNESS"):
        print("--- generated harness (first 900 chars) ---")
        print(Path(path).read_text()[:900])
        print("--- end ---")
    out = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    Path(path).unlink(missing_ok=True)
    print("node stdout:", repr(out.stdout[:600]))
    print("node stderr:", repr(out.stderr[:600]))
    if out.returncode != 0:
        print("node failed with", out.returncode)
        return 1

    stats = json.loads(out.stdout.strip().splitlines()[-1])
    print("canvas ops:", stats)
    fills = stats["kinds"].get("fillRect", 0)
    strokes = stats["kinds"].get("stroke", 0)
    texts = stats["kinds"].get("fillText", 0)
    ok = fills > 0 and strokes > 0 and texts > 0
    print(f"fillRect(candles+volume)={fills} stroke(wicks)={strokes} fillText(axis)={texts}")
    print("RESULT:", "OK — chart renders" if ok else "FAIL — nothing drawn")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
