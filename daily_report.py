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
import anthropic
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from market_indicators import collect_all
from telegram_notifier import send_message
from config import Config

_config = Config()
_claude = anthropic.Anthropic()

ACCUMULATION_PORTFOLIO = _config.ACCUMULATION_PORTFOLIO

# ── 포트폴리오 설정 ──────────────────────────────────────────────
_watch = os.getenv("WATCH_STOCKS", "")
PORTFOLIO = [t.strip() for t in _watch.split(",") if t.strip()] or \
            ["SPYM","QQQM","TQQQ","UPRO","CCJ","VRT","CEG","COPX","ETN"]
MA_PERIODS = [20, 50, 200]


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

    try:
        resp = _claude.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        print(f"[news_commentary] Claude 오류: {e}")
        return ""


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
        resp = _claude.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text
    except Exception as e:
        print(f"[accumulation_report] Claude 오류: {e}")
        return ""


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

    # 주요 뉴스 + 투자 대응
    news_items = fetch_market_news()
    commentary = generate_news_commentary(news_items, mkt_score, mkt_reasons)
    if commentary:
        lines.append("")
        lines.append(commentary)
        lines.append("━" * 15)

    # 적립 포트폴리오 점검 + 편입/퇴출 추천
    accum_report = generate_accumulation_report(mkt_score, news_items)
    if accum_report:
        lines.append("")
        lines.append(accum_report)
        lines.append("━" * 15)

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

    lines.append("\n" + "━" * 15)
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
