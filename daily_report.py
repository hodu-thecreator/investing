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
from news_headlines import build_news_section
from rebalancing import check_drifts, calc_portfolio_state
import ibkr_flex

_config = Config()

ACCUMULATION_PORTFOLIO = _config.ACCUMULATION_PORTFOLIO

# ── 포트폴리오 설정 (헌법 5조: 코어 5종목) ───────────────────────
CORE_TICKERS = list(_config.CORE_ALLOCATION.keys())   # QQQM, SPYM, GLDM, IBIT, SGOV
LEGACY_TICKERS = set(_config.LEGACY_TICKERS)           # 청산 예정 비헌법 종목
PORTFOLIO = CORE_TICKERS
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
    """현금(SGOV) 목표 비중 — 헌법 5조 기준 20% 고정.
    위험 점수가 매우 높으면 소폭 상향(방어), 그 외엔 헌법값 유지."""
    base = Config.CORE_ALLOCATION["SGOV"]  # 0.20
    if risk_score >= 7:
        return max(base, 0.25)
    return base


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

    lines = ["<b>💵 매수 탄약 (SGOV + 현금)</b>"]
    lines.append(f"  현재  <b>${cash_value:,.0f}</b>  ({ratio*100:.1f}%)")
    lines.append(f"  목표  {target_label}")
    lines.append(f"  총자산 ${total_value:,.0f}")

    if abs(diff_pct) < 1.5:
        lines.append("  ✅ 탄약 적정 (목표 비중)")
    elif diff_pct > 0:
        lines.append(f"  💰 탄약 여유 +{diff_pct:.1f}%p (${diff_usd:,.0f}) — 조정 대기")
    else:
        # 부족분은 매도가 아니라 월 납입금으로 재충전 (헌법 7조: 매도 안 함)
        monthly_krw = 1_750_000   # 월 평균 납입 ₩175만
        monthly_usd = monthly_krw / 1350   # 대략 환율
        months = (-diff_usd) / monthly_usd if monthly_usd else 0
        lines.append(
            f"  🔋 탄약 {abs(diff_pct):.1f}%p 소진 (${-diff_usd:,.0f}) "
            f"— 월 납입으로 약 {months:.0f}개월 재충전"
        )

    if risk_signals:
        lines.append(f"  <i>위험 신호: {' · '.join(risk_signals[:3])}</i>")

    # 노는 돈 감지 — 배당이 USD로 쌓여만 있으면 안내 (DRIP 대신 웅덩이에 투입)
    if idle_cash >= _config.IDLE_CASH_ALERT_USD:
        lines.append(
            f"  💤 노는 USD <b>${idle_cash:,.0f}</b> — 웅덩이 열리면 1순위 투입 (저수지 섹션 참고)"
        )

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


# ── 헌법 6조: S&P500 ATH 기준 조정 트리거 ────────────────────────

def _sp500_drawdown_from_ath() -> dict | None:
    """S&P500(SPY)의 전고점(ATH) 대비 현재 낙폭."""
    try:
        df = fetch_stock_data("SPY", period="5y")
        if df.empty or len(df) < 2:
            return None
        close   = df["Close"].squeeze()
        current = float(close.iloc[-1])
        ath     = float(close.max())
        dd      = (current - ath) / ath * 100 if ath else 0.0
        return {"current": current, "ath": ath, "drawdown": dd}
    except Exception as e:
        print(f"[sp500_ath] {e}")
        return None


def _calc_rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _qqq_drawdown_from_high() -> dict | None:
    """QQQ 52주 고점 대비 현재 낙폭. MDD 진입 구간 판단용."""
    try:
        df = fetch_stock_data("QQQ", period="1y")
        if df.empty:
            return None
        close = df["Close"].squeeze()
        current = float(close.iloc[-1])
        high = float(close.max())
        dd = (current - high) / high * 100 if high else 0.0
        return {"current": current, "high": high, "drawdown": dd}
    except Exception as e:
        print(f"[qqq_dd] {e}")
        return None


def _calc_deployable_cash(holdings: dict[str, float], idle_cash: float) -> tuple[float, float, float]:
    """SGOV 탄약(시세×수량) + 달러잔고. (total, sgov_val, idle) 반환."""
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


def _lev_exposure(positions: dict, bucket: str | None = None) -> float:
    """레버리지 ETF 현재 평가액 합산. bucket 지정 시 해당 버킷 노출만.
    미지정 시 헌법 6조 조정 트리거 캡 기준(QQQM/SPYM)만 — SOXQ 위성 레버 제외."""
    total = 0.0
    for tk, b in _config.LEVERAGE_BUCKET.items():
        if bucket:
            if b != bucket:
                continue
        elif b not in ("QQQM", "SPYM"):
            continue
        pos = (positions or {}).get(tk)
        if pos:
            total += pos.get("mark_price", 0) * pos.get("qty", 0)
    return total


def build_correction_section(holdings: dict[str, float], idle_cash: float,
                             total_portfolio: float, positions: dict,
                             indicators: dict | None = None) -> str:
    """
    헌법 6조 — S&P500 ATH 대비 낙폭으로 조정 단계 판정 + 행동 제시.
      -5%  : SGOV 25% → 코어(QQQM/SPYM) 추가
      -10% : SGOV 50% + SSO 2x (총자산 2% 캡)
      -20% : SGOV 100% + UPRO/TQQQ 3x (총자산 5% 캡)
      -30% : 비상금 외 전액
    평시(-5% 미만 낙폭)엔 "자동투자만, 레버리지 금지" 안내.
    QQQ MDD 구간 + 구조적 하락 경고 병행 표시 (MDD 전략 참조).
    """
    sp = _sp500_drawdown_from_ath()
    if sp is None:
        return ""
    dd = sp["drawdown"]

    total_cash, sgov_val, idle = _calc_deployable_cash(holdings, idle_cash)
    triggers = _config.CORRECTION_TRIGGERS

    # 현재 도달 단계 (가장 깊은 것)
    active = None
    for t in triggers:
        if dd <= t["drop"]:
            active = t

    lines = [
        "<b>🎯 조정 대응 가이드</b>  "
        "<i>(시장이 전고점에서 얼마나 빠졌는지에 따라 얼마를·무엇을 살지 안내)</i>"
    ]
    lines.append(
        f"  S&P500  ${sp['current']:,.2f}  "
        f"(전고점 ${sp['ath']:,.2f} 대비 <b>{dd:+.1f}%</b>)"
    )
    ammo_str = f"${total_cash:,.0f}"
    if sgov_val > 0 and idle > 0:
        ammo_str += f"  <i>(SGOV ${sgov_val:,.0f} + 달러 ${idle:,.0f})</i>"
    lines.append(f"  💰 매수 탄약  <b>{ammo_str}</b>")

    # ── QQQ MDD 구간 표시 ─────────────────────────────────────────
    qqq = _qqq_drawdown_from_high()
    if qqq:
        qqq_dd = qqq["drawdown"]
        mdd = _config.MDD_REFERENCE
        if qqq_dd <= mdd["TQQQ"]["avg_mdd"]:
            zone_icon, zone_label, exp_ret = "🔴", "TQQQ 평균 MDD 구간 진입", mdd["TQQQ"]["entry_return"]
        elif qqq_dd <= mdd["QLD"]["avg_mdd"]:
            zone_icon, zone_label, exp_ret = "🟠", "QLD 평균 MDD 구간 진입", mdd["QLD"]["entry_return"]
        elif qqq_dd <= mdd["QQQ"]["avg_mdd"]:
            zone_icon, zone_label, exp_ret = "🟡", "QQQ 평균 MDD 구간 진입", mdd["QQQ"]["entry_return"]
        elif qqq_dd <= -15:
            zone_icon, zone_label, exp_ret = "🟡", "1차 분할 진입 구간 (-15~-20%)", None
        else:
            zone_icon, zone_label, exp_ret = "⚪", "관망 (-15% 미만)", None
        ret_str = f"  기대수익 <b>+{exp_ret:.0f}%</b>" if exp_ret else ""
        lines.append(
            f"  QQQ  ${qqq['current']:.2f}  "
            f"(52주 고점 대비 <b>{qqq_dd:+.1f}%</b>)  "
            f"{zone_icon} {zone_label}{ret_str}"
        )
        lines.append(
            f"  <i>MDD 평균 기준: QQQ -20.2% / QLD -30.3% / TQQQ -39.8%  (1999-2026)</i>"
        )
    else:
        qqq = None  # 명시적으로 None 처리

    # ── 지금 행동 ──
    lines.append("")
    if active is None:
        lines.append("  ✅ <b>평시</b> — 자동투자만 진행. 레버리지 매수 금지.")
        nxt = triggers[0]
        gap = nxt["drop"] - dd  # dd는 음수, nxt["drop"]도 음수
        lines.append(f"  📍 첫 트리거({nxt['drop']}%)까지  S&P <b>{gap:.1f}%</b> 추가 하락 시")
    else:
        fire_amt = total_cash * active["fire"]
        lines.append(f"  📍 <b>지금 행동</b>  (현재 {active['drop']}% 구간)")
        if active["action"] == "all-in":
            usable = max(0.0, total_cash - _config.EMERGENCY_FUND_USD)
            lines.append(
                f"  • 🔥 비상금(${_config.EMERGENCY_FUND_USD:,.0f}) 외 전액 <b>${usable:,.0f}</b> 발사"
            )
        else:
            lines.append(
                f"  • SGOV 탄약 {active['fire']*100:.0f}% = <b>${fire_amt:,.0f}</b> 발사"
            )
        # 코어 50:50 (QQQM/SPYM 둘 다 30% 닻)
        core_each = (max(0.0, total_cash - _config.EMERGENCY_FUND_USD) if active["action"] == "all-in" else fire_amt) / 2
        lines.append(f"     → <b>QQQM</b> ${core_each:,.0f}  +  <b>SPYM</b> ${core_each:,.0f}  (코어 50:50)")

        # 레버리지 (캡 적용)
        if active["lev"] and active["cap"] > 0:
            cap_usd = total_portfolio * active["cap"]
            cur_lev = _lev_exposure(positions)
            headroom = max(0.0, cap_usd - cur_lev)
            lev_names = "/".join(active["lev"])
            lines.append(
                f"  • 레버리지 <b>{lev_names}</b>  "
                f"(총자산 {active['cap']*100:.0f}% = ${cap_usd:,.0f} 캡)"
            )
            if headroom > 0:
                lines.append(f"     → 여력 <b>${headroom:,.0f}</b> (현재 레버 ${cur_lev:,.0f})")
            else:
                lines.append(f"     → ✅ 캡 도달 (현재 ${cur_lev:,.0f}) — 추가 금지")
        else:
            lines.append("  • 레버리지: 대기 (-10%부터)")

    # ── 단계별 표 ──
    lines.append("")
    lines.append("  <i>단계별 가이드</i>")
    tier_icons = {-5: "🟡", -10: "🟠", -20: "🔴", -30: "⚫"}
    tier_desc = {
        -5:  "SGOV 25% → 코어",
        -10: "SGOV 50% + SSO 2x (자산 2%캡)",
        -20: "SGOV 100% + UPRO/TQQQ 3x (자산 5%캡)",
        -30: "비상금 외 전액 발사",
    }
    for t in triggers:
        marker = "👉" if active and t["drop"] == active["drop"] else "  "
        icon = tier_icons.get(t["drop"], "•")
        lines.append(f"  {marker} {icon} {t['drop']}%  {tier_desc.get(t['drop'],'')}")

    # 다음 단계까지 거리
    if active is not None:
        deeper = [t for t in triggers if t["drop"] < active["drop"]]
        if deeper:
            nxt = deeper[0]
            gap = nxt["drop"] - dd
            lines.append("")
            lines.append(f"  📉 다음 단계({nxt['drop']}%)까지  S&P <b>{gap:.1f}%</b> 추가 하락 시")

    # ── 구조적 하락 경고 ──────────────────────────────────────────
    struct_warn: list[str] = []
    if qqq and qqq["drawdown"] <= -40:
        struct_warn.append("QQQ -40% 초과 — 역사적 구조적 하락 구간")
    if indicators:
        cs = (indicators.get("credit_spread") or {})
        if not cs.get("error") and (cs.get("value") or 0) >= 3.5:
            struct_warn.append(f"신용스프레드 {cs['value']}% — 유동성 경색 위험 (2008형)")
        yc = (indicators.get("yield_curve") or {})
        if not yc.get("error") and (yc.get("value") or 0) < 0:
            struct_warn.append(f"금리차 역전 {yc['value']:+.2f}% — 침체 경고")
    if struct_warn:
        lines.append("")
        lines.append("  ⚠️ <b>구조적 하락 경고</b>  — TQQQ 진입 자제")
        for w in struct_warn:
            lines.append(f"    • {w}")
        lines.append("    <i>이 신호 해소 전: TQQQ 금지, QQQ·SGOV 현금 우선</i>")

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

<b>🔄 전략적 교체 제안</b> (판단될 때만 작성, 없으면 섹션 자체 생략):
  - "지금은 QQQM보다 SCHG를 모으는 게 낫다"처럼, 같은 카테고리 안에서
    신규 자금(적립·배당)을 어디로 보내는 게 더 유리한지 판단되면 제안
    (SPYM 카테고리에서 위성 SPMO를 쓰는 것과 같은 방식 — 기존 보유분 매도 아님)
  - 티커·대상 카테고리·이유 한 줄

<b>👩‍💼 아내 개별주 후보</b> (판단될 때만 작성, 없으면 섹션 자체 생략):
  - 호두의 5대 카테고리(S&P500/나스닥100/금/비트코인/현금성) 중 하나에 해당하면서
    장기 보유 가치가 뛰어난 개별주가 시장에서 눈에 띌 때만 제안 (아내 별도 계좌용)
  - 디폴트는 "없음" — 뚜렷한 종목 없으면 섹션을 쓰지 않음
  - 티커·해당 카테고리·추천 근거 한 줄

한국어. 간결하게. 판단할 근거가 부족한 섹션은 제목까지 통째로 생략하세요."""

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
                f"가용현금 {size_pct}% (${amt:,.0f}) 코어(QQQM/SPYM) 분할 매수",
                f"조정 구간 코어 {buy_count}종목 — 아래 조정 가이드 참조",
            ],
        ))

    # 4) 거시 위험 (risk_score ≥ 7) — 방어 모드 (매도 없이 탄약 비축)
    if risk_score >= 7:
        actions.append((
            "⚠️", "거시 위험 — 탄약 비축 모드",
            [
                f"위험점수 {risk_score} — 침체/거품 신호",
                "신규 레버리지 금지, SGOV 탄약 비축",
            ],
        ))

    # 5) 리밸런싱 (±10%p 이상, 연 1회 점검)
    if drifts:
        d = drifts[0]
        actions.append((
            "⚖️", "리밸런싱 점검 (연 1회)",
            [
                f"{d['category']} 드리프트 {d['drift_pct']:+.1f}%p (±10%p 초과)",
                "자동투자 비율 조정으로 자연 복원 우선",
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


# ── 마일스톤 진행률 (헌법 2·3조) ─────────────────────────────────

def build_milestone_section(total_portfolio: float) -> str:
    """자산 마일스톤 진행률 — 남과 비교 대신 목표까지의 거리."""
    if total_portfolio <= 0:
        return ""
    milestones = _config.MILESTONES
    # 다음 목표 찾기
    nxt = next((m for m in milestones if total_portfolio < m[0]), None)
    achieved = [m for m in milestones if total_portfolio >= m[0]]

    lines = ["<b>🧭 자유로 가는 길</b>  <i>(인생 목표 = 쉬고 싶을 때 쉬기)</i>"]
    lines.append(f"  현재 자산  <b>${total_portfolio:,.0f}</b>")

    if nxt is None:
        lines.append("  🎉 모든 마일스톤 달성 — $2M 돌파!")
        return "\n".join(lines)

    target, desc = nxt
    prev = achieved[-1][0] if achieved else 0
    span = target - prev
    progress = (total_portfolio - prev) / span if span > 0 else 0
    filled = int(round(progress * 10))
    bar = "▓" * filled + "░" * (10 - filled)
    remaining = target - total_portfolio

    lines.append(f"  {bar}  {progress*100:.0f}%")
    lines.append(f"  다음: <b>${target:,.0f}</b> ({desc})")
    lines.append(f"  남은 거리  <b>${remaining:,.0f}</b>")

    # 자유 시작점 ($500K) 강조
    FREEDOM = 500_000
    if total_portfolio < FREEDOM:
        lines.append(f"  ★ 자유 시작점 $500K까지  <b>${FREEDOM - total_portfolio:,.0f}</b>")

    if achieved:
        lines.append(f"  <i>달성: {' · '.join(f'${m[0]//1000}K' for m in achieved)}</i>")
    lines.append("  <i>비교는 5년 전 호두와만. SNS 자산 자랑 무시.</i>")
    return "\n".join(lines)


# ── 한국 양도세 공제 추적 (헌법 9조, 한국 phase 한정) ─────────────

def build_kr_tax_section(usd_krw: float, ibkr: dict | None = None) -> str:
    """
    한국 phase(2026.5~2027.11) 양도세 250만원 연 공제 활용 추적.
    유일하게 허용되는 매도(전략적 부분 매도+즉시 재매수로 평단 스텝업).
    """
    today = datetime.now()
    # 한국 phase 종료(2027-11) 이후엔 표시 안 함
    phase_end = datetime.strptime(_config.KR_PHASE_END, "%Y-%m")
    if today >= phase_end:
        return ""
    if not usd_krw or usd_krw <= 0:
        return ""

    from tax_korea import realized_ytd_usd
    realized_usd = realized_ytd_usd(ibkr)

    realized_krw = max(realized_usd * usd_krw, _config.KR_CGT_REALIZED_KRW_OVERRIDE)
    deduction = _config.KR_CGT_DEDUCTION_KRW
    headroom_krw = deduction - realized_krw
    headroom_usd = headroom_krw / usd_krw if usd_krw else 0

    lines = ["<b>🇰🇷 한국 양도세 공제 활용</b>  <i>(연 250만원 비과세)</i>"]
    lines.append(f"  올해 실현 차익  ₩{realized_krw:,.0f}  (${realized_usd:,.0f})")
    if headroom_krw > 0:
        lines.append(f"  남은 공제 여력  <b>₩{headroom_krw:,.0f}</b>  (≈ ${headroom_usd:,.0f})")
        lines.append(
            "  <i>전략: 공제 한도만큼 부분 매도 → 즉시 재매수로 평단 스텝업 "
            "(세금 0, 미래 양도세↓)</i>"
        )
    else:
        lines.append("  ✅ 올해 공제 한도 소진 — 추가 실현 매도 보류")
    return "\n".join(lines)


# ── 레버리지 익절 → 탄약 재장전 (헌법 예외: 레버리지는 임시 포지션) ──────

def build_leverage_harvest_plan(
    holdings: dict[str, float],
    idle_cash: float,
    total_portfolio: float,
    positions: dict,
) -> str:
    """
    ATH 근처에서 레버리지 ETF 부분 익절 → SGOV 탄약 재장전.

    트리거 3가지 모두 충족 시만 표시:
      1. S&P500 ATH 대비 낙폭 -3% 이내 (레버리지 고점 타이밍)
      2. SGOV/현금 비중이 목표(20%) 미달 — 탄약 부족
      3. 보유 레버리지 ETF 중 미실현 수익 ≥ 15% 인 것 존재
    코어(QQQM/SPYM/GLDM/IBIT)는 대상 제외 — 절대 매도 안 함.
    """
    if total_portfolio <= 0:
        return ""

    # Trigger 1: S&P near ATH
    sp = _sp500_drawdown_from_ath()
    if sp is None:
        return ""
    dd = sp["drawdown"]
    if dd < -5.0:
        return ""  # 조정 중 — 레버리지 익절 타이밍 아님

    # Trigger 2: 현금 비중 부족
    target_ratio = _config.CORE_ALLOCATION["SGOV"]  # 0.20
    cash_value = idle_cash
    for t, q in holdings.items():
        if not q or q <= 0:
            continue
        if t in _config.CASH_TICKERS:
            try:
                df = fetch_stock_data(t, period="5d")
                if not df.empty:
                    cash_value += float(df["Close"].squeeze().iloc[-1]) * q
            except Exception:
                pass

    target_cash_usd = total_portfolio * target_ratio
    cash_shortage = target_cash_usd - cash_value
    if cash_shortage < 200:
        return ""  # 탄약 충분 — 익절 불필요

    # Trigger 3: 레버리지 ETF 수익 확인
    # IBKR positions 없으면 transactions 모듈로 폴백
    try:
        from transactions import portfolio_summary
        tx_summary = portfolio_summary()
    except Exception:
        tx_summary = {}

    lev_tickers = list(_config.LEVERAGE_BUCKET.keys())  # QLD, TQQQ, SSO, UPRO, SOXL, USD
    candidates: list[dict] = []

    for t in lev_tickers:
        qty = holdings.get(t, 0) or 0
        if qty < 0.01:
            continue

        pos = (positions or {}).get(t)
        cur_price: float | None = None
        gain_pct: float | None = None
        unrealized: float | None = None

        if pos and pos.get("mark_price"):
            cur_price = float(pos["mark_price"])
            # cost_basis는 IBKR Flex가 이미 1주당 단가로 제공함 (qty로 다시 나누면 안 됨)
            cost_per_share = float(pos["cost_basis"]) if pos.get("cost_basis") else None
            if cost_per_share and cost_per_share > 0:
                gain_pct = (cur_price - cost_per_share) / cost_per_share * 100
                unrealized = pos.get("unrealized_pnl")
                if unrealized is None:
                    unrealized = (cur_price - cost_per_share) * qty

        if cur_price is None:
            tx = tx_summary.get(t, {})
            if tx.get("current_price"):
                cur_price = float(tx["current_price"])
                if tx.get("avg_price") and tx["avg_price"] > 0:
                    gain_pct = (cur_price - tx["avg_price"]) / tx["avg_price"] * 100
                    unrealized = tx.get("unrealized", None)

        if cur_price is None:
            try:
                df = fetch_stock_data(t, period="5d")
                if not df.empty:
                    cur_price = float(df["Close"].squeeze().iloc[-1])
            except Exception:
                continue

        if cur_price is None:
            continue

        if gain_pct is not None and gain_pct < 15:
            continue  # 수익 부족 — 익절 효과 미미

        # RSI — 과열 신호 없이 수익률만으로 익절을 권하면 상승 모멘텀을 너무 일찍 끊을 수 있음
        rsi = None
        try:
            rsi_df = fetch_stock_data(t, period="6mo")
            if not rsi_df.empty:
                rsi = calc_rsi(rsi_df)
        except Exception:
            pass

        candidates.append({
            "ticker": t,
            "qty": qty,
            "price": cur_price,
            "value": cur_price * qty,
            "gain_pct": gain_pct,
            "unrealized": unrealized,
            "bucket": _config.LEVERAGE_BUCKET[t],
            "rsi": rsi,
        })

    if not candidates:
        return ""

    candidates.sort(key=lambda x: (x["gain_pct"] or 0), reverse=True)

    lines = ["<b>🔋 레버리지 익절 → 탄약 재장전</b>  <i>(월 납입 대체)</i>"]
    lines.append(
        "  <i>현금(SGOV)이 목표치보다 부족한데, 보유한 레버리지 ETF가 많이 올라있어 "
        "일부만 팔아 현금을 채우자는 제안입니다.</i>"
    )
    lines.append(
        f"  S&P500  전고점 대비 <b>{dd:+.1f}%</b>  "
        f"← 시장이 고점 근처라 레버리지도 익절하기 좋은 시점"
    )
    cash_ratio = cash_value / total_portfolio
    lines.append(
        f"  현금(SGOV)  {cash_ratio*100:.1f}% / 목표 {target_ratio*100:.0f}%  "
        f"— 탄약 <b>${cash_shortage:,.0f}</b> 부족"
    )
    lines.append("")

    # 익절 플랜 계산 (수익률 높은 순, 최대 50% 매도)
    remaining = cash_shortage
    sell_plans: list[dict] = []
    for c in candidates:
        if remaining <= 50:
            break
        sell_value = min(c["value"] * 0.5, remaining)
        sell_qty = sell_value / c["price"]
        sell_pct = sell_qty / c["qty"] * 100
        sell_plans.append({**c, "sell_qty": sell_qty,
                           "sell_value": sell_value, "sell_pct": sell_pct})
        remaining -= sell_value

    lines.append("<b>📋 제안 매도 플랜 (SGOV로 전환)</b>")
    total_harvest = 0.0
    for p in sell_plans:
        gain_tag = (
            f"  <i>(수익 {p['gain_pct']:+.0f}%)</i>"
            if p["gain_pct"] is not None else ""
        )
        lines.append(
            f"  • <b>{p['ticker']}</b>  {p['sell_qty']:.2f}주 매도  "
            f"≈ <b>${p['sell_value']:,.0f}</b>  ({p['sell_pct']:.0f}% 부분 익절){gain_tag}"
        )
        total_harvest += p["sell_value"]

    new_cash = cash_value + total_harvest
    new_ratio = new_cash / total_portfolio
    lines.append(f"\n  📥 SGOV 매수  +<b>${total_harvest:,.0f}</b>")
    lines.append(
        f"  재장전 후 현금  {new_ratio*100:.1f}%  "
        f"({'✅ 목표 달성' if new_ratio >= target_ratio * 0.95 else f'목표 {target_ratio*100:.0f}% 미달'})"
    )
    if remaining > 100:
        lines.append(
            f"  <i>⚠️ 전액 회복 불가 (${remaining:,.0f} 잔여 부족 — 이후 납입금으로 보완)</i>"
        )

    # ── MDD 기반 분할 익절 타겟 ──────────────────────────────────
    lines.append("")
    lines.append(
        "<b>📤 수익률 기반 분할 익절 타겟</b>  "
        "<i>(미리 정해둔 목표 수익률 도달 시 일부 청산 — 참고용 알림)</i>"
    )
    targets = _config.LEV_HARVEST_TARGETS  # [(30, desc), (50, desc), (100, desc)]
    any_caution = False
    for gain_tgt, desc in targets:
        # 현재 해당 구간에 있는 레버리지 ETF 찾기 — RSI 과열(≥70) 여부로 긴급도 구분
        confirmed, caution = [], []
        for c in candidates:
            if c.get("gain_pct") is None or c["gain_pct"] < gain_tgt * 0.85:  # 85% 도달 시 준비
                continue
            if c.get("rsi") is not None and c["rsi"] < 70:
                caution.append(c["ticker"])
            else:
                confirmed.append(c["ticker"])

        if confirmed:
            marker, zone_tag = "👉", f"  ← <b>{', '.join(confirmed)}</b> 구간 도달"
        elif caution:
            marker, zone_tag = "💡", f"  ← <b>{', '.join(caution)}</b> 구간 도달 (RSI 과열 아님 — 급매 불필요)"
            any_caution = True
        else:
            marker, zone_tag = "  ", ""
        lines.append(f"  {marker} +{gain_tgt}%  {desc}{zone_tag}")
    if any_caution:
        lines.append(
            "  <i>💡 표시는 목표 수익률엔 도달했지만 RSI가 과열 구간이 아닌 경우 — "
            "상승 모멘텀이 살아있다면 더 들고 가도 됨, 강제 매도 신호 아님.</i>"
        )
    lines.append(
        "  <i>TQQQ는 단기·중기 반등 전략 — 장기 보유 시 레버리지 비용 누적으로 원금 잠식</i>"
    )

    lines.append("")
    lines.append("  <i>레버리지는 조정 시 임시 포지션 — 전고점 근처 익절 가능 (코어 제외).</i>")
    return "\n".join(lines)


def _watch_action_text(zone_idx: int) -> str:
    """개별주 워치(아내 별도 계좌)용 액션 한 줄 — '탄약 투입' 표현 대신 매수 비중 가이드.

    이 계좌는 공유 SGOV 탄약 풀이 없으므로 reservoir.zone_action()의
    "탄약 N% 투입" 문구를 그대로 쓰면 오해를 부름 — 매수 비중 가이드로 재해석.
    """
    if zone_idx <= 0:
        return "평소대로 매수"
    if zone_idx == 1:
        return "이번 매수 시점을 앞당기기"
    import reservoir
    fire = reservoir.zone_fire(zone_idx)
    return f"평소보다 {fire*100:.0f}% 더 많이 매수 고려"


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

    # 데이터 소스 표시 — IBKR 실패가 조용히 폴백으로 숨지 않도록 항상 명시
    if _ibkr_ok:
        lines.append(
            f"<i>📡 IBKR 실계좌 {len(_ibkr['positions'])}종목"
            f" · 현금 ${_ibkr['cash_usd']:,.2f}</i>"
        )
    else:
        reason = _ibkr["error"] or "응답은 정상이나 포지션 0건 (Flex Query에 Open Positions 섹션 누락 의심)"
        reason = reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"<i>⚠️ IBKR 조회 실패 → config 폴백 사용 중</i>")
        lines.append(f"<i>   사유: {reason}</i>")

    # ── [0] 오늘 할 일 — 헌법 6조 트리거 판정 (장중 알림과 동일 로직) ──
    try:
        from intraday_alert import _ath_trigger_status, _decide_action
        ath = _ath_trigger_status()
        headline, detail = _decide_action(ath, holdings, idle_cash)
        if headline:
            lines.append(f"\n👉 <b>오늘 할 일: {headline}</b>")
            lines.append(f"<i>{detail}</i>")
    except Exception as e:
        print(f"[now_headline] {e}")

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

    # ── [1-b] 전체 보유 종목 (IBKR 실계좌 — 보유 중인 모든 티커) ──────
    if _ibkr_ok:
        try:
            account_section = ibkr_flex.build_account_section(
                _ibkr["positions"], _ibkr["cash_usd"]
            )
            if account_section:
                lines.append("\n" + "━" * 28)
                lines.append(account_section)
        except Exception as e:
            print(f"[account_section] {e}")

    # ── [2] 코어 5종목 현황 (헌법 5조 — 매도 판단 없음, 보유 전제) ──
    lines.append("\n" + "━" * 28)
    lines.append("<b>🏦 코어 5종목 현황</b>  <i>(헌법 5조 — 5종목만)</i>")
    lines.append("")

    buy_count = 0
    judged: dict[str, dict] = {}

    for ticker in CORE_TICKERS:
        target_pct = _config.CORE_ALLOCATION.get(ticker, 0) * 100
        result = judge_ticker(ticker, mkt_score)
        judged[ticker] = result
        price = result["price"]
        drawdown = result["drawdown"]
        rsi = result.get("rsi")
        w52 = result.get("w52")

        if not price:
            lines.append(f"  ⚪ <b>{ticker}</b>  (목표 {target_pct:.0f}%)  데이터 없음")
            continue

        rsi_tag = ""
        if rsi is not None:
            mark = "🔴" if rsi >= 70 else ("🟢" if rsi <= 30 else "")
            rsi_tag = f"  RSI {rsi}{mark}"
        w52_tag = f"  52주 {w52['pos_pct']:.0f}%" if w52 else ""

        # 코어는 매수/매도 판단 대신 현황만. 낙폭 크면 조정 매수 후보 카운트.
        if drawdown <= -5:
            zone = "🟢 조정 매수 구간"
            buy_count += 1
        elif drawdown <= -2:
            zone = "🟡 소폭 조정"
        else:
            zone = "⚪ 전고점 근처"

        lines.append(
            f"  <b>{ticker}</b>  (목표 {target_pct:.0f}%)  ${price:.2f}  "
            f"({drawdown:+.1f}%){rsi_tag}{w52_tag}\n   → {zone}"
        )

    # 미취득 코어 종목 — IBKR 실제 holdings 기준으로 자동 탐지
    unacquired = [t for t in CORE_TICKERS if (holdings.get(t) or 0) < 0.001]
    if unacquired:
        lines.append("")
        lines.append("  <b>🎯 미취득 코어 (우선 매수 대상)</b>")
        for t in unacquired:
            tgt_pct = _config.CORE_ALLOCATION.get(t, 0) * 100
            r = judge_ticker(t, mkt_score)
            p = r.get("price") or 0
            dd_str = f"  ({r['drawdown']:+.1f}%)" if r.get("drawdown") else ""
            lines.append(f"  ⬜ <b>{t}</b>  (목표 {tgt_pct:.0f}%)  현재 ${p:.2f}{dd_str}  → 첫 매수 대기")

    # ── [2-b] 레거시 보유 종목 (청산 예정 — 세금 룰 따라) ────────────
    legacy_held = [(t, q) for t, q in holdings.items()
                   if q and q > 0.01 and t in LEGACY_TICKERS]
    if legacy_held:
        lines.append("")
        lines.append("<b>🗂 레거시 보유 (정리 예정)</b>")
        for t, q in sorted(legacy_held):
            r = judge_ticker(t, mkt_score)
            p = r.get("price") or 0
            lines.append(f"  • <b>{t}</b>  {q:g}주  ${p:.2f}")
        lines.append("  <i>신규 매수 금지. 세금 룰(한국 양도세 공제·NZ 면세기)에 맞춰 정리.</i>")

    # ── [2-b2] 새 종목 매수 감지 — 어느 그룹에도 없는 보유분 ──────────
    # 호두가 새 위성을 실제로 매수하면: 알려주고 비중/전략 재설계를 요청하게 안내
    known = (set(_config.CORE_ALLOCATION) | set(_config.SATELLITE_TICKERS)
             | set(_config.LEVERAGE_BUCKET) | LEGACY_TICKERS
             | set(_config.CASH_TICKERS)
             | set(getattr(_config, "EQUIVALENT_TICKERS", {})))
    new_held = [(t, q) for t, q in holdings.items()
                if q and q > 0.01 and t not in known]
    if new_held:
        lines.append("")
        lines.append("<b>🆕 새 종목 매수 감지</b>")
        for t, q in sorted(new_held):
            lines.append(f"  • <b>{t}</b>  {q:g}주")
        lines.append("  <i>아직 목표 비중에 없는 종목 — 비중·전략 재설계가 필요함.")
        lines.append("  봇 세션에서 \"새 종목 반영해줘\"라고 요청하면 전체 파이를 다시 짬.</i>")

    # ── [2-c] 개별주 워치 (정보용 — 이 계좌는 매수 안 함, 헌법 4조) ──
    watchlist = getattr(_config, "INDIVIDUAL_WATCHLIST", [])
    if watchlist:
        try:
            import reservoir
            lines.append("\n" + "━" * 28)
            lines.append("<b>👀 개별주 워치</b>  <i>(정보용 — 이 계좌는 매수 안 함)</i>")
            for t in watchlist:
                r = judge_ticker(t, mkt_score)
                price = r.get("price")
                if not price:
                    lines.append(f"  ⚪ <b>{t}</b>  데이터 없음")
                    continue
                drawdown = r.get("drawdown")
                rsi = r.get("rsi")
                rsi_tag = ""
                if rsi is not None:
                    mark = "🔴" if rsi >= 70 else ("🟢" if rsi <= 30 else "")
                    rsi_tag = f"  RSI {rsi}{mark}"
                idx, zone = reservoir.classify(drawdown if drawdown is not None else 0)
                action_tag = f"  → {reservoir.zone_label(idx)} · {_watch_action_text(idx)}" if zone else ""
                lines.append(f"  <b>{t}</b>  ${price:.2f}  ({drawdown:+.1f}%){rsi_tag}{action_tag}")
        except Exception as e:
            print(f"[watchlist] {e}")

    # ── [3] 현금 비중 + 위험점수 섹션 ────────────────────────────
    cash_section, available_cash, _total_portfolio = build_cash_section(
        holdings, idle_cash,
        _config.TARGET_CASH_RATIO, _config.CASH_TICKERS,
        risk_score=risk_score, risk_signals=risk_signals,
    )
    if cash_section:
        lines.append("\n" + "━" * 28)
        lines.append(cash_section)

    # ── [3-b] 레버리지 익절 → 탄약 재장전 (ATH 근처 + 현금 부족 시만) ──
    try:
        harvest_section = build_leverage_harvest_plan(
            holdings, idle_cash, _total_portfolio,
            _ibkr["positions"] if _ibkr_ok else {},
        )
        if harvest_section:
            lines.append("\n" + "━" * 28)
            lines.append(harvest_section)
    except Exception as e:
        print(f"[harvest] {e}")

    # ── [3-c] 코어 과열 부분 익절 (헌법 7조 예외, 2026.6 신설) ────
    try:
        import core_trim
        sp_dd = _sp500_drawdown_from_ath()
        sp_drawdown = sp_dd["drawdown"] if sp_dd else None
        cash_ratio = (available_cash / _total_portfolio) if _total_portfolio else 0
        target_cash_ratio = _config.CORE_ALLOCATION["SGOV"]
        trim_section = core_trim.build_core_trim_section(
            sp_drawdown, cash_ratio, target_cash_ratio, judged, holdings,
        )
        if trim_section:
            lines.append("\n" + "━" * 28)
            lines.append(trim_section)
    except Exception as e:
        print(f"[core_trim] {e}")

    # ── [4] 조정 대응 가이드 (헌법 6조: S&P500 ATH 트리거) ────────
    correction_section = build_correction_section(
        holdings, idle_cash, _total_portfolio,
        _ibkr["positions"] if _ibkr_ok else {},
        indicators=indicators,
    )
    if correction_section:
        lines.append("\n" + "━" * 28)
        lines.append(correction_section)

    # ── [4-b] 저수지 수위 (종목별 52주 고점 낙폭 — 어디에 쏠지) ────
    try:
        import reservoir
        res_state = calc_portfolio_state(holdings, idle_cash) if _ibkr_ok else None
        res_section = reservoir.build_reservoir_section(res_state)
        if res_section:
            lines.append("\n" + "━" * 28)
            lines.append(res_section)
    except Exception as e:
        print(f"[reservoir] {e}")

    # ── [6] 다가오는 이벤트 캘린더 ────────────────────────────────
    cal_section = build_calendar_section(holdings, days_ahead=14)
    if cal_section:
        lines.append("\n" + "━" * 28)
        lines.append(cal_section)

    # ── [6.5] 보유 종목 뉴스 헤드라인 (클릭 가능한 링크 포함) ──────
    try:
        news_section = build_news_section(holdings, top_n=3, max_age_hours=48)
        if news_section:
            lines.append("\n" + "━" * 28)
            lines.append(news_section)
    except Exception as e:
        print(f"[news] {e}")

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

    # ── [8] 예상 배당 섹션 ────────────────────────────────────────
    div_section = build_dividend_section(holdings, nzd_rate)
    if div_section:
        lines.append("\n" + "━" * 28)
        lines.append(div_section)

    # ── [8-b] 이번 달 재투자 배분 (배당 + 신규 납입 → 언더웨이트 수렴) ──
    try:
        from action_plan import estimate_monthly_dividend_usd, split_deposit, usd_krw_rate, sgov_buy_note
        div_usd = estimate_monthly_dividend_usd(holdings)
        deposit_krw = _config.MONTHLY_DEPOSIT_KRW
        deposit_usd = div_usd + (deposit_krw / usd_krw_rate() if deposit_krw > 0 else 0)
        if deposit_usd >= 1:
            alloc_state = calc_portfolio_state(holdings, idle_cash)
            plan = split_deposit(alloc_state, deposit_usd)
            if plan:
                src = []
                if div_usd >= 0.5:
                    src.append(f"배당 ${div_usd:,.0f}")
                if deposit_krw > 0:
                    src.append(f"납입 ₩{deposit_krw:,.0f}")
                lines.append("\n" + "━" * 28)
                lines.append(f"<b>💰 이번 달 재투자 배분</b>  {' + '.join(src)}  ≈ ${deposit_usd:,.0f}")
                for ticker, amt, cat in plan:
                    cur = alloc_state["categories"][cat]["current_pct"] * 100
                    tgt = alloc_state["categories"][cat]["target_pct"] * 100
                    gap_note = f"{cur:.0f}%→{tgt:.0f}%" if cur < tgt - 0.5 else "비중 유지"
                    lines.append(f"  <b>{ticker}</b>  ${amt:,.0f}  <i>{gap_note}</i>")
                    if ticker == "SGOV":
                        note = sgov_buy_note()
                        if note:
                            lines.append(note)
                lines.append("  <i>→ 언더웨이트부터 채워 목표 비중으로 수렴</i>")
    except Exception as e:
        print(f"[reinvest_plan] {e}")

    # ── [8-c] 배당 입금 감지 (IBKR 실제 입금 → 즉시 재투자 지시) ───
    if _ibkr_ok and _ibkr.get("dividends"):
        try:
            import dividend_tracker
            new_divs = dividend_tracker.find_new_dividends(_ibkr["dividends"])
            if new_divs:
                div_state = calc_portfolio_state(holdings, idle_cash)
                div_alert = dividend_tracker.build_dividend_alert_section(div_state, new_divs)
                if div_alert:
                    lines.append("\n" + "━" * 28)
                    lines.append(div_alert)
        except Exception as e:
            print(f"[dividend_tracker] {e}")

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
            buy_count=buy_count,
        )
        lines.append("\n" + "━" * 28)
        lines.append(plan)
    except Exception as e:
        print(f"[action_plan] {e}")

    # ── [11] 한국 양도세 공제 추적 (한국 phase 한정) ──────────────
    try:
        krw_rate = (indicators.get("usd_krw", {}) or {}).get("usd_to_krw", 0)
        tax_section = build_kr_tax_section(krw_rate, _ibkr)
        if tax_section:
            lines.append("\n" + "━" * 28)
            lines.append(tax_section)
    except Exception as e:
        print(f"[kr_tax] {e}")

    # ── [11-b] 자산 추이 (1주/1개월/1년 전 대비, ATH 워터마크) ────
    try:
        import asset_history
        history = asset_history.record_snapshot(_total_portfolio)
        hist_section = asset_history.build_asset_history_section(_total_portfolio, history)
        if hist_section:
            lines.append("\n" + "━" * 28)
            lines.append(hist_section)
    except Exception as e:
        print(f"[asset_history] {e}")

    # ── [12] 마일스톤 진행률 (동기 부여 — 클로저) ─────────────────
    try:
        ms_section = build_milestone_section(_total_portfolio)
        if ms_section:
            lines.append("\n" + "━" * 28)
            lines.append(ms_section)
    except Exception as e:
        print(f"[milestone] {e}")

    # ── [13] 거주국 로드맵 (헌법 1·9조 — phase 전환 D-day + 레거시 정리) ──
    try:
        import residency_roadmap
        roadmap_state = calc_portfolio_state(holdings, idle_cash) if _total_portfolio > 0 else None
        roadmap_section = residency_roadmap.build_roadmap_section(roadmap_state)
        if roadmap_section:
            lines.append("\n" + "━" * 28)
            lines.append(roadmap_section)
    except Exception as e:
        print(f"[residency_roadmap] {e}")

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
