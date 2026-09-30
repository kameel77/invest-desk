#!/usr/bin/env bash
# Daily signal-quality test. Intended for launchd / cron.
#   crontab entry: 30 18 * * 1-5  ~/Documents/Coding/github/trading-desk/run_daily.sh
# (weekdays only — GPW and US cash sessions are Mon-Fri)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p reports logs
LOG="logs/daily_backtest_$(date +%Y%m%d).log"
{
  echo "=== $(date '+%F %T') start ==="
  .venv/bin/python -m tools.daily_backtest
  echo "=== $(date '+%F %T') done (exit $?) ==="
} >>"$LOG" 2>&1
tail -40 "$LOG"
