#!/usr/bin/env python3
"""
Daily Report Orchestrator
매일 오전 8시에 포트폴리오 + 시장 심리 + 경제 지표를 텔레그램으로 전송합니다.

실행 방법:
  python daily_report.py           # 지금 즉시 한 번 전송
  python daily_report.py --daemon  # 백그라운드 스케줄러 (매일 08:00)
  python daily_report.py --test    # 텔레그램 전송 없이 콘솔에만 출력
"""

import argparse
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import yfinance as yf
from stock_agent import (
    PORTFOLIO,
    ALERT_THRESHOLDS,
    MA_PERIODS,
    fetch_stock_data,
    calc_moving_averages,
    calc_drawdown_from_high,
    ma_position_label,
    get_analyst_target,
)
from market_indicators import collect_all
from telegram_notifier import send_message

# ── 포매터 헬퍼 ──────────────────────────────────────────────────

def _emo_fg(score: float) -> str:
    if score >= 75: return "🟢"
    if score >= 55: return "🟡"
    if score >= 35: return "🟠"
    return "🔴"

def _emo_vix(v: float) -> str:
    if v >= 30: return "🔴"
    if v >= 20: return "🟠"
    if v >= 15: return "🟡"
    return "🟢"

def _emo_dd(dd: float) -> str:
    if dd <= -15: return "🔴"
    if dd <= -10: return "🟠"
    if dd <= -5:  return "🟡"
    return "🟢"

def _val(d: dict, key: str, fmt: str = ".2f", unit: str = "") -> str:
    v = d.get(key)
    if v is None: return "N/A"
    return f"{v:{fmt}}{unit}"

# ── 보고서 빌더 ──────────────────────────────────────────────────

def build_report() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []

    lines.append(f"<b>📊 투자 일일 브리핑</b>  {now}")
    lines.append("━" * 32)

    # ─ 1. 시장 심리 지표 ─────────────────────────────────────────
    lines.append("\n<b>[ 시장 심리 ]</b>")

    indicators = collect_all()

    fg = indicators.get("fear_greed", {})
    if not fg.get("error"):
        score = fg["score"]
        emo = _emo_fg(score)
        lines.append(
            f"{emo} CNN 공포/탐욕: <b>{score}</b> ({fg['rating']})"
            f"  ┃   1주전 {fg['week_ago']} / 1달전 {fg['month_ago']}"
        )
    else:
        lines.append(f"⚪ CNN 공포/탐욕: {fg.get('error', '오류')}")

    vix = indicators.get("vix", {})
    if not vix.get("error"):
        cv = vix["current"]
        chg = vix["change"]
        sign = "+" if chg >= 0 else ""
        emo = _emo_vix(cv)
        lines.append(f"{emo} VIX: <b>{cv}</b> ({sign}{chg})  ┃  {vix['level']}")
    else:
        lines.append(f"⚪ VIX: {vix.get('error', '오류')}")

    pc = indicators.get("put_call", {})
    if not pc.get("error"):
        lines.append(
            f"📉 Put/Call: <b>{pc['current']}</b>  ┃  {pc['level']}"
            f"  <i>({pc.get('note','')})</i>"
        )
    else:
        lines.append(f"⚪ Put/Call: {pc.get('error', '오류')}")

    aaii = indicators.get("aaii", {})
    if not aaii.get("error"):
        bull = aaii.get("bullish")
        bear = aaii.get("bearish")
        neu  = aaii.get("neutral")
        date = aaii.get("date", "")
        if bull is not None:
            lines.append(
                f"📋 AAII 심리 ({date}): "
                f"강세 <b>{bull:.1f}%</b>  중립 {neu:.1f}%  약세 {bear:.1f}%"
            )
    else:
        lines.append(f"⚪ AAII: {aaii.get('error', '오류')}")

    # ─ 2. 경제 지표 (FRED) ───────────────────────────────────────
    lines.append("\n<b>[ 경제 지표 ]</b>")

    def _fred_line(label: str, data: dict, unit: str = "", fmt: str = ".2f") -> str:
        if data.get("error"):
            return f"⚪ {label}: {data['error']}"
        val = data.get("value")
        chg = data.get("change")
        dt  = data.get("date", "")
        if val is None:
            return f"⚪ {label}: N/A"
        chg_str = f"  (Δ{chg:+.3g}{unit})" if chg is not None else ""
        return f"• {label}: <b>{val:{fmt}}{unit}</b>{chg_str}  <i>{dt}</i>"

    lines.append(_fred_line("연방기금금리", indicators["fed_rate"], unit="%"))
    lines.append(_fred_line("JOLTS 구인건수", indicators["jolts"], unit="K", fmt=".0f"))
    lines.append(_fred_line("소비자물가(CPI)", indicators["cpi"]))
    lines.append(_fred_line("소비자심리(미시간)", indicators["consumer_sentiment"], fmt=".1f"))
    lines.append(_fred_line("소비자신뢰(CB)", indicators["consumer_confidence"], fmt=".1f"))
    lines.append(_fred_line("마진부채", indicators["margin_debt"], unit="B", fmt=".1f"))

    # ─ 3. 포트폴리오 현황 ────────────────────────────────────────
    lines.append("\n<b>[ 포트폴리오 현황 ]</b>")
    lines.append("<pre>티커     현재가   52주고점  하락률</pre>")

    buy_alerts = []

    for ticker in PORTFOLIO:
        df = fetch_stock_data(ticker)
        if df.empty:
            lines.append(f"<code>{ticker:<6}  데이터 없음</code>")
            continue

        dd   = calc_drawdown_from_high(df)
        mas  = calc_moving_averages(df)
        curr = dd.get("current", 0)
        high = dd.get("high_52w", 0)
        draw = dd.get("drawdown_pct", 0)
        emo  = _emo_dd(draw)

        ma_lbl = ma_position_label(curr, mas)
        lines.append(
            f"{emo} <b>{ticker}</b>  ${curr:.2f}"
            f"  /  고점 ${high:.2f}  /  <b>{draw:+.1f}%</b>"
        )
        lines.append(f"   <i>MA: {ma_lbl}</i>")

        # 알림 임계치 체크
        for thr in ALERT_THRESHOLDS:
            if draw <= thr:
                buy_alerts.append(
                    f"🔔 <b>{ticker}</b> 고점 대비 {abs(thr)}% 이상 하락 → 분할매수 검토"
                )
                break

    # ─ 4. 매수 타이밍 알림 ───────────────────────────────────────
    if buy_alerts:
        lines.append("\n<b>[ 매수 타이밍 알림 ]</b>")
        lines.extend(buy_alerts)

    # ─ 5. 참고 링크 ──────────────────────────────────────────────
    lines.append("\n<b>[ 참고 링크 ]</b>")
    lines.append(
        "• <a href='https://edition.cnn.com/markets/fear-and-greed'>CNN 공포/탐욕</a>"
        "  • <a href='https://en.macromicro.me/charts/449/us-cboe-options-put-call-ratio'>Put/Call 비율</a>"
    )
    lines.append(
        "• <a href='https://www.aaii.com/sentimentsurvey'>AAII 심리</a>"
        "  • <a href='https://www.gurufocus.com/economic_indicators/4264/finra-investor-margin-debt'>마진부채</a>"
    )
    lines.append(
        "• <a href='https://www.isabelnet.com/blog/'>IsabelNet 블로그</a>"
        "  • <a href='https://finance.yahoo.com/quote/%5EVIX/'>VIX</a>"
    )

    lines.append("\n━" * 16)
    lines.append("🤖 <i>Stock Agent — 매일 오전 8시 자동 발송</i>")

    return "\n".join(lines)


# ── 스케줄러 ─────────────────────────────────────────────────────

def run_once(test_mode: bool = False):
    print(f"[{datetime.now():%H:%M:%S}] 보고서 생성 중...")
    report = build_report()
    if test_mode:
        # 콘솔 출력 (HTML 태그 제거)
        import re
        clean = re.sub(r"<[^>]+>", "", report)
        print(clean)
    else:
        ok = send_message(report)
        print(f"[{datetime.now():%H:%M:%S}] 텔레그램 전송 {'✅ 성공' if ok else '❌ 실패'}")


def run_daemon(hour: int = 8, minute: int = 0):
    import schedule

    schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(run_once)
    print(f"스케줄러 시작 — 매일 {hour:02d}:{minute:02d} 전송 (Ctrl+C로 종료)")

    # 오늘 아직 발송 전이면 즉시 한 번 실행
    now = datetime.now()
    if now.hour < hour or (now.hour == hour and now.minute < minute):
        pass  # 오늘 발송 시간 대기
    else:
        print("오늘 발송 시간이 지났습니다. 내일부터 자동 발송됩니다.")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── 진입점 ───────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="투자 일일 브리핑 전송")
    parser.add_argument("--daemon", action="store_true", help="매일 08:00 자동 전송 모드")
    parser.add_argument("--test",   action="store_true", help="텔레그램 없이 콘솔 출력")
    parser.add_argument("--hour",   type=int, default=8,  help="발송 시각 (시, 기본 8)")
    parser.add_argument("--minute", type=int, default=0,  help="발송 시각 (분, 기본 0)")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(hour=args.hour, minute=args.minute)
    else:
        run_once(test_mode=args.test)
