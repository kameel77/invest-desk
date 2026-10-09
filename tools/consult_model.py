"""Narzędzie do konsultacji z modelami LLM natywnie przez Antigravity (agy).

Domyślny model podstawowy: claude-opus-5-5-medium
Fallback:                 claude-sonnet-5-5-medium

Użycie:
    python3 tools/consult_model.py "Pytanie do modelu"
    cat prompt.txt | python3 tools/consult_model.py
    python3 -c "from tools.consult_model import consult; print(consult('Hej'))"
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

PRIMARY_MODEL = "claude-opus-5-5-medium"
FALLBACK_MODEL = "claude-sonnet-5-5-medium"

AGY_BIN = Path(os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "/home/clawdbot/.local/bin/agy"))
if not AGY_BIN.exists():
    AGY_BIN = Path("/home/clawdbot/.local/bin/agy")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("consult_model")


def _run_agy(model: str, prompt: str, timeout: int = 180) -> str:
    cmd = [
        str(AGY_BIN),
        "--model", model,
        "--print", prompt,
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        raise RuntimeError(f"agy zwrócił kod {res.returncode}: {err}")
    out = res.stdout.strip()
    if not out:
        raise RuntimeError("agy zwrócił pustą odpowiedź.")
    return out


def consult(
    prompt: str,
    primary_model: str = PRIMARY_MODEL,
    fallback_model: str = FALLBACK_MODEL,
    timeout: int = 180,
) -> str:
    """Wysyła prompt do modelu podstawowego, a w razie błędu przełącza się na fallback."""
    try:
        logger.info("Wysyłanie zapytania do modelu podstawowego: %s", primary_model)
        return _run_agy(primary_model, prompt, timeout=timeout)
    except Exception as exc:
        logger.warning(
            "Model %s napotkał błąd (%s). Uruchamianie fallbacku: %s",
            primary_model, exc, fallback_model
        )
        try:
            return _run_agy(fallback_model, prompt, timeout=timeout)
        except Exception as fb_exc:
            logger.error("Fallback do %s również zakończony błędem: %s", fallback_model, fb_exc)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Konsultacja z modelem Claude w Antigravity.")
    parser.add_argument("prompt", nargs="?", help="Treść zapytania (lub ze stdin)")
    parser.add_argument("--primary", default=PRIMARY_MODEL, help=f"Model główny (domyślnie: {PRIMARY_MODEL})")
    parser.add_argument("--fallback", default=FALLBACK_MODEL, help=f"Model zapasowy (domyślnie: {FALLBACK_MODEL})")
    parser.add_argument("--timeout", type=int, default=180, help="Timeout w sekundach (domyślnie: 180)")
    args = parser.parse_args()

    if args.prompt:
        prompt_text = args.prompt
    elif not sys.stdin.isatty():
        prompt_text = sys.stdin.read().strip()
    else:
        parser.print_help()
        sys.exit(1)

    if not prompt_text:
        print("Błąd: pusty prompt.", file=sys.stderr)
        sys.exit(1)

    response = consult(
        prompt=prompt_text,
        primary_model=args.primary,
        fallback_model=args.fallback,
        timeout=args.timeout,
    )
    print(response)


if __name__ == "__main__":
    main()
