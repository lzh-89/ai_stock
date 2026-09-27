#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] stock analysis outcomes scheduled refresh start"
  "${ROOT_DIR}/refresh_stock_analysis_outcomes.sh" --limit 1000
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] stock analysis outcomes scheduled refresh done"
} >> "${LOG_DIR}/stock_analysis_outcomes.log" 2>&1
