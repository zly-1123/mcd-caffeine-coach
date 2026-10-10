#!/usr/bin/env python3
"""今天别喝多了 · 麦咖啡因管家（mcd-caffeine-coach）命令行入口。

核心主张：麦当劳 MCP 给了你 160 条营养数据和实时菜单，但没有一个字提到咖啡因。
本工具把麦咖啡与饮品线的每一杯翻译成「咖啡因当量 + 代谢时间线 + 今晚截止时刻」，
回答的是"我现在还能不能喝"和"几点前必须喝完"，而不再是"哪杯更便宜"。

用法示例：
    python scripts/coach.py drinks                 # 饮品全景（咖啡因 + 每元咖啡因）
    python scripts/coach.py cup "冰美式大杯"         # 单杯解码
    python scripts/coach.py today                  # 今日已摄入多少（读历史订单）
    python scripts/coach.py plan                   # 今晚还能喝什么

加 --offline 可用内置数据快照运行，无需 MCP Token，便于评审复现。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Windows 默认控制台编码可能是 GBK，会直接让中文与 ¥ 符号抛 UnicodeEncodeError。
# 这里强制 UTF-8 输出，保证在 Windows / macOS / Linux 上表现一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from caffeine import (  # noqa: E402
    daily_budget_mg,
    estimate_caffeine,
    latest_sip_time,
    load_model,
    parse_size,
    parse_temperature,
    residual_mg,
    required_clearance_hours,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MENU_SNAPSHOT = os.path.join(ROOT, "data", "menu_snapshot.json")
DETAILS_SNAPSHOT = os.path.join(ROOT, "data", "drink_details_snapshot.json")
DRINK_CATEGORY_PATTERN = ("\u5496\u5561", "\u996e\u54c1")  # 咖啡 / 饮品


# --------------------------------------------------------------------------
# 数据加载
# --------------------------------------------------------------------------
def load_menu_snapshot() -> Dict[str, Any]:
    with open(MENU_SNAPSHOT, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_details_snapshot() -> Dict[str, Any]:
    if not os.path.isfile(DETAILS_SNAPSHOT):
        return {}
    with open(DETAILS_SNAPSHOT, "r", encoding="utf-8") as fh:
        return json.load(fh)


def describe_modifications(detail: Dict[str, Any]) -> List[str]:
    """把 query-meal-detail 的特调结构压成人类可读选项。"""
    groups: List[str] = []
    for group in ((detail or {}).get("modification") or {}).get("items", []) or []:
        labels = []
        for value in group.get("values", []) or []:
            name = value.get("name", "")
            price = value.get("price") or 0
            labels.append(f"{name}(+{price / 100:g}\u5143)" if price else name)
        if labels:
            groups.append("\u3001".join(labels))
    return groups


def fetch_menu_live(store_code: str) -> Dict[str, Any]:
    from mcd_client import McdMcpClient

    client = McdMcpClient.from_env()
    code = store_code or client.known_store_code()
    if not code:
        raise SystemExit("\u65e0\u6cd5\u786e\u5b9a\u95e8\u5e97\u7f16\u7801\uff1a\u8bf7\u7528 --store \u6307\u5b9a\uff0c\u6216\u786e\u4fdd\u8d26\u53f7\u6709\u5386\u53f2\u8ba2\u5355\u3002")
    result = client.menu(code)
    # 联网返回的是 {success, code, message, data:{categories, meals}}，
    # 而离线快照保存的是其中的 data 本身，两者需要归一化。
    return result.get("data", result) or {}


def iter_drinks(menu: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
    """从菜单里筛出饮品条目，并带上实时价格。"""
    details = details or {}
    meals = menu.get("meals", {}) or {}
    seen_codes = set()
    for category in menu.get("categories", []) or []:
        cat_name = (category.get("name") or "").replace("\n", "")
        if not any(p in cat_name for p in DRINK_CATEGORY_PATTERN):
            continue
        for entry in category.get("meals", []) or []:
            code = str(entry.get("code", ""))
            # 同一餐品可能同时挂在"麦咖啡™"和"饮品"两个分类下，按编码去重。
            if code in seen_codes:
                continue
            seen_codes.add(code)
            detail = meals.get(code, {}) or {}
            # 菜单里的品名不含杯型（如"麦咖啡™奶铁"），而 query-meal-detail 返回的是
            # 可直接下单的具体规格（如"麦咖啡™冰奶铁中杯"），因此优先采用后者。
            variant = details.get(code) or {}
            name = variant.get("name") or detail.get("name") or code
            price = _to_float(detail.get("currentPrice"))
            yield {
                "code": code,
                "name": name,
                "category": cat_name,
                "price": price,
                "original_price": _to_float(detail.get("originalPrice")),
                "discount_type": detail.get("discountType") or "",
                "tags": entry.get("tags") or [],
                "modifications": describe_modifications(variant),
                "support_modify": bool(variant.get("supportModify")),
            }


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------
def format_money(value: Optional[float]) -> str:
    return "-" if value is None else f"\u00a5{value:.2f}"


def drink_rows(menu: Dict[str, Any], model: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in iter_drinks(menu, details):
        est = estimate_caffeine(item["name"], model)
        per_yuan = None
        if item["price"] and est.caffeine_mg > 0:
            per_yuan = est.caffeine_mg / item["price"]
        rows.append({**item, "estimate": est, "mg_per_yuan": per_yuan})
    return rows


def print_drink_table(rows: List[Dict[str, Any]], model: Dict[str, Any]) -> None:
    col_name = "\u996e\u54c1"
    col_price = "\u73b0\u4ef7"
    col_caffeine = "\u5496\u5561\u56e0"
    col_per_yuan = "\u6bcf\u5143\u5496\u5561\u56e0"
    col_size = "\u676f\u578b"
    print(f"{col_name:<24}{col_price:>8}{col_caffeine:>10}{col_per_yuan:>14}  {col_size}")
    print("-" * 72)
    for row in sorted(rows, key=lambda r: (-r["estimate"].caffeine_mg, r["name"])):
        est = row["estimate"]
        if not est.matched:
            caffeine = "\u672a\u6536\u5f55"
        else:
            caffeine = f"{est.caffeine_mg:.0f}mg"
        per_yuan = f"{row['mg_per_yuan']:.1f} mg/\u5143" if row["mg_per_yuan"] else "-"
        size = est.size or "-"
        name = row["name"][:22]
        print(f"{name:<24}{format_money(row['price']):>8}{caffeine:>10}{per_yuan:>14}  {size}")
    print()
    print("* \u5496\u5561\u56e0\u4e3a\u672c\u9879\u76ee\u57fa\u4e8e\u516c\u5f00\u8403\u53d6\u53c2\u8003\u503c\u8bbe\u5b9a\u7684**\u4f30\u7b97\u503c**\uff0c\u975e\u9ea6\u5f53\u52b3\u5b98\u65b9\u6570\u636e\uff1b\u9ea6\u5f53\u52b3 MCP \u672a\u63d0\u4f9b\u5496\u5561\u56e0\u5b57\u6bb5\u3002")
    print(f"* \u6a21\u578b\u7248\u672c\uff1a{model.get('schema_version')}\u3000\u534a\u8870\u671f\uff1a{model['pharmacokinetics']['half_life_hours']}h\u3000\u65e5\u9884\u7b97\uff1a{daily_budget_mg(model):.0f}mg")


def print_cup_decode(name: str, model: Dict[str, Any], bedtime: time, threshold: float, now: datetime) -> None:
    est = estimate_caffeine(name, model)
    pk = model["pharmacokinetics"]
    half_life = float(pk["half_life_hours"])

    print(f"\n\u3010{name}\u3011")
    print(f"  \u5496\u5561\u56e0\u5f53\u91cf\uff1a{est.caffeine_mg:.0f} mg\uff08{est.confidence}\uff09")
    if est.matched:
        print(f"  \u5339\u914d\u89c4\u5219\uff1a{est.matched_rule}\u3000\u676f\u578b\uff1a{est.size or '-'}\u3000\u6e29\u5ea6\uff1a{est.temperature or '-'}")
        print(f"  \u4f9d\u636e\uff1a{est.basis}")

    print(f"  \u5185\u4f53\u6b8b\u4f59\u65f6\u95f4\u7ebf\uff08\u534a\u8870\u671f {half_life}h\uff09\uff1a")
    for hours in (1, 2, 4, 6, 8):
        residual = residual_mg(est.caffeine_mg, hours, half_life)
        bar = "\u2588" * max(0, int(residual / max(est.caffeine_mg, 1) * 20))
        print(f"    +{hours}h  {residual:6.1f} mg  {bar}")

    cutoff = latest_sip_time(est.caffeine_mg, bedtime, threshold, half_life, reference_day=now)
    print()
    if cutoff is None:
        print(f"  \u7ed3\u8bba\uff1a\u8fd9\u676f\u7684\u5496\u5561\u56e0\u4e0d\u8d85\u8fc7 {threshold:.0f}mg\uff0c\u5bf9\u5c31\u5bdd\u4e0d\u6784\u6210\u9650\u5236\u3002")
    else:
        need = required_clearance_hours(est.caffeine_mg, threshold, half_life)
        print(f"  \u5c31\u5bdd\u65f6\u95f4\uff1a{bedtime.strftime('%H:%M')}\u3000\u5c31\u5bdd\u6b8b\u4f59\u9608\u503c\uff1a{threshold:.0f}mg")
        print(f"  \u9700\u8981\u6e05\u9664\u65f6\u957f\uff1a{need:.2f} \u5c0f\u65f6")
        print(f"  \u2713 \u6700\u665a\u5f00\u59cb\u559d\u7684\u65f6\u523b\uff1a{cutoff.strftime('%H:%M')}")
        if now > cutoff:
            print(f"  \u26a0 \u73b0\u5728\u5df2\u8fc7\u622a\u6b62\u65f6\u523b\uff0c\u5efa\u8bae\u6362\u4f4e\u5496\u5561\u56e0\u9009\u9879\u6216\u63d0\u524d\u5c31\u5bdd\u9884\u671f\u3002")


def print_cup_customization(name: str) -> None:
    """展示 MCP 返回的特调结构（冰量/加料等），说明结论如何落到一杯可下单的饮品。"""
    details = load_details_snapshot()
    if not details:
        return
    target = normalize_lookup(name)
    hit = None
    for code, detail in details.items():
        if not detail:
            continue
        candidate = normalize_lookup(detail.get("name", ""))
        if candidate == target or target in candidate or candidate in target:
            hit = (code, detail)
            break
    if not hit:
        return
    code, detail = hit
    groups = describe_modifications(detail)
    if not groups:
        return
    print()
    print(f"  \u53ef\u4e0b\u5355\u89c4\u683c\uff1a{detail.get('name')}\uff08\u7f16\u7801 {code}\uff09")
    for idx, group in enumerate(groups, 1):
        print(f"    \u7279\u8c03\u7ec4 {idx}\uff1a{group}")


def normalize_lookup(value: str) -> str:
    return (value or "").replace("\u2122", "").replace(" ", "")


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_drinks(args, model) -> int:
    menu = load_menu_snapshot() if args.offline else fetch_menu_live(args.store)
    rows = drink_rows(menu, model, load_details_snapshot())
    print_drink_table(rows, model)
    if args.json:
        print(json.dumps(
            [
                {
                    "code": r["code"],
                    "name": r["name"],
                    "price": r["price"],
                    "caffeine_mg": r["estimate"].caffeine_mg,
                    "mg_per_yuan": r["mg_per_yuan"],
                    "matched_rule": r["estimate"].matched_rule,
                }
                for r in rows
            ],
            ensure_ascii=False,
            indent=2,
        ))
    return 0


def cmd_cup(args, model) -> int:
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    print_cup_decode(args.name, model, args.bedtime, args.threshold, now)
    print_cup_customization(args.name)
    return 0


def cmd_today(args, model) -> int:
    if args.offline:
        print("\u79bb\u7ebf\u6a21\u5f0f\u4e0b\u65e0\u6cd5\u8bfb\u53d6\u8d26\u6237\u5386\u53f2\u8ba2\u5355\uff1b\u8bf7\u53bb\u6389 --offline \u5e76\u914d\u7f6e MCD_MCP_TOKEN\u3002")
        return 2
    from mcd_client import McdMcpClient

    client = McdMcpClient.from_env()
    orders = client.orders().get("data", {}) or {}
    today = args.date or datetime.now().strftime("%Y-%m-%d")

    total = 0.0
    hits: List[Tuple[str, float]] = []
    for order in orders.get("list", []) or []:
        if not str(order.get("createTime", "")).startswith(today):
            continue
        for product in order.get("orderProductList", []) or []:
            candidates = product.get("comboItemList") or [product]
            for c in candidates:
                name = c.get("name") or c.get("productName") or ""
                est = estimate_caffeine(name, model)
                if est.caffeine_mg > 0:
                    hits.append((name, est.caffeine_mg))
                    total += est.caffeine_mg

    budget = daily_budget_mg(model)
    print(f"\u65e5\u671f\uff1a{today}")
    if not hits:
        print("  \u4eca\u65e5\u5386\u53f2\u8ba2\u5355\u4e2d\u672a\u68c0\u6d4b\u5230\u542b\u5496\u5561\u56e0\u7684\u9910\u54c1\u3002")
    for name, mg in hits:
        print(f"  - {name:<24} {mg:6.0f} mg")
    print(f"\n  \u4eca\u65e5\u7d2f\u8ba1\uff1a{total:.0f} mg / {budget:.0f} mg\uff08\u5269\u4f59 {budget - total:.0f} mg\uff09")
    return 0


def cmd_plan(args, model) -> int:
    menu = load_menu_snapshot() if args.offline else fetch_menu_live(args.store)
    rows = drink_rows(menu, model, load_details_snapshot())
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    half_life = float(model["pharmacokinetics"]["half_life_hours"])

    ok_rows, late_rows = [], []
    for row in rows:
        est = row["estimate"]
        if not est.matched:
            continue
        cutoff = latest_sip_time(est.caffeine_mg, args.bedtime, args.threshold, half_life, reference_day=now)
        (ok_rows if cutoff is None or now <= cutoff else late_rows).append((row, cutoff))

    print(f"\n\u73b0\u5728\uff1a{now.strftime('%H:%M')}\u3000\u5c31\u5bdd\uff1a{args.bedtime.strftime('%H:%M')}\u3000\u9608\u503c\uff1a{args.threshold:.0f}mg\uff08\u534a\u8870\u671f {half_life}h\uff09")
    print(f"\n\u2713 \u73b0\u5728\u8fd8\u80fd\u559d\uff08{len(ok_rows)} \u9879\uff09")
    for row, cutoff in sorted(ok_rows, key=lambda x: x[0]["estimate"].caffeine_mg):
        c = "\u65e0\u9650\u5236" if cutoff is None else f"{cutoff.strftime('%H:%M')} \u524d"
        print(f"    {row['name'][:22]:<24}{format_money(row['price']):>8}{row['estimate'].caffeine_mg:>7.0f}mg   \u6700\u665a {c}")

    if late_rows:
        print(f"\n\u26a0 \u5df2\u8fc7\u622a\u6b62\u65f6\u523b\uff08{len(late_rows)} \u9879\uff09\uff0c\u60f3\u559d\u5c31\u5f97\u63a8\u8fdf\u6216\u7f29\u77ed\u7761\u7720\u9884\u671f")
        for row, cutoff in sorted(late_rows, key=lambda x: x[0]["estimate"].caffeine_mg)[:6]:
            print(f"    {row['name'][:22]:<24}{format_money(row['price']):>8}{row['estimate'].caffeine_mg:>7.0f}mg   \u5e94\u5728 {cutoff.strftime('%H:%M')} \u524d")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def parse_bedtime(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise argparse.ArgumentTypeError(f"\u5c31\u5bdd\u65f6\u95f4\u683c\u5f0f\u5e94\u4e3a HH:MM\uff0c\u5f53\u524d\u4e3a {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    # 注意：argparse 的子解析器会用自身默认值覆盖主解析器已解析的命名空间，
    # 所以这里统一用 SUPPRESS，让"未显式传入"的参数不覆盖任何值，
    # 真正的默认值在主解析器上用 set_defaults 声明一次。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS, help="\u4f7f\u7528\u5185\u7f6e\u6570\u636e\u5feb\u7167\uff0c\u4e0d\u8054\u7f51\u3001\u4e0d\u9700 Token")
    common.add_argument("--store", default=argparse.SUPPRESS, help="\u95e8\u5e97\u7f16\u7801")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="\u989d\u5916\u8f93\u51fa JSON")
    common.add_argument(
        "--bedtime",
        type=parse_bedtime,
        default=argparse.SUPPRESS,
        help="\u5c31\u5bdd\u65f6\u95f4 HH:MM",
    )
    common.add_argument(
        "--threshold",
        type=float,
        default=argparse.SUPPRESS,
        help="\u5c31\u5bdd\u65f6\u53ef\u63a5\u53d7\u7684\u6b8b\u4f59\u5496\u5561\u56e0 mg",
    )
    common.add_argument("--now", default=argparse.SUPPRESS, help="\u6a21\u62df\u5f53\u524d\u65f6\u95f4\uff08ISO\uff0c\u4f8b 2026-10-10T15:30\uff09")
    common.add_argument("--date", default=argparse.SUPPRESS, help="today \u5b50\u547d\u4ee4\u4f7f\u7528\u7684\u65e5\u671f YYYY-MM-DD")

    parser = argparse.ArgumentParser(
        prog="coach",
        description="\u4eca\u5929\u522b\u559d\u591a\u4e86 \u00b7 \u9ea6\u5496\u5561\u56e0\u7ba1\u5bb6 \u2014\u2014 \u628a\u9ea6\u5f53\u52b3\u996e\u54c1\u7ffb\u8bd1\u6210\u5496\u5561\u56e0\u4e0e\u4ee3\u8c22\u65f6\u95f4\u7ebf",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("drinks", parents=[common], help="\u996e\u54c1\u5168\u666f\uff1a\u5496\u5561\u56e0\u4e0e\u6bcf\u5143\u5496\u5561\u56e0")
    cup = sub.add_parser("cup", parents=[common], help="\u5355\u676f\u89e3\u7801")
    cup.add_argument("name", help="\u996e\u54c1\u540d\u79f0\uff0c\u5982 \u51b0\u7f8e\u5f0f\u5927\u676f")
    sub.add_parser("today", parents=[common], help="\u4eca\u65e5\u5df2\u6444\u5165\u5496\u5561\u56e0\uff08\u8bfb\u5386\u53f2\u8ba2\u5355\uff09")
    sub.add_parser("plan", parents=[common], help="\u4eca\u665a\u8fd8\u80fd\u559d\u4ec0\u4e48")
    return parser


def apply_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """补齐未传入的可选参数。

    这里不能用 parser.set_defaults：parents 机制会让主解析器与子解析器共享同一个
    action 对象，set_defaults 会连带改写子解析器的默认值，导致 "--offline" 写在
    子命令前时被子解析器覆盖回 False。解析完成后再补默认值可以彻底规避。
    """
    defaults = {
        "offline": False,
        "store": os.environ.get("MCD_STORE_CODE", ""),
        "json": False,
        "bedtime": parse_bedtime(os.environ.get("MCD_BEDTIME", "23:30")),
        "threshold": float(os.environ.get("MCD_SLEEP_CAFFEINE_THRESHOLD_MG", "30")),
        "now": "",
        "date": "",
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = apply_defaults(build_parser().parse_args(argv))
    model = load_model()
    handlers = {"drinks": cmd_drinks, "cup": cmd_cup, "today": cmd_today, "plan": cmd_plan}
    try:
        return handlers[args.command](args, model)
    except Exception as exc:  # noqa: BLE001
        print(f"\n\u8fd0\u884c\u5931\u8d25\uff1a{exc}", file=sys.stderr)
        print("\u63d0\u793a\uff1a\u52a0 --offline \u53ef\u7528\u5185\u7f6e\u6570\u636e\u5feb\u7167\u8fd0\u884c\uff0c\u65e0\u9700 Token\u3002", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
