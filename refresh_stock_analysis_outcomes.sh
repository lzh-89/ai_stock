#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

cd "${ROOT_DIR}"
python3 scripts/update_stock_analysis_outcomes.py "$@" 2>&1 | tee -a "${LOG_DIR}/stock_analysis_outcomes.log"
