#!/usr/bin/env python3
"""
헌법 9조 — 한국 phase 양도세 250만원 공제 실행 플랜 (/tax).

여력만 보여주는 게 아니라 "어떤 종목을 몇 주 팔면 한도를 채우는지"를
계산해 준다. 우선순위:
  1. 레거시 종목 (정리 대상 — 공제로 세금 0에 처분, 재매수 금지)
  2. 코어 종목 (매도 후 즉시 재매수 → 평단 스텝업, 포지션 불변)
미실현 이익이 없는 종목은 제외 (손실 실현은 공제 낭비).
"""
from datetime import datetime

from config import Config

_config = Config()

KR_CGT_RATE = 0.22  # 해외주식 양도세 22% (지방세 포함)


def plan_sales(
    positions: dict,
    headroom_usd: float,
    legacy: set[str],
    core: set[str],
) -> list[dict]:
    """
    공제 여력(headroom_usd)을 채우는 종목별 매도 플랜.

    positions: {sym: {qty, cost_basis, mark_price}} (cost_basis = 주당 평단)
    Returns: [{ticker, shares, gain_usd, is_legacy}, ...]
    """
    if headroom_usd <= 0:
        return []

    cands = []
    for sym, d in positions.items():
        qty = d.get("qty") or 0
        cost = d.get("cost_basis") or 0
        mark = d.get("mark_price") or 0
        gain_ps = mark - cost
        if qty <= 0 or mark <= 0 or gain_ps <= 0:
            continue
        cands.append({
            "ticker": sym,
            "qty": qty,
            "gain_ps": gain_ps,
            "total_gain": gain_ps * qty,
            "is_legacy": sym in legacy,
            "is_core": sym in core,
        })

    # 레거시 먼저(정리 겸용), 그 안에선 총이익 큰 순
    cands.sort(key=lambda c: (not c["is_legacy"], -c["total_gain"]))

    plan = []
    left = headroom_usd
    for c in cands:
        if left <= 1:
            break
        shares = min(c["qty"], left / c["gain_ps"])
        # 1주 이상 보유 종목은 정수 주 단위로 (소수점 잔량 방지)
        if c["qty"] >= 1:
            shares = min(c["qty"], float(int(shares)) if shares >= 1 else round(shares, 2))
        else:
            shares = round(shares, 4)
        if shares <= 0:
            continue
        gain = shares * c["gain_ps"]
        plan.append({
            "ticker": c["ticker"],
            "shares": shares,
            "gain_usd": gain,
            "is_legacy": c["is_legacy"],
        })
        left -= gain
    return plan


def _positions_from_transactions() -> dict:
    """IBKR 실패 시 거래기록 기반 폴백 → plan_sales 입력 포맷으로 변환."""
    try:
        import transactions
        out = {}
        for t, d in transactions.portfolio_summary().items():
            if d["qty"] > 0 and d.get("current_price"):
                out[t] = {
                    "qty": d["qty"],
                    "cost_basis": d["avg_price"],
                    "mark_price": d["current_price"],
                }
        return out
    except Exception as e:
        print(f"[tax] 거래기록 폴백 실패: {e}")
        return {}


def build_tax_message() -> str:
    today = datetime.now()
    phase_end = datetime.strptime(_config.KR_PHASE_END, "%Y-%m")
    if today >= phase_end:
        return (
            "🇳🇿 한국 phase 종료 — NZ Transitional 기간은 양도차익 면세.\n"
            "재배분 최적기입니다 (헌법 9조)."
        )

    from action_plan import usd_krw_rate
    fx = usd_krw_rate()

    realized_usd = 0.0
    try:
        from transactions import realized_ytd
        realized_usd = realized_ytd()
    except Exception as e:
        print(f"[tax] realized_ytd 실패: {e}")

    realized_krw = realized_usd * fx
    headroom_krw = _config.KR_CGT_DEDUCTION_KRW - realized_krw
    headroom_usd = headroom_krw / fx if fx else 0

    lines = ["<b>🇰🇷 양도세 공제 실행 플랜</b>  <i>(연 ₩250만 비과세)</i>"]
    lines.append(
        f"  올해 실현  ₩{realized_krw:,.0f}"
        f"  →  남은 공제  <b>₩{max(headroom_krw, 0):,.0f}</b> (≈${max(headroom_usd, 0):,.0f})"
    )

    if headroom_krw <= 0:
        lines.append("  ✅ 올해 공제 한도 소진 — 추가 실현 매도 금지 (초과분 22% 과세)")
        return "\n".join(lines)

    lines.append(f"  공제 활용 시 절세  <b>₩{headroom_krw * KR_CGT_RATE:,.0f}</b> (22%)")

    import ibkr_flex
    ibkr = ibkr_flex.get_account_data()
    positions = ibkr["positions"] if not ibkr["error"] else _positions_from_transactions()
    if not positions:
        lines.append("\n  ❌ 포지션 조회 실패 — /ibkrsync 후 재시도")
        return "\n".join(lines)

    plan = plan_sales(
        positions, headroom_usd,
        legacy=set(_config.LEGACY_TICKERS),
        core=set(_config.CORE_ALLOCATION.keys()),
    )
    if not plan:
        lines.append("\n  📭 미실현 이익 보유 종목 없음 — 실현할 차익이 없습니다")
        return "\n".join(lines)

    lines.append("")
    lines.append("<b>📋 매도 플랜 (공제 한도 채우기)</b>")
    total_gain = 0.0
    for i, p in enumerate(plan, 1):
        total_gain += p["gain_usd"]
        sh = f"{p['shares']:,.0f}주" if p["shares"] >= 1 else f"{p['shares']}주"
        note = (
            "레거시 정리 — 재매수 금지, 대금은 SGOV로"
            if p["is_legacy"]
            else "즉시 재매수 → 평단 스텝업 (포지션 불변)"
        )
        lines.append(
            f"  {i}. <b>{p['ticker']}</b>  {sh} 매도 → 차익 ${p['gain_usd']:,.0f} 실현"
        )
        lines.append(f"     <i>{note}</i>")

    lines.append("")
    lines.append(f"  합계 실현 차익  <b>${total_gain:,.0f}</b> (≈₩{total_gain*fx:,.0f})")
    lines.append("  ⏰ 결제일(T+1) 기준 과세연도 — 12월 마지막 거래일 전까지 실행")
    lines.append("  <i>헌법 7조: 세금 최적화는 유일하게 허용된 매도입니다.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_tax_message())
