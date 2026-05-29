#!/usr/bin/env python3
"""
Daily Report — 종합 판단 버전
모든 지표를 내부적으로 분석해서 종목별 매수/홀딩/매도 결론만 전송합니다.
"""

import re
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import os
import time
import pandas as pd
import claude_client
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from market_indicators import collect_all, get_nzd_krw_cross, format_change_chip
from telegram_notifier import send_message
from config import Config
from events import build_calendar_section, get_dividend_schedule
from rebalancing import check_drifts, calc_portfolio_state
import ibkr_flex

_config = Config()

ACCUMULATION_PORTFOLIO = _config.ACCUMULATION_PORTFOLIO

# ── 포트폴리오 설정 ──────────────────────────────────────────────
_watch = os.getenv("WATCH_STOCKS", "")
PORTFOLIO = [t.strip() for t in _watch.split(",") if t.strip()] or \
            ["QQQI","SPYI","ETN","MU","VRT","AEHR","GEV",
             "SOXL","UPRO","QLD","TQQQ","SSO","QQQM","SOXQ","SPYM","SCHD"]
MA_PERIODS = [50, 200]


def fetch_stock_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(period=period)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"[fetch_stock_data] {ticker} attempt {attempt+1}: {e}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return pd.DataFrame()


def calc_moving_averages(df: pd.DataFrame) -> dict:
    result = {}
    close = df["Close"].squeeze()
    for p in MA_PERIODS:
        if len(close) >= p:
            result[p] = float(close.rolling(p).mean().iloc[-1])
    return result


def calc_drawdown_from_high(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"current": 0, "high": 0, "drawdown_pct": 0}
    close = df["Close"].squeeze()
    current = float(close.iloc[-1])
    high = float(close.max())
    drawdown_pct = (current - high) / high * 100 if high else 0
    return {"current": current, "high": high, "drawdown_pct": drawdown_pct}


def calc_rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    close = df["Close"].squeeze()
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    return round(rsi, 1)


def calc_52w_position(df: pd.DataFrame) -> dict | None:
    close = df["Close"].squeeze()
    if len(close) < 50:
        return None
    last252 = close.iloc[-252:] if len(close) >= 252 else close
    low52 = float(last252.min())
    high52 = float(last252.max())
    current = float(close.iloc[-1])
    pos = (current - low52) / (high52 - low52) * 100 if high52 != low52 else 50
    return {"low": low52, "high": high52, "pos_pct": round(pos, 1)}


def calc_macro_risk_score(indicators: dict) -> tuple[int, list[str]]:
    """
    거시 위험 점수 (0~10+, 높을수록 위험).
    현금 비중 목표를 동적으로 조절하는 데 사용.
    """
    score = 0
    signals = []

    buffett = indicators.get("buffett", {})
    if not buffett.get("error") and buffett.get("value"):
        v = buffett["value"]
        if v >= 220:
            score += 3; signals.append(f"버핏지수 {v:.0f}% 극단 거품")
        elif v >= 200:
            score += 2; signals.append(f"버핏지수 {v:.0f}% 거품 구간")
        elif v >= 180:
            score += 1; signals.append(f"버핏지수 {v:.0f}% 고평가")

    spread = indicators.get("credit_spread", {})
    if not spread.get("error") and spread.get("value"):
        v = spread["value"]
        if v >= 4.0:
            score += 3; signals.append(f"신용스프레드 {v}% 위기")
        elif v >= 3.0:
            score += 2; signals.append(f"신용스프레드 {v}% 위험")
        elif v >= 2.5:
            score += 1; signals.append(f"신용스프레드 {v}% 주의")

    yc = indicators.get("yield_curve", {})
    if not yc.get("error") and yc.get("value") is not None:
        v = yc["value"]
        prev = yc.get("prev")
        if prev is not None and prev < 0 and v >= 0:
            score += 3; signals.append("금리차 역전 해소 직후 — 6~12개월 주의")
        elif v < 0:
            score += 2; signals.append(f"금리차 역전 중 ({v:+.2f}%)")

    fg = indicators.get("fear_greed", {})
    if not fg.get("error") and fg.get("score"):
        s = fg["score"]
        if s >= 80:
            score += 2; signals.append(f"극도 탐욕 (F&G {s})")
        elif s >= 70:
            score += 1; signals.append(f"탐욕 (F&G {s})")

    vix = indicators.get("vix", {})
    if not vix.get("error") and vix.get("current"):
        v = vix["current"]
        if v < 15:
            score += 1; signals.append(f"VIX {v} 과열 (낮은 변동성)")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error") and aaii.get("bullish") is not None:
        bull = aaii["bullish"]
        if bull >= 60:
            score += 2; signals.append(f"AAII 강세 {bull:.0f}% 과열")
        elif bull >= 55:
            score += 1; signals.append(f"AAII 강세 {bull:.0f}%")

    breadth = indicators.get("breadth", {})
    if not breadth.get("error") and breadth.get("pct_above_200") is not None:
        p200 = breadth["pct_above_200"]
        if p200 >= 85:
            score += 1; signals.append(f"섹터 {p200}% 200일선 위 과매수")

    return score, signals


def calc_cash_target(risk_score: int) -> float:
    """위험 점수 → 현금 목표 비중"""
    if risk_score >= 7:
        return 0.30
    elif risk_score >= 5:
        return 0.25
    elif risk_score >= 3:
        return 0.22
    return 0.20


def check_extreme_overheated(ticker_data: dict) -> dict | None:
    """극단 과열 판단 — risk_score 7+ 상황에서만 표시하는 선택적 익절 신호"""
    rsi = ticker_data.get("rsi")
    w52 = ticker_data.get("w52")
    if rsi is None or not w52:
        return None
    if rsi >= 78 and w52["pos_pct"] >= 97:
        return {
            "emoji": "⚠️",
            "reason": f"RSI {rsi} · 52주 {w52['pos_pct']:.0f}% — 극단 과열, 부분 차익 고려",
        }
    return None


def build_cash_section(holdings: dict[str, float], idle_cash: float,
                       base_target_ratio: float, cash_tickers: list[str],
                       risk_score: int = 0, risk_signals: list[str] = None) -> tuple[str, float, float]:
    """현재 현금 비중 vs 목표 비중 추적. (section_text, available_cash, total_portfolio) 반환."""
    cash_value = idle_cash
    total_value = idle_cash
    available_cash = idle_cash  # SGOV + idle

    for ticker, shares in holdings.items():
        if shares <= 0:
            continue
        try:
            df = fetch_stock_data(ticker, period="5d")
            if df.empty:
                continue
            price = float(df["Close"].squeeze().iloc[-1])
            value = price * shares
            total_value += value
            if ticker in cash_tickers:
                cash_value += value
                available_cash += value
        except Exception:
            continue

    if total_value <= 0:
        return "", available_cash, 0.0

    target_ratio = calc_cash_target(risk_score)
    ratio = cash_value / total_value
    target_value = total_value * target_ratio
    diff_pct = (ratio - target_ratio) * 100
    diff_usd = cash_value - target_value

    if risk_score >= 7:
        target_label = f"🔴 {target_ratio*100:.0f}%  (위험 {risk_score}점 — 방어)"
    elif risk_score >= 5:
        target_label = f"🟠 {target_ratio*100:.0f}%  (위험 {risk_score}점)"
    elif risk_score >= 3:
        target_label = f"🟡 {target_ratio*100:.0f}%  (위험 {risk_score}점)"
    else:
        target_label = f"🟢 {target_ratio*100:.0f}%"

    lines = ["<b>💵 현금 비중</b>"]
    lines.append(f"  현재  <b>${cash_value:,.0f}</b>  ({ratio*100:.1f}%)")
    lines.append(f"  목표  {target_label}")
    lines.append(f"  총자산 ${total_value:,.0f}")

    if abs(diff_pct) < 1.5:
        lines.append("  ✅ 목표 달성")
    elif diff_pct > 0:
        lines.append(f"  💰 목표 +{diff_pct:.1f}%p 초과 (여유 ${diff_usd:.0f})")
    else:
        lines.append(f"  ⚠️ 목표 {abs(diff_pct):.1f}%p 부족  (${-diff_usd:.0f} 미달)")

    if risk_signals:
        lines.append(f"  <i>위험 신호: {' · '.join(risk_signals[:3])}</i>")

    return "\n".join(lines), available_cash, total_value


def build_dividend_section(holdings: dict[str, float], nzd_rate: float = 0) -> str:
    """보유 주수 기반 이번 달 예상 배당금 계산"""
    rows = []
    total_annual = 0.0

    for ticker, shares in holdings.items():
        if shares < 0.01:
            continue
        try:
            info = yf.Ticker(ticker).info
            # trailingAnnualDividendRate = 주당 연간 배당금 (USD) — yield보다 정확
            annual_rate = info.get("trailingAnnualDividendRate") or 0
            price = info.get("regularMarketPrice") or info.get("previousClose") or 0
            if annual_rate and shares:
                annual = shares * annual_rate
                total_annual += annual
                monthly = annual / 12
                div_yield_pct = annual_rate / price * 100 if price else 0
                if monthly >= 0.5:
                    rows.append((ticker, monthly, div_yield_pct))
        except Exception:
            pass

    if not rows:
        return ""

    rows.sort(key=lambda x: x[1], reverse=True)
    lines = ["<b>💰 예상 배당 (이번 달)</b>"]
    for ticker, monthly, yld in rows:
        lines.append(f"  <b>{ticker}</b>  ${monthly:.2f}  <i>({yld:.1f}%/yr)</i>")

    monthly_total = total_annual / 12
    nzd_str = f"  ≈  NZD {monthly_total * nzd_rate:.0f}" if nzd_rate else ""
    lines.append(f"  ─────────────────────")
    lines.append(f"  <b>합계  ${monthly_total:.2f}/월{nzd_str}</b>")
    lines.append(f"  <i>(연 ${total_annual:.2f}{f'  ≈  NZD {total_annual * nzd_rate:.0f}' if nzd_rate else ''})</i>")
    return "\n".join(lines)


# ── 추가매수 없는 홀딩 전용 / 레버리지 전략 전용 종목 ────────────

_NO_ADD_BUY = {"QQQI", "SPYI"}

# 본주별 단계적 레버리지 전략
# etfs: [(ticker, 비중)] — 비중 합산 1.0
_LEV_STRATEGY = {
    "QQQM": {
        "name": "나스닥100",
        "tiers": [
            {"drop": -5,  "label": "1차", "lev": "~1.5x", "etfs": [("QLD",  1.0)],                 "ratio": 0.10},
            {"drop": -10, "label": "2차", "lev": "2x",    "etfs": [("QLD",  1.0)],                 "ratio": 0.20},
            {"drop": -15, "label": "3차", "lev": "2.5x",  "etfs": [("QLD",  0.5), ("TQQQ", 0.5)], "ratio": 0.35},
            {"drop": -20, "label": "4차", "lev": "3x",    "etfs": [("TQQQ", 1.0)],                 "ratio": 0.50},
        ],
    },
    "SPYM": {
        "name": "S&P500",
        "tiers": [
            {"drop": -5,  "label": "1차", "lev": "~1.5x", "etfs": [("SSO",  1.0)],                 "ratio": 0.10},
            {"drop": -10, "label": "2차", "lev": "2x",    "etfs": [("SSO",  1.0)],                 "ratio": 0.20},
            {"drop": -15, "label": "3차", "lev": "2.5x",  "etfs": [("SSO",  0.5), ("UPRO", 0.5)], "ratio": 0.35},
            {"drop": -20, "label": "4차", "lev": "3x",    "etfs": [("UPRO", 1.0)],                 "ratio": 0.50},
        ],
    },
    "SOXQ": {
        "name": "반도체",
        "tiers": [
            {"drop": -10, "label": "1차", "lev": "3x",    "etfs": [("SOXL", 1.0)], "ratio": 0.10},
            {"drop": -15, "label": "2차", "lev": "3x",    "etfs": [("SOXL", 1.0)], "ratio": 0.20},
            {"drop": -20, "label": "3차", "lev": "3x",    "etfs": [("SOXL", 1.0)], "ratio": 0.35},
            {"drop": -25, "label": "4차", "lev": "3x",    "etfs": [("SOXL", 1.0)], "ratio": 0.50},
        ],
    },
}


def build_buy_zones(holdings: dict[str, float]) -> str:
    """
    개별 종목 3단계 매수 구간.
    - QQQI/SPYI 등 추가매수 없는 종목 제외
    - QQQM/SPYM/SOXQ 등 레버리지 전략 종목 제외 (레버리지 가이드에서 별도 표시)
    - 나머지 보유 종목만: 200일선 / 52주 고점-20% / 52주 저점+5%
    """
    skip = _NO_ADD_BUY | set(_LEV_STRATEGY.keys())
    rows = []
    for ticker, qty in holdings.items():
        if not qty or qty <= 0 or ticker in skip:
            continue
        try:
            df = fetch_stock_data(ticker, period="1y")
            if df.empty or len(df) < 50:
                continue
            close = df["Close"].squeeze()
            current = float(close.iloc[-1])
            ma200 = float(close.rolling(min(200, len(close))).mean().iloc[-1])
            high52 = float(close.max())
            low52  = float(close.min())
            z1 = round(ma200, 2)
            z2 = round(high52 * 0.80, 2)
            z3 = round(low52  * 1.05, 2)

            def pct_from(target: float) -> str:
                return f"{(target - current) / current * 100:+.1f}%"

            rows.append((ticker, current, z1, z2, z3,
                         pct_from(z1), pct_from(z2), pct_from(z3)))
        except Exception:
            continue

    if not rows:
        return ""

    lines = ["<b>📉 매수 구간 (기타 보유 종목)</b>"]
    for ticker, cur, z1, z2, z3, p1, p2, p3 in rows:
        lines.append(
            f"  <b>{ticker}</b>  ${cur:.2f}\n"
            f"    🟡 1차 ${z1}  ({p1})  · 200일선\n"
            f"    🟠 2차 ${z2}  ({p2})  · 52주 고점 -20%\n"
            f"    🔴 3차 ${z3}  ({p3})  · 52주 저점 근처"
        )
    return "\n".join(lines)


def _calc_rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _timing_note(close: pd.Series) -> str:
    """하락 가속 중인지, 반등 신호인지 한 줄 판단."""
    if len(close) < 6:
        return ""
    rsi    = _calc_rsi(close)
    ma5    = float(close.rolling(5).mean().iloc[-1])
    cur    = float(close.iloc[-1])
    ret5d  = (cur - float(close.iloc[-6])) / float(close.iloc[-6]) * 100

    if rsi is not None and rsi <= 30:
        return f"⚡ RSI {rsi:.0f} 과매도 — 반등 가능성↑"
    if ret5d <= -3 and cur < ma5:
        return f"⏸ 하락 진행 중 (5일 {ret5d:+.1f}%) — 분할 대기"
    if ret5d > 1.0 and cur > ma5:
        return "🟢 반등 시작 — 진입 우호"
    return ""


def _calc_deployable_cash(holdings: dict[str, float], idle_cash: float) -> tuple[float, float, float]:
    """SGOV 시세 × 수량 + 달러잔고 = 총 가용현금. (total, sgov_val, idle) 반환."""
    sgov_price = 0.0
    sgov_qty   = holdings.get("SGOV", 0) or 0
    if sgov_qty > 0:
        try:
            df = fetch_stock_data("SGOV", period="5d")
            if not df.empty:
                sgov_price = float(df["Close"].squeeze().iloc[-1])
        except Exception:
            pass
    sgov_val = round(sgov_price * sgov_qty, 2)
    total    = round(sgov_val + idle_cash, 2)
    return total, sgov_val, idle_cash


def _tier_emoji(lev: str) -> str:
    if lev == "3x":   return "🔴"
    if lev == "2.5x": return "🟠"
    if lev == "2x":   return "🟡"
    return "🟢"


def build_leverage_guide(holdings: dict[str, float], idle_cash: float,
                         total_portfolio: float = 0.0) -> str:
    """
    QQQM/SPYM/SOXQ 낙폭 → 레버리지 ETF 단계별 매수 가이드.
    가용현금 = SGOV 시세×수량 + 달러잔고.

    단계별 레버리지:
      QQQM/SPYM  -5%  1차 ~1.5x (QLD/SSO 소량)
                 -10% 2차  2x   (QLD/SSO)
                 -15% 3차  2.5x (QLD+TQQQ / SSO+UPRO 반반)
                 -20% 4차  3x   (TQQQ/UPRO)
      SOXQ       -10% 1차  3x   (SOXL 소량, 2x 없음)
    """
    total_cash, sgov_val, idle = _calc_deployable_cash(holdings, idle_cash)
    if total_cash <= 0:
        return ""

    lines = [
        "<b>📐 레버리지 전략 가이드</b>",
        f"  💰 가용현금  <b>${total_cash:,.0f}</b>"
        + (f"  <i>(달러 ${idle:,.0f} + SGOV ${sgov_val:,.0f})</i>" if sgov_val > 0 else ""),
    ]

    # 단계별 매수 후 현금 비중 시뮬레이션 (총자산 알 때만)
    if total_portfolio > total_cash:
        lines.append("")
        lines.append("  <i>매수 후 예상 현금 비중 (누적)</i>")
        cumulative_buy = 0.0
        for drop, ratio, label in [(-5, 0.10, "1차"), (-10, 0.20, "2차"),
                                    (-15, 0.35, "3차"), (-20, 0.50, "4차")]:
            cumulative_buy += total_cash * ratio
            remaining_cash = total_cash - cumulative_buy
            new_cash_pct   = remaining_cash / total_portfolio * 100
            if new_cash_pct >= 18:
                icon = "✅"
            elif new_cash_pct >= 12:
                icon = "⚠️"
            else:
                icon = "🔴"
            lines.append(
                f"  {icon} {label} 후  현금 ${remaining_cash:,.0f}  ({new_cash_pct:.1f}%)"
                + ("  ← 매도 검토" if new_cash_pct < 12 else "")
            )

    any_signal = False
    for base_ticker, info in _LEV_STRATEGY.items():
        try:
            df = fetch_stock_data(base_ticker, period="3mo")
            if df.empty:
                continue
            close  = df["Close"].squeeze()
            cur    = float(close.iloc[-1])
            high60 = float(close.rolling(min(60, len(close))).max().iloc[-1])
            dd     = (cur - high60) / high60 * 100
            name   = info["name"]
            tiers  = info["tiers"]

            active = None
            for t in reversed(tiers):
                if dd <= t["drop"]:
                    active = t
                    break

            lines.append("")
            lines.append(f"  <b>{base_ticker}</b> ({name})  현재 {dd:+.1f}%  (60일 고점 대비)")

            for t in tiers:
                amt      = total_cash * t["ratio"]
                etf_strs = "  +  ".join(
                    f"<b>{etf}</b> ${amt*w:,.0f}" for etf, w in t["etfs"]
                )
                marker = "👉" if active and t["drop"] == active["drop"] else "  "
                lines.append(
                    f"  {marker} {_tier_emoji(t['lev'])} {t['label']} {t['drop']}%  "
                    f"[{t['lev']}]  {etf_strs}  <i>({t['ratio']*100:.0f}%)</i>"
                )

            if active:
                any_signal = True
                note = _timing_note(close)
                etf_buy = "  +  ".join(
                    f"{etf} ${total_cash * active['ratio'] * w:,.0f}"
                    for etf, w in active["etfs"]
                )
                lines.append(f"     ▶ 지금: {etf_buy}")
                if note:
                    lines.append(f"     {note}")
            else:
                first_tier = tiers[0]
                gap = (cur * (1 + first_tier["drop"] / 100) - cur) / cur * 100
                first_etf = first_tier["etfs"][0][0]
                lines.append(
                    f"     ⚪ 대기 중  1차({first_tier['drop']}%)까지 <b>{gap:+.1f}%</b>  → {first_etf} 준비"
                )
        except Exception as e:
            print(f"[leverage_guide] {base_ticker}: {e}")

    if any_signal:
        lines.append("")
        lines.append("  <i>※ 같은 구간 지속 시 매일 1회분씩 분할 추가</i>")

    return "\n".join(lines)


# ── 현금 비중 회복 매도 계획 ─────────────────────────────────────

# 트리밍 허용 종목 (추가매수 없는 홀딩이거나 레버리지 차익실현 대상)
_TRIM_PRIORITY = [
    # (ticker, 분류, 최소 수익률 기준)
    # 레버리지 — 반등 시 수익 실현
    ("TQQQ", "레버리지", 0.20),
    ("UPRO", "레버리지", 0.20),
    ("QLD",  "레버리지", 0.20),
    ("SSO",  "레버리지", 0.20),
    ("SOXL", "레버리지", 0.25),
    # 추가매수 없는 커버드콜 ETF — 과중 시 일부 트리밍 허용
    ("QQQI", "배당홀딩", 0.0),
    ("SPYI", "배당홀딩", 0.0),
]


def build_cash_restore_plan(
    ibkr_positions: dict,
    total_portfolio: float,
    current_cash: float,
    target_cash_ratio: float = 0.20,
    warn_threshold: float = 0.15,
) -> str:
    """
    현금 비중이 warn_threshold 미만일 때 매도 후보 제시.

    ibkr_positions: {symbol: {qty, cost_basis, mark_price, unrealized_pnl}}
    매도 우선순위:
      1) 레버리지 ETF — 취득가 대비 +20%+ 수익 난 것부터
      2) 추가매수 없는 배당 홀딩 (QQQI/SPYI) — 목표 비중 초과분
    목표: 현금을 target_cash_ratio(20%)까지 회복
    """
    if total_portfolio <= 0:
        return ""

    cash_ratio = current_cash / total_portfolio
    if cash_ratio >= warn_threshold:
        return ""   # 현금 충분 — 섹션 숨김

    needed_cash = total_portfolio * target_cash_ratio - current_cash
    lines = [
        "<b>⚖️ 현금 회복 매도 계획</b>",
        f"  현재 현금 <b>{cash_ratio*100:.1f}%</b>  (목표 {target_cash_ratio*100:.0f}%,  부족 <b>${needed_cash:,.0f}</b>)",
    ]

    candidates = []
    for ticker, category, min_gain in _TRIM_PRIORITY:
        pos = ibkr_positions.get(ticker)
        if not pos or pos.get("qty", 0) <= 0:
            continue
        qty        = pos["qty"]
        cost       = pos.get("cost_basis", 0)
        mark       = pos.get("mark_price", 0)
        if cost <= 0 or mark <= 0:
            continue
        gain_pct   = (mark - cost) / cost
        if gain_pct < min_gain:
            continue
        total_val  = mark * qty
        candidates.append({
            "ticker":    ticker,
            "category":  category,
            "gain_pct":  gain_pct,
            "mark":      mark,
            "qty":       qty,
            "total_val": total_val,
        })

    # 수익률 높은 순 정렬
    candidates.sort(key=lambda x: -x["gain_pct"])

    if not candidates:
        lines.append("  • 매도 후보 없음 (레버리지 미보유 or 수익 미달)")
        lines.append(f"  • 현금 충당 방법: SGOV 매수 or 일부 배당 ETF 트리밍 수동 검토")
        return "\n".join(lines)

    lines.append("  <i>수익률 높은 순 — 합산 목표 금액 도달 시 중단</i>")
    cumulative  = 0.0
    restored_pct = cash_ratio
    for c in candidates:
        if cumulative >= needed_cash:
            break
        # 전체 포지션의 최대 50% 매도 (한번에 다 팔지 않음)
        sell_val  = min(c["total_val"] * 0.5, needed_cash - cumulative)
        sell_qty  = sell_val / c["mark"]
        cumulative   += sell_val
        restored_pct  = (current_cash + cumulative) / total_portfolio
        lines.append(
            f"  🔻 <b>{c['ticker']}</b>  수익 <b>{c['gain_pct']*100:+.1f}%</b>  "
            f"→ {sell_qty:.2f}주 매도  ${sell_val:,.0f}  "
            f"<i>(잔여 {c['qty']-sell_qty:.2f}주)</i>"
        )

    final_cash_pct = (current_cash + min(cumulative, needed_cash)) / total_portfolio * 100
    icon = "✅" if final_cash_pct >= 18 else "⚠️"
    lines.append(f"\n  {icon} 매도 후 예상 현금  <b>{final_cash_pct:.1f}%</b>")
    return "\n".join(lines)



# ── 시장 뉴스 수집 + Claude 코멘터리 ────────────────────────────

def _extract_news_item(item: dict) -> dict | None:
    """yfinance 뉴스 항목에서 title/link/publisher/summary 추출 (구/신 포맷 모두 지원)."""
    content = item.get("content") if isinstance(item.get("content"), dict) else None
    src = content or item

    title = src.get("title") or item.get("title")
    if not title:
        return None

    link = item.get("link")
    if not link and isinstance(src.get("canonicalUrl"), dict):
        link = src["canonicalUrl"].get("url")
    if not link and isinstance(src.get("clickThroughUrl"), dict):
        link = src["clickThroughUrl"].get("url")

    publisher = item.get("publisher") or ""
    if not publisher and isinstance(src.get("provider"), dict):
        publisher = src["provider"].get("displayName", "")

    summary = (src.get("summary") or item.get("summary") or "")[:120]

    return {"title": title, "link": link or "", "publisher": publisher, "summary": summary}


def fetch_market_news() -> list[dict]:
    """yfinance로 주요 지수 관련 최신 뉴스 수집"""
    news_items = []
    seen = set()
    for sym in ["SPY", "QQQ"]:
        try:
            for item in (yf.Ticker(sym).news or [])[:6]:
                n = _extract_news_item(item)
                if not n or n["title"] in seen:
                    continue
                seen.add(n["title"])
                news_items.append(n)
        except Exception:
            pass
    return news_items[:8]


def generate_news_commentary(news_items: list[dict], mkt_score: int, mkt_reasons: list[str]) -> str:
    """Claude로 뉴스 요약 + 투자 대응 포인트 생성"""
    if not news_items:
        return ""

    news_text = "\n".join(
        f"- {it['title']}" + (f": {it['summary']}" if it["summary"] else "")
        for it in news_items
    )
    mkt_ctx = f"시장 점수 {mkt_score:+d}" + (
        f" ({', '.join(mkt_reasons)})" if mkt_reasons else ""
    )

    prompt = f"""오늘의 주요 시장 뉴스:
{news_text}

현재 시장 상황: {mkt_ctx}

다음 두 파트를 텔레그램 HTML 형식으로 간결하게 작성해주세요:

<b>📰 오늘의 주요 이슈</b>
• 이슈 1
• 이슈 2
• 이슈 3

<b>🛡 투자 대응 포인트</b>
• 대응 1
• 대응 2

총 10줄 이내. 한국어."""

    result = claude_client.call(prompt, max_tokens=512)
    if not result:
        print("[news_commentary] Claude 응답 없음")
    return result


# ── 적립 포트폴리오 평가 ────────────────────────────────────────

def _fetch_ticker_quick(ticker: str) -> dict:
    """3개월 종가 데이터로 MA20·고점 대비 낙폭 계산"""
    try:
        df = yf.Ticker(ticker).history(period="3mo")
        if df is None or df.empty:
            return {}
        close = df["Close"].dropna()
        if len(close) < 3:
            return {}
        current = float(close.iloc[-1])
        ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
        high = float(close.max())
        return {
            "price": round(current, 2),
            "above_ma20": (current > ma20) if ma20 else None,
            "drawdown_3mo": round((current - high) / high * 100, 1),
        }
    except Exception as e:
        print(f"[_fetch_ticker_quick] {ticker} 실패: {e}")
        return {}


def fetch_accumulation_data(tickers: list) -> dict:
    """적립 포트폴리오 전체 데이터 병렬 수집"""
    result = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_ticker_quick, t): t for t in tickers}
        for fut in as_completed(futures, timeout=45):
            ticker = futures[fut]
            try:
                data = fut.result()
                if data:
                    result[ticker] = data
            except Exception:
                pass
    return result


# 종목 설명 사전 — Claude가 포트폴리오 중복·맥락을 파악하는 데 사용
_TICKER_DESC: dict[str, str] = {
    "QQQI": "나스닥100 커버드콜 고배당", "SPYI": "S&P500 커버드콜 고배당",
    "SPYM": "S&P500 적립형", "QQQM": "나스닥100 적립형",
    "SCHD": "배당 ETF", "DIVO": "배당 ETF(커버드콜)",
    "DGRW": "배당성장 ETF", "QDVO": "커버드콜 배당 ETF",
    "BITX": "비트코인 2x 레버리지", "ETHU": "이더리움 2x 레버리지",
    "ETN": "이튼(전력인프라·전기화)", "NVDA": "엔비디아(AI GPU)",
    "VRT": "버티브홀딩스(AI 데이터센터 냉각)", "CCJ": "카메코(우라늄 광산)",
    "CEG": "컨스텔레이션에너지(원전)", "AVGO": "브로드컴(AI 반도체·네트워크)",
    "XOM": "엑슨모빌(에너지·석유)", "COPX": "구리광산 ETF(산업금속)",
    "SOXQ": "반도체 ETF", "SOXX": "반도체 ETF(iShares·대형주)",
    "SOXL": "반도체 3x 레버리지", "QLD": "나스닥100 2x 레버리지",
    "SSO": "S&P500 2x 레버리지", "TQQQ": "나스닥100 3x 레버리지",
    "UPRO": "S&P500 3x 레버리지", "SLV": "은 ETF(실물)",
    "GLDM": "금 ETF(실물·저비용)", "ARKK": "혁신성장 ETF(캐시우드)",
    "SGOV": "초단기 국채 ETF(현금성 대피처)", "CRCL": "써클인터넷그룹(스테이블코인)",
}

# 노출 영역 그룹 — 편입 추천 시 중복 방지에 사용
_COVERAGE_GROUPS = {
    "금(실물)": ["GLDM"],
    "은(실물)": ["SLV"],
    "반도체": ["SOXQ", "SOXX", "SOXL", "NVDA", "AVGO"],
    "나스닥레버리지": ["QLD", "TQQQ", "QQQM"],
    "S&P500레버리지": ["SSO", "UPRO", "SPYM"],
    "비트코인": ["BITX"],
    "이더리움": ["ETHU"],
    "원전·우라늄": ["CCJ", "CEG"],
    "AI인프라": ["VRT", "ETN", "NVDA", "AVGO"],
    "구리": ["COPX"],
    "석유": ["XOM"],
    "배당": ["SCHD", "DIVO", "DGRW", "QDVO", "QQQI", "SPYI"],
}


def generate_accumulation_report(mkt_score: int, news_items: list[dict]) -> str:
    """적립 포트폴리오 유지/중단 판단 + 편입/퇴출 추천 (Claude)"""
    portfolio_data = fetch_accumulation_data(ACCUMULATION_PORTFOLIO)
    if not portfolio_data:
        return ""

    # 종목 요약 텍스트 (설명 포함)
    ticker_lines = []
    for t in ACCUMULATION_PORTFOLIO:
        desc = _TICKER_DESC.get(t, "")
        d = portfolio_data.get(t)
        if not d:
            ticker_lines.append(f"{t}({desc}): 데이터 없음")
            continue
        ma_str = "MA20↑" if d["above_ma20"] else ("MA20↓" if d["above_ma20"] is False else "MA-")
        ticker_lines.append(f"{t}({desc}): ${d['price']} {ma_str} {d['drawdown_3mo']:+.1f}%")

    # 이미 커버된 영역 정리 (중복 추천 방지용)
    covered = []
    held = set(ACCUMULATION_PORTFOLIO)
    for area, tickers in _COVERAGE_GROUPS.items():
        if any(t in held for t in tickers):
            covered.append(area)
    covered_str = ", ".join(covered)

    news_titles = " / ".join(it["title"] for it in news_items[:4]) if news_items else ""

    prompt = f"""소액 DCA(매일 $1~3) 투자자 포트폴리오 점검 요청.

[현재 보유 종목 현황]
{chr(10).join(ticker_lines)}

[시장 점수] {mkt_score:+d}
[최근 뉴스] {news_titles}
[이미 커버된 노출 영역] {covered_str}

━━━ 작성 지침 ━━━
아래 두 파트를 텔레그램 HTML 형식으로 작성해주세요.

<b>📦 적립 포트폴리오 점검</b>
각 종목 한 줄씩, 판단 기준:
  ✅ 계속 모으기 — 추세·기술 지표 양호
  ⏸ 잠시 멈추기 — 하락 추세, 레버리지 손실 배율 위험
  ⬇️ 비중 축소 고려 — thesis 훼손 or 과도한 비중
이유에 구체적 수치(MA20 위/아래, 낙폭) 반드시 포함.
레버리지(2x·3x)·크립토 ETF는 더 보수적 기준 적용.

<b>🌐 편입/퇴출 추천</b>
🔵 편입 고려 (최대 3개):
  - 현재 세계 동향상 추가 의미가 있는 종목
  - ⚠️ 이미 커버된 영역({covered_str})과 겹치는 종목은 추천 금지
    예외: 동일 자산군이라도 접근 방식이 명확히 다를 때만 허용하고 차이를 명시
  - 티커·설명·편입 근거 한 줄
🔴 퇴출/중단 고려 (최대 3개):
  - 현재 보유 중이지만 thesis 훼손되었거나 중복 과도한 종목
  - 티커·이유 한 줄

한국어. 간결하게."""

    try:
        result = claude_client.call(prompt, max_tokens=1500)
        if not result:
            print("[accumulation_report] Claude 응답 없음")
        return result
    except Exception as e:
        print(f"[accumulation_report] 오류: {e}")
        return ""


# ── 시장 환경 점수 (-10 ~ +10, 양수 = 매수 우호적) ──────────────

def market_score(indicators: dict) -> tuple:
    """지표들을 종합해 시장 환경 점수와 근거 반환"""
    score = 0
    reasons = []

    fg = indicators.get("fear_greed", {})
    if not fg.get("error"):
        s = fg["score"]
        if s <= 20:
            score += 4; reasons.append(f"극도 공포 (F&G {s})")
        elif s <= 35:
            score += 3; reasons.append(f"공포 (F&G {s})")
        elif s <= 45:
            score += 1; reasons.append(f"약한 공포 (F&G {s})")
        elif s >= 80:
            score -= 3; reasons.append(f"극도 탐욕 (F&G {s})")
        elif s >= 65:
            score -= 2; reasons.append(f"탐욕 (F&G {s})")
        elif s >= 55:
            score -= 1; reasons.append(f"약한 탐욕 (F&G {s})")

    vix = indicators.get("vix", {})
    if not vix.get("error"):
        v = vix["current"]
        if v >= 40:
            score += 5; reasons.append(f"VIX {v} 🔥 극공포 — 적극 매수 구간")
        elif v >= 30:
            score += 3; reasons.append(f"VIX {v} — 매수 적극 검토")
        elif v >= 20:
            score += 2; reasons.append(f"VIX {v} — 매수 기회")
        elif v < 15:
            score -= 1; reasons.append(f"VIX {v} (과열 주의)")

    pc = indicators.get("put_call", {})
    if not pc.get("error"):
        r = pc["current"]
        if r >= 1.0:
            score += 2; reasons.append(f"Put/Call {r} (극공포)")
        elif r >= 0.8:
            score += 1; reasons.append(f"Put/Call {r} (공포)")
        elif r < 0.6:
            score -= 1; reasons.append(f"Put/Call {r} (탐욕)")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error") and aaii.get("bearish") is not None:
        bear = aaii["bearish"]
        if bear >= 50:
            score += 3; reasons.append(f"AAII 약세 {bear:.0f}% — 강한 역발상 신호")
        elif bear >= 40:
            score += 2; reasons.append(f"AAII 약세 {bear:.0f}% — 매수 신호")
        elif bear >= 35:
            score += 1; reasons.append(f"AAII 약세 우세 ({bear:.0f}%)")

    breadth = indicators.get("breadth", {})
    if not breadth.get("error"):
        p200 = breadth.get("pct_above_200", 50)
        if p200 <= 30:
            score += 2; reasons.append(f"섹터 {p200}%만 200일선 위 (저점 신호)")
        elif p200 <= 45:
            score += 1; reasons.append(f"섹터 {p200}% 200일선 위")
        elif p200 >= 80:
            score -= 1; reasons.append(f"섹터 {p200}% 200일선 위 (과열)")

    return score, reasons


# ── 종목별 판단 로직 ─────────────────────────────────────────────

def judge_ticker(ticker: str, mkt_score: int) -> dict:
    """종목별 매수/홀딩/매도 판단"""
    df = fetch_stock_data(ticker)
    if df.empty:
        return {"action": "데이터없음", "emoji": "⚪", "reasons": ["데이터 수집 실패"],
                "drawdown": 0, "price": 0, "score": 0, "rsi": None, "w52": None}

    dd = calc_drawdown_from_high(df)
    mas = calc_moving_averages(df)
    rsi = calc_rsi(df)
    w52 = calc_52w_position(df)
    price = dd.get("current", 0)
    drawdown = dd.get("drawdown_pct", 0)

    stock_score = 0
    reasons = []

    # 1. 고점 대비 하락률
    if drawdown <= -20:
        stock_score += 4; reasons.append(f"고점 대비 {drawdown:.1f}% 급락")
    elif drawdown <= -15:
        stock_score += 3; reasons.append(f"고점 대비 {drawdown:.1f}% 하락")
    elif drawdown <= -10:
        stock_score += 2; reasons.append(f"고점 대비 {drawdown:.1f}% 하락")
    elif drawdown <= -5:
        stock_score += 1; reasons.append(f"고점 대비 {drawdown:.1f}% 조정")
    elif drawdown >= -2:
        stock_score -= 1; reasons.append(f"고점 근처 ({drawdown:.1f}%)")

    # 2. 이평선 위치
    above_mas = sum(1 for p in MA_PERIODS if p in mas and price >= mas[p])
    below_mas = sum(1 for p in MA_PERIODS if p in mas and price < mas[p])

    if below_mas >= 2:
        stock_score += 2; reasons.append("50/200일선 모두 하회")
    elif below_mas == 1:
        stock_score += 1; reasons.append("주요 이평선 하회")
    elif above_mas >= 2:
        stock_score -= 1; reasons.append("50/200일선 모두 상회 (고점 주의)")

    # 3. RSI 보조 신호 (판단 점수에 반영 안 함 — 표시만)
    # 4. 시장 환경 가중치 (레버리지 ETF는 민감도 높임)
    leverage = ticker in ("TQQQ", "UPRO", "SPYM", "SOXL", "QLD", "SSO")
    env_weight = 2 if leverage else 1
    total = stock_score + (mkt_score * env_weight // 3)

    # 5. 최종 판단 (적립식 장기투자 기준 — 매도 신호 신중하게)
    if total >= 5:
        action, emoji = "📈 적극 매수", "🟢"
    elif total >= 3:
        action, emoji = "📈 매수", "🟢"
    elif total >= 1:
        action, emoji = "🔍 분할매수 검토", "🟡"
    elif total >= -2:
        action, emoji = "⏸ 홀딩", "⚪"
    elif total >= -4:
        action, emoji = "💵 현금 비중 확대 검토", "🟠"
    else:
        action, emoji = "📉 매도 고려", "🔴"

    return {
        "action": action,
        "emoji": emoji,
        "reasons": reasons,
        "drawdown": drawdown,
        "price": price,
        "score": total,
        "rsi": rsi,
        "w52": w52,
    }


# ── 지표 → 액션 태그 ────────────────────────────────────────────

def _indicator_action(name: str, value) -> str:
    """지표명+값에 따른 한 줄 액션 태그. 빈 문자열이면 표시 생략."""
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if name == "fear_greed":
        if v <= 24: return "  → 적극 매수"
        if v <= 49: return "  → 분할 매수 우호"
        if v >= 75: return "  → 일부 차익 검토"
        if v >= 55: return "  → 신규 진입 자제"
    elif name == "vix":
        if v >= 30: return "  → 패닉, 매수 기회"
        if v >= 20: return "  → 변동성↑ 분할"
    elif name == "aaii_bullish":
        if v >= 55: return "  → 역지표, 매도 검토"
        if v <= 30: return "  → 역지표, 매수 우호"
    elif name == "put_call":
        if v >= 1.20: return "  → 과매도, 매수 우호"
        if v < 0.70:  return "  → 과매수, 자제"
    elif name == "breadth_200":
        if v >= 70: return "  → 과열"
        if v <= 30: return "  → 바닥 신호"
    elif name == "buffett":
        if v >= 180: return "  → 거품, 방어"
    elif name == "credit_spread":
        if v >= 5.0: return "  → 위험, 현금↑"
    elif name == "yield_curve":
        if v < 0: return "  → 침체 신호"
    return ""


# ── 배당 일정 섹션 (배당락일 + 입금예정일) ──────────────────────

def build_dividend_schedule_section(holdings: dict, days_ahead: int = 90) -> str:
    sched = get_dividend_schedule(holdings, days_ahead=days_ahead)
    if not sched:
        return ""

    lines = [f"<b>📅 배당 일정 ({days_ahead}일 이내)</b>"]
    total_upcoming = 0.0

    for d in sched:
        status   = d.get("status", "upcoming")
        ex_str   = f"{d['ex_date']:%m/%d}"
        ex_diff  = d["ex_days_left"]

        amt_str = ""
        if d["amount"]:
            if d["total"]:
                amt_str = f"  <b>+${d['total']:.2f}</b>  <i>(${d['amount']:.4f}×{d['qty']:g})</i>"
            else:
                amt_str = f"  ${d['amount']:.4f}/주"

        if status == "paid":
            # 최근 입금 완료
            pay_str = f"{d['pay_date']:%m/%d}" if d["pay_date"] else "?"
            lines.append(f"  ✅ <b>{d['ticker']}</b>  배당락 {ex_str}  입금 {pay_str} 완료{amt_str}")
            if d["total"]:
                total_upcoming += d["total"]

        elif status == "pending":
            # 배당락 지났지만 입금 전
            pay_diff = d["pay_days_left"]
            pay_str  = f"{d['pay_date']:%m/%d}" if d["pay_date"] else "?"
            pay_tag  = f"D-{pay_diff}"
            lines.append(f"  💵 <b>{d['ticker']}</b>  배당락 {ex_str} 완료  입금 {pay_str} ({pay_tag}){amt_str}")
            if d["total"]:
                total_upcoming += d["total"]

        else:
            # upcoming — 배당락 앞
            ex_tag = "오늘" if ex_diff == 0 else "내일" if ex_diff == 1 else f"D-{ex_diff}"
            if d["pay_date"]:
                pay_str = f"{d['pay_date']:%m/%d}"
                pay_tag = f"D-{d['pay_days_left']}" if d["pay_days_left"] and d["pay_days_left"] > 0 else "?"
                pay_part = f"  입금 {pay_str} ({pay_tag})"
            else:
                pay_part = "  입금 미정"
            lines.append(f"  📌 <b>{d['ticker']}</b>  배당락 {ex_str} ({ex_tag}){pay_part}{amt_str}")
            if d["total"]:
                total_upcoming += d["total"]

    if total_upcoming > 0:
        lines.append(f"\n  💰 <b>예상 수령 합계  +${total_upcoming:.2f}</b>")
    return "\n".join(lines)


# ── 오늘의 액션 플랜 ────────────────────────────────────────────

def build_action_plan(
    indicators: dict,
    mkt_score: int,
    risk_score: int,
    available_cash: float,
    drifts: list,
    extreme_overheat: list,
    buy_count: int,
) -> str:
    """모든 시그널을 종합해 우선순위 액션 2-3개 도출."""
    fg  = indicators.get("fear_greed", {})
    vix = indicators.get("vix", {})
    fg_score  = fg.get("score") if not fg.get("error") else None
    vix_val   = vix.get("current") if not vix.get("error") else None

    actions = []

    # 1) VIX 패닉 = 최우선
    if vix_val is not None and vix_val >= 30:
        amt = available_cash * 0.20
        actions.append((
            "⭐⭐⭐", "패닉 매수 기회",
            [
                f"VIX {vix_val} — 극단 변동성",
                f"가용현금 ${available_cash:,.0f} 중 20% (${amt:,.0f}) 1차 진입",
                "5단계 분할로 추가 하락 대비",
            ],
        ))

    # 2) 공포/탐욕 극단 공포 = 매수
    if fg_score is not None and fg_score <= 24:
        amt = available_cash * 0.30
        actions.append((
            "⭐⭐⭐", "극단 공포 — 적극 매수 구간",
            [
                f"공포/탐욕 {fg_score} — 역지표 매수 신호",
                f"가용현금 ${available_cash:,.0f} 중 30% (${amt:,.0f}) 분할 매수",
                f"우선 후보: 매수 리스트({buy_count}개) 확인",
            ],
        ))

    # 3) 매수 우호 (mkt_score ≥ 3)
    elif mkt_score >= 3:
        size_pct = 20 if mkt_score >= 6 else 10
        amt = available_cash * (size_pct / 100)
        actions.append((
            "⭐⭐" if mkt_score >= 6 else "⭐", "매수 우호 구간",
            [
                f"시장 점수 +{mkt_score} — 매수 시그널",
                f"가용현금 {size_pct}% (${amt:,.0f}) 분할 매수 검토",
                f"매수 리스트({buy_count}개) 중 200일선 근접 종목 우선",
            ],
        ))

    # 4) 거시 위험 (risk_score ≥ 7)
    if risk_score >= 7:
        actions.append((
            "⚠️", "거시 위험 — 방어 자세",
            [
                f"위험점수 {risk_score} — 침체/거품 신호",
                "신규 매수 자제, 현금 비중 확대",
                "차익 실현 후보 점검 (RSI 70+ 종목)",
            ],
        ))

    # 5) 극단 과열 — 차익 실현
    if extreme_overheat:
        actions.append((
            "🔴", "차익 실현 검토",
            [
                f"과열 종목 {len(extreme_overheat)}개 — 일부 매도 고려",
                "10-20% 부분 매도로 리스크 축소",
            ],
        ))

    # 6) 리밸런싱
    if drifts:
        d = drifts[0]
        actions.append((
            "⚖️", "리밸런싱 필요",
            [
                f"{d['category']} 드리프트 {d['drift_pct']:+.1f}%p",
                "타깃 비중으로 복원 검토",
            ],
        ))

    # 7) 시그널 약함 = 중립
    if not actions:
        actions.append((
            "⏸", "특별한 액션 없음",
            [
                "정기 DCA만 진행, 신규 진입 보류",
                "관찰 모드 — 지표 변동 모니터링",
            ],
        ))

    lines = ["<b>🎯 오늘의 액션 플랜</b>"]
    for i, (prio, title, details) in enumerate(actions[:4], 1):
        lines.append(f"\n{i}️⃣  {prio}  <b>{title}</b>")
        for det in details:
            lines.append(f"   • {det}")
    return "\n".join(lines)


# ── 리포트 빌더 ──────────────────────────────────────────────────

def build_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"<b>📊 투자 판단 브리핑</b>  {now}")
    lines.append("━" * 28)

    # ── 지표 수집 ──────────────────────────────────────────────────
    indicators = collect_all()
    mkt_score, mkt_reasons = market_score(indicators)
    risk_score, risk_signals = calc_macro_risk_score(indicators)
    nzd_rate = indicators.get("nzd", {}).get("usd_to_nzd", 0)

    holdings, idle_cash, _ibkr = ibkr_flex.resolve_holdings_and_cash(_config)
    _ibkr_ok = _ibkr["error"] is None and bool(_ibkr["positions"])

    # ── [1] 주요 지표 섹션 ─────────────────────────────────────────
    lines.append("\n<b>📈 주요 지표</b>")

    fg = indicators.get("fear_greed", {})
    if not fg.get("error"):
        trend = ""
        if fg.get("week_ago"):
            diff = fg["score"] - fg["week_ago"]
            trend = f"  {'↑' if diff > 0 else '↓'}{abs(diff):.0f} (1주 전 {fg['week_ago']})"
        tag = _indicator_action("fear_greed", fg.get("score"))
        lines.append(f"  공포/탐욕  <b>{fg['score']}</b> {fg.get('rating','')}{trend}{tag}")

    vix = indicators.get("vix", {})
    if not vix.get("error"):
        chg = f"{'↑' if vix['change'] > 0 else '↓'}{abs(vix['change'])}"
        tag = _indicator_action("vix", vix.get("current"))
        lines.append(f"  VIX        <b>{vix['current']}</b> {chg}  |  {vix['level']}{tag}")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error") and aaii.get("bullish") is not None:
        tag = _indicator_action("aaii_bullish", aaii["bullish"])
        lines.append(
            f"  AAII       강세 {aaii['bullish']:.0f}%  중립 {aaii.get('neutral',0):.0f}%  약세 {aaii['bearish']:.0f}%{tag}"
        )

    pc = indicators.get("put_call", {})
    if not pc.get("error"):
        tag = _indicator_action("put_call", pc.get("current"))
        lines.append(f"  Put/Call   {pc['current']}  ({pc['level']}){tag}")

    breadth = indicators.get("breadth", {})
    if not breadth.get("error"):
        tag = _indicator_action("breadth_200", breadth.get("pct_above_200"))
        lines.append(
            f"  섹터 MA    50일선 위 {breadth['pct_above_50']}%  |  200일선 위 {breadth['pct_above_200']}%{tag}"
        )

    fed = indicators.get("fed_rate", {})
    if not fed.get("error") and fed.get("value"):
        lines.append(f"  기준금리   {fed['value']}%")

    krw = indicators.get("usd_krw", {})
    nzd = indicators.get("nzd", {})
    has_krw = not krw.get("error") and krw.get("usd_to_krw")
    has_nzd = not nzd.get("error") and nzd.get("usd_to_nzd")
    if has_krw:
        suffix = f"(1주 전 ₩{krw['week_ago']:,.0f})" if krw.get("week_ago") else "(1주)"
        lines.append(f"  USD/KRW    <b>₩{krw['usd_to_krw']:,.2f}</b>{format_change_chip(krw.get('change_pct'), suffix)}")
    if has_nzd:
        suffix = f"(1주 전 NZ${nzd['week_ago']:.4f})" if nzd.get("week_ago") else "(1주)"
        lines.append(f"  USD/NZD    NZ${nzd['usd_to_nzd']:.4f}{format_change_chip(nzd.get('change_pct'), suffix)}")
    cross = get_nzd_krw_cross(krw, nzd) if (has_krw and has_nzd) else None
    if cross:
        suffix = f"(1주 전 ₩{cross['week_ago']:,.0f})" if cross.get("week_ago") else "(1주)"
        lines.append(f"  NZD/KRW    ₩{cross['nzd_to_krw']:,.2f}{format_change_chip(cross.get('change_pct'), suffix)}")

    # ── 거시 경고 지표 ────────────────────────────────────────────
    buffett = indicators.get("buffett", {})
    spread = indicators.get("credit_spread", {})
    yc = indicators.get("yield_curve", {})

    macro_lines = []
    if not buffett.get("error"):
        tag = _indicator_action("buffett", buffett.get("value"))
        macro_lines.append(f"  버핏지수      <b>{buffett['value']:.0f}%</b>  {buffett['level']}{tag}")
    if not spread.get("error"):
        tag = _indicator_action("credit_spread", spread.get("value"))
        macro_lines.append(f"  신용스프레드  {spread['value']}%  {spread['level']}{tag}")
    if not yc.get("error"):
        tag = _indicator_action("yield_curve", yc.get("value"))
        macro_lines.append(f"  장단기금리차  {yc['value']:+.2f}%  {yc['level']}{tag}")

    if macro_lines:
        lines.append("")
        lines.append("<b>🌍 거시 경고</b>")
        lines.extend(macro_lines)

    if mkt_score >= 6:
        mkt_label = "🟢 강한 매수 구간"
    elif mkt_score >= 3:
        mkt_label = "🟢 매수 우호적"
    elif mkt_score >= 1:
        mkt_label = "🟡 중립 (분할 매수 검토)"
    elif mkt_score >= -2:
        mkt_label = "⚪ 중립"
    elif mkt_score >= -4:
        mkt_label = "🟠 주의 (현금 비중 확대)"
    else:
        mkt_label = "🔴 위험 (방어적 포지션)"

    lines.append(f"\n  종합: <b>{mkt_label}</b>  (점수 {mkt_score:+d})")

    # ── Claude 섹션 (뉴스 해설 + 포트폴리오 점검) ─────────────────
    news_items = fetch_market_news()
    commentary = generate_news_commentary(news_items, mkt_score, mkt_reasons)
    if commentary:
        lines.append("\n" + "━" * 28)
        lines.append(commentary)

    accum_report = generate_accumulation_report(mkt_score, news_items)
    if accum_report:
        lines.append("\n" + "━" * 28)
        lines.append(accum_report)

    # ── [2] 종목별 판단 섹션 ──────────────────────────────────────
    lines.append("\n" + "━" * 28)
    lines.append("<b>🏦 종목별 판단</b>")

    buy_list, hold_list, cash_list, sell_list = [], [], [], []
    extreme_overheat_list = []

    for ticker in PORTFOLIO:
        result = judge_ticker(ticker, mkt_score)
        action = result["action"]
        emoji = result["emoji"]
        price = result["price"]
        drawdown = result["drawdown"]
        reasons = result["reasons"]
        rsi = result.get("rsi")
        w52 = result.get("w52")

        rsi_tag = ""
        if rsi is not None:
            if rsi >= 70:
                rsi_tag = f"  RSI {rsi}🔴"
            elif rsi <= 30:
                rsi_tag = f"  RSI {rsi}🟢"
            else:
                rsi_tag = f"  RSI {rsi}"

        w52_tag = f"  52주 {w52['pos_pct']:.0f}%" if w52 else ""

        line = f"{emoji} <b>{ticker}</b>  ${price:.2f}  ({drawdown:+.1f}%){rsi_tag}{w52_tag}"
        line += f"\n   → {action}"
        if reasons:
            line += f"  <i>{' · '.join(reasons[:2])}</i>"

        # 극단 과열 감지 (위험점수 7+ 상황에서만 표시)
        if risk_score >= 7 and ticker in holdings and holdings[ticker] > 0.01:
            eo = check_extreme_overheated(result)
            if eo:
                extreme_overheat_list.append(
                    f"{eo['emoji']} <b>{ticker}</b>  ${price:.2f}{rsi_tag}{w52_tag}"
                    f"\n   <i>{eo['reason']}</i>"
                )

        if "적극 매수" in action or ("매수" in action and "현금" not in action):
            buy_list.append(line)
        elif "현금" in action:
            cash_list.append(line)
        elif "매도" in action:
            sell_list.append(line)
        else:
            hold_list.append(line)

    if buy_list:
        lines.append("")
        lines.append("🟢 <b>매수 기회</b>")
        lines.append("")
        lines.extend(("\n" + l) for l in buy_list)
    if hold_list:
        lines.append("")
        lines.append("⚪ <b>홀딩</b>")
        lines.append("")
        lines.extend(("\n" + l) for l in hold_list)
    if cash_list:
        lines.append("")
        lines.append("🟠 <b>현금 비중 확대 검토</b>")
        lines.append("")
        lines.extend(("\n" + l) for l in cash_list)
    if sell_list:
        lines.append("")
        lines.append("🔴 <b>매도 고려</b>")
        lines.append("")
        lines.extend(("\n" + l) for l in sell_list)

    # ── 극단 과열 경보 (위험점수 7+ 일 때만) ─────────────────────
    if extreme_overheat_list:
        lines.append("\n" + "━" * 28)
        lines.append(
            "<b>⚠️ 극단 과열 경보</b>  "
            f"<i>(위험점수 {risk_score} — 일부 차익 검토 가능)</i>"
        )
        lines.extend(extreme_overheat_list)

    # ── [3] 현금 비중 + 위험점수 섹션 ────────────────────────────
    cash_section, available_cash, _total_portfolio = build_cash_section(
        holdings, idle_cash,
        _config.TARGET_CASH_RATIO, _config.CASH_TICKERS,
        risk_score=risk_score, risk_signals=risk_signals,
    )
    if cash_section:
        lines.append("\n" + "━" * 28)
        lines.append(cash_section)

    # ── [4] 레버리지 매수 가이드 ─────────────────────────────────
    lev_section = build_leverage_guide(holdings, idle_cash, total_portfolio=_total_portfolio)
    if lev_section:
        lines.append("\n" + "━" * 28)
        lines.append(lev_section)

    # ── [4-b] 매수 구간 ──────────────────────────────────────────
    buy_zone_section = build_buy_zones(holdings)
    if buy_zone_section:
        lines.append("\n" + "━" * 28)
        lines.append(buy_zone_section)

    # ── [5] 오늘의 동적 DCA 권장 금액 ─────────────────────────────
    # ── [6] 다가오는 이벤트 캘린더 ────────────────────────────────
    cal_section = build_calendar_section(holdings, days_ahead=14)
    if cal_section:
        lines.append("\n" + "━" * 28)
        lines.append(cal_section)

    # ── [7] 리밸런싱 알림 (드리프트 ±5%p 초과 시만) ──────────────
    drifts: list = []
    try:
        reb_state = calc_portfolio_state(holdings, idle_cash) if _ibkr_ok else None
        drifts = check_drifts(state=reb_state) or []
        if drifts:
            lines.append("\n" + "━" * 28)
            lines.append("<b>⚖️ 리밸런싱 알림</b>")
            for d in drifts:
                arrow = "🔴" if d["drift_pct"] > 0 else "🔵"
                tip = ""
                if d["drift_pct"] < 0 and d["preferred"]:
                    tip = f" → {', '.join(d['preferred'])} 위주"
                lines.append(
                    f"  {arrow} <b>{d['category']}</b>  "
                    f"{d['current_pct']:.1f}% / {d['target_pct']:.0f}% "
                    f"({d['drift_pct']:+.1f}%p){tip}"
                )
    except Exception as e:
        print(f"[rebalance] {e}")

    # ── [7-b] 현금 회복 매도 계획 (현금 15% 미만일 때만) ────────────
    if _ibkr_ok and _total_portfolio > 0:
        _cur_cash, _, _ = _calc_deployable_cash(holdings, idle_cash)
        restore_plan = build_cash_restore_plan(
            ibkr_positions=_ibkr["positions"],
            total_portfolio=_total_portfolio,
            current_cash=_cur_cash,
            target_cash_ratio=_config.TARGET_CASH_RATIO,
        )
        if restore_plan:
            lines.append("\n" + "━" * 28)
            lines.append(restore_plan)

    # ── [8] 예상 배당 섹션 ────────────────────────────────────────
    div_section = build_dividend_section(holdings, nzd_rate)
    if div_section:
        lines.append("\n" + "━" * 28)
        lines.append(div_section)

    # ── [9] 배당 일정 (배당락일 + 입금예정일) ─────────────────────
    div_sched = build_dividend_schedule_section(holdings, days_ahead=90)
    if div_sched:
        lines.append("\n" + "━" * 28)
        lines.append(div_sched)

    # ── [10] 오늘의 액션 플랜 ─────────────────────────────────────
    try:
        plan = build_action_plan(
            indicators=indicators,
            mkt_score=mkt_score,
            risk_score=risk_score,
            available_cash=available_cash,
            drifts=drifts,
            extreme_overheat=extreme_overheat_list,
            buy_count=len(buy_list),
        )
        lines.append("\n" + "━" * 28)
        lines.append(plan)
    except Exception as e:
        print(f"[action_plan] {e}")

    lines.append("\n" + "━" * 28)
    lines.append("🤖 <i>Stock Agent — 평일 미국 장 오픈 후 30분 자동 발송 (DST 자동 반영)</i>")

    return "\n".join(lines)


# ── 실행 ─────────────────────────────────────────────────────────

def should_skip_run() -> tuple[bool, str]:
    if os.getenv("FORCE_SEND") == "1":
        return False, "FORCE_SEND=1 (수동 실행)"
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return True, f"주말 ({now_et:%a %H:%M ET})"
    return False, f"실행 ({now_et:%H:%M ET})"


def run_once(test_mode: bool = False):
    if not test_mode:
        skip, reason = should_skip_run()
        if skip:
            print(f"[{datetime.now():%H:%M:%S}] 스킵: {reason}")
            return
        print(f"[{datetime.now():%H:%M:%S}] {reason}")

    print(f"[{datetime.now():%H:%M:%S}] 보고서 생성 중...")
    report = build_report()
    if test_mode:
        clean = re.sub(r"<[^>]+>", "", report)
        print(clean)
    else:
        ok = send_message(report)
        print(f"[{datetime.now():%H:%M:%S}] 텔레그램 전송 {'✅ 성공' if ok else '❌ 실패'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="텔레그램 없이 콘솔 출력")
    args = parser.parse_args()
    run_once(test_mode=args.test)
