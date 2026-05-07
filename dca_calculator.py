#!/usr/bin/env python3
"""
오늘의 동적 DCA(적립) 권장 금액 계산.

실제 적립 스케줄(config.DCA_SCHEDULE) 기준으로 종목별 조정 금액을 표시.
Value Averaging 원리: VIX·낙폭 클수록 많이, 위험과열일수록 적게.
"""
import os

MIN_MULT = 0.2
MAX_MULT = 3.0


def calc_dca_multiplier(
    indicators: dict,
    risk_score: int,
    base_drawdowns: dict | None = None,
) -> tuple[float, list[str]]:
    mult = 1.0
    reasons = []

    if risk_score >= 7:
        mult *= 0.25; reasons.append(f"위험 {risk_score}점 극과열")
    elif risk_score >= 5:
        mult *= 0.5;  reasons.append(f"위험 {risk_score}점 과열")
    elif risk_score >= 3:
        mult *= 0.75; reasons.append(f"위험 {risk_score}점 보수")

    vix = (indicators.get("vix") or {}).get("current") or 0
    if vix >= 40:
        mult *= 3.0; reasons.append(f"VIX {vix:.0f} 극공포")
    elif vix >= 30:
        mult *= 2.0; reasons.append(f"VIX {vix:.0f} 공포")
    elif vix >= 20:
        mult *= 1.4; reasons.append(f"VIX {vix:.0f} 매수 기회")

    if base_drawdowns:
        max_dd = min(base_drawdowns.values())
        if max_dd <= -20:
            mult *= 2.0; reasons.append(f"본주 {max_dd:.0f}% 급락")
        elif max_dd <= -12:
            mult *= 1.5; reasons.append(f"본주 {max_dd:.0f}% 하락")
        elif max_dd <= -7:
            mult *= 1.2; reasons.append(f"본주 {max_dd:.0f}% 조정")

    fg = (indicators.get("fear_greed") or {}).get("score")
    if fg is not None:
        if fg <= 20:
            mult *= 1.3; reasons.append(f"F&G {fg} 극공포")
        elif fg >= 80:
            mult *= 0.7; reasons.append(f"F&G {fg} 극탐욕")

    return round(max(MIN_MULT, min(MAX_MULT, mult)), 2), reasons


def build_dca_section(
    indicators: dict,
    risk_score: int,
    base_drawdowns: dict | None = None,
) -> str:
    from config import Config
    schedule = Config.DCA_SCHEDULE
    if not schedule:
        return ""

    mult, reasons = calc_dca_multiplier(indicators, risk_score, base_drawdowns)
    adj = mult != 1.0

    # 배수 라벨
    if mult >= 2.0:
        label = "🟢 적극 매수"
    elif mult >= 1.3:
        label = "🟢 매수 우호"
    elif mult <= 0.4:
        label = "🔴 과열 — 최소만"
    elif mult <= 0.7:
        label = "🟡 보수적"
    else:
        label = "⚪ 평소대로"

    reason_str = f"  <i>{' · '.join(reasons[:2])}</i>" if reasons else ""
    header = f"<b>📍 오늘의 적립 권장</b>  {label}  (×{mult}){reason_str}"

    lines = [header, ""]

    # 종목별 금액
    biweekly = [(t, v) for t, v in schedule.items() if v["interval"] == "biweekly"]
    monthly  = [(t, v) for t, v in schedule.items() if v["interval"] == "monthly"]

    def fmt_row(items):
        parts = []
        for t, v in items:
            base = v["amount"]
            adjusted = round(base * mult)
            if adj:
                parts.append(f"<b>{t}</b>  ${base}→<b>${adjusted}</b>")
            else:
                parts.append(f"<b>{t}</b>  ${base}")
        return parts

    if biweekly:
        lines.append("  <i>2주마다:</i>  " + "   ".join(fmt_row(biweekly)))
    if monthly:
        lines.append("  <i>월마다:</i>    " + "   ".join(fmt_row(monthly)))

    return "\n".join(lines)


if __name__ == "__main__":
    from market_indicators import collect_all
    from daily_report import calc_macro_risk_score
    ind = collect_all()
    rs, _ = calc_macro_risk_score(ind)
    print(build_dca_section(ind, rs, {"SPYM": -8.0}))
