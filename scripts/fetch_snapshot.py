#!/usr/bin/env python3
"""生成离线数据快照，让评审无需 MCP Token 也能复现全部结论。

这是本项目的一次性采集步骤，采集内容全部来自麦当劳官方 MCP 的只读工具：
    list-nutrition-foods   160 条餐品营养数据
    query-meals            指定门店实时菜单（含现价/原价/优惠类型）
    query-meal-detail      每个饮品条目的规格与特调结构

产物写入 data/ 目录，均不含任何 Token 或个人身份信息。

用法：
    set MCD_MCP_TOKEN=...   (Windows)
    python scripts/fetch_snapshot.py --store 1950891
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Windows 默认控制台可能是 GBK，会导致中文品名与 ™ 字符抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import McdMcpClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DRINK_CATEGORY_PATTERN = ("\u5496\u5561", "\u996e\u54c1")


def dump(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"  wrote {os.path.relpath(path, ROOT)}  ({os.path.getsize(path)} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="\u91c7\u96c6\u9ea6\u5f53\u52b3 MCP \u53ea\u8bfb\u6570\u636e\u5feb\u7167")
    parser.add_argument("--store", default=os.environ.get("MCD_STORE_CODE", ""))
    parser.add_argument("--timeout", type=float, default=40.0)
    args = parser.parse_args()

    client = McdMcpClient.from_env()
    store_code = args.store or client.known_store_code()
    if not store_code:
        print("\u65e0\u6cd5\u786e\u5b9a\u95e8\u5e97\u7f16\u7801\uff0c\u8bf7\u7528 --store \u6307\u5b9a\u3002", file=sys.stderr)
        return 2
    print(f"\u76ee\u6807\u95e8\u5e97\uff1a{store_code}")

    print("[1/3] list-nutrition-foods")
    nutrition = client.nutrition_table()
    dump(os.path.join(DATA_DIR, "nutrition_snapshot.json"), nutrition)

    print("[2/3] query-meals")
    menu = client.menu(store_code)
    dump(os.path.join(DATA_DIR, "menu_snapshot.json"), menu.get("data", menu))

    print("[3/3] query-meal-detail")
    meals = (menu.get("data", menu) or {}).get("meals", {}) or {}
    details = {}
    for category in (menu.get("data", menu) or {}).get("categories", []) or []:
        cat_name = (category.get("name") or "").replace("\n", "")
        if not any(p in cat_name for p in DRINK_CATEGORY_PATTERN):
            continue
        for entry in category.get("meals", []) or []:
            code = str(entry.get("code", ""))
            if code in details:
                continue
            detail = client.meal_detail(code, store_code)
            details[code] = detail.get("data", detail)
            name = (details[code] or {}).get("name", code)
            print(f"  - {code} {name}")
    dump(os.path.join(DATA_DIR, "drink_details_snapshot.json"), details)

    tools = client.list_tools()
    dump(
        os.path.join(DATA_DIR, "tools_list_snapshot.json"),
        {"count": len(tools), "tools": [{"name": t.get("name"), "description": t.get("description")} for t in tools]},
    )
    print("\u5b8c\u6210\u3002")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
