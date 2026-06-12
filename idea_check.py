#!/usr/bin/env python3
"""
헌법 8조 — 새 투자 아이디어 8문 통과제 자동 판정 (/idea TICKER).

"이거 사도 돼?"라는 FOMO를 룰로 받아내는 가드.
자동 확인 가능한 항목(트랙레코드·보수·규모·5종목 원칙)은 데이터로 판정,
나머지는 체크리스트로 제시. 8문 중 7개 미만 통과 → 즉시 거부.
"""
from datetime import datetime

from config import Config

_config = Config()

MIN_TRACK_YEARS = 15        # ETF 외 (사실상 금지 — 개별주는 8문 이전 즉시 거부)
MIN_TRACK_YEARS_ETF = 5     # 지수 ETF — 2026.6 헌법 개정 (기존 15년)
MAX_EXPENSE_PCT = 0.15
MIN_AUM_USD = 10_000_000_000


def _fetch_fund_info(ticker: str) -> dict:
    """yfinance에서 상장일·보수·규모 조회. 실패한 필드는 None."""
    out = {"inception": None, "expense_pct": None, "aum": None, "name": None,
           "quote_type": None}
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        out["name"] = info.get("longName") or info.get("shortName")
        out["quote_type"] = info.get("quoteType")
        epoch = info.get("fundInceptionDate")
        if epoch:
            out["inception"] = datetime.fromtimestamp(epoch)
        exp = info.get("netExpenseRatio") or info.get("annualReportExpenseRatio")
        if exp is not None:
            # yfinance가 0.0015(비율) 또는 0.15(%) 두 포맷을 혼용 → % 로 정규화
            out["expense_pct"] = exp * 100 if exp < 0.02 else exp
        aum = info.get("totalAssets") or info.get("marketCap")
        if aum:
            out["aum"] = float(aum)
    except Exception as e:
        print(f"[idea] {ticker} 정보 조회 실패: {e}")
    return out


def evaluate(ticker: str) -> str:
    ticker = ticker.upper().strip()
    if not ticker:
        return "사용법: <code>/idea TICKER</code>  (예: /idea SCHD)"

    core = set(_config.CORE_ALLOCATION.keys())
    if ticker in core:
        return (
            f"✅ <b>{ticker}</b>는 이미 코어 5종목입니다.\n"
            f"추가 매수는 /now 의 납입 배분을 따르세요."
        )
    if ticker in set(_config.LEGACY_TICKERS):
        return (
            f"🗑 <b>{ticker}</b>는 청산 예정 레거시입니다.\n"
            f"신규 매수 금지 — /tax 로 세금 0 정리 플랜을 확인하세요."
        )
    if ticker in _config.SATELLITE_TICKERS:
        cap = _config.SATELLITE_TICKERS[ticker] * 100
        return (
            f"🛰 <b>{ticker}</b>는 승인된 위성입니다 (상한 {cap:.0f}%).\n"
            f"매수는 저수지 구간에서만 — /dip 으로 수위를 확인하세요."
        )

    info = _fetch_fund_info(ticker)

    # 헌법 4조: 개별주는 즉시 거부
    if info["quote_type"] == "EQUITY":
        name = f" ({info['name']})" if info["name"] else ""
        return f"⛔ <b>{ticker}</b>{name} — 개별주는 예외 없이 금지. 지수 ETF만 검토."

    # 자동 체크 3가지: 트랙레코드 / 보수 / 규모 — 탈락 사유만 보여줌
    is_etf = info["quote_type"] == "ETF"
    min_years = MIN_TRACK_YEARS_ETF if is_etf else MIN_TRACK_YEARS
    facts, fails = [], []

    if info["inception"]:
        years = (datetime.now() - info["inception"]).days / 365.25
        if years >= min_years:
            facts.append(f"트랙레코드 {years:.0f}년")
        else:
            fails.append(f"상장 {years:.1f}년 (<{min_years}년)")
    else:
        facts.append("상장일 확인 불가")

    if info["expense_pct"] is not None:
        if info["expense_pct"] <= MAX_EXPENSE_PCT:
            facts.append(f"보수 {info['expense_pct']:.2f}%")
        else:
            fails.append(f"보수 {info['expense_pct']:.2f}% (>{MAX_EXPENSE_PCT}%)")
    else:
        facts.append("보수 확인 불가")

    if info["aum"]:
        if info["aum"] >= MIN_AUM_USD:
            facts.append(f"규모 ${info['aum']/1e9:.0f}B")
        else:
            fails.append(f"규모 ${info['aum']/1e9:.1f}B (<$10B)")
    else:
        facts.append("규모 확인 불가")

    name = f" <i>({info['name']})</i>" if info["name"] else ""
    lines = [f"<b>🔍 {ticker}</b>{name}"]
    if fails:
        lines.append(f"<b>거부</b> — {' · '.join(fails)}")
    else:
        lines.append("<b>거부</b> — 기준은 통과했지만 코어5+위성2에 빈 자리 없음")
        lines.append(f"  {' · '.join(facts)}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(evaluate(sys.argv[1] if len(sys.argv) > 1 else "SCHD"))
