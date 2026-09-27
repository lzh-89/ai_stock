from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.providers.sector_provider import get_sector_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Check morning sector radar data source.")
    parser.add_argument("--source", default="akshare", choices=["akshare", "mock"])
    parser.add_argument("--sector-type", default="all", choices=["all", "industry", "concept"])
    parser.add_argument("--leader-limit", default=5, type=int)
    args = parser.parse_args()

    provider = get_sector_provider(args.source)
    started = time.perf_counter()
    payload = provider.fetch_sector_snapshot(args.sector_type)
    sectors = payload.get("sectors") or []
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    counts: Dict[str, int] = {"industry": 0, "concept": 0}
    fund_flow_ok = 0
    fund_flow_missing = 0
    constituent_ok = 0
    constituent_failed = 0
    samples = []
    for sector in sectors:
        sector_type = str(sector.get("sectorType") or "")
        if sector_type in counts:
            counts[sector_type] += 1
        if sector.get("fundFlowStatus") == "ok":
            fund_flow_ok += 1
        else:
            fund_flow_missing += 1
        if len(samples) >= 8:
            continue
        try:
            leaders = provider.fetch_constituents(str(sector.get("sectorName") or ""), sector_type, limit=args.leader_limit)
            constituent_ok += 1
        except Exception as error:  # noqa: BLE001 - diagnostic output should keep the source error.
            leaders = []
            constituent_failed += 1
            sector = {**sector, "constituentError": str(error)}
        samples.append(
            {
                "sectorCode": sector.get("sectorCode"),
                "sectorName": sector.get("sectorName"),
                "sectorType": sector.get("sectorType"),
                "changePct": sector.get("changePct"),
                "turnoverAmount": sector.get("turnoverAmount"),
                "fundFlowStatus": sector.get("fundFlowStatus"),
                "leaderCount": len(leaders),
                "constituentError": sector.get("constituentError", ""),
            }
        )

    report: Dict[str, Any] = {
        "source": payload.get("source"),
        "requestedSource": args.source,
        "sectorType": args.sector_type,
        "elapsedMs": elapsed_ms,
        "sectorCount": len(sectors),
        "counts": counts,
        "fundFlow": {"ok": fund_flow_ok, "missing": fund_flow_missing},
        "constituents": {"sampleOk": constituent_ok, "sampleFailed": constituent_failed},
        "warnings": payload.get("warnings") or [],
        "samples": samples,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
