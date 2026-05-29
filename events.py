#!/usr/bin/env python3
"""
이벤트 캘린더 — 보유 종목 실적/배당락일 + 거시 이벤트(FOMC, CPI).
N일 이내 다가오는 이벤트만 추려서 표시.
"""
from concurrent.futures import ThreadPoolExecutor
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


def _ex_div_info(ticker: str) -> dict:
    """배당락일 + 지급일 + 마지막 배당금."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "ex_date":  _safe_date(info.get("exDividendDate")),
            "pay_date": _safe_date(info.get("dividendDate")),
            "amount":   float(info["lastDividendValue"]) if info.get("lastDividendValue") else None,
        }
    except Exception:
        return {"ex_date": None, "pay_date": None, "amount": None}


def _ex_div_date(ticker: str) -> date | None:
    return _ex_div_info(ticker)["ex_date"]


def _ticker_events(ticker: str) -> dict:
    """단일 종목 실적·배당 fetch (병렬 호출용)."""
    div = _ex_div_info(ticker)
    return {
        "ticker":       ticker,
        "earnings":     _earnings_date(ticker),
        "div_date":     div["ex_date"],
        "div_pay_date": div["pay_date"],
        "div_amount":   div["amount"],
    }


def _fetch_holdings_events(holdings: dict[str, float]) -> list[dict]:
    tickers = [t for t, q in holdings.items() if q >= 0.01]
    if not tickers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as ex:
        return list(ex.map(_ticker_events, tickers))


def collect_events(holdings: dict[str, float], days_ahead: int = 14) -> list[tuple]:
    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    items = []

    for ev in _fetch_holdings_events(holdings):
        t = ev["ticker"]
        if ev["earnings"] and today <= ev["earnings"] <= horizon:
            items.append((ev["earnings"], "earnings", f"📊 {t} 실적 발표"))
        if ev["div_date"] and today <= ev["div_date"] <= horizon:
            amt = f" ${ev['div_amount']:.4f}" if ev["div_amount"] else ""
            items.append((ev["div_date"], "dividend", f"💰 {t} 배당락{amt}"))

    for s in FOMC_DATES_2026:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        if today <= d <= horizon:
            items.append((d, "fomc", "🏦 FOMC 회의"))

    for s in CPI_DATES_2026:
        d = datetime.strptime(s, "%Y-%m-%d").date()
        if today <= d <= horizon:
            items.append((d, "cpi", "📈 CPI 발표"))

    items.sort(key=lambda x: x[0])
    return items


def get_dividend_schedule(holdings: dict[str, float], days_ahead: int = 90) -> list[dict]:
    """보유 종목 다가오는 배당 일정 (배당락 + 지급일 + 수량×금액).

    표시 기준:
    - 미래 배당락: 오늘~days_ahead 이내
    - 배당락 지났지만 입금 전: ex_date ≤ today AND pay_date > today (단, ex_date가 45일 이내여야 함)
    - 최근 입금 완료: pay_date가 지난 14일 이내
    """
    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    stale_cutoff = today - timedelta(days=45)  # 45일 넘은 ex_date는 무시
    paid_cutoff  = today - timedelta(days=14)  # 최근 14일 입금분 표시

    out = []
    for ev in _fetch_holdings_events(holdings):
        ex  = ev["div_date"]
        pay = ev["div_pay_date"]
        if not ex or ex < stale_cutoff:
            continue

        # 미래 배당락
        upcoming_ex = today <= ex <= horizon
        # 배당락 지났지만 입금 아직 (pay_date > today)
        pending_pay = (ex < today) and (pay is not None) and (pay > today)
        # 최근 입금 완료 (pay_date가 지난 14일 이내)
        recent_paid = (pay is not None) and (paid_cutoff <= pay <= today)

        if not (upcoming_ex or pending_pay or recent_paid):
            continue

        qty = holdings.get(ev["ticker"], 0) or 0
        per_share = ev["div_amount"]
        total = (per_share * qty) if (per_share and qty) else None

        status = "paid" if recent_paid else ("pending" if pending_pay else "upcoming")
        out.append({
            "ticker":        ev["ticker"],
            "ex_date":       ex,
            "pay_date":      pay,
            "amount":        per_share,
            "qty":           qty,
            "total":         total,
            "ex_days_left":  (ex - today).days,
            "pay_days_left": (pay - today).days if pay else None,
            "status":        status,
        })
    out.sort(key=lambda x: x["ex_date"])
    return out


def get_upcoming_dividends(holdings: dict[str, float], days_ahead: int = 7) -> list[dict]:
    """보유 종목 중 days_ahead일 이내 배당락일 + 배당금. 병렬 fetch."""
    today = datetime.now().date()
    horizon = today + timedelta(days=days_ahead)
    out = []
    for ev in _fetch_holdings_events(holdings):
        d = ev["div_date"]
        if d and today <= d <= horizon:
            out.append({
                "ticker":    ev["ticker"],
                "date":      d,
                "amount":    ev["div_amount"],
                "days_left": (d - today).days,
            })
    out.sort(key=lambda x: x["date"])
    return out


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
        lines.append("  <i>⚠️ 거시 이벤트 있음 — 신규 매수 신중히</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    from config import Config
    print(build_calendar_section(Config().HOLDINGS))
