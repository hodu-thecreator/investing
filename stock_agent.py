#!/usr/bin/env python3
"""
Stock Investment Assistant Agent
매일 아침 포트폴리오 종목의 현재가, 고점 대비 하락률, 이평선 분석을 제공합니다.
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# ── 포트폴리오 설정 ──────────────────────────────────────────────
PORTFOLIO = ["SPYM", "QQQM", "TQQQ", "UPRO", "CCJ", "VRT", "CEG", "COPX", "ETN"]

# 고점 대비 하락 시 레버리지 매수 알림 기준 (%)
ALERT_THRESHOLDS = [-5, -10, -15, -20]

# 이평선 기간 설정
MA_PERIODS = [5, 20, 50, 100, 200]

# ── 데이터 수집 ──────────────────────────────────────────────────

def fetch_stock_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    """yfinance로 주가 데이터 가져오기"""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period)
        return df
    except Exception as e:
        print(f"  [오류] {ticker} 데이터 수집 실패: {e}")
        return pd.DataFrame()


def get_analyst_target(ticker: str) -> dict:
    """애널리스트 목표주가 및 추천 가져오기"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return {
            "target_mean": info.get("targetMeanPrice"),
            "target_low": info.get("targetLowPrice"),
            "target_high": info.get("targetHighPrice"),
            "recommendation": info.get("recommendationKey", "N/A").upper(),
        }
    except Exception:
        return {"target_mean": None, "target_low": None, "target_high": None, "recommendation": "N/A"}


def get_latest_news(ticker: str, max_items: int = 3) -> list[str]:
    """종목 최신 뉴스 헤드라인 가져오기"""
    try:
        stock = yf.Ticker(ticker)
        news = stock.news or []
        headlines = []
        for item in news[:max_items]:
            content = item.get("content", {})
            title = content.get("title") or item.get("title", "")
            if title:
                headlines.append(f"  • {title}")
        return headlines
    except Exception:
        return []

# ── 분석 함수 ────────────────────────────────────────────────────

def calc_moving_averages(df: pd.DataFrame) -> dict:
    """이평선 현재값 계산"""
    if df.empty:
        return {}
    result = {}
    close = df["Close"]
    for period in MA_PERIODS:
        if len(close) >= period:
            result[period] = close.rolling(period).mean().iloc[-1]
    return result


def calc_drawdown_from_high(df: pd.DataFrame, lookback_days: int = 252) -> dict:
    """52주 고점 대비 현재 하락률 계산"""
    if df.empty:
        return {}
    recent = df["Close"].tail(lookback_days)
    high_52w = recent.max()
    current = recent.iloc[-1]
    drawdown_pct = (current - high_52w) / high_52w * 100
    return {
        "current": current,
        "high_52w": high_52w,
        "drawdown_pct": drawdown_pct,
    }


def ma_position_label(current: float, mas: dict) -> str:
    """현재가 대비 이평선 위치 요약 (위 ↑ / 아래 ↓)"""
    if not mas:
        return "N/A"
    parts = []
    for period in sorted(mas):
        symbol = "↑" if current >= mas[period] else "↓"
        parts.append(f"{period}일{symbol}")
    return "  ".join(parts)


def buy_alerts(ticker: str, drawdown_pct: float) -> list[str]:
    """고점 대비 하락 임계치 도달 시 알림 메시지 생성"""
    alerts = []
    for threshold in ALERT_THRESHOLDS:
        if drawdown_pct <= threshold:
            alerts.append(
                f"  🔔 {ticker} 고점 대비 {abs(threshold)}% 이상 하락 → 분할매수 검토"
            )
            break  # 가장 낮은 임계치 하나만 표시
    return alerts

# ── 시장 온도 체크 ────────────────────────────────────────────────

def market_temperature() -> str:
    """S&P500 구성 종목 중 200일선 위 비율로 시장 과열/침체 판단"""
    # S&P500 지수 자체를 대용으로 사용
    try:
        spy = yf.Ticker("SPY")
        df = spy.history(period="1y")
        if df.empty:
            return "시장 데이터 없음"
        close = df["Close"]
        ma200 = close.rolling(200).mean().iloc[-1]
        current = close.iloc[-1]
        pct_above = (current - ma200) / ma200 * 100

        if pct_above > 10:
            label = "과매수 (주의)"
        elif pct_above > 0:
            label = "상승 추세"
        elif pct_above > -10:
            label = "200일선 하회 (조심)"
        else:
            label = "침체 구간 (기회 탐색)"

        return f"SPY {current:.2f} / 200MA {ma200:.2f} ({pct_above:+.1f}%)  → {label}"
    except Exception as e:
        return f"시장 온도 계산 실패: {e}"

# ── 메인 리포트 출력 ─────────────────────────────────────────────

def print_report(show_news: bool = True):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("=" * 70)
    print(f"  📈  주식 투자 도우미  |  {now}")
    print("=" * 70)

    # 시장 온도
    print("\n[시장 온도]")
    print(f"  {market_temperature()}")

    all_alerts = []

    print("\n[포트폴리오 현황]")
    print(f"{'종목':<8} {'현재가':>8} {'52주고점':>10} {'하락률':>8}  이평선 위치")
    print("-" * 70)

    for ticker in PORTFOLIO:
        df = fetch_stock_data(ticker)
        if df.empty:
            print(f"{ticker:<8}  데이터 없음")
            continue

        dd = calc_drawdown_from_high(df)
        mas = calc_moving_averages(df)
        current = dd.get("current", 0)
        high = dd.get("high_52w", 0)
        drawdown = dd.get("drawdown_pct", 0)

        ma_label = ma_position_label(current, mas)
        print(f"{ticker:<8} ${current:>7.2f}  ${high:>8.2f}  {drawdown:>+6.1f}%  {ma_label}")

        alerts = buy_alerts(ticker, drawdown)
        all_alerts.extend(alerts)

    # 이평선 상세
    print("\n[이평선 상세 (현재가 기준)]")
    print(f"{'종목':<8} {'5일':>7} {'20일':>7} {'50일':>7} {'100일':>8} {'200일':>8}")
    print("-" * 50)
    for ticker in PORTFOLIO:
        df = fetch_stock_data(ticker)
        if df.empty:
            continue
        mas = calc_moving_averages(df)
        current = df["Close"].iloc[-1]
        row = f"{ticker:<8}"
        for p in MA_PERIODS:
            ma_val = mas.get(p)
            if ma_val:
                diff = (current - ma_val) / ma_val * 100
                row += f"  {diff:>+5.1f}%"
            else:
                row += f"  {'N/A':>5}"
        print(row)

    # 애널리스트 목표주가
    print("\n[애널리스트 목표주가]")
    print(f"{'종목':<8} {'추천':<12} {'평균목표':>10} {'저':>8} {'고':>8}")
    print("-" * 50)
    for ticker in PORTFOLIO:
        info = get_analyst_target(ticker)
        rec = info["recommendation"]
        mean = f"${info['target_mean']:.2f}" if info["target_mean"] else "  N/A"
        low = f"${info['target_low']:.2f}" if info["target_low"] else "  N/A"
        high = f"${info['target_high']:.2f}" if info["target_high"] else "  N/A"
        print(f"{ticker:<8} {rec:<12} {mean:>10} {low:>8} {high:>8}")

    # 매수 알림
    if all_alerts:
        print("\n[매수 타이밍 알림]")
        for alert in all_alerts:
            print(alert)

    # 뉴스
    if show_news:
        print("\n[종목별 최신 뉴스]")
        for ticker in PORTFOLIO:
            headlines = get_latest_news(ticker)
            if headlines:
                print(f"\n  [{ticker}]")
                for h in headlines:
                    print(h)

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="주식 투자 도우미 에이전트")
    parser.add_argument("--no-news", action="store_true", help="뉴스 출력 생략")
    parser.add_argument(
        "--ticker",
        type=str,
        help="단일 종목 빠른 조회 (예: --ticker AAPL)",
    )
    args = parser.parse_args()

    if args.ticker:
        t = args.ticker.upper()
        df = fetch_stock_data(t)
        dd = calc_drawdown_from_high(df)
        mas = calc_moving_averages(df)
        analyst = get_analyst_target(t)
        news = get_latest_news(t)

        print(f"\n[{t} 단일 종목 분석]")
        print(f"  현재가   : ${dd.get('current', 0):.2f}")
        print(f"  52주 고점: ${dd.get('high_52w', 0):.2f}")
        print(f"  고점 대비: {dd.get('drawdown_pct', 0):+.1f}%")
        print(f"  이평선   : {ma_position_label(dd.get('current', 0), mas)}")
        print(f"  추천     : {analyst['recommendation']}")
        if analyst["target_mean"]:
            print(f"  목표주가 : ${analyst['target_mean']:.2f} (저 ${analyst['target_low']:.2f} / 고 ${analyst['target_high']:.2f})")
        if news:
            print("  최신뉴스 :")
            for h in news:
                print(h)
    else:
        print_report(show_news=not args.no_news)
