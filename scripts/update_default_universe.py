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
from backend.app.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the dynamic default universe used by Tencent provider")
    parser.add_argument("--source", choices=["auto", "sina", "akshare"], default="auto")
    parser.add_argument("--size", type=int, default=80)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    settings = load_settings()
    service = AnalysisService(settings, Database(settings.database_path))
    result = service.refresh_default_universe(source=args.source, max_size=args.size, force=True)
    summary = {
        "updated": result.get("updated", False),
        "name": result.get("name", ""),
        "source": result.get("source", ""),
        "generatedAt": result.get("generatedAt", ""),
        "count": result.get("count", 0),
        "stale": result.get("stale", True),
        "cachePath": result.get("cachePath", ""),
        "providerWarning": result.get("providerWarning", ""),
        "top": result.get("items", [])[:10],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["updated"] else 1


if __name__ == "__main__":
    sys.exit(main())
