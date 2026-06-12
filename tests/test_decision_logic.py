#!/usr/bin/env python3
"""결정 엔진 순수 로직 테스트 — 네트워크 없이 실행 가능.

실행: python -m unittest tests.test_decision_logic -v
"""
import sys
import tempfile
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
    targets = {"N100": 0.35, "SP": 0.35, "금": 0.07, "BTC": 0.03, "현금": 0.20}
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


class TestReservoirClassify(unittest.TestCase):
    """저수지 수위 분류 — 헌법 6조 저수지 매수 가이드 (2026.6 개정)."""

    def setUp(self):
        from reservoir import classify
        self.classify = classify

    def test_full_water(self):
        idx, zone = self.classify(-1.0)
        self.assertEqual(idx, 0)
        self.assertIsNone(zone)

    def test_zone_boundaries(self):
        for dd, want in [(-3.0, 1), (-6.9, 1), (-7.0, 2), (-15.0, 3), (-25.0, 4), (-50.0, 4)]:
            idx, _ = self.classify(dd)
            self.assertEqual(idx, want, f"dd={dd}")

    def test_volatility_scale(self):
        # IBIT(×3): -8%는 만수위(기준 -9%), -22%는 저수지(기준 -21%), -75%는 댐 바닥
        self.assertEqual(self.classify(-8.0, scale=3.0)[0], 0)
        self.assertEqual(self.classify(-22.0, scale=3.0)[0], 2)
        self.assertEqual(self.classify(-75.0, scale=3.0)[0], 4)

    def test_monotonic_in_drawdown(self):
        prev = 0
        for dd in range(0, -60, -1):
            idx, _ = self.classify(float(dd))
            self.assertGreaterEqual(idx, prev)
            prev = idx


class TestCoreTrim(unittest.TestCase):
    """코어 과열 부분 익절 — 헌법 7조 예외 (2026.6 신설)."""

    def setUp(self):
        from core_trim import build_core_trim_section
        self.build = build_core_trim_section
        self.holdings = {"QQQM": 100, "SPYM": 100, "GLDM": 50, "IBIT": 10}
        self.judged_hot = {
            "QQQM": {"rsi": 75, "drawdown": -2.5},
            "SPYM": {"rsi": 60, "drawdown": -1.0},
            "GLDM": {"rsi": 40, "drawdown": -5.0},
            "IBIT": {"rsi": 50, "drawdown": -8.0},
        }

    def test_skips_when_far_from_ath(self):
        out = self.build(-5.0, 0.12, 0.20, self.judged_hot, self.holdings)
        self.assertEqual(out, "")

    def test_skips_when_cash_sufficient(self):
        out = self.build(-1.0, 0.18, 0.20, self.judged_hot, self.holdings)
        self.assertEqual(out, "")

    def test_skips_when_no_overheated(self):
        cool = {t: {"rsi": 50, "drawdown": -3.0} for t in self.judged_hot}
        out = self.build(-1.0, 0.12, 0.20, cool, self.holdings)
        self.assertEqual(out, "")

    def test_skips_overheated_still_at_high(self):
        """RSI 70+여도 신고가 행진 중(꺾이지 않음)이면 매도 제안 안 함."""
        running = dict(self.judged_hot)
        running["QQQM"] = {"rsi": 75, "drawdown": -0.3}
        out = self.build(-1.0, 0.12, 0.20, running, self.holdings)
        self.assertEqual(out, "")

    def test_trims_most_overheated(self):
        out = self.build(-1.0, 0.12, 0.20, self.judged_hot, self.holdings)
        self.assertIn("QQQM", out)
        self.assertIn("RSI 75", out)
        self.assertNotIn("SPYM", out)

    def test_cash_depleted_triggers_without_ath(self):
        """매수 탄약이 완전히 바닥나면 S&P ATH 근처가 아니어도 트림 안내 (2026.6 신설)."""
        out = self.build(-8.0, 0.0, 0.20, self.judged_hot, self.holdings)
        self.assertIn("QQQM", out)
        self.assertIn("매도", out)

    def test_cash_depleted_but_no_overheated_skips(self):
        cool = {t: {"rsi": 50, "drawdown": -3.0} for t in self.judged_hot}
        out = self.build(-8.0, 0.0, 0.20, cool, self.holdings)
        self.assertEqual(out, "")


class TestSatelliteCandidates(unittest.TestCase):
    """위성 한도(2개) 유지 + 통과 종목은 정보성 후보로만 기록 (2026.6 신설)."""

    def setUp(self):
        import idea_check
        self.idea_check = idea_check
        self._orig_file = idea_check.CANDIDATES_FILE
        self._tmpdir = tempfile.TemporaryDirectory()
        idea_check.CANDIDATES_FILE = Path(self._tmpdir.name) / "satellite_candidates.json"

    def tearDown(self):
        self.idea_check.CANDIDATES_FILE = self._orig_file
        self._tmpdir.cleanup()

    def test_save_and_load_roundtrip(self):
        self.idea_check._save_candidate(
            "SCHG", {"name": "Schwab US Large-Cap Growth ETF", "expense_pct": 0.04, "aum": 3.0e10}
        )
        candidates = self.idea_check._load_candidates()
        self.assertIn("SCHG", candidates)
        self.assertEqual(candidates["SCHG"]["expense_pct"], 0.04)
        self.assertIn("first_seen", candidates["SCHG"])

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(self.idea_check._load_candidates(), {})

    def test_core_trim_notes_recorded_candidates(self):
        from core_trim import build_core_trim_section
        self.idea_check._save_candidate("SCHG", {"name": "Schwab US Large-Cap Growth ETF"})
        holdings = {"QQQM": 100, "SPYM": 100, "GLDM": 50, "IBIT": 10}
        judged_hot = {
            "QQQM": {"rsi": 75, "drawdown": -2.5}, "SPYM": {"rsi": 60, "drawdown": -1.0},
            "GLDM": {"rsi": 40, "drawdown": -5.0}, "IBIT": {"rsi": 50, "drawdown": -8.0},
        }
        out = build_core_trim_section(-1.0, 0.12, 0.20, judged_hot, holdings)
        self.assertIn("SCHG", out)
        self.assertIn("위성 교체 후보", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
