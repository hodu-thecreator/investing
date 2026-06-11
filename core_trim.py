#!/usr/bin/env python3
"""
코어 과열 부분 익절 (헌법 7조 예외, 2026.6 신설).

"많이 오르고 현금이 필요하면 판다" — 코어 5종목(QQQM/SPYM/GLDM/IBIT)도
S&P ATH 근처 + RSI 과열(70+) + 현금(SGOV) 비중 부족 3가지를 모두
충족할 때만 가장 과열된 종목 보유분의 5%를 부분 매도해 SGOV로 전환.
"""
from config import Config

_config = Config()

CORE_TRIM_TICKERS = [t for t in _config.CORE_ALLOCATION if t != "SGOV"]


def build_core_trim_section(
    sp_drawdown: float | None,
    cash_ratio: float,
    target_cash_ratio: float,
    judged: dict[str, dict],
    holdings: dict[str, float],
) -> str:
    """3조건(ATH 근처, 코어 과열, 현금 부족) 모두 충족 시만 안내."""
    if sp_drawdown is None or sp_drawdown < -2.0:
        return ""

    gap = target_cash_ratio - cash_ratio
    if gap < _config.CORE_TRIM_CASH_GAP:
        return ""

    overheated = []
    for t in CORE_TRIM_TICKERS:
        qty = (holdings or {}).get(t, 0) or 0
        d = judged.get(t) or {}
        rsi = d.get("rsi")
        if qty > 0 and rsi is not None and rsi >= _config.CORE_TRIM_RSI:
            overheated.append((t, rsi, qty))
    if not overheated:
        return ""

    overheated.sort(key=lambda x: -x[1])
    ticker, rsi, qty = overheated[0]
    trim_qty = qty * _config.CORE_TRIM_PCT

    lines = ["<b>✂️ 코어 과열 부분 익절</b>  <i>(헌법 7조 예외 — 5%만)</i>"]
    lines.append(
        f"  <b>{ticker}</b>  RSI {rsi:.0f} (과열)  ·  "
        f"현금 {cash_ratio*100:.1f}% / 목표 {target_cash_ratio*100:.0f}% (부족)"
    )
    lines.append(f"  → {ticker} {trim_qty:.2f}주(5%) 매도 → SGOV로 전환, 포지션은 유지")
    lines.append("  <i>많이 오르고 현금이 필요할 때만 — 일상적 매도 아님</i>")
    return "\n".join(lines)
