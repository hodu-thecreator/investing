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
            f"🗑 <b>{ticker}</b>는 청산 예정 레거시입니다 (헌법 5조).\n"
            f"신규 매수 금지 — /tax 로 세금 0 정리 플랜을 확인하세요."
        )
    if ticker in _config.SATELLITE_TICKERS:
        cap = _config.SATELLITE_TICKERS[ticker] * 100
        return (
            f"🛰 <b>{ticker}</b>는 승인된 위성입니다 (헌법 5조, 상한 {cap:.0f}%).\n"
            f"매수는 저수지 구간에서만 — /dip 으로 수위를 확인하세요."
        )

    info = _fetch_fund_info(ticker)
    checks: list[tuple[str, bool | None, str]] = []  # (질문, 통과여부, 근거)

    # 헌법 4조: 개별주는 8문 이전 즉시 거부
    if info["quote_type"] == "EQUITY":
        name = f" ({info['name']})" if info["name"] else ""
        return (
            f"⛔ <b>{ticker}</b>{name} — <b>개별주는 예외 없이 금지</b> (헌법 4조).\n"
            f"8문 통과제 이전에 거부됩니다. 지수 ETF만 검토 대상입니다."
        )

    # 1. 트랙레코드 — 지수 ETF는 5년+, 그 외 15년+ (2026.6 개정)
    is_etf = info["quote_type"] == "ETF"
    min_years = MIN_TRACK_YEARS_ETF if is_etf else MIN_TRACK_YEARS
    rule_tag = "지수 ETF 기준" if is_etf else "ETF 미확인 — 15년 기준"
    if info["inception"]:
        years = (datetime.now() - info["inception"]).days / 365.25
        checks.append((f"트랙레코드 {min_years}년+ ({rule_tag})", years >= min_years,
                       f"{info['inception']:%Y.%m} 상장 ({years:.1f}년)"))
    else:
        checks.append((f"트랙레코드 {min_years}년+ ({rule_tag})", None, "상장일 확인 불가"))

    # 2. 보수 0.15% 이하
    if info["expense_pct"] is not None:
        checks.append((f"보수 {MAX_EXPENSE_PCT}% 이하", info["expense_pct"] <= MAX_EXPENSE_PCT,
                       f"{info['expense_pct']:.2f}%"))
    else:
        checks.append((f"보수 {MAX_EXPENSE_PCT}% 이하", None, "보수 확인 불가"))

    # 3. 규모 $10B+
    if info["aum"]:
        checks.append(("규모 $10B+", info["aum"] >= MIN_AUM_USD,
                       f"${info['aum']/1e9:.1f}B"))
    else:
        checks.append(("규모 $10B+", None, "규모 확인 불가"))

    # 4~5. 출처/기관 보고서 — 수동 판단
    checks.append(("출처가 Bloomberg/Reuters급", None, "직접 확인"))
    checks.append(("주요 기관 보고서에 존재", None, "직접 확인"))

    # 6~7. 자산배분 빈자리 / 단순 원칙 — 자동 (코어5+위성2 꽉 참 → 위성 교체만 가능)
    checks.append(("현재 자산배분에 빈 자리", False, "코어5+위성2 꽉 참 (위성 교체만 가능)"))
    checks.append(("코어5+위성2 단순 원칙 유지", False, f"{ticker} 추가 시 초과"))

    # 8. 30년 보유 가능 — 수동
    checks.append(("20~30년 보유 가능", None, "스스로에게 질문"))

    passed = sum(1 for _, ok, _ in checks if ok is True)
    failed = sum(1 for _, ok, _ in checks if ok is False)
    max_possible = 8 - failed  # 미확인 항목이 전부 통과한다 가정해도

    name = f" <i>({info['name']})</i>" if info["name"] else ""
    lines = [f"<b>🔍 {ticker}{name} — 헌법 8조 8문 통과제</b>", ""]
    for i, (q, ok, why) in enumerate(checks, 1):
        mark = "✅" if ok is True else "❌" if ok is False else "❔"
        lines.append(f"  {i}. {mark} {q}  <i>{why}</i>")

    lines.append("")
    if max_possible < 7:
        lines.append(f"<b>판정: 거부</b> — 확정 탈락 {failed}개, 7문 통과 불가능")
    elif passed >= 7:
        lines.append(f"<b>판정: 검토 가능</b> ({passed}/8 통과) — 그래도 디폴트는 무시")
    else:
        lines.append(
            f"<b>판정: 보류</b> — 자동 통과 {passed}, 탈락 {failed},"
            f" 나머지 ❔를 직접 확인해도 7문 넘기 어려움"
        )
    lines.append("<i>디폴트: 무시. 자산의 99%는 5종목으로 결정됩니다.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(evaluate(sys.argv[1] if len(sys.argv) > 1 else "SCHD"))
