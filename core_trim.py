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


# ── 동적 현금 목표 갭 — 점진적 익절 (2026.6 신설) ──────────────────
# calc_macro_risk_score()가 위험 신호를 감지해 현금 목표를 헌법값(20%) 위로
# 올렸을 때(config.CASH_TARGET_LADDER)만 작동. 갭을 한 번에 메우지 않고
# 리포트 1회당 상한(CASH_TRIM_CAP_PCT_PER_REPORT)까지만, "오른쪽 어깨"
# (이미 꺾임 확인된) 과열 종목부터 순서대로 점진 익절 — 헌법 7조 균형.
TRIM_CANDIDATE_TICKERS = CORE_TRIM_TICKERS + list(_config.SATELLITE_TICKERS)


def build_dynamic_cash_trim_section(
    risk_score: int,
    cash_ratio: float,
    target_cash_ratio: float,
    judged: dict[str, dict],
    holdings: dict[str, float],
    total_portfolio: float,
) -> str:
    """위험점수로 현금 목표가 헌법값 위로 상향된 경우에만 동작 — 평시엔 무동작."""
    base_target = _config.CORE_ALLOCATION["SGOV"]
    if target_cash_ratio <= base_target or total_portfolio <= 0:
        return ""

    gap = target_cash_ratio - cash_ratio
    if gap <= 0:
        return ""

    candidates = []
    for t in TRIM_CANDIDATE_TICKERS:
        qty = (holdings or {}).get(t, 0) or 0
        d = judged.get(t) or {}
        rsi, dd, price = d.get("rsi"), d.get("drawdown"), d.get("price")
        if qty <= 0 or not price or rsi is None or rsi < _config.CORE_TRIM_RSI:
            continue
        # 신고가 행진 중이면 패스 — "오른쪽 어깨"(꺾임 확인)만 후보
        if dd is None or dd > _config.CORE_TRIM_PULLBACK:
            continue
        candidates.append({"ticker": t, "rsi": rsi, "dd": dd, "qty": qty, "price": price})
    if not candidates:
        return ""

    candidates.sort(key=lambda x: -x["rsi"])
    budget_pct = min(gap, _config.CASH_TRIM_CAP_PCT_PER_REPORT)
    budget_usd = budget_pct * total_portfolio

    lines = ["<b>✂️ 단계적 익절 — 현금 목표 상향</b>"]
    lines.append(
        f"  위험 {risk_score}점 → 현금 목표 {target_cash_ratio*100:.0f}% "
        f"(현재 {cash_ratio*100:.1f}%, 갭 {gap*100:.1f}%p)"
    )
    lines.append(f"  이번 리포트 한도 {budget_pct*100:.1f}%p (${budget_usd:,.0f}) — 점진 진행")

    remaining = budget_usd
    sold_any = False
    for c in candidates:
        if remaining <= 0:
            break
        max_sell_value = c["qty"] * c["price"] * _config.CASH_TRIM_MAX_PER_TICKER_PCT
        sell_value = min(remaining, max_sell_value)
        if sell_value <= 0:
            continue
        sell_qty = sell_value / c["price"]
        lines.append(
            f"  <b>{c['ticker']}</b>  RSI {c['rsi']:.0f} · 고점대비 {c['dd']:+.1f}% — "
            f"{sell_qty:.2f}주 매도 → SGOV 전환 (${sell_value:,.0f})"
        )
        remaining -= sell_value
        sold_any = True

    if not sold_any:
        return ""

    leftover_gap_usd = gap * total_portfolio - (budget_usd - remaining)
    if leftover_gap_usd > 1:
        lines.append(f"  <i>잔여 갭 ${leftover_gap_usd:,.0f} — 다음 리포트에서 단계적으로 계속</i>")
    lines.append("  <i>재투입처는 적립 포트폴리오 점검의 전략적 교체 제안 참고</i>")

    return "\n".join(lines)
