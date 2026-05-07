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
from market_indicators import collect_all
from telegram_notifier import send_message
from config import Config
from dca_calculator import build_dca_section
from events import build_calendar_section
from news_headlines import build_news_section

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
                       risk_score: int = 0, risk_signals: list[str] = None) -> tuple[str, float]:
    """현재 현금 비중 vs 목표 비중 추적. (section_text, available_cash) 반환."""
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
        return "", available_cash

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
        needed = -diff_usd
        lines.append(f"  ⚠️ 목표 {abs(diff_pct):.1f}%p 부족")
        lines.append(f"  → 신규 적립금 <b>${needed:.0f}</b>를 SGOV에 배분 권장")

    if risk_signals:
        lines.append(f"  <i>위험 신호: {' · '.join(risk_signals[:3])}</i>")

    return "\n".join(lines), available_cash


def build_dividend_section(holdings: dict[str, float], nzd_rate: float = 0) -> str:
    """보유 주수 기반 이번 달 예상 배당금 계산"""
    rows = []
    total_annual = 0.0

    for ticker, shares in holdings.items():
        if shares < 0.01:
            continue
        try:
            info = yf.Ticker(ticker).info
            price = info.get("regularMarketPrice") or info.get("previousClose", 0)
            div_yield = info.get("dividendYield") or 0
            if div_yield and price and shares:
                annual = price * shares * div_yield
                total_annual += annual
                monthly = annual / 12
                if monthly >= 0.5:
                    rows.append((ticker, monthly, div_yield * 100))
        except Exception:
            pass

    if not rows:
        return ""

    rows.sort(key=lambda x: x[1], reverse=True)
    lines = ["<b>💰 예상 배당 (이번 달)</b>"]
    for ticker, monthly, yld in rows:
        lines.append(f"  <b>{ticker}</b>  ${monthly:.0f}  <i>({yld:.1f}%/yr)</i>")

    monthly_total = total_annual / 12
    nzd_str = f"  ≈  NZD {monthly_total * nzd_rate:.0f}" if nzd_rate else ""
    lines.append(f"  ─────────────────────")
    lines.append(f"  <b>합계  ${monthly_total:.0f}/월{nzd_str}</b>")
    lines.append(f"  <i>(연 ${total_annual:.0f}{f'  ≈  NZD {total_annual * nzd_rate:.0f}' if nzd_rate else ''})</i>")
    return "\n".join(lines)


# ── 레버리지 매수 가이드 ──────────────────────────────────────────

# 본주 → 레버리지 ETF 매핑 (2x / 3x)
_LEVERAGE_MAP = {
    "SPYM": {"2x": "SSO",  "3x": "UPRO", "name": "S&P500"},
    "QQQM": {"2x": "QLD",  "3x": "TQQQ", "name": "나스닥100"},
    "SOXQ": {"2x": None,   "3x": "SOXL", "name": "반도체"},
}


def _calc_entry_timing(close: pd.Series) -> dict:
    """
    매수 타이밍 분석 — 모멘텀(5일 수익률) + RSI + 5일선 위치.
    소수점 매수가 가능하므로 분할 진입을 권장하고,
    하락 가속 구간에는 사이즈를 줄여 다음 신호를 기다리게 한다.

    multiplier: 권장 금액에 곱하는 계수 (0.3 ~ 1.3)
    """
    if len(close) < 14:
        return {"phase": "unknown", "label": "데이터 부족", "multiplier": 1.0}

    current = float(close.iloc[-1])
    ma5 = float(close.rolling(5).mean().iloc[-1])
    ret_5d = (current - float(close.iloc[-6])) / float(close.iloc[-6]) * 100 if len(close) >= 6 else 0
    ret_1d = (current - float(close.iloc[-2])) / float(close.iloc[-2]) * 100 if len(close) >= 2 else 0

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])

    above_ma5 = current > ma5

    # 1) 과매도 반등: RSI 30 이하 → 적극 진입
    if rsi <= 30:
        return {
            "phase": "oversold",
            "advice": f"🟢 적극 진입 (RSI {rsi:.0f} 과매도)",
            "multiplier": 1.3,
        }
    # 2) 반등 시작: 5일 양전환 + 5일선 상회
    if ret_5d > 0.5 and above_ma5:
        return {
            "phase": "rebound",
            "advice": f"🟢 반등 신호 — 지금 진입",
            "multiplier": 1.3,
        }
    # 3) 하락 가속: 5일 -3%↓ + 5일선 하회 + RSI 35↑ (아직 과매도 전)
    if ret_5d <= -3 and not above_ma5 and rsi > 35:
        return {
            "phase": "falling",
            "advice": f"⏸ 하락 진행 중 — 대기 (RSI {rsi:.0f})",
            "multiplier": 0.3,
        }
    # 4) 안정화 — 그 외
    return {
        "phase": "stabilizing",
        "advice": "🟡 분할 매수 시작",
        "multiplier": 1.0,
    }


def build_leverage_guide(available_cash: float) -> str:
    """
    SPYM/QQQM/SOXQ의 60일 고점 대비 낙폭 + 진입 타이밍을 분석해
    2x/3x 레버리지 매수 시점과 권장 금액을 제시.

    포지션 사이징 (분할 매수 친화적):
      -5~-10%  → 2x ETF, 가용현금의 1.5%  × 타이밍 계수
      -10~-15% → 3x ETF, 가용현금의 3%    × 타이밍 계수
      -15%+    → 3x ETF, 가용현금의 5%    × 타이밍 계수

    같은 신호가 며칠씩 반복되어도 매번 1회분만 들어가도록 작은 비율로 설계.
    """
    guide_lines = []
    any_signal = False

    for base_ticker, lev in _LEVERAGE_MAP.items():
        try:
            df = fetch_stock_data(base_ticker, period="3mo")
            if df.empty:
                continue
            close = df["Close"].squeeze()
            current = float(close.iloc[-1])
            high_60d = float(close.rolling(min(60, len(close))).max().iloc[-1])
            dd = (current - high_60d) / high_60d * 100

            name = lev["name"]
            timing = _calc_entry_timing(close)
            mult = timing["multiplier"]

            if dd <= -15:
                lev_t = lev["3x"]
                amt = available_cash * 0.05 * mult
                advice = timing["advice"]
                guide_lines.append(
                    f"🔴 <b>{base_ticker}</b> {dd:+.1f}%  →  <b>{lev_t}</b> ${amt:.0f}  |  {advice}"
                )
                any_signal = True
            elif dd <= -10:
                lev_t = lev["3x"]
                amt = available_cash * 0.03 * mult
                advice = timing["advice"]
                guide_lines.append(
                    f"🟠 <b>{base_ticker}</b> {dd:+.1f}%  →  <b>{lev_t}</b> ${amt:.0f}  |  {advice}"
                )
                any_signal = True
            elif dd <= -5:
                lev_t = lev["2x"]
                if lev_t:
                    amt = available_cash * 0.015 * mult
                    advice = timing["advice"]
                    guide_lines.append(
                        f"🟡 <b>{base_ticker}</b> {dd:+.1f}%  →  <b>{lev_t}</b> ${amt:.0f}  |  {advice}"
                    )
                    any_signal = True
            else:
                guide_lines.append(f"⚪ {base_ticker} {dd:+.1f}%  — 대기 중")
        except Exception as e:
            print(f"[leverage_guide] {base_ticker} 오류: {e}")

    if not guide_lines:
        return ""

    lines = [f"<b>📐 레버리지 가이드</b>  <i>가용현금 ${available_cash:,.0f}</i>"]
    lines.extend(guide_lines)
    if any_signal:
        lines.append("<i>1회 진입 금액 — 같은 신호 반복 시 분할 추가</i>")
    return "\n".join(lines)


# ── 시장 뉴스 수집 + Claude 코멘터리 ────────────────────────────

def fetch_market_news() -> list[dict]:
    """yfinance로 주요 지수 관련 최신 뉴스 수집"""
    news_items = []
    seen = set()
    for sym in ["SPY", "QQQ"]:
        try:
            for item in (yf.Ticker(sym).news or [])[:6]:
                title = item.get("title", "")
                if title and title not in seen:
                    seen.add(title)
                    news_items.append({
                        "title": title,
                        "summary": item.get("summary", "")[:120],
                    })
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

    # ── [1] 주요 지표 섹션 ─────────────────────────────────────────
    lines.append("\n<b>📈 주요 지표</b>")

    fg = indicators.get("fear_greed", {})
    if not fg.get("error"):
        trend = ""
        if fg.get("week_ago"):
            diff = fg["score"] - fg["week_ago"]
            trend = f"  {'↑' if diff > 0 else '↓'}{abs(diff):.0f} (1주 전 {fg['week_ago']})"
        lines.append(f"  공포/탐욕  <b>{fg['score']}</b> {fg.get('rating','')}{trend}")

    vix = indicators.get("vix", {})
    if not vix.get("error"):
        chg = f"{'↑' if vix['change'] > 0 else '↓'}{abs(vix['change'])}"
        lines.append(f"  VIX        <b>{vix['current']}</b> {chg}  |  {vix['level']}")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error") and aaii.get("bullish") is not None:
        lines.append(
            f"  AAII       강세 {aaii['bullish']:.0f}%  중립 {aaii.get('neutral',0):.0f}%  약세 {aaii['bearish']:.0f}%"
        )

    pc = indicators.get("put_call", {})
    if not pc.get("error"):
        lines.append(f"  Put/Call   {pc['current']}  ({pc['level']})")

    breadth = indicators.get("breadth", {})
    if not breadth.get("error"):
        lines.append(
            f"  섹터 MA    50일선 위 {breadth['pct_above_50']}%  |  200일선 위 {breadth['pct_above_200']}%"
        )

    fed = indicators.get("fed_rate", {})
    if not fed.get("error") and fed.get("value"):
        lines.append(f"  기준금리   {fed['value']}%")

    krw = indicators.get("usd_krw", {})
    if not krw.get("error") and krw.get("usd_to_krw"):
        chg = ""
        if krw.get("change_pct") is not None:
            arrow = "↑" if krw["change_pct"] > 0 else "↓"
            chg = f"  {arrow}{abs(krw['change_pct']):.2f}% (1주 전 ₩{krw['week_ago']:,.0f})"
        lines.append(f"  USD/KRW    <b>₩{krw['usd_to_krw']:,.2f}</b>{chg}")

    # ── 거시 경고 지표 ────────────────────────────────────────────
    buffett = indicators.get("buffett", {})
    spread = indicators.get("credit_spread", {})
    yc = indicators.get("yield_curve", {})

    macro_lines = []
    if not buffett.get("error"):
        macro_lines.append(f"  버핏지수    <b>{buffett['value']:.0f}%</b>  {buffett['level']}")
    if not spread.get("error"):
        macro_lines.append(f"  신용 스프레드  {spread['value']}%  {spread['level']}")
    if not yc.get("error"):
        macro_lines.append(f"  장단기 금리차  {yc['value']:+.2f}%  {yc['level']}")

    if macro_lines:
        lines.append("\n<b>🌍 거시 경고</b>")
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
        if risk_score >= 7 and ticker in _config.HOLDINGS and _config.HOLDINGS[ticker] > 0.01:
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
        lines.append("\n🟢 <b>매수 기회</b>")
        lines.extend(buy_list)
    if hold_list:
        lines.append("\n⚪ <b>홀딩</b>")
        lines.extend(hold_list)
    if cash_list:
        lines.append("\n🟠 <b>현금 비중 확대 검토</b>")
        lines.extend(cash_list)
    if sell_list:
        lines.append("\n🔴 <b>매도 고려</b>")
        lines.extend(sell_list)

    # ── 극단 과열 경보 (위험점수 7+ 일 때만) ─────────────────────
    if extreme_overheat_list:
        lines.append("\n" + "━" * 28)
        lines.append(
            "<b>⚠️ 극단 과열 경보</b>  "
            f"<i>(위험점수 {risk_score} — 일부 차익 검토 가능)</i>"
        )
        lines.extend(extreme_overheat_list)

    # ── [3] 현금 비중 + 위험점수 섹션 ────────────────────────────
    cash_section, available_cash = build_cash_section(
        _config.HOLDINGS, _config.IDLE_CASH_USD,
        _config.TARGET_CASH_RATIO, _config.CASH_TICKERS,
        risk_score=risk_score, risk_signals=risk_signals,
    )
    if cash_section:
        lines.append("\n" + "━" * 28)
        lines.append(cash_section)

    # ── [4] 레버리지 매수 가이드 ─────────────────────────────────
    lev_section = build_leverage_guide(available_cash)
    if lev_section:
        lines.append("\n" + "━" * 28)
        lines.append(lev_section)

    # ── [5] 오늘의 동적 DCA 권장 금액 ─────────────────────────────
    base_dds = {}
    for base_ticker in ("SPYM", "QQQM", "SOXQ"):
        try:
            df = fetch_stock_data(base_ticker, period="3mo")
            if df.empty:
                continue
            close = df["Close"].squeeze()
            current = float(close.iloc[-1])
            high_60d = float(close.rolling(min(60, len(close))).max().iloc[-1])
            base_dds[base_ticker] = (current - high_60d) / high_60d * 100
        except Exception:
            pass
    dca_section = build_dca_section(indicators, risk_score, base_dds)
    if dca_section:
        lines.append("\n" + "━" * 28)
        lines.append(dca_section)

    # ── [6] 다가오는 이벤트 캘린더 ────────────────────────────────
    cal_section = build_calendar_section(_config.HOLDINGS, days_ahead=14)
    if cal_section:
        lines.append("\n" + "━" * 28)
        lines.append(cal_section)

    # ── [7] 보유 종목 뉴스 헤드라인 ───────────────────────────────
    news_section = build_news_section(_config.HOLDINGS, top_n=3, max_age_hours=48)
    if news_section:
        lines.append("\n" + "━" * 28)
        lines.append(news_section)

    # ── [8] 예상 배당 섹션 ────────────────────────────────────────
    div_section = build_dividend_section(_config.HOLDINGS, nzd_rate)
    if div_section:
        lines.append("\n" + "━" * 28)
        lines.append(div_section)

    lines.append("\n" + "━" * 28)
    lines.append("🤖 <i>Stock Agent — 평일 미국 장 오픈 후 30분 자동 발송 (DST 자동 반영)</i>")

    return "\n".join(lines)


# ── 실행 ─────────────────────────────────────────────────────────

def should_skip_run() -> tuple[bool, str]:
    """
    미국 동부 시간 기준 장 오픈 후 30분(10:00 ET ± 30분) 시점인지 확인.
    DST(EDT/EST)를 자동 처리.
    GitHub Actions에서 cron 두 개(14 UTC, 15 UTC)를 등록하므로
    그 중 하나만 실제 발송하기 위한 게이트.
    """
    if os.getenv("FORCE_SEND") == "1":
        return False, "FORCE_SEND=1 (수동 실행)"

    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return True, f"주말 ({now_et:%a %H:%M ET})"

    target_min = 10 * 60  # 10:00 ET
    current_min = now_et.hour * 60 + now_et.minute
    diff = current_min - target_min
    if abs(diff) > 30:
        return True, f"발송 시간 아님 (현재 {now_et:%H:%M ET}, 목표 10:00 ±30분)"

    return False, f"발송 시간 맞음 ({now_et:%H:%M ET})"


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
