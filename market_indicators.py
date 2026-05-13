#!/usr/bin/env python3
"""
Market Indicators Module
CNN 공포/탐욕, VIX, Put/Call 비율, AAII 심리, FRED 경제지표, NZD 환율을 수집합니다.
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


# ── VIX 지수 ─────────────────────────────────────────────────────

def get_vix() -> dict:
    for attempt in range(3):
        try:
            hist = yf.Ticker("^VIX").history(period="5d")
            if hist is not None and not hist.empty:
                current = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current
                change = round(current - prev, 2)
                if current >= 40:
                    level = "극공포 🔥 적극 매수 구간"
                elif current >= 30:
                    level = "극공포 — 매수 적극 검토"
                elif current >= 20:
                    level = "공포 — 매수 기회"
                elif current >= 15:
                    level = "중립"
                else:
                    level = "과열 (주의)"
                return {"current": round(current, 2), "change": change, "level": level}
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return {"error": "VIX 데이터 없음"}


# ── CBOE Put/Call 비율 ────────────────────────────────────────────

def get_put_call_ratio() -> dict:
    for attempt in range(3):
        try:
            hist = yf.Ticker("^PCCE").history(period="5d")
            if hist is not None and not hist.empty:
                current = round(float(hist["Close"].iloc[-1]), 3)
                level = (
                    "극도 공포 (매수 신호)" if current >= 1.0
                    else "공포" if current >= 0.8
                    else "중립" if current >= 0.6
                    else "탐욕 (과열)"
                )
                return {"current": current, "level": level}
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return {"error": "PCR 데이터 없음"}


# ── AAII 투자자 심리 설문 ─────────────────────────────────────────

def get_aaii_sentiment() -> dict:
    url = "https://www.aaii.com/sentimentsurvey/sent_results"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"id": "sentiment"})
        if not table:
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


# ── S&P 500 섹터 MA 위치 (브레드스 대용) ─────────────────────────

def get_market_breadth() -> dict:
    """
    주요 섹터 ETF들의 50/200일선 위치로 시장 전반 강도 측정.
    Barchart 대용 — 섹터별 이평선 상회 비율.
    """
    sector_etfs = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
    above_50, above_200, total = 0, 0, 0
    for sym in sector_etfs:
        try:
            df = yf.Ticker(sym).history(period="1y")
            if df.empty:
                continue
            close = df["Close"].squeeze()
            price = float(close.iloc[-1])
            if len(close) >= 50:
                ma50 = float(close.rolling(50).mean().iloc[-1])
                if price > ma50:
                    above_50 += 1
            if len(close) >= 200:
                ma200 = float(close.rolling(200).mean().iloc[-1])
                if price > ma200:
                    above_200 += 1
            total += 1
        except Exception:
            pass
    if total == 0:
        return {"error": "섹터 데이터 없음"}
    pct_50 = round(above_50 / total * 100)
    pct_200 = round(above_200 / total * 100)
    return {
        "pct_above_50": pct_50,
        "pct_above_200": pct_200,
        "sectors_checked": total,
    }


# ── NZD/USD 환율 ─────────────────────────────────────────────────

def get_nzd_rate() -> dict:
    """USD → NZD 환율 + 1주일 변동률"""
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=NZD", timeout=8)
        r.raise_for_status()
        current = float(r.json()["rates"]["NZD"])
        from datetime import datetime, timedelta
        wago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            r2 = requests.get(
                f"https://api.frankfurter.app/{wago}?from=USD&to=NZD", timeout=8
            )
            r2.raise_for_status()
            week_ago = float(r2.json()["rates"]["NZD"])
            change_pct = (current - week_ago) / week_ago * 100
        except Exception:
            week_ago, change_pct = None, None
        return {
            "usd_to_nzd": round(current, 4),
            "week_ago": round(week_ago, 4) if week_ago else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "source": "frankfurter",
        }
    except Exception:
        pass
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
        r.raise_for_status()
        rate = r.json()["rates"]["NZD"]
        return {"usd_to_nzd": round(rate, 4), "source": "er-api"}
    except Exception:
        pass
    return {"error": "환율 조회 실패"}


# ── USD/KRW 환율 ─────────────────────────────────────────────────

def get_usd_krw() -> dict:
    """USD → KRW 환율 + 1주일 변동률"""
    try:
        r = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=KRW", timeout=8
        )
        r.raise_for_status()
        current = float(r.json()["rates"]["KRW"])
        # 1주일 전 환율
        from datetime import datetime, timedelta
        wago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            r2 = requests.get(
                f"https://api.frankfurter.app/{wago}?from=USD&to=KRW", timeout=8
            )
            r2.raise_for_status()
            week_ago = float(r2.json()["rates"]["KRW"])
            change_pct = (current - week_ago) / week_ago * 100
        except Exception:
            week_ago, change_pct = None, None
        return {
            "usd_to_krw": round(current, 2),
            "week_ago": round(week_ago, 2) if week_ago else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
        }
    except Exception:
        pass
    try:
        df = yf.Ticker("USDKRW=X").history(period="10d")
        if df is not None and not df.empty:
            current = float(df["Close"].iloc[-1])
            week_ago = float(df["Close"].iloc[0]) if len(df) >= 5 else None
            change_pct = (current - week_ago) / week_ago * 100 if week_ago else None
            return {
                "usd_to_krw": round(current, 2),
                "week_ago": round(week_ago, 2) if week_ago else None,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
            }
    except Exception:
        pass
    return {"error": "USD/KRW 환율 조회 실패"}


# ── FRED 경제지표 ─────────────────────────────────────────────────

def _fred_latest(series_id: str, label: str) -> dict:
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
        return {"date": latest["date"], "value": val, "prev": prev_val, "change": change}
    except Exception as e:
        return {"error": str(e)}


def get_fed_rate() -> dict:
    return _fred_latest("FEDFUNDS", "연방기금금리")


def get_consumer_sentiment() -> dict:
    return _fred_latest("UMCSENT", "소비자심리")


# ── 거시 경고 지표 ───────────────────────────────────────────────

def get_buffett_indicator() -> dict:
    """
    버핏지수 근사 = Wilshire 5000 / GDP × 100.
    역사적 평균 ~100%, 200%+ = 거품 구간.

    WILL5000PRFC는 인덱스값이라 정확한 시총이 아니지만,
    GDP 대비 비율 트렌드를 추적하는 데는 충분합니다.
    """
    if not FRED_API_KEY:
        return {"error": "FRED_API_KEY 미설정"}
    try:
        wilshire = _fred_latest("WILL5000PRFC", "Wilshire 5000")
        gdp = _fred_latest("GDP", "GDP")
        if wilshire.get("error") or gdp.get("error"):
            return {"error": "FRED 데이터 없음"}

        # WILL5000PRFC 인덱스 값 ≈ 시총(billion USD).
        # 2024년말 ~62000 ≈ $62T 시총, GDP ≈ $28T 이라 ratio ~220%
        market_cap_b = wilshire["value"] * 1.0
        ratio = market_cap_b / gdp["value"] * 100

        if ratio >= 220:
            level = "🔥 극단 거품"
        elif ratio >= 200:
            level = "⚠️ 거품 구간"
        elif ratio >= 150:
            level = "고평가"
        elif ratio >= 100:
            level = "공정 가치"
        else:
            level = "저평가"

        return {
            "value": round(ratio, 0),
            "level": level,
            "wilshire": round(wilshire["value"], 0),
            "gdp": round(gdp["value"], 0),
            "date": wilshire.get("date"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_credit_spread() -> dict:
    """
    Moody's BAA 회사채 - 10Y 미국채 스프레드.
    기관 위험회피 신호. 평소 1.5~2.5%, 3%+ 위험, 4%+ 위기.
    """
    result = _fred_latest("BAA10Y", "신용스프레드")
    if result.get("error"):
        return result
    v = result["value"]
    if v >= 4.0:
        level = "🔥 위기 수준"
    elif v >= 3.0:
        level = "⚠️ 위험 확대"
    elif v >= 2.5:
        level = "주의"
    else:
        level = "정상"
    result["level"] = level
    return result


def get_yield_curve() -> dict:
    """
    10Y - 2Y 미국채 금리차.
    음수(역전) = 침체 예고, 정상화(역전 해소) 후 6~12개월이 위험.
    """
    result = _fred_latest("T10Y2Y", "장단기금리차")
    if result.get("error"):
        return result
    v = result["value"]
    prev = result.get("prev")
    if v < 0:
        level = "🔻 역전 (침체 예고)"
    elif v < 0.3:
        if prev is not None and prev < 0:
            level = "⚠️ 정상화 직후 (6~12개월 주의)"
        else:
            level = "평탄"
    else:
        level = "정상"
    result["level"] = level
    return result


# ── 전체 수집 ────────────────────────────────────────────────────

def collect_all() -> dict:
    print("  시장 지표 수집 중...", flush=True)
    return {
        "fear_greed": get_fear_greed(),
        "vix": get_vix(),
        "put_call": get_put_call_ratio(),
        "aaii": get_aaii_sentiment(),
        "breadth": get_market_breadth(),
        "nzd": get_nzd_rate(),
        "usd_krw": get_usd_krw(),
        "fed_rate": get_fed_rate(),
        "consumer_sentiment": get_consumer_sentiment(),
        "buffett": get_buffett_indicator(),
        "credit_spread": get_credit_spread(),
        "yield_curve": get_yield_curve(),
    }


if __name__ == "__main__":
    import json
    data = collect_all()
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
