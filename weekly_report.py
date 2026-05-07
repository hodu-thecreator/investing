#!/usr/bin/env python3
"""
주간 마감 리포트 — 토요일 1회.

내용:
  1) 보유 종목 1주일 수익률
  2) 거시 지표 1주일 변동 (VIX, F&G, 위험점수)
  3) 이번주 거래 요약 (매수/매도 횟수, 실현 손익)
  4) 다음주 주요 이벤트 5일치 미리보기
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

from config import Config
from market_indicators import collect_all
from telegram_notifier import send_message
from events import collect_events
from transactions import _load as _load_transactions
from rebalancing import build_rebalance_section

_config = Config()


def weekly_return(ticker: str) -> dict | None:
    """5거래일 수익률 (1주일 ≈ 5거래일)."""
    try:
        df = yf.Ticker(ticker).history(period="10d")
        if df is None or df.empty or len(df) < 2:
            return None
        close = df["Close"].squeeze()
        current = float(close.iloc[-1])
        # 5거래일 전 종가 (없으면 가장 오래된 것)
        idx = -6 if len(close) >= 6 else 0
        prev = float(close.iloc[idx])
        ret_pct = (current - prev) / prev * 100
        return {"current": current, "prev": prev, "return_pct": ret_pct}
    except Exception:
        return None


def this_week_trades() -> dict:
    """이번주(7일) 거래 요약."""
    cutoff = datetime.now() - timedelta(days=7)
    records = _load_transactions()
    buys, sells, realized = 0, 0, 0.0

    # 평단 추적용 (실현 손익 계산)
    holdings: dict[str, dict] = {}
    for r in records:
        t = r["ticker"]
        h = holdings.setdefault(t, {"qty": 0.0, "cost": 0.0})
        # 거래 일자 파싱
        try:
            d = datetime.fromisoformat(r.get("ts", r["date"]))
        except Exception:
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d")
            except Exception:
                d = datetime.now()
        is_recent = d >= cutoff

        if r["type"] == "buy":
            if is_recent:
                buys += 1
            h["qty"] += r["qty"]
            h["cost"] += r["qty"] * r["price"]
        else:
            avg = h["cost"] / h["qty"] if h["qty"] > 0 else r["price"]
            pnl = r["qty"] * (r["price"] - avg)
            if is_recent:
                sells += 1
                realized += pnl
            h["qty"] -= r["qty"]
            h["cost"] -= r["qty"] * avg
            if h["qty"] < 1e-6:
                h["qty"] = 0
                h["cost"] = 0

    return {"buys": buys, "sells": sells, "realized": round(realized, 2)}


def build_weekly_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d")
    lines = [f"<b>📅 주간 마감 리포트</b>  {now}"]
    lines.append("━" * 28)

    indicators = collect_all()

    # ── 거시 지표 1주일 변동 ─────────────────────────────────────
    lines.append("\n<b>🌍 거시 지표 (1주 변동)</b>")
    fg = indicators.get("fear_greed", {})
    if not fg.get("error") and fg.get("week_ago"):
        diff = fg["score"] - fg["week_ago"]
        arrow = "↑" if diff > 0 else "↓"
        lines.append(
            f"  공포/탐욕  <b>{fg['score']}</b> {fg.get('rating','')} "
            f"({arrow}{abs(diff):.0f} vs 1주 전 {fg['week_ago']})"
        )
    vix = indicators.get("vix", {})
    if not vix.get("error"):
        lines.append(f"  VIX        <b>{vix['current']}</b>  {vix['level']}")
    krw = indicators.get("usd_krw", {})
    if not krw.get("error") and krw.get("change_pct") is not None:
        arrow = "↑" if krw["change_pct"] > 0 else "↓"
        lines.append(
            f"  USD/KRW    ₩{krw['usd_to_krw']:,.2f}  {arrow}{abs(krw['change_pct']):.2f}%"
        )

    # ── 보유 종목 주간 수익률 ────────────────────────────────────
    lines.append("\n<b>💼 보유 종목 주간 수익률</b>")
    rows = []
    for ticker, qty in _config.HOLDINGS.items():
        if not qty or qty <= 0:
            continue
        wr = weekly_return(ticker)
        if not wr:
            continue
        rows.append((ticker, wr))

    rows.sort(key=lambda x: -x[1]["return_pct"])
    if rows:
        for ticker, wr in rows:
            emoji = "🟢" if wr["return_pct"] >= 0 else "🔴"
            sign = "+" if wr["return_pct"] >= 0 else ""
            lines.append(
                f"  {emoji} <b>{ticker}</b>  ${wr['current']:.2f}  "
                f"<b>{sign}{wr['return_pct']:.2f}%</b>"
            )

        avg_ret = sum(r[1]["return_pct"] for r in rows) / len(rows)
        avg_emoji = "🟢" if avg_ret >= 0 else "🔴"
        lines.append(f"\n  평균 수익률  {avg_emoji} <b>{avg_ret:+.2f}%</b>")
    else:
        lines.append("  데이터 없음")

    # ── 이번주 거래 요약 ──────────────────────────────────────────
    trades = this_week_trades()
    lines.append("\n<b>📜 이번주 거래</b>")
    if trades["buys"] == 0 and trades["sells"] == 0:
        lines.append("  거래 없음")
    else:
        lines.append(f"  매수 {trades['buys']}회  매도 {trades['sells']}회")
        if trades["sells"] > 0:
            emoji = "🟢" if trades["realized"] >= 0 else "🔴"
            lines.append(f"  실현 손익  {emoji} <b>${trades['realized']:+,.2f}</b>")

    # ── 리밸런싱 점검 ──────────────────────────────────────────────
    try:
        rebal = build_rebalance_section()
        if rebal:
            lines.append("\n" + "━" * 28)
            lines.append(rebal)
    except Exception as e:
        print(f"[weekly] rebalance 오류: {e}")

    # ── 다음주 이벤트 5일 미리보기 ────────────────────────────────
    upcoming = collect_events(_config.HOLDINGS, days_ahead=5)
    if upcoming:
        lines.append("\n<b>📅 다음주 주요 일정 (5일)</b>")
        for d, kind, label in upcoming[:8]:
            lines.append(f"  {d:%m/%d}  {label}")

    lines.append("\n" + "━" * 28)
    lines.append("🤖 <i>주간 마감 — 매주 토요일 자동 발송</i>")
    return "\n".join(lines)


def main():
    if os.getenv("FORCE_WEEKLY") != "1":
        # 토요일에만 발송 (cron 보호용 추가 가드)
        if datetime.now().weekday() != 5:  # 5 = Saturday
            print(f"[weekly] 토요일 아님 — 스킵 ({datetime.now():%A})")
            return

    report = build_weekly_report()
    print(report)
    ok = send_message(report)
    print(f"[weekly] 발송 {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
