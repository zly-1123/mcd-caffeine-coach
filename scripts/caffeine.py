"""今天别喝多了 · 麦咖啡因管家 —— 咖啡因估算与代谢模型。

设计原则（也是本项目与"再做一个点单助手"的根本区别）：
1. 官方 MCP 不提供咖啡因数据，本模块用集中、可审计、可覆盖的模型补上这块空白。
2. 所有数值计算由确定性代码完成，不交给大模型口算，因此结果可复现、可回归测试。
3. 估算值一律标注来源为"模型估算"，绝不伪装成麦当劳官方数据。

纯标准库实现，无第三方依赖。
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Sequence, Tuple

DEFAULT_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "caffeine_model.json",
)

SIZE_TOKENS = ("迷你", "小", "中", "大")
SIZE_ALIASES_FALLBACK = {"迷你杯": "迷你", "小杯": "小", "中杯": "中", "大杯": "大"}


def load_model(path: str = DEFAULT_MODEL_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def normalize_name(name: str) -> str:
    """去掉商标符号与空白，便于匹配。"""
    cleaned = name.replace("\u2122", "").replace("\u00ae", "")
    return re.sub(r"\s+", "", cleaned)


def parse_size(name: str, model: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """从任意位置提取杯型：'大杯鲜萃咖啡' 与 '冰美式大杯' 都要能识别。"""
    cleaned = normalize_name(name)
    aliases = dict(SIZE_ALIASES_FALLBACK)
    if model:
        aliases.update(model.get("size_aliases", {}))
    for group in (("迷你杯", "小杯", "中杯", "大杯"), SIZE_TOKENS):
        for raw in group:
            if raw in cleaned:
                return aliases.get(raw, raw)
    return None


def parse_temperature(name: str) -> Optional[str]:
    cleaned = normalize_name(name)
    if "冰" in cleaned:
        return "冰"
    if "热" in cleaned:
        return "热"
    return None


def drink_name(name: str) -> str:
    """去掉杯型后的核心品名，用于展示与聚合。"""
    out = normalize_name(name)
    for raw in ("迷你杯", "小杯", "中杯", "大杯"):
        out = out.replace(raw, "")
    return out


@dataclass
class CaffeineEstimate:
    raw_name: str
    matched_rule: Optional[str]
    caffeine_mg: float
    size: Optional[str]
    temperature: Optional[str]
    basis: str
    confidence: str = "估算"

    @property
    def matched(self) -> bool:
        return self.matched_rule is not None

    @property
    def display_name(self) -> str:
        return drink_name(self.raw_name)


def _pick_size_value(size_mg: Dict[str, Any], size: Optional[str]) -> Tuple[float, str]:
    if size and size in size_mg:
        return float(size_mg[size]), f"{size}杯"
    if "默认" in size_mg:
        return float(size_mg["默认"]), "默认规格"
    for candidate in ("中", "大", "小", "迷你"):
        if candidate in size_mg:
            return float(size_mg[candidate]), f"{candidate}杯(代入)"
    return 0.0, "未定义"


def estimate_caffeine(product_name: str, model: Dict[str, Any]) -> CaffeineEstimate:
    """把任意餐品名映射到咖啡因估算值；未命中的品名明确标记为未收录。"""
    cleaned = normalize_name(product_name)
    size = parse_size(product_name, model)
    temperature = parse_temperature(product_name)

    for rule in model.get("rules", []):
        pattern = rule.get("match", "")
        if pattern and re.search(pattern, cleaned):
            mg, size_label = _pick_size_value(rule.get("size_mg", {}), size)
            basis = f"{rule.get('basis', '')} 本次按 {size_label} 取值。"
            return CaffeineEstimate(
                raw_name=product_name,
                matched_rule=rule.get("id"),
                caffeine_mg=mg,
                size=size,
                temperature=temperature,
                basis=basis,
            )

    return CaffeineEstimate(
        raw_name=product_name,
        matched_rule=None,
        caffeine_mg=0.0,
        size=size,
        temperature=temperature,
        basis="该餐品未收录进咖啡因模型规则，因此不给出估算值，避免误导。可在 data/caffeine_model.json 的 rules 中补充。",
        confidence="未收录",
    )


def residual_mg(dose_mg: float, hours_elapsed: float, half_life_hours: float) -> float:
    """一次摄入后经过 hours_elapsed 小时的体内残余量（一阶消除）。"""
    if dose_mg <= 0:
        return 0.0
    if hours_elapsed <= 0:
        return float(dose_mg)
    if half_life_hours <= 0:
        raise ValueError("half_life_hours 必须为正数")
    return float(dose_mg) * (0.5 ** (hours_elapsed / half_life_hours))


def required_clearance_hours(dose_mg: float, threshold_mg: float, half_life_hours: float) -> float:
    """把这杯的残余降到 threshold 以下，至少需要多少小时。"""
    if dose_mg <= threshold_mg:
        return 0.0
    if threshold_mg <= 0:
        raise ValueError("threshold_mg 必须为正数")
    return half_life_hours * math.log2(dose_mg / threshold_mg)


def latest_sip_time(
    dose_mg: float,
    bedtime: time,
    threshold_mg: float,
    half_life_hours: float,
    reference_day: Optional[datetime] = None,
) -> Optional[datetime]:
    """最晚开始喝这杯的时刻；若整杯残余都不超阈，返回 None 表示不构成限制。"""
    need_hours = required_clearance_hours(dose_mg, threshold_mg, half_life_hours)
    if need_hours <= 0:
        return None
    day = reference_day or datetime.now()
    bed_dt = datetime.combine(day.date(), bedtime)
    return bed_dt - timedelta(hours=need_hours)


def cumulative_residual_mg(doses: Sequence[Tuple[float, float]], half_life_hours: float) -> float:
    """doses 为 [(摄入量mg, 距现在的小时数), ...]，返回当前体内总残余。"""
    return float(sum(residual_mg(mg, hrs, half_life_hours) for mg, hrs in doses))


def daily_budget_mg(model: Dict[str, Any], pregnant: bool = False) -> float:
    pk = model.get("pharmacokinetics", {})
    key = "pregnancy_budget_mg" if pregnant else "daily_budget_mg"
    value = pk.get(key, 400)
    return float(value)


def remaining_budget_mg(consumed_mg: float, model: Dict[str, Any], pregnant: bool = False) -> float:
    return daily_budget_mg(model, pregnant) - float(consumed_mg)
