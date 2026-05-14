#!/usr/bin/env python3
"""
아침 마감 요약 — 매일 한국 시간 08:00 (Tue-Sat).

미국장 마감 후 결과를 한 페이지로 압축.
저녁 23:00 daily_report와는 별개로 동작.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from config import Config
from telegram_notifier import send_message
from market_indicators import (
    get_vix, get_usd_krw, get_nzd_rate, get_nzd_krw_cross, format_change_chip,
)
from events import collect_events, get_upcoming_dividends
import ibkr_flex

_config = Config()

INDICES = [
    ("SPY",  "S&P500"),
    ("QQQ",  "나스닥100"),
    ("DIA",  "다우"),
    ("IWM",  "러셀2000"),
]


def daily_change(ticker: str) -> dict | None:
    """전일 종가 대비 변동."""
    try:
        df = yf.Ticker(ticker).history(period="5d")
        if df is None or df.empty or len(df) < 2:
            return None
        close = df["Close"].squeeze()
        cur = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        pct = (cur - prev) / prev * 100
        return {"price": cur, "change_pct": pct, "prev": prev}
    except Exception:
        return None


def build_morning_recap() -> str:
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%-m/%-d %a")
    lines = [f"<b>🌅 미국 장 마감 요약</b>  {today} 아침"]
    lines.append("━" * 28)

    _holdings, _, _ibkr = ibkr_flex.resolve_holdings_and_cash(_config)
    if _ibkr["error"] is None and _ibkr["positions"]:
        acct_section = ibkr_flex.build_account_section(_ibkr["positions"], _ibkr["cash_usd"])
        if acct_section:
            lines.append("")
            lines.append(acct_section)

    # ── 주요 지수 ────────────────────────────────────────────────
    lines.append("")
    lines.append("<b>📊 주요 지수</b>")
    for sym, label in INDICES:
        d = daily_change(sym)
        if not d:
            continue
        emoji = "🟢" if d["change_pct"] >= 0 else "🔴"
        sign = "+" if d["change_pct"] >= 0 else ""
        lines.append(
            f"  {emoji} <b>{label:<10}</b>  ${d['price']:>7.2f}  "
            f"<b>{sign}{d['change_pct']:.2f}%</b>"
        )

    # VIX
    vix = get_vix()
    if not vix.get("error"):
        chg = vix.get("change", 0)
        emoji = "🔴" if chg > 0 else "🟢"
        lines.append(f"  {emoji} <b>{'VIX':<10}</b>  {vix['current']:>8}  ({chg:+.2f})")

    # ── 보유 종목 큰 변동 ────────────────────────────────────────
    movers = []
    for ticker, qty in _holdings.items():
        if not qty or qty <= 0:
            continue
        d = daily_change(ticker)
        if not d or abs(d["change_pct"]) < 3.0:
            continue
        movers.append((ticker, d))

    if movers:
        movers.sort(key=lambda x: x[1]["change_pct"], reverse=True)
        lines.append("")
        lines.append("<b>💼 내 보유 종목 큰 변동 (±3%+)</b>")
        for ticker, d in movers:
            emoji = "🟢" if d["change_pct"] >= 0 else "🔴"
            sign = "+" if d["change_pct"] >= 0 else ""
            lines.append(
                f"  {emoji} <b>{ticker}</b>  ${d['price']:.2f}  "
                f"<b>{sign}{d['change_pct']:.2f}%</b>"
            )

    # ── 환율 ──────────────────────────────────────────────────────
    krw = get_usd_krw()
    nzd = get_nzd_rate()
    has_krw = not krw.get("error") and krw.get("usd_to_krw")
    has_nzd = not nzd.get("error") and nzd.get("usd_to_nzd")
    if has_krw or has_nzd:
        lines.append("")
        lines.append("<b>💱 환율</b>")
    if has_krw:
        lines.append(f"  USD/KRW  ₩{krw['usd_to_krw']:,.2f}{format_change_chip(krw.get('change_pct'))}")
    if has_nzd:
        lines.append(f"  USD/NZD  NZ${nzd['usd_to_nzd']:.4f}{format_change_chip(nzd.get('change_pct'))}")
    cross = get_nzd_krw_cross(krw, nzd) if (has_krw and has_nzd) else None
    if cross:
        lines.append(f"  NZD/KRW  ₩{cross['nzd_to_krw']:,.2f}{format_change_chip(cross.get('change_pct'))}")

    # ── 오늘 일정 (24시간 이내) ──────────────────────────────────
    upcoming = collect_events(_holdings, days_ahead=1)
    if upcoming:
        lines.append("")
        lines.append("<b>📅 오늘 일정</b>")
        for d, kind, label in upcoming[:5]:
            lines.append(f"  • {label}")

    # ── 배당락일 임박 (7일 이내) ─────────────────────────────────
    divs = get_upcoming_dividends(_holdings, days_ahead=7)
    if divs:
        lines.append("")
        lines.append("<b>💰 배당락일 임박</b>")
        for d in divs:
            timing = "오늘" if d["days_left"] == 0 else f"{d['days_left']}일 후"
            amt = f"  ${d['amount']:.4f}" if d["amount"] else ""
            lines.append(f"  💵 <b>{d['ticker']}</b>  배당락 {timing} ({d['date']}){amt}")

    lines.append("")
    lines.append("━" * 28)
    lines.append("🤖 <i>아침 요약 — 평일 08:00 KST</i>")
    return "\n".join(lines)


def main():
    if os.getenv("FORCE_MORNING") != "1":
        # KST 기준 화~토만 발송 (월~금 미국장 마감 후)
        kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if kst.weekday() == 6:  # 일요일
            print(f"[morning] 일요일 — 스킵")
            return

    report = build_morning_recap()
    print(report)
    ok = send_message(report)
    print(f"[morning] 발송 {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
