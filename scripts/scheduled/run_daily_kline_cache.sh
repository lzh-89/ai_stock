#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily kline cache refresh ====="
  python3 scripts/update_daily_kline_cache.py --days 80 --recent-runs 30
} >> "${LOG_DIR}/daily_kline_cache.log" 2>&1
