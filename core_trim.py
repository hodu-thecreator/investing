#!/usr/bin/env python3
"""
코어 과열 부분 익절 (헌법 7조 예외, 2026.6 신설).

"많이 오르고 현금이 필요하면 판다" — 코어 5종목(QQQM/SPYM/GLDM/IBIT)도
S&P ATH 근처(또는 현금이 사실상 0%) + RSI 과열(70+) + 현금(SGOV) 비중 부족
조건을 충족할 때만 가장 과열된 종목 보유분의 5%를 부분 매도해 SGOV로 전환.
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
    """(ATH 근처 또는 현금 사실상 0%) + 코어 과열·꺾임 + 현금 부족 충족 시만 안내.

    과열(RSI 70+)이어도 자체 신고가 행진 중이면 더 갈 수 있으니 안 팖 —
    고점에서 CORE_TRIM_PULLBACK 이상 꺾인 종목만 후보.
    """
    near_ath = sp_drawdown is not None and sp_drawdown >= -2.0
    cash_depleted = cash_ratio < _config.CORE_TRIM_CASH_FLOOR
    if not near_ath and not cash_depleted:
        return ""

    gap = target_cash_ratio - cash_ratio
    if gap < _config.CORE_TRIM_CASH_GAP:
        return ""

    overheated = []
    for t in CORE_TRIM_TICKERS:
        qty = (holdings or {}).get(t, 0) or 0
        d = judged.get(t) or {}
        rsi = d.get("rsi")
        dd = d.get("drawdown")
        if qty <= 0 or rsi is None or rsi < _config.CORE_TRIM_RSI:
            continue
        # 신고가 행진 중이면 패스 — 꺾임이 확인된 과열만
        if dd is None or dd > _config.CORE_TRIM_PULLBACK:
            continue
        overheated.append((t, rsi, qty, dd))
    if not overheated:
        return ""

    overheated.sort(key=lambda x: -x[1])
    ticker, rsi, qty, dd = overheated[0]
    trim_qty = qty * _config.CORE_TRIM_PCT

    lines = ["<b>✂️ 코어 과열 부분 익절</b>"]
    lines.append(
        f"  <b>{ticker}</b>  RSI {rsi:.0f}  ·  고점 대비 {dd:+.1f}%  ·  "
        f"현금 {cash_ratio*100:.1f}% / 목표 {target_cash_ratio*100:.0f}%"
    )
    lines.append(f"  → {ticker} {trim_qty:.2f}주(5%) 매도 → SGOV 전환, 포지션 유지")
    lines.append("  <i>재투입처는 적립 포트폴리오 점검의 전략적 교체 제안 참고</i>")

    return "\n".join(lines)
