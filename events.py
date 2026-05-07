#!/usr/bin/env python3
"""
이벤트 캘린더 — 보유 종목 실적/배당락일 + 거시 이벤트(FOMC, CPI).
N일 이내 다가오는 이벤트만 추려서 표시.
"""
from datetime import datetime, date, timedelta
import yfinance as yf

# 2026년 거시 이벤트 일정 (참고용 — 매년 업데이트 필요)
FOMC_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29",
    "2026-06-17", "2026-07-29", "2026-09-16",
    "2026-11-04", "2026-12-16",
]

# CPI 발표는 보통 매월 두 번째 주 화/수 (BLS 공식 일정 기준)
CPI_DATES_2026 = [
    "2026-01-14", "2026-02-11", "2026-03-12", "2026-04-14",
    "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-12",
    "2026-09-10", "2026-10-15", "2026-11-12", "2026-12-10",
]


def _safe_date(value) -> date | None:
    """yfinance가 반환하는 다양한 날짜 형식을 date로 변환"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).date()
        except Exception:
            return None
    if isinstance(value, list) and value:
        return _safe_date(value[0])
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _earnings_date(ticker: str) -> date | None:
    try:
        cal = yf.Ticker(ticker).calendar
        if isinstance(cal, dict):
            return _safe_date(cal.get("Earnings Date"))
        if hasattr(cal, "columns"):
            for col in cal.columns:
                if "Earnings" in str(col):
                    return _safe_date(cal[col].iloc[0])
    except Exception:
        pass
    return None


def _ex_div_date(ticker: str) -> date | None:
    try:
        info = yf.Ticker(ticker).info
        return _safe_date(info.get("exDividendDate"))
    except Exception:
        return None


def collect_events(holdings: dict[str, float], days_ahead: int = 14) -> list[tuple]:
    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    items = []

    # 보유 종목 실적/배당
    for ticker, shares in holdings.items():
        if shares < 0.01:
            continue
        ed = _earnings_date(ticker)
        if ed and today <= ed <= horizon:
            items.append((ed, "earnings", f"📊 {ticker} 실적 발표"))
        dd = _ex_div_date(ticker)
        if dd and today <= dd <= horizon:
            items.append((dd, "dividend", f"💰 {ticker} 배당락"))

    # FOMC
    for s in FOMC_DATES_2026:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        if today <= d <= horizon:
            items.append((d, "fomc", "🏦 FOMC 회의"))

    # CPI
    for s in CPI_DATES_2026:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        if today <= d <= horizon:
            items.append((d, "cpi", "📈 CPI 발표"))

    items.sort(key=lambda x: x[0])
    return items


def build_calendar_section(holdings: dict[str, float], days_ahead: int = 14) -> str:
    today = datetime.now().date()
    items = collect_events(holdings, days_ahead)
    if not items:
        return ""

    lines = [f"<b>📅 다가오는 이벤트 ({days_ahead}일 이내)</b>"]
    for d, kind, label in items[:8]:
        diff = (d - today).days
        d_str = "오늘" if diff == 0 else "내일" if diff == 1 else f"D-{diff}"
        date_str = d.strftime("%m/%d")
        lines.append(f"  {label}  <b>{d_str}</b>  ({date_str})")

    has_macro = any(k in ("fomc", "cpi") for _, k, _ in items)
    if has_macro:
        lines.append("  <i>거시 이벤트 전엔 신규 적립 일시 정지 권장</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    from config import Config
    print(build_calendar_section(Config().HOLDINGS))
