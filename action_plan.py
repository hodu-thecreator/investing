#!/usr/bin/env python3
"""
결정 엔진 — "지금 뭐 하면 돼?"에 한 방에 답하는 /now, /goal.

헌법을 실행 계산으로 변환:
  - 헌법 6조 ATH 트리거 판정 → 단일 행동 한 줄
  - 월 납입금을 언더웨이트 버킷부터 채우는 무매도 수렴 배분
    (rebalance-by-contribution: 팔지 않고 목표 비중으로 수렴)
  - 헌법 3조 마일스톤별 예상 도달 시기 (복리 + 월 납입)
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Config

_config = Config()


# ── 환율 ─────────────────────────────────────────────────────────

def usd_krw_rate() -> float:
    """USD/KRW 환율. 조회 실패 시 config 폴백."""
    try:
        from market_indicators import get_usd_krw
        v = (get_usd_krw() or {}).get("usd_to_krw")
        if v and v > 0:
            return float(v)
    except Exception as e:
        print(f"[action_plan] 환율 조회 실패: {e}")
    return _config.FX_USDKRW_FALLBACK


# ── 납입 배분 (rebalance-by-contribution) ────────────────────────

def split_deposit(state: dict, deposit_usd: float) -> list[tuple[str, float, str]]:
    """
    납입금을 언더웨이트 버킷부터 채워 목표 비중으로 수렴시키는 배분.
    매도 없이 신규 자금만으로 리밸런싱 (헌법 7조: 자연 리밸런싱).

    state: rebalancing.calc_portfolio_state() 결과
    Returns: [(preferred_ticker, usd_amount, category), ...] 금액 내림차순
    """
    cats = state.get("categories") or {}
    total = state.get("total", 0) or 0
    if deposit_usd <= 0 or not cats:
        return []

    new_total = total + deposit_usd
    target_sum = sum(d["target_pct"] for d in cats.values()) or 1.0

    # 납입 후 목표 금액 대비 부족분
    needs = {}
    for cat, d in cats.items():
        gap = d["target_pct"] * new_total - d["value"]
        if gap > 0:
            needs[cat] = gap
    need_sum = sum(needs.values())

    alloc: dict[str, float] = {}
    if need_sum <= 0:
        # 전 버킷 오버웨이트(이례적) → 목표 비중 그대로
        for cat, d in cats.items():
            alloc[cat] = deposit_usd * d["target_pct"] / target_sum
    elif need_sum <= deposit_usd:
        # 부족분 전부 채우고 남는 돈은 목표 비중대로
        alloc = dict(needs)
        rest = deposit_usd - need_sum
        for cat, d in cats.items():
            alloc[cat] = alloc.get(cat, 0) + rest * d["target_pct"] / target_sum
    else:
        # 납입금이 부족분보다 작음 → 부족분 비례 배분 (가장 언더웨이트부터)
        for cat, gap in needs.items():
            alloc[cat] = deposit_usd * gap / need_sum

    out = []
    for cat, amt in alloc.items():
        if amt < 1:
            continue
        pref = (cats[cat].get("preferred") or [cat])[0]
        out.append((pref, amt, cat))
    out.sort(key=lambda x: -x[1])
    return out


# ── 마일스톤 ETA ─────────────────────────────────────────────────

def months_to_target(
    current: float, target: float, monthly: float, annual_return: float,
) -> float | None:
    """월 납입 + 복리로 target 도달까지 걸리는 개월 수. 도달 불가면 None."""
    if current >= target:
        return 0.0
    i = (1 + annual_return) ** (1 / 12) - 1
    if i <= 0:
        return (target - current) / monthly if monthly > 0 else None
    denom = current * i + monthly
    if denom <= 0:
        return None
    return math.log((target * i + monthly) / denom) / math.log(1 + i)


def _fmt_target(usd: float) -> str:
    if usd >= 1_000_000:
        v = usd / 1_000_000
        return f"${v:g}M"
    return f"${usd/1000:,.0f}K"


def _eta_str(months: float | None) -> str:
    if months is None:
        return "납입 필요"
    if months <= 0:
        return "✅ 달성"
    eta = datetime.now()
    y, m = divmod(eta.month - 1 + int(round(months)), 12)
    eta = eta.replace(year=eta.year + y, month=m + 1)
    return f"{eta:%Y.%m} · {months/12:.1f}년 후"


# ── /goal — 자유로 가는 길 ───────────────────────────────────────

def build_goal_message(total: float, monthly_krw: float | None = None) -> str:
    fx = usd_krw_rate()
    monthly_krw = monthly_krw or _config.MONTHLY_DEPOSIT_KRW
    monthly_usd = monthly_krw / fx if fx else 0
    r = _config.EXPECTED_ANNUAL_RETURN

    lines = ["<b>🧭 자유로 가는 길</b>"]
    lines.append(
        f"  현재 <b>${total:,.0f}</b> · 월 ₩{monthly_krw/10_000:,.0f}만 납입"
        f" · 연 {r*100:.0f}% 가정"
    )
    lines.append("")

    nxt_found = False
    for target, desc in _config.MILESTONES:
        if total >= target:
            lines.append(f"  ✅ {_fmt_target(target)}  {desc}")
            continue
        m = months_to_target(total, target, monthly_usd, r)
        marker = "👉" if not nxt_found else "　"
        nxt_found = True
        lines.append(f"  {marker} <b>{_fmt_target(target)}</b>  {desc}  —  {_eta_str(m)}")

    # 다음 목표 진행률 바
    nxt = next((mm for mm in _config.MILESTONES if total < mm[0]), None)
    if nxt:
        prev = max([mm[0] for mm in _config.MILESTONES if total >= mm[0]], default=0)
        span = nxt[0] - prev
        prog = (total - prev) / span if span > 0 else 0
        bar = "▓" * int(round(prog * 10)) + "░" * (10 - int(round(prog * 10)))
        lines.append("")
        lines.append(f"  {bar}  {prog*100:.0f}% → {_fmt_target(nxt[0])}")

    lines.append("")
    lines.append("  <i>변수는 둘뿐: 납입을 유지하는 것 + 시장에 머무르는 것.</i>")
    lines.append("  <i>비교는 5년 전 호두와만.</i>")
    return "\n".join(lines)


# ── /now — 지금 할 일 ────────────────────────────────────────────

def build_now_message(
    holdings: dict, idle_cash: float, monthly_krw: float | None = None,
) -> str:
    from rebalancing import calc_portfolio_state
    from intraday_alert import _ath_trigger_status, _decide_action

    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m-%d %H:%M KST")
    lines = [f"<b>🧭 지금 할 일</b>  {now_kst}", "━" * 28]

    # 1) 헌법 6조 트리거 판정 → 단일 행동 (장중 알림과 동일 로직 = 일관성)
    ath = _ath_trigger_status()
    headline, detail = _decide_action(ath, holdings, idle_cash)
    lines.append(f"\n👉 <b>{headline}</b>")
    lines.append(f"<i>{detail}</i>")

    # 2) 이번 달 납입 배분 — 언더웨이트 버킷부터 무매도 수렴
    fx = usd_krw_rate()
    monthly_krw = monthly_krw or _config.MONTHLY_DEPOSIT_KRW
    deposit_usd = monthly_krw / fx if fx else 0

    state = calc_portfolio_state(holdings, idle_cash)
    plan = split_deposit(state, deposit_usd)
    if plan:
        lines.append("")
        lines.append(
            f"<b>💰 이번 달 납입 배분</b>"
            f"  ₩{monthly_krw:,.0f} ≈ ${deposit_usd:,.0f}  <i>(₩{fx:,.0f}/$)</i>"
        )
        for ticker, amt, cat in plan:
            cur = state["categories"][cat]["current_pct"] * 100
            tgt = state["categories"][cat]["target_pct"] * 100
            gap_note = f"{cur:.0f}%→{tgt:.0f}%" if cur < tgt - 0.5 else "비중 유지"
            lines.append(f"  <b>{ticker}</b>  ${amt:,.0f}  <i>{gap_note}</i>")
        lines.append("  <i>→ 언더웨이트부터 채워 목표 비중으로 수렴 (매도 없음)</i>")

    # 3) 마일스톤 한 줄
    total = state.get("total", 0)
    nxt = next((m for m in _config.MILESTONES if total < m[0]), None)
    if total > 0 and nxt:
        monthly_usd = deposit_usd
        m = months_to_target(total, nxt[0], monthly_usd, _config.EXPECTED_ANNUAL_RETURN)
        lines.append("")
        lines.append(
            f"🧭 {_fmt_target(nxt[0])}까지 <b>${nxt[0]-total:,.0f}</b>"
            f"  ·  납입 유지 시 {_eta_str(m)}"
        )

    lines.append("\n<i>룰이 결정하고, 호두는 실행만 합니다.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    import ibkr_flex
    h, c, _ = ibkr_flex.resolve_holdings_and_cash(_config)
    print(build_now_message(h, c))
