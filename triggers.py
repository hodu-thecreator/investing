#!/usr/bin/env python3
"""
실시간 트리거 모니터링.

매시간(또는 30분마다) 실행 → 시장 상태 변화가 임계 돌파했을 때만 알림 푸시.
중복 발사 방지를 위해 마지막 상태(밴드)를 저장하고 변경됐을 때만 트리거.

트리거 종류:
  1) VIX 임계 진입/이탈
  2) 위험점수 등급 변경 (상승만 알림)
  3) 본주 ETF 60일 고점 대비 -7%/-12%/-20% 돌파
  4) 공포/탐욕 극단 진입 (≤20 또는 ≥80)
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from market_indicators import collect_all
from telegram_notifier import send_message

TRIGGER_STATE_FILE = Path(__file__).parent / ".trigger_state.json"

# 본주 ETF — 추적 대상
BASE_ETFS = ["SPY", "QQQ", "SPYM", "QQQM", "SOXQ"]


def _load_state() -> dict:
    try:
        return json.loads(TRIGGER_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    TRIGGER_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _is_market_hours() -> bool:
    """미국 장 시간(평일 9:30 ~ 16:30 ET) 여부."""
    if os.getenv("FORCE_TRIGGER") == "1":
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60 + 30


def _vix_band(v: float) -> str:
    if v >= 40: return "extreme"
    if v >= 30: return "high"
    if v >= 25: return "elevated"
    if v >= 20: return "moderate"
    return "low"


def _risk_band(s: int) -> str:
    if s >= 7: return "extreme"
    if s >= 5: return "high"
    if s >= 3: return "moderate"
    return "low"


def _drawdown_band(dd: float) -> str:
    if dd <= -20: return "20"
    if dd <= -12: return "12"
    if dd <= -7: return "7"
    return "0"


def _fg_band(s: int) -> str:
    if s <= 20: return "extreme_fear"
    if s >= 80: return "extreme_greed"
    return "neutral"


def check_triggers() -> tuple[list[str], dict]:
    """변경된 트리거 메시지 + 새 상태 반환"""
    indicators = collect_all()
    from daily_report import calc_macro_risk_score, fetch_stock_data

    old = _load_state()
    new: dict = {}
    alerts: list[str] = []

    # 1) VIX
    vix = (indicators.get("vix") or {}).get("current") or 0
    band = _vix_band(vix)
    new["vix_band"] = band
    if old.get("vix_band") != band:
        if band in ("extreme", "high", "elevated"):
            alerts.append(
                f"⚠️ <b>VIX 알림</b>\n"
                f"VIX <b>{vix}</b> — {band.upper()} 진입\n"
                f"매수 기회 확대 (위기일수록 적극)"
            )
        elif band in ("moderate", "low") and old.get("vix_band") in ("extreme", "high"):
            alerts.append(
                f"✅ <b>VIX 정상화</b>\n"
                f"VIX <b>{vix}</b> — 변동성 진정"
            )

    # 2) 위험 점수 등급
    risk_score, risk_signals = calc_macro_risk_score(indicators)
    rband = _risk_band(risk_score)
    new["risk_band"] = rband
    new["risk_score"] = risk_score
    prev_score = old.get("risk_score", 0) or 0
    if old.get("risk_band") != rband and risk_score > prev_score:
        emoji = {"extreme": "🔴", "high": "🟠", "moderate": "🟡"}.get(rband, "🟢")
        alerts.append(
            f"{emoji} <b>위험 점수 상승</b>\n"
            f"위험 <b>{risk_score}점</b> ({rband.upper()})\n"
            f"<i>{' · '.join(risk_signals[:2])}</i>\n"
            f"현금 비중 목표 조정 필요"
        )

    # 3) 공포/탐욕 극단
    fg = (indicators.get("fear_greed") or {}).get("score")
    if fg is not None:
        fband = _fg_band(int(fg))
        new["fg_band"] = fband
        if old.get("fg_band") != fband:
            if fband == "extreme_fear":
                alerts.append(
                    f"💎 <b>공포/탐욕 극공포 진입</b>\n"
                    f"F&G <b>{fg}</b> — 역사적 매수 기회"
                )
            elif fband == "extreme_greed":
                alerts.append(
                    f"🔥 <b>공포/탐욕 극탐욕 진입</b>\n"
                    f"F&G <b>{fg}</b> — 신규 매수 보수적으로"
                )

    # 4) 본주 ETF 낙폭
    for etf in BASE_ETFS:
        try:
            df = fetch_stock_data(etf, period="3mo")
            if df.empty:
                continue
            close = df["Close"].squeeze().dropna()
            if close.empty:
                continue
            current = float(close.iloc[-1])
            high_60d = float(close.rolling(min(60, len(close))).max().iloc[-1])
            dd = (current - high_60d) / high_60d * 100
        except Exception as e:
            print(f"[trigger] {etf} 오류: {e}")
            continue

        band = _drawdown_band(dd)
        key = f"{etf}_dd_band"
        new[key] = band
        prev_band = old.get(key, "0")
        # 더 깊은 밴드로 진입했을 때만 알림 (반등 시 알림 X)
        if int(band) > int(prev_band) and band != "0":
            if band == "20":
                alerts.append(
                    f"🔥 <b>{etf} -20% 돌파!</b>\n"
                    f"60일 고점 대비 <b>{dd:+.1f}%</b>\n"
                    f"역대급 매수 기회 — 3x 레버리지 적극 검토"
                )
            elif band == "12":
                alerts.append(
                    f"🔴 <b>{etf} -12% 돌파</b>\n"
                    f"60일 고점 대비 <b>{dd:+.1f}%</b>\n"
                    f"3x 레버리지 매수 시점"
                )
            elif band == "7":
                alerts.append(
                    f"🟠 <b>{etf} -7% 돌파</b>\n"
                    f"60일 고점 대비 <b>{dd:+.1f}%</b>\n"
                    f"2x 레버리지 매수 검토"
                )

    return alerts, new


def main():
    if not _is_market_hours():
        print(f"[trigger] 시장 시간 아님 — 스킵 ({datetime.now(ZoneInfo('America/New_York')):%a %H:%M ET})")
        return

    alerts, new_state = check_triggers()
    print(f"[trigger] {len(alerts)}개 알림")
    for alert in alerts:
        send_message(alert)
    _save_state(new_state)


if __name__ == "__main__":
    main()
