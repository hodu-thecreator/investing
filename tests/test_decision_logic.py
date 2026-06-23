#!/usr/bin/env python3
"""결정 엔진 순수 로직 테스트 — 네트워크 없이 실행 가능.

실행: python -m unittest tests.test_decision_logic -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

# 외부 의존성 스텁 (오프라인 실행용)
for mod in ("yfinance", "pandas", "bs4"):
    sys.modules.setdefault(mod, types.ModuleType(mod))
sys.modules["bs4"].BeautifulSoup = object
sys.modules["pandas"].DataFrame = type("DataFrame", (), {})
sys.modules["pandas"].Series = type("Series", (), {})
if "requests" not in sys.modules:
    _requests_stub = types.ModuleType("requests")
    _requests_stub.RequestException = Exception
    sys.modules["requests"] = _requests_stub
elif not hasattr(sys.modules["requests"], "RequestException"):
    sys.modules["requests"].RequestException = Exception
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
sys.modules.setdefault("anthropic", types.SimpleNamespace(Anthropic=lambda *a, **k: None))
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
        self.assertIn("전략적 교체 제안", out)

    def test_cash_depleted_triggers_without_ath(self):
        """매수 탄약이 완전히 바닥나면 S&P ATH 근처가 아니어도 트림 안내 (2026.6 신설)."""
        out = self.build(-8.0, 0.0, 0.20, self.judged_hot, self.holdings)
        self.assertIn("QQQM", out)
        self.assertIn("매도", out)

    def test_cash_depleted_but_no_overheated_skips(self):
        cool = {t: {"rsi": 50, "drawdown": -3.0} for t in self.judged_hot}
        out = self.build(-8.0, 0.0, 0.20, cool, self.holdings)
        self.assertEqual(out, "")


class TestDynamicCashTarget(unittest.TestCase):
    """동적 현금 목표 사다리 (헌법 5조 신설, 2026.6) — 위험점수 → 현금 목표."""

    def setUp(self):
        from daily_report import calc_cash_target
        self.calc = calc_cash_target

    def test_base_target_when_calm(self):
        self.assertEqual(self.calc(0), 0.20)
        self.assertEqual(self.calc(2), 0.20)

    def test_ladder_steps_up_with_risk(self):
        self.assertEqual(self.calc(3), 0.25)
        self.assertEqual(self.calc(5), 0.35)
        self.assertEqual(self.calc(7), 0.45)
        self.assertEqual(self.calc(9), 0.50)

    def test_caps_at_50_for_extreme_risk(self):
        self.assertEqual(self.calc(20), 0.50)


class TestDynamicCashTrim(unittest.TestCase):
    """동적 현금 목표 갭 — 점진적 익절 (헌법 7조 신설, 2026.6)."""

    def setUp(self):
        from core_trim import build_dynamic_cash_trim_section
        self.build = build_dynamic_cash_trim_section
        self.holdings = {"QQQM": 100, "SPMO": 50, "SPYM": 100, "GLDM": 50, "IBIT": 10}
        self.judged_hot = {
            "QQQM": {"rsi": 78, "drawdown": -2.0, "price": 100.0},
            "SPMO": {"rsi": 74, "drawdown": -3.0, "price": 80.0},
            "SPYM": {"rsi": 60, "drawdown": -1.0, "price": 90.0},
            "GLDM": {"rsi": 40, "drawdown": -5.0, "price": 50.0},
            "IBIT": {"rsi": 50, "drawdown": -8.0, "price": 60.0},
        }

    def test_no_action_when_target_still_base(self):
        out = self.build(1, 0.18, 0.20, self.judged_hot, self.holdings, 100_000)
        self.assertEqual(out, "")

    def test_no_action_when_no_gap(self):
        out = self.build(5, 0.40, 0.35, self.judged_hot, self.holdings, 100_000)
        self.assertEqual(out, "")

    def test_no_action_when_no_overheated_candidates(self):
        cool = {t: {"rsi": 50, "drawdown": -3.0, "price": 100.0} for t in self.judged_hot}
        out = self.build(5, 0.22, 0.35, cool, self.holdings, 100_000)
        self.assertEqual(out, "")

    def test_skips_overheated_still_at_high(self):
        running = dict(self.judged_hot)
        running["QQQM"] = {"rsi": 78, "drawdown": -0.3, "price": 100.0}
        out = self.build(5, 0.22, 0.35, running, self.holdings, 100_000)
        self.assertNotIn("QQQM", out)
        self.assertIn("SPMO", out)

    def test_trims_overheated_capped_per_report(self):
        out = self.build(5, 0.22, 0.35, self.judged_hot, self.holdings, 100_000)
        self.assertIn("QQQM", out)
        self.assertIn("SPMO", out)
        self.assertNotIn("SPYM", out)
        self.assertIn("한도 5.0%p ($5,000)", out)
        self.assertIn("다음 리포트에서 단계적으로", out)

    def test_per_ticker_sell_capped(self):
        """종목 1개당 보유 평가액의 15%까지만 매도 (포지션 유지)."""
        out = self.build(5, 0.22, 0.35, self.judged_hot, self.holdings, 100_000)
        # QQQM 100주 * $100 * 15% = $1,500
        self.assertIn("$1,500", out)


class TestReservoirStages(unittest.TestCase):
    """저수지 단계 표기 + 탄약 투입액 계산 (2026.6 개정)."""

    def setUp(self):
        import reservoir
        self.reservoir = reservoir

    def test_zone_label_numbered(self):
        self.assertEqual(self.reservoir.zone_label(0), "0단계 (만수위)")
        self.assertEqual(self.reservoir.zone_label(1), "1단계")
        self.assertEqual(self.reservoir.zone_label(2), "2단계")

    def test_zone_fire_increases_with_depth(self):
        fires = [self.reservoir.zone_fire(i) for i in range(0, 5)]
        for a, b in zip(fires, fires[1:]):
            self.assertLessEqual(a, b)
        self.assertEqual(self.reservoir.zone_fire(0), 0.0)
        self.assertEqual(self.reservoir.zone_fire(4), 1.0)

    def test_build_reservoir_section_shows_dollar_amount(self):
        levels = [
            {"ticker": "GLDM", "price": 50.0, "high": 60.0, "dd": -10.0,
             "scale": 1.0, "zone_idx": 2},
        ]
        state = {"ticker_values": {"SGOV": 8_000}, "idle_cash": 2_000, "total": 50_000}
        with patch.object(self.reservoir, "fetch_levels", return_value=levels):
            out = self.reservoir.build_reservoir_section(state)
        self.assertIn("2단계", out)
        self.assertIn("$2,500", out)  # (8000+2000) * 25%

    def test_build_reservoir_section_full_water(self):
        levels = [
            {"ticker": "QQQM", "price": 100.0, "high": 100.0, "dd": 0.0,
             "scale": 1.0, "zone_idx": 0},
        ]
        with patch.object(self.reservoir, "fetch_levels", return_value=levels):
            out = self.reservoir.build_reservoir_section({})
        self.assertIn("0단계 (만수위)", out)


class TestDecideAction(unittest.TestCase):
    """ATH 트리거 판정 — '자동투자만 유지' 제거 (2026.6 개정)."""

    def test_no_ath_data_returns_no_headline(self):
        from intraday_alert import _decide_action
        headline, detail = _decide_action(None, {}, 0)
        self.assertIsNone(headline)
        self.assertIn("전고점", detail)

    def test_no_active_trigger_returns_no_headline(self):
        from intraday_alert import _decide_action
        ath = {
            "current": 100, "ath": 101, "drawdown": -1.0, "active": None,
            "triggers": [{"drop": -5, "fire": 0.25, "action": "core", "lev": [], "cap": 0.0}],
        }
        headline, detail = _decide_action(ath, {}, 0)
        self.assertIsNone(headline)
        self.assertIn("다음 트리거", detail)

    def test_active_trigger_returns_headline(self):
        from intraday_alert import _decide_action
        ath = {
            "current": 90, "ath": 100, "drawdown": -10.0,
            "active": {"drop": -10, "fire": 0.5, "action": "core+lev", "lev": ["SSO"], "cap": 0.02},
            "triggers": [],
        }
        headline, detail = _decide_action(ath, {"SGOV": 0}, 1_000)
        self.assertIsNotNone(headline)
        self.assertIn("QQQM", headline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
