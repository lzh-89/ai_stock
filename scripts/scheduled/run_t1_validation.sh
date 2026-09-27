#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
SLOT="${1:-$(date '+%H:%M')}"
mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') T+1 validation freeze ${SLOT} ====="
  python3 scripts/update_t1_overnight.py validation-freeze
} >> "${LOG_DIR}/t1-overnight-scheduled.log" 2>&1
