#!/usr/bin/env python3
"""
장중 급락 알림 — bot_polling(매분 실행)에 끼워서 동작.

미국장 시간(9:30~16:00 ET) 동안 SPY/QQQ가 전일 종가 대비 2/3/5% 이상 하락하면
텔레그램 알림. ATH 누적 낙폭(헌법 6조 트리거) 기준으로 "지금 할 일"을
한 줄·달러 단위로 명확히 제시 — 자다가 폰 진동으로 깨도 바로 판단 가능.
반등 후 재하락 시 같은 단계 재알림(새 매수타점), 1일 최대 6회.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from telegram_notifier import send_message

INDICES = [("SPY", "S&P500"), ("QQQ", "나스닥100")]
DROP_TIERS = [-5, -3, -2]
COOLDOWN_SEC = 600
INDEX_THRESHOLD = -2.0
HOLDING_THRESHOLD = -3.0
# 반등 후 재하락 시 재알림: 단계 기준보다 이만큼 회복하면 해당 단계 재무장
REARM_RECOVERY_PCT = 1.0
MAX_ALERTS_PER_DAY = 6  # 스팸 방지 상한


def _is_market_hours() -> bool:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 570 <= minutes <= 960  # 9:30 ~ 16:00 ET


def _intraday_change(ticker: str) -> tuple[float | None, float | None]:
    try:
        t = yf.Ticker(ticker)
        daily = t.history(period="2d", interval="1d")
        if len(daily) < 2:
            return None, None
        prev_close = float(daily["Close"].iloc[-2])
        intraday = t.history(period="1d", interval="5m")
        if intraday.empty:
            return None, None
        current = float(intraday["Close"].iloc[-1])
        return current, (current - prev_close) / prev_close * 100
    except Exception:
        return None, None


def _prune_old_keys(state: dict):
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    for key in list(state.keys()):
        if key.startswith("intraday_") and key not in (f"intraday_{today}", "intraday_last_check"):
            del state[key]


def _ath_trigger_status() -> dict | None:
    """S&P500 ATH 대비 낙폭 + 현재 도달한 헌법 6조 트리거 판정."""
    try:
        df = yf.Ticker("SPY").history(period="5y")
        if df.empty:
            return None
        close = df["Close"]
        current = float(close.iloc[-1])
        ath = float(close.max())
        dd = (current - ath) / ath * 100 if ath else 0.0

        from config import Config
        triggers = Config.CORRECTION_TRIGGERS
        active = None
        for tr in triggers:
            if dd <= tr["drop"]:
                active = tr
        return {"current": current, "ath": ath, "drawdown": dd,
                "active": active, "triggers": triggers}
    except Exception as e:
        print(f"[intraday] ATH status fetch failed: {e}")
        return None


def _decide_action(ath: dict | None, holdings: dict, idle_cash: float) -> tuple[str, str]:
    """ATH 트리거 상태 → (행동 한 줄, 상세 한 줄). 항상 명확한 단일 행동을 반환."""
    if ath is None:
        return "자동투자만 유지", "ATH 데이터 조회 실패 — /report 로 확인"

    dd = ath["drawdown"]
    active = ath["active"]

    if active is None:
        nxt = ath["triggers"][0]
        gap = dd - nxt["drop"]
        return (
            "자동투자만 유지 — 추가 행동 없음",
            f"ATH 대비 {dd:+.1f}%  ·  다음 트리거({nxt['drop']}%)까지 {gap:.1f}%p 남음",
        )

    from config import Config

    sgov_qty = (holdings or {}).get("SGOV", 0) or 0
    sgov_price = 0.0
    if sgov_qty > 0:
        price, _ = _intraday_change("SGOV")
        sgov_price = price or 0.0
    total_cash = sgov_qty * sgov_price + (idle_cash or 0)

    if active["action"] == "all-in":
        usable = max(0.0, total_cash - Config.EMERGENCY_FUND_USD)
        return (
            f"비상금 외 전액 ${usable:,.0f} 발사 → QQQM/SPYM 50:50 매수",
            f"ATH 대비 {dd:+.1f}%  ·  -30% 트리거 도달 (역대급 구간)",
        )

    fire_amt = total_cash * active["fire"]
    core_each = fire_amt / 2
    headline = f"QQQM ${core_each:,.0f} + SPYM ${core_each:,.0f} 매수"
    detail = (
        f"ATH 대비 {dd:+.1f}%  ·  {active['drop']}% 트리거 도달 "
        f"→ SGOV 탄약 {active['fire']*100:.0f}%(${fire_amt:,.0f}) 발사"
    )
    if active["lev"]:
        detail += f"  ·  +레버리지 {'/'.join(active['lev'])}(/report 캡 확인)"
    return headline, detail


def _build_alert(idx_drops: list, hold_drops: list, tier: int,
                 holdings: dict, idle_cash: float, alert_no: int = 1) -> str:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M KST")
    now_et  = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    sev = "🟢🟢" if tier <= -5 else "🟢" if tier <= -3 else "🟡"
    no_tag = f"  (오늘 {alert_no}번째)" if alert_no > 1 else ""

    ath = _ath_trigger_status()
    headline, detail = _decide_action(ath, holdings, idle_cash)

    lines = [f"<b>{sev} 조정 알림</b>  {now_kst} ({now_et}){no_tag}"]
    lines.append("━" * 28)
    lines.append(f"\n👉 <b>지금 할 일: {headline}</b>")
    lines.append(f"<i>{detail}</i>")
    if alert_no > 1:
        lines.append("<i>↩️ 반등 후 재하락 — 새 매수타점 구간</i>")

    lines.append("\n" + "━" * 28)
    idx_str = "  ·  ".join(f"{name} <b>{chg:+.1f}%</b>" for _, name, _, chg in idx_drops)
    lines.append(f"📊 오늘 일중  {idx_str}")

    if hold_drops:
        hold_str = "  ·  ".join(f"{t} {chg:+.1f}%" for t, _, chg in hold_drops)
        lines.append(f"💼 보유 중 -3%+  {hold_str}")

    lines.append("\n<i>떨어진 게 아니라 싸진 거예요. 매도 안 함. 1일 1회 이상 확인 금지.</i>")
    return "\n".join(lines)


def check_and_alert(state: dict, holdings: dict, idle_cash: float = 0.0) -> bool:
    """급락 감지 + 알림. state는 inline mutate. 알림 발송 시 True."""
    if not _is_market_hours():
        return False

    now_ts = time.time()
    if now_ts - state.get("intraday_last_check", 0) < COOLDOWN_SEC:
        return False
    state["intraday_last_check"] = now_ts
    _prune_old_keys(state)

    # 지수 변동 조회 — 재무장 판정을 위해 하락 여부와 무관하게 전부 수집
    changes = []
    idx_drops = []
    for sym, name in INDICES:
        price, chg = _intraday_change(sym)
        if chg is None:
            continue
        changes.append(chg)
        if chg <= INDEX_THRESHOLD:
            idx_drops.append((sym, name, price, chg))

    if not changes:
        return False
    worst = min(changes)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    alerted_key = f"intraday_{today}"
    rec = state.get(alerted_key)
    if isinstance(rec, list):           # 구버전 포맷 호환
        rec = {"tiers": rec, "count": len(rec)}
    elif not isinstance(rec, dict):
        rec = {"tiers": [], "count": 0}

    # 재무장: 단계 기준보다 REARM_RECOVERY_PCT 이상 회복하면 같은 단계 재알림 허용
    # 예: -2% 알림 후 -0.9%까지 반등 → 다시 -2% 떨어지면 새 매수타점으로 재알림
    rec["tiers"] = [t for t in rec["tiers"] if worst <= t + REARM_RECOVERY_PCT]
    state[alerted_key] = rec

    if not idx_drops:
        return False
    tier = next((t for t in DROP_TIERS if worst <= t), None)
    if tier is None:
        return False
    if tier in rec["tiers"]:
        return False
    if rec["count"] >= MAX_ALERTS_PER_DAY:
        return False

    hold_drops = []
    for ticker, qty in (holdings or {}).items():
        if not qty or qty <= 0:
            continue
        price, chg = _intraday_change(ticker)
        if chg is not None and chg <= HOLDING_THRESHOLD:
            hold_drops.append((ticker, price, chg))
    hold_drops.sort(key=lambda x: x[2])

    msg = _build_alert(idx_drops, hold_drops, tier, holdings, idle_cash, alert_no=rec["count"] + 1)
    if not send_message(msg):
        return False

    rec["tiers"].append(tier)
    rec["count"] += 1
    state[alerted_key] = rec
    return True
