#!/usr/bin/env python3
"""
자산 추이 기록 — data/asset_history.json에 일별 스냅샷을 누적해 git에 커밋.

GitHub Actions의 actions/cache는 evict될 수 있어 다년치 자산 추이에는
부적합 → data/ 디렉터리를 git으로 영속화 (daily_report 워크플로에서 커밋).

1주/1개월/1년 전 대비 변화와 사상 최고치(ATH) 대비 위치를 보여주는
"📈 자산 추이" 섹션을 daily_report에 제공한다.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

HISTORY_FILE = Path(__file__).parent / "data" / "asset_history.json"


def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def record_snapshot(total_usd: float, history: list[dict] | None = None) -> list[dict]:
    """오늘 스냅샷을 추가(이미 있으면 갱신)하고 파일에 저장."""
    if total_usd <= 0:
        return history if history is not None else load_history()

    history = load_history() if history is None else history
    today = datetime.now().strftime("%Y-%m-%d")

    if history and history[-1].get("date") == today:
        history[-1]["total"] = round(total_usd, 2)
    else:
        history.append({"date": today, "total": round(total_usd, 2)})

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history


def _value_on_or_before(history: list[dict], target: datetime) -> float | None:
    """target 날짜 이전(포함) 가장 최근 스냅샷 값."""
    best = None
    for h in history:
        try:
            d = datetime.strptime(h["date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue
        if d <= target:
            best = h["total"]
    return best


def build_asset_history_section(total_usd: float, history: list[dict]) -> str:
    """1주/1개월/1년 전 대비 변화 + ATH 대비 위치."""
    if total_usd <= 0 or not history:
        return ""

    now = datetime.now()
    rows = []
    for days, label in ((7, "1주 전"), (30, "1개월 전"), (365, "1년 전")):
        prev = _value_on_or_before(history, now - timedelta(days=days))
        if prev is None or prev <= 0:
            continue
        diff = total_usd - prev
        pct = diff / prev * 100
        sign = "+" if diff >= 0 else "-"
        arrow = "▲" if diff >= 0 else "▼"
        rows.append(
            f"  {label}  ${prev:,.0f}  →  {arrow} {sign}${abs(diff):,.0f} ({sign}{abs(pct):.1f}%)"
        )

    if not rows:
        return ""

    lines = ["<b>📈 자산 추이</b>", f"  현재  <b>${total_usd:,.0f}</b>"]
    lines.extend(rows)

    ath = max([h.get("total", 0) for h in history] + [total_usd])
    if total_usd >= ath - 0.01:
        lines.append("  🏆 사상 최고치 갱신")
    else:
        from_ath = (total_usd - ath) / ath * 100
        lines.append(f"  ATH ${ath:,.0f} 대비 {from_ath:.1f}%")

    return "\n".join(lines)
