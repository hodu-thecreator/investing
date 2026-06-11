#!/usr/bin/env python3
"""
거주국 phase 로드맵 (헌법 1·9조) — 현재 phase, 다음 전환까지 D-day,
phase별 세금/행동 리마인더 + 레거시(헌법 외) 종목 정리 현황.

phase 순서: 한국 → NZ Transitional(면세) → NZ FIF(1년) → 호주(영구)
"""
from datetime import datetime

from config import Config

_config = Config()

_LABELS = ["🇰🇷 한국", "🇳🇿 뉴질랜드 (Transitional)", "🇳🇿 뉴질랜드 (FIF)", "🇦🇺 호주 (영구)"]

_ACTIONS = [
    "양도세 연 250만원 공제로 레거시 정리 + 코어 평단 스텝업 (/tax 참고)",
    "해외 소득 면세 — 재배분 최적기. 레거시 잔여분 전량 정리 검토 + KiwiSaver 즉시 가입",
    "FIF FDR 5% deemed income 적용 — 1년만 버티기, 큰 변경 자제",
    "SGOV → AGVT/BILL/HISA 교체, Super 자동 확인. CGT 12개월 보유 50% 할인 활용",
]


def _phase_boundaries() -> list[datetime]:
    return [
        datetime.strptime(_config.KR_PHASE_END, "%Y-%m"),
        datetime.strptime(_config.NZ_FIF_START, "%Y-%m"),
        datetime.strptime(_config.AU_MOVE, "%Y-%m"),
    ]


def current_phase_index(today: datetime | None = None) -> int:
    today = today or datetime.now()
    idx = 0
    for b in _phase_boundaries():
        if today >= b:
            idx += 1
    return idx


def build_roadmap_section(state: dict | None = None) -> str:
    today = datetime.now()
    boundaries = _phase_boundaries()
    idx = current_phase_index(today)

    lines = [f"<b>🧭 거주국 로드맵</b>  현재: {_LABELS[idx]}"]

    if idx < len(boundaries):
        nxt_date = boundaries[idx]
        days = (nxt_date - today).days
        lines.append(f"  다음 전환: {_LABELS[idx + 1]}  D-{days}  ({nxt_date:%Y.%m})")

    lines.append(f"  <i>{_ACTIONS[idx]}</i>")

    if state:
        unclassified = state.get("unclassified") or {}
        total = state.get("total") or 0
        legacy_value = sum(unclassified.values())
        if total > 0 and legacy_value > 0:
            pct = legacy_value / total * 100
            lines.append(
                f"  레거시 비중  ${legacy_value:,.0f}  ({pct:.1f}%)  — phase 세금 룰 따라 정리"
            )

    return "\n".join(lines)
