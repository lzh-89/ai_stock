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
from backend.app.services.t1_overnight_selection_service import T1OvernightSelectionService
from backend.app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run T+1 generation and validation jobs without the web server")
    parser.add_argument("action", choices=["verify", "backfill", "verify-pending", "auto-run", "validation-freeze"])
    parser.add_argument("--run-id", type=int, action="append", default=[])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--slot", default="")
    parser.add_argument("--scope", choices=["final", "all"], default="final")
    parser.add_argument("--period", choices=["1m", "15m"], default="1m")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    settings = load_settings()
    database = Database(settings.database_path)
    service = T1OvernightSelectionService(settings, AnalysisService(settings, database), database)

    if args.action == "verify":
        if not args.run_id:
            parser.error("verify requires at least one --run-id")
        result = {
            "count": len(args.run_id),
            "results": [service.verify_run(run_id, trigger_source="scheduled_cli") for run_id in args.run_id],
        }
    elif args.action == "backfill":
        if not args.run_id:
            parser.error("backfill requires at least one --run-id")
        result = {
            "count": len(args.run_id),
            "results": [
                service.backfill_validation_minutes(
                    run_id,
                    scope=args.scope,
                    force=args.force,
                    period=args.period,
                )
                for run_id in args.run_id
            ],
        }
    elif args.action == "verify-pending":
        result = service.verify_pending_runs(limit=max(1, args.limit))
    elif args.action == "auto-run":
        result = service.run_auto_run_due(
            trigger_source="scheduled_cli",
            slot=args.slot,
            force=args.force,
        )
    else:
        result = service.run_validation_auto_freeze_due(
            trigger_source="scheduled_cli",
            slot=args.slot,
            force=args.force,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
