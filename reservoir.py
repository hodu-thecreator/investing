#!/usr/bin/env python3
"""
저수지 수위 모니터 (헌법 6조 저수지 매수 가이드, 2026.6 개정).

종목별 52주 고점 대비 낙폭을 웅덩이 구간으로 분류해
"지금 어디가 얼마나 싸졌고, 뭘 하면 되는지"를 보여준다.

역할 분담 (헌법 6조 S&P 트리거와 충돌 방지):
  - 얼마 쏠지  → S&P500 ATH 트리거 (SGOV 탄약 비율)
  - 어디에 쏠지 → 이 모듈의 종목별 수위 (가장 깊은 웅덩이부터, 코어 → 위성 순)

용도: /dip 명령 + 일일 리포트 섹션 + 장중 구간 진입 알림 (bot_once 폴링).
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Config

_config = Config()

CHECK_COOLDOWN_SEC = 900          # 폴링 알림 점검 주기 (15분)
MAX_ZONE_ALERTS_PER_DAY = 4

# zone index: 0 = 만수위, 1~ = RESERVOIR_ZONES 순서 (깊을수록 큼)
_FULL_LABEL = "☀️ 만수위"


def classify(dd_pct: float, scale: float = 1.0) -> tuple[int, dict | None]:
    """낙폭(%) → (zone index, zone dict). 만수위면 (0, None).

    scale: 변동성 보정 — IBIT처럼 출렁임이 큰 자산은 같은 의미의
    낙폭이 더 깊다고 보고 기준선을 scale배로 늘림.
    """
    idx, hit = 0, None
    for i, z in enumerate(_config.RESERVOIR_ZONES, start=1):
        if dd_pct <= z["dd"] * scale:
            idx, hit = i, z
    return idx, hit


def zone_label(idx: int) -> str:
    if idx <= 0:
        return _FULL_LABEL
    return _config.RESERVOIR_ZONES[idx - 1]["label"]


def zone_action(idx: int) -> str:
    if idx <= 0:
        return "정기 매수만 — 추격 매수 금지"
    return _config.RESERVOIR_ZONES[idx - 1]["action"]


def fetch_levels(tickers: list[str] | None = None) -> list[dict]:
    """종목별 (현재가, 52주 고점, 낙폭, zone). 조회 실패 종목은 생략."""
    import yfinance as yf
    tickers = tickers or _config.RESERVOIR_WATCH
    out = []
    for t in tickers:
        try:
            df = yf.Ticker(t).history(period="1y")
            if df is None or df.empty:
                continue
            close = df["Close"]
            price = float(close.iloc[-1])
            high = float(close.max())
            dd = (price - high) / high * 100 if high else 0.0
            scale = _config.RESERVOIR_SCALE.get(t, 1.0)
            idx, _zone = classify(dd, scale)
            out.append({
                "ticker": t, "price": price, "high": high,
                "dd": dd, "scale": scale, "zone_idx": idx,
            })
        except Exception as e:
            print(f"[reservoir] {t} 조회 실패: {e}")
    # 깊은 웅덩이(=매수 우선순위) 순으로 정렬
    out.sort(key=lambda x: (-x["zone_idx"], x["dd"]))
    return out


def _gauge(dd: float, scale: float) -> str:
    """수위 게이지 — 가득 찬 바 = 52주 고점, 댐 바닥 기준까지 비워짐."""
    floor = _config.RESERVOIR_ZONES[-1]["dd"] * scale   # 예: -25 (IBIT는 -75)
    level = max(0.0, 1.0 + dd / abs(floor))             # dd=0 → 1.0, dd=floor → 0.0
    filled = round(level * 8)
    return "▓" * filled + "░" * (8 - filled)


def _satellite_note(ticker: str, state: dict | None) -> str:
    """위성 종목이면 상한 대비 현재 비중 노트."""
    cap = _config.SATELLITE_TICKERS.get(ticker)
    if cap is None:
        return ""
    if not state or not state.get("total"):
        return f"  <i>위성 · 상한 {cap*100:.0f}%</i>"
    cur = (state.get("ticker_values", {}).get(ticker, 0)) / state["total"]
    room = "여유 있음" if cur < cap else "상한 도달 — 매수 금지"
    return f"  <i>위성 {cur*100:.1f}%/{cap*100:.0f}% · {room}</i>"


def build_reservoir_section(state: dict | None = None) -> str:
    """일일 리포트 + /dip 섹션."""
    levels = fetch_levels()
    if not levels:
        return ""

    lines = ["<b>🏞 저수지 수위</b>  <i>(52주 고점 대비 — 어디에 쏠지)</i>"]
    for lv in levels:
        scale_tag = f" (×{lv['scale']:g} 보정)" if lv["scale"] != 1.0 else ""
        lines.append(
            f"  {_gauge(lv['dd'], lv['scale'])}  <b>{lv['ticker']}</b>"
            f"  {lv['dd']:+.1f}%  {zone_label(lv['zone_idx'])}{scale_tag}"
            f"{_satellite_note(lv['ticker'], state)}"
        )

    deepest = levels[0]
    idle = (state or {}).get("idle_cash", 0) or 0
    if deepest["zone_idx"] > 0:
        lines.append("")
        lines.append(f"  👉 <b>{deepest['ticker']}</b> 우선 — {zone_action(deepest['zone_idx'])}")
        if idle >= _config.IDLE_CASH_ALERT_USD:
            lines.append(f"  💤 노는 돈 <b>${idle:,.0f}</b> — 여기에 먼저 투입")
        lines.append("  <i>얼마 쏠지는 S&P ATH 트리거 기준 (/now 확인)</i>")
    else:
        lines.append("  <i>전 종목 만수위 — 정기 매수만, 추격 금지</i>")
        if idle >= _config.IDLE_CASH_ALERT_USD:
            lines.append(f"  💤 노는 돈 <b>${idle:,.0f}</b> — 대기 OK, 웅덩이 열리면 알림함")
    return "\n".join(lines)


# ── 장중 구간 진입 알림 (bot_once 폴링에서 호출) ──────────────────

def check_zone_alerts(state: dict, holdings: dict | None = None,
                      idle_cash: float = 0.0) -> bool:
    """종목이 더 깊은 웅덩이로 진입하면 알림. state는 inline mutate.

    회복해서 얕은 구간으로 올라오면 기록도 따라 올라감 → 재하락 시 재알림(재무장).
    """
    from intraday_alert import _is_market_hours
    if not _is_market_hours():
        return False

    now_ts = time.time()
    if now_ts - state.get("reservoir_last_check", 0) < CHECK_COOLDOWN_SEC:
        return False
    state["reservoir_last_check"] = now_ts

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    count_key = f"reservoir_count_{today}"
    for k in list(state.keys()):
        if k.startswith("reservoir_count_") and k != count_key:
            del state[k]
    if state.get(count_key, 0) >= MAX_ZONE_ALERTS_PER_DAY:
        return False

    levels = fetch_levels()
    if not levels:
        return False

    rec: dict = state.get("reservoir_zones", {})
    entered = []
    for lv in levels:
        prev = rec.get(lv["ticker"], 0)
        if lv["zone_idx"] > prev:
            entered.append(lv)
        rec[lv["ticker"]] = lv["zone_idx"]     # 회복 시 기록 하향 = 재무장
    state["reservoir_zones"] = rec

    if not entered:
        return False

    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M KST")
    lines = [f"<b>🏞 저수지 진입 알림</b>  {now_kst}", "━" * 28]
    for lv in entered:
        lines.append(
            f"\n💧 <b>{lv['ticker']}</b>  {zone_label(lv['zone_idx'])} 진입"
            f"  (52주 고점 대비 {lv['dd']:+.1f}%)"
        )
        lines.append(f"   → {zone_action(lv['zone_idx'])}")
        note = _satellite_note(lv["ticker"], None)
        if note:
            lines.append(f" {note.strip()}")

    # 왜 떨어지나 — VIX + SPY 당일 등락 한 줄 (정보 과잉 방지: 딱 한 줄만)
    try:
        from market_indicators import get_vix
        from intraday_alert import _intraday_change
        vix = get_vix()
        _, spy_chg = _intraday_change("SPY")
        if "current" in vix and spy_chg is not None:
            lines.append(f"\n📰 SPY {spy_chg:+.1f}%  ·  VIX {vix['current']:.1f} ({vix['level']})")
    except Exception as e:
        print(f"[reservoir] 컨텍스트 조회 실패: {e}")

    # 얼마 쏠지 — 헌법 6조 S&P 트리거 한 줄
    try:
        from intraday_alert import _ath_trigger_status, _decide_action
        ath = _ath_trigger_status()
        headline, detail = _decide_action(ath, holdings or {}, 0.0)
        lines.append(f"\n👉 <b>{headline}</b>")
        lines.append(f"<i>{detail}</i>")
    except Exception as e:
        print(f"[reservoir] 트리거 판정 실패: {e}")

    # 노는 돈 — 모아둔 배당 USD가 있으면 이 웅덩이가 쓸 타이밍
    if idle_cash >= _config.IDLE_CASH_ALERT_USD:
        lines.append(f"\n💤 노는 돈 <b>${idle_cash:,.0f}</b> — 이 웅덩이에 1순위 투입")

    lines.append("\n<i>떨어진 게 아니라 싸진 거예요. 웅덩이가 깊을수록 좋은 가격.</i>")

    from telegram_notifier import send_message
    if not send_message("\n".join(lines)):
        return False
    state[count_key] = state.get(count_key, 0) + 1
    return True


if __name__ == "__main__":
    print(build_reservoir_section())
