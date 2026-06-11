#!/usr/bin/env python3
"""
배당 입금 감지 — IBKR Flex CashTransaction(배당)에서 신규 입금을 찾아
언더웨이트 버킷 재투자 지시를 안내한다 (헌법 7조: 배당 재투자, 매도 없음).

dedup 상태는 data/dividend_state.json에 저장 (daily_report 워크플로에서 git 커밋).
최초 실행 시(상태 파일이 비어있음)에는 기존 입금 내역을 전부 "이미 본 것"으로
기록만 하고 알림은 보내지 않는다 (과거 입금 일괄 알림 방지).
"""
import json
from pathlib import Path

STATE_FILE = Path(__file__).parent / "data" / "dividend_state.json"


def _load_seen() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


def _key(div: dict) -> str:
    return f"{div.get('date')}|{div.get('symbol')}|{div.get('amount')}"


def find_new_dividends(dividends: list[dict]) -> list[dict]:
    """이전에 보지 못한 배당 입금만 추출하고 상태 갱신."""
    if not dividends:
        return []

    seen = _load_seen()
    first_run = not seen
    new = []
    keys = set(seen)
    for d in dividends:
        k = _key(d)
        if k in seen:
            continue
        keys.add(k)
        if not first_run:
            new.append(d)

    if keys != seen:
        _save_seen(keys)
    return new


def build_dividend_alert_section(state: dict, new_dividends: list[dict]) -> str:
    """신규 배당 입금 안내 — 즉시 매수 대신 모아뒀다가 웅덩이에 투입.

    웅덩이가 이미 열려 있으면 지금 투입 지시, 만수위면 대기 안내.
    (DRIP은 고점에도 사버려서 비활성 — 호두 룰: 좋은 날까지 모은다)
    """
    if not new_dividends:
        return ""

    by_sym: dict[str, float] = {}
    for d in new_dividends:
        by_sym[d["symbol"]] = by_sym.get(d["symbol"], 0.0) + d["amount"]
    total = sum(by_sym.values())
    if total <= 0:
        return ""

    lines = ["<b>💵 배당 입금 감지</b>"]
    detail = "  ·  ".join(f"{sym} ${amt:,.2f}" for sym, amt in by_sym.items())
    lines.append(f"  {detail}  =  합계 <b>${total:,.2f}</b>")

    # 웅덩이 열려 있나? — 열려 있으면 지금 투입, 아니면 모아두기
    zone_open = False
    try:
        import reservoir
        levels = reservoir.fetch_levels()
        zone_open = bool(levels) and levels[0]["zone_idx"] > 0
    except Exception:
        pass

    if zone_open:
        from action_plan import split_deposit
        plan = split_deposit(state, total)
        if plan:
            lines.append("  → 웅덩이 열림 — 언더웨이트 버킷에 투입:")
            for ticker, amt, cat in plan:
                cur = state["categories"][cat]["current_pct"] * 100
                tgt = state["categories"][cat]["target_pct"] * 100
                gap_note = f"{cur:.0f}%→{tgt:.0f}%" if cur < tgt - 0.5 else "비중 유지"
                lines.append(f"    <b>{ticker}</b>  ${amt:,.2f}  <i>{gap_note}</i>")
    else:
        lines.append("  → ☀️ 만수위 — 추격 매수 금지. USD로 모아두기 (웅덩이 열리면 알림)")

    lines.append("  <i>헌법 7조: 배당은 재투자, 매도 없음</i>")
    return "\n".join(lines)
