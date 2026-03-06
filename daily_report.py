#!/usr/bin/env python3
"""
Daily Report — 종합 판단 버전
모든 지표를 내부적으로 분석해서 종목별 매수/홀딩/매도 결론만 전송합니다.
"""

import re
import argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import os
import time
import requests
import pandas as pd
import yfinance as yf
from market_indicators import collect_all
from telegram_notifier import send_message

# GitHub Actions 환경에서 Yahoo Finance 차단 우회용 세션
_yf_session = requests.Session()
_yf_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

# ── 포트폴리오 설정 ──────────────────────────────────────────────
PORTFOLIO = os.getenv("WATCH_STOCKS", "SPYM,QQQM,TQQQ,UPRO,CCJ,VRT,CEG,COPX,ETN").split(",")
MA_PERIODS = [20, 50, 200]


def fetch_stock_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    for attempt in range(3):
        try:
            t = yf.Ticker(ticker, session=_yf_session)
            df = t.history(period=period)
            if df is not None and not df.empty:
                return df
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return pd.DataFrame()


def calc_moving_averages(df: pd.DataFrame) -> dict:
    result = {}
    for p in MA_PERIODS:
        if len(df) >= p:
            result[p] = df["Close"].rolling(p).mean().iloc[-1]
    return result


def calc_drawdown_from_high(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"current": 0, "high": 0, "drawdown_pct": 0}
    current = df["Close"].iloc[-1]
    high = df["Close"].max()
    drawdown_pct = (current - high) / high * 100 if high else 0
    return {"current": current, "high": high, "drawdown_pct": drawdown_pct}


# ── 시장 환경 점수 (-10 ~ +10, 양수 = 매수 우호적) ──────────────

def market_score(indicators: dict) -> tuple:
    """지표들을 종합해 시장 환경 점수와 근거 반환"""
    score = 0
    reasons = []

    fg = indicators.get("fear_greed", {})
    if not fg.get("error"):
        s = fg["score"]
        if s <= 25:
            score += 3; reasons.append(f"극도 공포 (F&G {s})")
        elif s <= 45:
            score += 2; reasons.append(f"공포 구간 (F&G {s})")
        elif s >= 75:
            score -= 2; reasons.append(f"극도 탐욕 (F&G {s})")
        elif s >= 60:
            score -= 1; reasons.append(f"탐욕 구간 (F&G {s})")

    vix = indicators.get("vix", {})
    if not vix.get("error"):
        v = vix["current"]
        if v >= 30:
            score += 3; reasons.append(f"VIX 극공포 ({v})")
        elif v >= 20:
            score += 2; reasons.append(f"VIX 공포 ({v})")
        elif v < 15:
            score -= 1; reasons.append(f"VIX 낮음 ({v})")

    pc = indicators.get("put_call", {})
    if not pc.get("error"):
        r = pc["current"]
        if r >= 1.0:
            score += 2; reasons.append(f"Put/Call 극공포 ({r})")
        elif r >= 0.8:
            score += 1; reasons.append(f"Put/Call 공포 ({r})")
        elif r < 0.6:
            score -= 1; reasons.append(f"Put/Call 탐욕 ({r})")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error") and aaii.get("bearish") is not None:
        bear = aaii["bearish"]
        if bear >= 45:
            score += 2; reasons.append(f"AAII 약세 과반 ({bear:.0f}%) → 역발상 매수 신호")
        elif bear >= 35:
            score += 1; reasons.append(f"AAII 약세 우세 ({bear:.0f}%)")

    return score, reasons


# ── 종목별 판단 로직 ─────────────────────────────────────────────

def judge_ticker(ticker: str, mkt_score: int) -> dict:
    """종목별 매수/홀딩/매도 판단"""
    df = fetch_stock_data(ticker)
    if df.empty:
        return {"action": "데이터없음", "emoji": "⚪", "reasons": ["데이터 수집 실패"], "drawdown": 0, "price": 0, "score": 0}

    dd = calc_drawdown_from_high(df)
    mas = calc_moving_averages(df)
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
    above_mas = sum(1 for p in [20, 50, 200] if p in mas and price >= mas[p])
    below_mas = sum(1 for p in [20, 50, 200] if p in mas and price < mas[p])

    if below_mas >= 3:
        stock_score += 2; reasons.append("단·중·장기 이평선 전부 하회")
    elif below_mas == 2:
        stock_score += 1; reasons.append("주요 이평선 2개 하회")
    elif above_mas >= 3:
        stock_score -= 1; reasons.append("이평선 전부 상회 (고점 주의)")

    # 3. 시장 환경 가중치 (레버리지 ETF는 민감도 높임)
    leverage = ticker in ("TQQQ", "UPRO", "SPYM")
    env_weight = 2 if leverage else 1
    total = stock_score + (mkt_score * env_weight // 3)

    # 4. 최종 판단
    if total >= 4:
        action, emoji = "📈 매수", "🟢"
    elif total >= 2:
        action, emoji = "🔍 분할매수 검토", "🟡"
    elif total <= -2:
        action, emoji = "📉 매도 고려", "🔴"
    else:
        action, emoji = "⏸ 홀딩", "⚪"

    return {
        "action": action,
        "emoji": emoji,
        "reasons": reasons,
        "drawdown": drawdown,
        "price": price,
        "score": total,
    }


# ── 리포트 빌더 ──────────────────────────────────────────────────

def build_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"<b>📊 투자 판단 브리핑</b>  {now}")
    lines.append("━" * 30)

    # 시장 환경 분석
    indicators = collect_all()
    mkt_score, mkt_reasons = market_score(indicators)

    if mkt_score >= 4:
        mkt_label = "🟢 매수 우호적"
    elif mkt_score >= 2:
        mkt_label = "🟡 중립 (신중 매수)"
    elif mkt_score <= -2:
        mkt_label = "🔴 리스크 높음"
    else:
        mkt_label = "⚪ 중립"

    lines.append(f"\n<b>시장 환경: {mkt_label}</b>")
    if mkt_reasons:
        lines.append("  " + " · ".join(mkt_reasons))

    # 종목별 판단
    lines.append(f"\n<b>종목별 판단</b>")

    buy_list, hold_list, sell_list = [], [], []

    for ticker in PORTFOLIO:
        result = judge_ticker(ticker, mkt_score)
        action = result["action"]
        emoji = result["emoji"]
        price = result["price"]
        drawdown = result["drawdown"]
        reasons = result["reasons"]

        line = f"{emoji} <b>{ticker}</b>  ${price:.2f}  ({drawdown:+.1f}%)  → {action}"
        if reasons:
            line += f"\n   <i>{' · '.join(reasons)}</i>"

        if "매수" in action:
            buy_list.append(line)
        elif "매도" in action:
            sell_list.append(line)
        else:
            hold_list.append(line)

    if buy_list:
        lines.append("\n🟢 <b>매수 대상</b>")
        lines.extend(buy_list)
    if sell_list:
        lines.append("\n🔴 <b>매도 검토</b>")
        lines.extend(sell_list)
    if hold_list:
        lines.append("\n⚪ <b>홀딩</b>")
        lines.extend(hold_list)

    lines.append("\n━" * 15)
    lines.append("🤖 <i>Stock Agent — 매일 08:00 자동 발송</i>")

    return "\n".join(lines)


# ── 실행 ─────────────────────────────────────────────────────────

def run_once(test_mode: bool = False):
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
