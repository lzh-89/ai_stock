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
from backend.app.services.stock_analysis_feedback_service import StockAnalysisFeedbackService
from backend.app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Update V2 stock analysis strategy trade outcomes")
    parser.add_argument("--code", default="", help="Optional 6-digit stock code")
    parser.add_argument("--from", dest="date_from", default="", help="Analysis date from, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default="", help="Analysis date to, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=None, help="Alias of --limit for backfill batches")
    parser.add_argument("--mode", choices=["backfill", "range", "recent", "latest", "force"], default="backfill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-history", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load_settings()
    service = StockAnalysisFeedbackService(settings, Database(settings.database_path), version="v2")
    result = service.update_trade_outcomes(
        code=args.code,
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.batch_size if args.batch_size is not None else args.limit,
        dry_run=args.dry_run,
        force_history=args.force_history,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
