#!/usr/bin/env python3
"""
포트폴리오 리밸런싱 점검.

목표 카테고리 비중 vs 실제 비중 비교 → ±5%p 이상 드리프트 시 경고 + 액션 제안.

특이사항:
  - SPYI/QQQI는 배당 수익 목적의 의도적 오버웨이트.
    카테고리 합산엔 포함하되 신규 매수 추천에선 제외 (preferred 우선).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from config import Config

_config = Config()

# ── 헌법 5조: 코어 5종목 목표 비중 ────────────────────────────────
# 레버리지(SSO/UPRO/QLD/TQQQ)는 조정 시 임시 포지션 — 각 코어 노출에 합산.
# SPYI/QQQI/SOXQ 등 레거시는 미분류로 빠지며 정리 대상.
TARGET_ALLOCATION: dict[str, dict] = {
    "S&P500": {
        "target": 0.30,
        "tickers": ["SPYM", "SPMO", "SSO", "UPRO"],   # SPMO = 위성(상한 10%), SSO/UPRO = 레버 노출
        "preferred": ["SPYM"],
        "note": "조정 시 SSO/UPRO 임시 매수 — 평시엔 SPYM, 위성 SPMO는 저수지 구간만",
    },
    "Nasdaq100": {
        "target": 0.30,
        "tickers": ["QQQM", "QLD", "TQQQ"],   # QLD/TQQQ = Nasdaq 레버 노출
        "preferred": ["QQQM"],
        "note": "조정 시 QLD/TQQQ 임시 매수 — 평시엔 QQQM",
    },
    "금": {
        "target": 0.07,
        "tickers": ["GLDM"],
        "preferred": ["GLDM"],
    },
    "비트코인": {
        "target": 0.03,
        "tickers": ["IBIT"],
        "preferred": ["IBIT"],
    },
    "현금": {
        "target": 0.29,
        "tickers": ["SGOV", "BIL", "SHV", "SHY"],
        "preferred": ["SGOV"],
    },
}

DRIFT_THRESHOLD = 0.10  # 헌법 7조: ±10%p (연 1회 점검)


def _price(ticker: str) -> float | None:
    try:
        df = yf.Ticker(ticker).history(period="2d")
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _fetch_prices(tickers: list[str]) -> dict[str, float]:
    """병렬로 가격 조회 — /rebalance 응답 속도 개선."""
    results: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_price, t): t for t in tickers}
        for f in as_completed(futures):
            t = futures[f]
            p = f.result()
            if p is not None:
                results[t] = p
    return results


def calc_portfolio_state(
    holdings: dict | None = None,
    idle_cash: float | None = None,
) -> dict:
    holdings = holdings if holdings is not None else _config.HOLDINGS
    idle_cash = idle_cash if idle_cash is not None else _config.IDLE_CASH_USD

    active = [t for t, q in holdings.items() if q and q > 0]
    prices = _fetch_prices(active)

    ticker_values: dict[str, float] = {}
    for t in active:
        if t in prices:
            ticker_values[t] = holdings[t] * prices[t]

    total = sum(ticker_values.values()) + idle_cash
    if total <= 0:
        return {"total": 0, "categories": {}, "ticker_values": {}, "unclassified": {}}

    categories: dict[str, dict] = {}
    used = set()
    for cat, conf in TARGET_ALLOCATION.items():
        cat_value = idle_cash if cat == "현금" else 0
        breakdown = []
        for t in conf["tickers"]:
            v = ticker_values.get(t, 0)
            if v > 0:
                breakdown.append((t, v, v / total * 100))
                cat_value += v
                used.add(t)
        cur_pct = cat_value / total
        categories[cat] = {
            "target_pct": conf["target"],
            "current_pct": cur_pct,
            "value": cat_value,
            "drift_pct": cur_pct - conf["target"],
            "breakdown": breakdown,
            "preferred": conf.get("preferred", []),
            "note": conf.get("note", ""),
        }

    unclassified = {t: v for t, v in ticker_values.items() if t not in used}
    return {
        "total": total,
        "idle_cash": idle_cash,
        "categories": categories,
        "ticker_values": ticker_values,
        "unclassified": unclassified,
    }


def check_drifts(state: dict | None = None) -> list[dict]:
    """드리프트가 임계 초과한 카테고리만 반환 (간단 알림용)."""
    if state is None:
        state = calc_portfolio_state()
    out = []
    for cat, d in state.get("categories", {}).items():
        if abs(d["drift_pct"]) >= DRIFT_THRESHOLD:
            out.append({
                "category": cat,
                "drift_pct": d["drift_pct"] * 100,
                "current_pct": d["current_pct"] * 100,
                "target_pct": d["target_pct"] * 100,
                "preferred": d["preferred"],
                "value_gap": (d["target_pct"] - d["current_pct"]) * state["total"],
            })
    return out


def build_rebalance_section(state: dict | None = None) -> str:
    if state is None:
        state = calc_portfolio_state()
    if state["total"] <= 0:
        return ""

    total = state["total"]
    lines = ["<b>⚖️ 포트폴리오 리밸런싱</b>"]
    lines.append(f"  총 자산  <b>${total:,.2f}</b>")
    lines.append("")

    actions: list[str] = []
    for cat, d in state["categories"].items():
        cur_pct = d["current_pct"] * 100
        tgt_pct = d["target_pct"] * 100
        drift = d["drift_pct"] * 100

        if abs(d["drift_pct"]) < DRIFT_THRESHOLD:
            emoji = "🟢"
        elif d["drift_pct"] > 0:
            emoji = "🔴"
        else:
            emoji = "🔵"

        sign = "+" if drift >= 0 else ""
        lines.append(
            f"  {emoji} <b>{cat}</b>  {cur_pct:.1f}% / {tgt_pct:.0f}%  ({sign}{drift:.1f}%p)"
        )

        if abs(d["drift_pct"]) >= DRIFT_THRESHOLD:
            gap = abs(d["drift_pct"]) * total
            if d["drift_pct"] > 0:
                actions.append(
                    f"🔴 <b>{cat}</b> 오버웨이트 ${gap:,.0f} — 신규 매수 일시중단"
                )
            else:
                tip = f" ({', '.join(d['preferred'])} 위주)" if d["preferred"] else ""
                actions.append(
                    f"🔵 <b>{cat}</b> 언더웨이트 ${gap:,.0f} 추가 매수{tip}"
                )

    if state.get("unclassified"):
        lines.append("")
        for t, v in sorted(state["unclassified"].items(), key=lambda x: -x[1]):
            pct = v / total * 100
            lines.append(f"  ⚪ <b>{t}</b> (분류 외)  {pct:.1f}%  ${v:,.0f}")

    if actions:
        lines.append("")
        lines.append("<b>📌 권장 액션</b>")
        lines.extend(f"  {a}" for a in actions)
    else:
        lines.append("\n  ✅ 모든 카테고리 ±5%p 이내 — 리밸런싱 불필요")

    notes = [d["note"] for d in state["categories"].values() if d.get("note")]
    if notes:
        lines.append("")
        for n in notes:
            lines.append(f"  <i>※ {n}</i>")

    return "\n".join(lines)


if __name__ == "__main__":
    print(build_rebalance_section())
