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
from backend.app.services.stock_analysis_feedback_service import StockAnalysisFeedbackService, append_review_log
from backend.app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate DeepSeek review report for failed stock analysis cases")
    parser.add_argument("--from", dest="date_from", default="", help="Analysis date from, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default="", help="Analysis date to, YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--version", choices=["v1", "v2"], default="v1", help="Feedback table version")
    parser.add_argument("--dry-run", action="store_true", help="Do not save report and lessons")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load_settings()
    service = StockAnalysisFeedbackService(settings, Database(settings.database_path), version=args.version)
    append_review_log(f"review start version={args.version} from={args.date_from} to={args.date_to} limit={args.limit} dry_run={args.dry_run}")
    result = service.review_failures(
        date_from=args.date_from,
        date_to=args.date_to,
        limit=args.limit,
        save=not args.dry_run,
    )
    append_review_log(
        f"review done saved={result.get('saved')} report_id={result.get('reportId')} cases={result.get('caseCount')}"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
