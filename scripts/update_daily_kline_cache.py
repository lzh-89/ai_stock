from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.database import Database
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.daily_kline_cache_service import DailyKlineCacheService
from backend.app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh daily K-line data used by T+1 validation")
    parser.add_argument("--days", type=int, default=80)
    parser.add_argument("--recent-runs", type=int, default=30)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load_settings()
    database = Database(settings.database_path)
    service = DailyKlineCacheService(AnalysisService(settings, database), database)
    result = service.refresh(days=args.days, recent_runs=args.recent_runs)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("updated") or result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
