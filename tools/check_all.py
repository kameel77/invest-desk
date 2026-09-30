"""Full health check: every endpoint the dashboard calls, plus the renderer.

Run with the server up:  .venv/bin/python -m tools.check_all
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8770"
PY = str(ROOT / ".venv" / "bin" / "python")


def get(path: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def post(path: str, body: dict) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def main() -> int:
    failures: list[str] = []
    watchlist = get("/api/watchlist")[1].get("symbols", [])
    sample = (watchlist or ["CDR.WA"])[:3]

    checks: list[tuple[str, str, bool]] = [
        ("health", "/api/health", True),
        ("account", "/api/account", True),
        ("quotes", "/api/quotes", True),
        ("equity", "/api/equity", True),
        ("signals", "/api/signals", True),
        ("orders", "/api/orders", True),
    ]
    for name, path, _ in checks:
        status, body = get(path)
        ok = status == 200
        summary = f"{len(body)} keys" if isinstance(body, dict) else str(body)[:40]
        print(f"{'ok ' if ok else 'FAIL'} {name:9} HTTP {status}  {summary}")
        if not ok:
            failures.append(f"{name} -> HTTP {status}: {body}")

    for sym in sample:
        status, body = get(f"/api/candles/{sym}?limit=200")
        cnt = body.get("count", 0) if isinstance(body, dict) else 0
        ok = status == 200 and cnt > 0
        print(f"{'ok ' if ok else 'FAIL'} candles {sym:9} HTTP {status}  bars={cnt}")
        if not ok:
            failures.append(f"candles {sym} -> HTTP {status} bars={cnt}")

        status, body = get(f"/api/analysis/{sym}")
        ok = status == 200
        score = body.get("score") if isinstance(body, dict) else None
        print(f"{'ok ' if ok else 'FAIL'} analysis {sym:8} HTTP {status}  score={score}")
        if not ok:
            failures.append(f"analysis {sym} -> HTTP {status}")

    for sym in sample:
        r = subprocess.run([PY, "-m", "tools.check_chart_render", sym],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        ok = "RESULT: OK" in r.stdout
        line = next((l for l in r.stdout.splitlines() if "fillRect" in l), "")
        print(f"{'ok ' if ok else 'FAIL'} render  {sym:9} {line}")
        if not ok:
            failures.append(f"render {sym}")

    print("\n" + ("ALL CHECKS PASSED" if not failures else "FAILURES:\n  " + "\n  ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
