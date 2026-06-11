#!/usr/bin/env python3
"""결정 엔진 순수 로직 테스트 — 네트워크 없이 실행 가능.

실행: python -m unittest tests.test_decision_logic -v
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 외부 의존성 스텁 (오프라인 실행용)
for mod in ("yfinance", "requests", "pandas"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("telegram_notifier", types.SimpleNamespace(
    send_message=lambda *a, **k: True, TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
    _api=lambda *a, **k: {}))

from action_plan import split_deposit, months_to_target  # noqa: E402
from tax_korea import plan_sales  # noqa: E402


def _state(values: dict[str, float], targets: dict[str, float],
           preferred: dict[str, str]) -> dict:
    total = sum(values.values())
    cats = {}
    for cat, v in values.items():
        cats[cat] = {
            "target_pct": targets[cat],
            "current_pct": v / total if total else 0,
            "value": v,
            "drift_pct": (v / total if total else 0) - targets[cat],
            "preferred": [preferred[cat]],
        }
    return {"total": total, "categories": cats}


class TestSplitDeposit(unittest.TestCase):
    targets = {"N100": 0.30, "SP": 0.30, "금": 0.07, "BTC": 0.03, "현금": 0.29}
    preferred = {"N100": "QQQM", "SP": "SPYM", "금": "GLDM", "BTC": "IBIT", "현금": "SGOV"}

    def test_underweight_first(self):
        # 금/BTC 0 보유 → 납입금이 거기부터 가야 함
        st = _state({"N100": 30_000, "SP": 30_000, "금": 0, "BTC": 0, "현금": 29_000},
                    self.targets, self.preferred)
        plan = split_deposit(st, 1_200)
        alloc = {t: amt for t, amt, _ in plan}
        self.assertIn("GLDM", alloc)
        self.assertIn("IBIT", alloc)
        # 금(7%) 갭이 BTC(3%) 갭보다 크므로 더 많이 배분
        self.assertGreater(alloc["GLDM"], alloc["IBIT"])

    def test_sums_to_deposit(self):
        st = _state({"N100": 10_000, "SP": 40_000, "금": 5_000, "BTC": 2_000, "현금": 20_000},
                    self.targets, self.preferred)
        plan = split_deposit(st, 1_500)
        self.assertAlmostEqual(sum(amt for _, amt, _ in plan), 1_500, delta=2)

    def test_overweight_gets_nothing_when_deposit_small(self):
        # SP 심한 오버웨이트 + 납입금 < 부족분 → SPYM 배분 없어야 함
        st = _state({"N100": 10_000, "SP": 60_000, "금": 2_000, "BTC": 1_000, "현금": 10_000},
                    self.targets, self.preferred)
        plan = split_deposit(st, 1_000)
        tickers = [t for t, _, _ in plan]
        self.assertNotIn("SPYM", tickers)
        self.assertIn("QQQM", tickers)

    def test_zero_deposit(self):
        st = _state({"N100": 100}, {"N100": 1.0}, {"N100": "QQQM"})
        self.assertEqual(split_deposit(st, 0), [])


class TestMonthsToTarget(unittest.TestCase):
    def test_already_reached(self):
        self.assertEqual(months_to_target(200_000, 200_000, 1_000, 0.07), 0.0)

    def test_known_range(self):
        # $78K → $200K, 월 $1,268, 연 7% → 대략 5~7년 사이
        m = months_to_target(78_000, 200_000, 1_268, 0.07)
        self.assertIsNotNone(m)
        self.assertTrue(55 <= m <= 90, f"got {m}")

    def test_no_contribution_no_principal(self):
        self.assertIsNone(months_to_target(0, 100_000, 0, 0.07))

    def test_monotonic_in_contribution(self):
        slow = months_to_target(50_000, 500_000, 500, 0.07)
        fast = months_to_target(50_000, 500_000, 3_000, 0.07)
        self.assertLess(fast, slow)


class TestPlanSales(unittest.TestCase):
    legacy = {"QQQI", "SPYI"}
    core = {"QQQM", "SPYM", "GLDM", "IBIT", "SGOV"}

    def test_legacy_first(self):
        positions = {
            "QQQM": {"qty": 100, "cost_basis": 100, "mark_price": 120},  # +$2,000
            "QQQI": {"qty": 100, "cost_basis": 50, "mark_price": 55},    # +$500
        }
        plan = plan_sales(positions, 800, self.legacy, self.core)
        self.assertEqual(plan[0]["ticker"], "QQQI")  # 레거시 우선

    def test_fills_headroom_not_over(self):
        positions = {
            "QQQI": {"qty": 1_000, "cost_basis": 50, "mark_price": 55},  # $5/주 차익
        }
        plan = plan_sales(positions, 800, self.legacy, self.core)
        total = sum(p["gain_usd"] for p in plan)
        self.assertLessEqual(total, 800 + 1)
        self.assertGreater(total, 700)  # 정수 주 반올림 감안 거의 채움

    def test_skips_losers(self):
        positions = {
            "SPYI": {"qty": 100, "cost_basis": 60, "mark_price": 50},  # 손실
        }
        self.assertEqual(plan_sales(positions, 800, self.legacy, self.core), [])

    def test_caps_at_holding(self):
        positions = {
            "QQQI": {"qty": 10, "cost_basis": 50, "mark_price": 60},  # 최대 $100
            "QQQM": {"qty": 100, "cost_basis": 100, "mark_price": 105},
        }
        plan = plan_sales(positions, 600, self.legacy, self.core)
        by_ticker = {p["ticker"]: p for p in plan}
        self.assertLessEqual(by_ticker["QQQI"]["shares"], 10)
        self.assertIn("QQQM", by_ticker)  # 모자란 만큼 코어로 이어감

    def test_no_headroom(self):
        self.assertEqual(plan_sales({}, 0, self.legacy, self.core), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
