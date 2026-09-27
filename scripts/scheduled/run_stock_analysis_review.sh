#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] stock analysis review scheduled start"
  "${ROOT_DIR}/review_stock_analysis_failures.sh" --limit 120
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] stock analysis review scheduled done"
} >> "${LOG_DIR}/stock_analysis_review.log" 2>&1
