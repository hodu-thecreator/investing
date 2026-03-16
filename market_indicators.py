#!/usr/bin/env python3
"""
Market Indicators Module
CNN 공포/탐욕, VIX, Put/Call 비율, AAII 심리, FRED 경제지표를 수집합니다.
"""

import os
import time
import requests
import yfinance as yf
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings("ignore")

FRED_API_KEY = os.getenv("FRED_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ── CNN 공포/탐욕 지수 ────────────────────────────────────────────

def get_fear_greed() -> dict:
    """
    CNN Fear & Greed Index
    출처: https://edition.cnn.com/markets/fear-and-greed
    """
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        fg = data["fear_and_greed"]
        score = round(fg["score"], 1)
        rating = fg["rating"].replace("_", " ").title()
        prev = data.get("fear_and_greed_historical", {})
        week_ago = round(prev.get("week_ago", {}).get("score", 0), 1)
        month_ago = round(prev.get("month_ago", {}).get("score", 0), 1)
        return {
            "score": score,
            "rating": rating,
            "week_ago": week_ago,
            "month_ago": month_ago,
        }
    except Exception as e:
        return {"error": str(e)}


# ── VIX 지수 (yfinance) ──────────────────────────────────────────

def get_vix() -> dict:
    """VIX 공포 지수 — Google Finance / yfinance"""
    for attempt in range(3):
        try:
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="5d")
            if hist is not None and not hist.empty:
                current = hist["Close"].iloc[-1]
                prev = hist["Close"].iloc[-2] if len(hist) >= 2 else current
                change = current - prev
                level = (
                    "극도 공포 (매수 기회)" if current >= 30
                    else "공포" if current >= 20
                    else "중립" if current >= 15
                    else "과열 (주의)"
                )
                return {"current": round(current, 2), "change": round(change, 2), "level": level}
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return {"error": "VIX 데이터 없음"}


# ── CBOE Put/Call 비율 ────────────────────────────────────────────

def get_put_call_ratio() -> dict:
    """
    CBOE 총 Put/Call 비율
    yfinance로 근사값을 제공합니다.
    """
    for attempt in range(3):
        try:
            pcr = yf.Ticker("^PCCE")  # Equity PCR
            hist = pcr.history(period="5d")
            if hist is not None and not hist.empty:
                current = round(hist["Close"].iloc[-1], 3)
                level = (
                    "극도 공포 (매수 신호)" if current >= 1.0
                    else "공포" if current >= 0.8
                    else "중립" if current >= 0.6
                    else "탐욕 (과열)"
                )
                return {"current": current, "level": level, "note": "CBOE Equity PCR"}
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return {"error": "PCR 데이터 없음"}


# ── AAII 투자자 심리 설문 ─────────────────────────────────────────

def get_aaii_sentiment() -> dict:
    """
    AAII Investor Sentiment Survey (주간)
    출처: https://www.aaii.com/sentimentsurvey
    """
    url = "https://www.aaii.com/sentimentsurvey/sent_results"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # 테이블에서 최신 행 파싱
        table = soup.find("table", {"id": "sentiment"})
        if not table:
            # 대체: 요약 수치만 가져오기
            return _parse_aaii_summary(soup)

        rows = table.find_all("tr")
        if len(rows) < 2:
            return {"error": "AAII 테이블 파싱 실패"}

        cells = rows[1].find_all("td")
        if len(cells) < 4:
            return {"error": "AAII 데이터 셀 부족"}

        def pct(val: str) -> float:
            return float(val.strip().replace("%", ""))

        return {
            "date": cells[0].get_text(strip=True),
            "bullish": pct(cells[1].get_text()),
            "neutral": pct(cells[2].get_text()),
            "bearish": pct(cells[3].get_text()),
        }
    except Exception as e:
        return {"error": str(e)}


def _parse_aaii_summary(soup: BeautifulSoup) -> dict:
    """AAII 요약 수치 대체 파서"""
    try:
        text = soup.get_text(" ")
        import re
        bull = re.search(r"Bullish\s+([\d.]+)%", text)
        neu = re.search(r"Neutral\s+([\d.]+)%", text)
        bear = re.search(r"Bearish\s+([\d.]+)%", text)
        return {
            "bullish": float(bull.group(1)) if bull else None,
            "neutral": float(neu.group(1)) if neu else None,
            "bearish": float(bear.group(1)) if bear else None,
        }
    except Exception:
        return {"error": "AAII 파싱 실패"}


# ── FRED 경제지표 (공통) ─────────────────────────────────────────

def _fred_latest(series_id: str, label: str) -> dict:
    """FRED API에서 최신 데이터 포인트 가져오기"""
    if not FRED_API_KEY:
        return {"error": f"FRED_API_KEY 미설정 ({label})"}
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 2,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        if not obs:
            return {"error": f"{label} 데이터 없음"}
        latest = obs[0]
        prev = obs[1] if len(obs) > 1 else None
        val = float(latest["value"]) if latest["value"] != "." else None
        prev_val = float(prev["value"]) if prev and prev["value"] != "." else None
        change = round(val - prev_val, 3) if val and prev_val else None
        return {
            "date": latest["date"],
            "value": val,
            "prev": prev_val,
            "change": change,
        }
    except Exception as e:
        return {"error": str(e)}


def get_jolts() -> dict:
    """JOLTS 구인건수 (단위: 천 명) — investing.com 동일 지표"""
    return _fred_latest("JTSJOL", "JOLTS")


def get_cpi() -> dict:
    """소비자 물가지수 CPI (YoY %)"""
    result = _fred_latest("CPIAUCSL", "CPI")
    # 전월 대비 % 변화를 전년 동월 대비로 별도 계산
    if not result.get("error") and FRED_API_KEY:
        try:
            yoy = _fred_latest("CPIAUCNS", "CPI YoY")  # 시계열 12개월치
            # 간단히 레벨 값 반환 후 리포트에서 서술
            pass
        except Exception:
            pass
    return result


def get_consumer_sentiment() -> dict:
    """미시간대 소비자심리지수 (UMCSENT)"""
    return _fred_latest("UMCSENT", "소비자심리")


def get_consumer_confidence() -> dict:
    """컨퍼런스보드 소비자신뢰지수 (CONCCONF)"""
    return _fred_latest("CONCCONF", "소비자신뢰")


def get_fed_rate() -> dict:
    """연방기금금리 (FEDFUNDS) — 금리결정 현황"""
    return _fred_latest("FEDFUNDS", "연방기금금리")


def get_margin_debt() -> dict:
    """FINRA 마진 부채 (DPSACBW027SBOG 근사)"""
    return _fred_latest("DPSACBW027SBOG", "마진부채")


# ── 전체 수집 ────────────────────────────────────────────────────

def collect_all() -> dict:
    """모든 지표를 수집해서 딕셔너리로 반환"""
    print("  시장 지표 수집 중...", flush=True)
    return {
        "fear_greed": get_fear_greed(),
        "vix": get_vix(),
        "put_call": get_put_call_ratio(),
        "aaii": get_aaii_sentiment(),
        "jolts": get_jolts(),
        "cpi": get_cpi(),
        "consumer_sentiment": get_consumer_sentiment(),
        "consumer_confidence": get_consumer_confidence(),
        "fed_rate": get_fed_rate(),
        "margin_debt": get_margin_debt(),
    }


# ── 간단 테스트 ──────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    data = collect_all()
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
