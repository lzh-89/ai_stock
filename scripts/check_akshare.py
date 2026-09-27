from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.providers.akshare_provider import AKShareProvider


def main() -> int:
    provider = AKShareProvider()
    if not provider.available():
        print("AKShare 未安装，请先运行：python3 -m pip install -r requirements.txt")
        return 1

    print("AKShare 已安装，开始请求真实行情快照...")
    try:
        rows = provider.fetch_snapshot()
    except Exception as error:  # noqa: BLE001 - diagnostic script should show provider errors.
        print("AKShare 请求失败：")
        print(str(error))
        return 2

    print(f"AKShare 请求成功，共 {len(rows)} 条。")
    for item in rows[:5]:
        print(f"{item.code} {item.name} 最新价={item.price} 涨跌幅={item.change}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
