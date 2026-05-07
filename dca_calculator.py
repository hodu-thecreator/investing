#!/usr/bin/env python3
"""
오늘의 동적 DCA(적립) 권장 금액 계산.

기본 DCA 금액 × 시장 상황 가중치 = 오늘 권장 금액
- 위험점수 높음 → 가중치 ↓ (과열 시 적게)
- VIX 높음, 본주 낙폭 큼 → 가중치 ↑ (위기 시 많이)

검증된 'Value Averaging' 변형 — 가격이 쌀 때 더 많이, 비쌀 때 덜 산다.
"""
import os

# 기본 일일 DCA 금액 (USD)
DEFAULT_DCA_USD = float(os.getenv("DEFAULT_DCA_USD", "5"))

# 가중치 캡
MIN_MULT = 0.2
MAX_MULT = 3.0


def calc_dca_multiplier(
    indicators: dict,
    risk_score: int,
    base_drawdowns: dict | None = None,
) -> tuple[float, list[str]]:
    """DCA 가중치 + 근거 반환"""
    mult = 1.0
    reasons = []

    # 1) 위험점수 — 과열일수록 적게
    if risk_score >= 7:
        mult *= 0.25
        reasons.append(f"위험 {risk_score}점 극단 과열")
    elif risk_score >= 5:
        mult *= 0.5
        reasons.append(f"위험 {risk_score}점 과열")
    elif risk_score >= 3:
        mult *= 0.75
        reasons.append(f"위험 {risk_score}점 보수")

    # 2) VIX — 높을수록 많이 (공포 = 기회)
    vix = (indicators.get("vix") or {}).get("current") or 0
    if vix >= 40:
        mult *= 3.0
        reasons.append(f"VIX {vix} 극공포")
    elif vix >= 30:
        mult *= 2.0
        reasons.append(f"VIX {vix} 공포")
    elif vix >= 20:
        mult *= 1.4
        reasons.append(f"VIX {vix} 매수 기회")

    # 3) 본주 ETF 최대 낙폭 — 클수록 많이
    if base_drawdowns:
        max_dd = min(base_drawdowns.values())
        if max_dd <= -20:
            mult *= 2.0
            reasons.append(f"본주 {max_dd:.0f}% 급락")
        elif max_dd <= -12:
            mult *= 1.5
            reasons.append(f"본주 {max_dd:.0f}% 하락")
        elif max_dd <= -7:
            mult *= 1.2
            reasons.append(f"본주 {max_dd:.0f}% 조정")

    # 4) 공포/탐욕 보조
    fg = (indicators.get("fear_greed") or {}).get("score")
    if fg is not None:
        if fg <= 20:
            mult *= 1.3
            reasons.append(f"F&G {fg} 극공포")
        elif fg >= 80:
            mult *= 0.7
            reasons.append(f"F&G {fg} 극탐욕")

    mult = max(MIN_MULT, min(MAX_MULT, mult))
    return round(mult, 2), reasons


def build_dca_section(
    indicators: dict,
    risk_score: int,
    base_drawdowns: dict | None = None,
) -> str:
    base = DEFAULT_DCA_USD
    mult, reasons = calc_dca_multiplier(indicators, risk_score, base_drawdowns)
    today = base * mult

    lines = ["<b>📍 오늘의 적립 권장</b>"]
    lines.append(f"  기본 ${base:.0f}  ×  <b>{mult:.2f}배</b>  =  <b>${today:.2f}</b>")

    if mult >= 2.0:
        lines.append("  🟢 적극 매수 — 평소보다 많이 사세요")
    elif mult >= 1.3:
        lines.append("  🟢 매수 우호적")
    elif mult <= 0.4:
        lines.append("  🔴 과열 — 거의 사지 마세요")
    elif mult <= 0.7:
        lines.append("  🟡 보수적 — 평소보다 적게")
    else:
        lines.append("  ⚪ 평소대로")

    if reasons:
        lines.append(f"  <i>{' · '.join(reasons[:3])}</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    from market_indicators import collect_all
    from daily_report import calc_macro_risk_score

    ind = collect_all()
    rs, _ = calc_macro_risk_score(ind)
    print(build_dca_section(ind, rs, {"SPYM": -3.2, "QQQM": -8.1, "SOXQ": -1.5}))
