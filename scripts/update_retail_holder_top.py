from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.retail_holder_store import latest_retail_holders, refresh_retail_holder_cache


def main() -> int:
    parser = argparse.ArgumentParser(description="Update cached A-share shareholder account ranking")
    parser.add_argument("--symbol", default="最新", help="AKShare 股东户数口径；默认 最新，也可传季度末如 20260331")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--query", default="", help="按代码或名称搜索，默认输出股东户数最多的前 N 只")
    parser.add_argument("--no-force", action="store_true", help="优先读取 24 小时内缓存")
    args = parser.parse_args()

    if args.no_force:
        payload = latest_retail_holders(limit=args.limit, query=args.query, force=False)
    else:
        refresh_retail_holder_cache(symbol=args.symbol)
        payload = latest_retail_holders(limit=args.limit, query=args.query, force=False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
