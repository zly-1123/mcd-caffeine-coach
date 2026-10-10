"""咖啡因模型的回归测试。

这些测试是"数值不进大模型"这一主张的证据：所有结论都能被离线复现。
运行：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import datetime, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from caffeine import (  # noqa: E402
    cumulative_residual_mg,
    daily_budget_mg,
    estimate_caffeine,
    latest_sip_time,
    load_model,
    parse_size,
    parse_temperature,
    remaining_budget_mg,
    required_clearance_hours,
    residual_mg,
)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.model = load_model()

    def test_size_in_any_position(self):
        cases = {
            "\u9ea6\u5496\u5561\u2122\u51b0\u5976\u94c1\u4e2d\u676f": "\u4e2d",
            "\u51b0\u7f8e\u5f0f\u5927\u676f": "\u5927",
            "\u5927\u676f\u9c9c\u8403\u5496\u5561": "\u5927",
            "\u5c0f\u676f\u9c9c\u8403\u5496\u5561": "\u5c0f",
            "\u4e2d\u676f\u6021\u6cc9+C": "\u4e2d",
            "\u53ef\u4e50\u5c0f\u676f": "\u5c0f",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_size(name, self.model), expected)

    def test_temperature(self):
        self.assertEqual(parse_temperature("\u51b0\u7f8e\u5f0f\u4e2d\u676f"), "\u51b0")
        self.assertEqual(parse_temperature("\u70ed\u7f8e\u5f0f\u4e2d\u676f"), "\u70ed")
        self.assertIsNone(parse_temperature("\u53ef\u4e50\u4e2d\u676f"))


class TestEstimate(unittest.TestCase):
    def setUp(self):
        self.model = load_model()

    def test_americano_scales_with_size(self):
        self.assertEqual(estimate_caffeine("\u51b0\u7f8e\u5f0f\u5c0f\u676f", self.model).caffeine_mg, 63)
        self.assertEqual(estimate_caffeine("\u51b0\u7f8e\u5f0f\u4e2d\u676f", self.model).caffeine_mg, 126)
        self.assertEqual(estimate_caffeine("\u51b0\u7f8e\u5f0f\u5927\u676f", self.model).caffeine_mg, 189)

    def test_oat_latte_matches_before_plain_latte(self):
        est = estimate_caffeine("\u51b0\u71d5\u9ea6\u5976\u94c1\u4e2d\u676f", self.model)
        self.assertEqual(est.matched_rule, "latte_oat")
        self.assertEqual(est.caffeine_mg, 126)

    def test_oat_milk_alone_is_zero(self):
        est = estimate_caffeine("\u51b0\u71d5\u9ea6\u5976\u5927\u676f", self.model)
        self.assertEqual(est.matched_rule, "oat_milk")
        self.assertEqual(est.caffeine_mg, 0)

    def test_milk_is_zero(self):
        self.assertEqual(estimate_caffeine("\u70ed\u725b\u5976\u4e2d\u676f", self.model).caffeine_mg, 0)

    def test_lemon_tea_wins_over_plain_black_tea(self):
        est = estimate_caffeine("\u9633\u5149\u67e0\u6aac\u7ea2\u8336\u4e2d\u676f", self.model)
        self.assertEqual(est.matched_rule, "lemon_tea")
        self.assertEqual(est.caffeine_mg, 40)
        self.assertEqual(estimate_caffeine("\u9521\u5170\u7ea2\u8336", self.model).caffeine_mg, 47)

    def test_big_brew_coffee(self):
        self.assertEqual(estimate_caffeine("\u5927\u676f\u9c9c\u8403\u5496\u5561", self.model).caffeine_mg, 140)

    def test_cola(self):
        self.assertEqual(estimate_caffeine("\u53ef\u4e50\u4e2d\u676f", self.model).caffeine_mg, 40)
        self.assertEqual(estimate_caffeine("\u65e0\u7cd6\u53ef\u53e3\u53ef\u4e50\u4e2d\u676f", self.model).caffeine_mg, 40)

    def test_zero_caffeine_drinks(self):
        for name in ["\u96ea\u78a7\u4e2d\u676f", "\u7eaf\u60a6", "100% \u82f9\u679c\u6c41(\u76d2\u88c5)", "\u3010\u7f8e\u6c41\u6e90\u3011\u201c\u9ec4\u91d1\u6a59\u6a59\u201d"]:
            with self.subTest(name=name):
                self.assertEqual(estimate_caffeine(name, self.model).caffeine_mg, 0)

    def test_unknown_item_is_flagged_not_guessed(self):
        est = estimate_caffeine("\u4e0d\u5b58\u5728\u7684\u795e\u79d8\u9910\u54c1", self.model)
        self.assertFalse(est.matched)
        self.assertEqual(est.confidence, "\u672a\u6536\u5f55")
        self.assertEqual(est.caffeine_mg, 0.0)


class TestPharmacokinetics(unittest.TestCase):
    def test_half_life_decay(self):
        self.assertAlmostEqual(residual_mg(126, 5, 5), 63.0, places=6)
        self.assertAlmostEqual(residual_mg(126, 10, 5), 31.5, places=6)
        self.assertAlmostEqual(residual_mg(126, 0, 5), 126.0, places=6)

    def test_zero_dose_stays_zero(self):
        self.assertEqual(residual_mg(0, 3, 5), 0.0)

    def test_clearance_hours(self):
        # 126mg -> 30mg 需要 5 * log2(4.2) 小时
        self.assertAlmostEqual(required_clearance_hours(126, 30, 5), 5 * 2.070389, places=5)
        self.assertEqual(required_clearance_hours(20, 30, 5), 0.0)

    def test_latest_sip_time_is_derived_from_bedtime(self):
        day = datetime(2026, 10, 10, 9, 0)
        cutoff = latest_sip_time(126, time(23, 30), 30, 5, reference_day=day)
        self.assertIsNotNone(cutoff)
        self.assertEqual(cutoff.date(), day.date())
        # 23:30 - 5*log2(126/30) 小时 = 13:08:53
        self.assertEqual((cutoff.hour, cutoff.minute), (13, 8))
        self.assertAlmostEqual(cutoff.second, 53, delta=2)

    def test_low_dose_imposes_no_restriction(self):
        self.assertIsNone(latest_sip_time(20, time(23, 30), 30, 5))

    def test_cumulative_residual(self):
        # 两杯中杯美式，一杯刚喝、一杯 5 小时前
        total = cumulative_residual_mg([(126, 0), (126, 5)], 5)
        self.assertAlmostEqual(total, 189.0, places=6)

    def test_budget(self):
        model = load_model()
        self.assertEqual(daily_budget_mg(model), 400)
        self.assertEqual(daily_budget_mg(model, pregnant=True), 200)
        self.assertEqual(remaining_budget_mg(126, model), 274)


if __name__ == "__main__":
    unittest.main()
