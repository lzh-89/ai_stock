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
    parser = argparse.ArgumentParser(description="Update stock analysis outcome labels and returns")
    parser.add_argument("--code", default="", help="Optional 6-digit stock code")
    parser.add_argument("--from", dest="date_from", default="", help="Analysis date from, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default="", help="Analysis date to, YYYY-MM-DD")
    parser.add_argument("--horizons", default="3,5,7,10", help="Comma separated trading-day horizons")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=None, help="Alias of --limit for backfill batches")
    parser.add_argument("--mode", choices=["backfill", "range", "recent", "latest", "force"], default="backfill")
    parser.add_argument("--version", choices=["v1", "v2"], default="v1", help="Feedback table version")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-history", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load_settings()
    service = StockAnalysisFeedbackService(settings, Database(settings.database_path), version=args.version)
    result = service.update_outcomes(
        code=args.code,
        date_from=args.date_from,
        date_to=args.date_to,
        horizons=_parse_horizons(args.horizons),
        limit=args.batch_size if args.batch_size is not None else args.limit,
        dry_run=args.dry_run,
        force_history=args.force_history,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _parse_horizons(value: str) -> list[int]:
    result = []
    for part in str(value or "").split(","):
        try:
            number = int(part.strip())
        except ValueError:
            continue
        if number > 0:
            result.append(number)
    return result or [3, 5, 7, 10]


if __name__ == "__main__":
    sys.exit(main())
