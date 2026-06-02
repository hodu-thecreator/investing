#!/usr/bin/env python3
"""
거래(매수/매도) 기록 관리 — 평균 단가, 실현/미실현 손익 계산.

데이터는 .transactions.json 으로 영속화.
GitHub Actions에서는 actions/cache 로 보존.
"""
import json
from datetime import datetime
from pathlib import Path
import yfinance as yf

TRANSACTIONS_FILE = Path(__file__).parent / ".transactions.json"


def _load() -> list[dict]:
    try:
        return json.loads(TRANSACTIONS_FILE.read_text())
    except Exception:
        return []


def _save(records: list[dict]):
    TRANSACTIONS_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2)
    )


def add_buy(ticker: str, qty: float, price: float, date: str | None = None) -> dict:
    records = _load()
    rec = {
        "type": "buy",
        "ticker": ticker.upper(),
        "qty": float(qty),
        "price": float(price),
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    records.append(rec)
    _save(records)
    return rec


def add_sell(ticker: str, qty: float, price: float, date: str | None = None) -> dict:
    records = _load()
    rec = {
        "type": "sell",
        "ticker": ticker.upper(),
        "qty": float(qty),
        "price": float(price),
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    records.append(rec)
    _save(records)
    return rec


def undo_last() -> dict | None:
    records = _load()
    if not records:
        return None
    last = records.pop()
    _save(records)
    return last


def _aggregate() -> dict:
    """거래 기록 → 티커별 {qty, total_cost, realized}"""
    holdings: dict[str, dict] = {}
    for r in _load():
        t = r["ticker"]
        if t not in holdings:
            holdings[t] = {"qty": 0.0, "total_cost": 0.0, "realized": 0.0}
        h = holdings[t]
        if r["type"] == "buy":
            h["qty"] += r["qty"]
            h["total_cost"] += r["qty"] * r["price"]
        else:
            avg = h["total_cost"] / h["qty"] if h["qty"] > 0 else r["price"]
            h["realized"] += r["qty"] * (r["price"] - avg)
            h["qty"] -= r["qty"]
            h["total_cost"] -= r["qty"] * avg
            if h["qty"] < 1e-6:
                h["qty"] = 0
                h["total_cost"] = 0
    return holdings


def realized_ytd(year: int | None = None) -> float:
    """올해(또는 지정 연도) 실현 차익 합계 (USD). 평단 추적으로 계산."""
    year = year or datetime.now().year
    holdings: dict[str, dict] = {}
    realized = 0.0
    for r in sorted(_load(), key=lambda x: x.get("date", "")):
        t = r["ticker"]
        h = holdings.setdefault(t, {"qty": 0.0, "cost": 0.0})
        if r["type"] == "buy":
            h["qty"] += r["qty"]
            h["cost"] += r["qty"] * r["price"]
        else:  # sell
            avg = h["cost"] / h["qty"] if h["qty"] > 0 else r["price"]
            pnl = r["qty"] * (r["price"] - avg)
            try:
                sell_year = datetime.fromisoformat(r.get("ts", r["date"])).year
            except Exception:
                try:
                    sell_year = datetime.strptime(r["date"], "%Y-%m-%d").year
                except Exception:
                    sell_year = year
            if sell_year == year:
                realized += pnl
            h["qty"] -= r["qty"]
            h["cost"] -= r["qty"] * avg
            if h["qty"] < 1e-6:
                h["qty"] = 0.0
                h["cost"] = 0.0
    return round(realized, 2)


def _current_price(ticker: str) -> float | None:
    try:
        df = yf.Ticker(ticker).history(period="5d")
        if df.empty:
            return None
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def portfolio_summary() -> dict:
    agg = _aggregate()
    summary = {}
    for t, h in agg.items():
        if h["qty"] < 1e-6 and abs(h["realized"]) < 0.01:
            continue
        avg = h["total_cost"] / h["qty"] if h["qty"] > 0 else 0
        cur = _current_price(t) if h["qty"] > 0 else avg
        unrealized = h["qty"] * (cur - avg) if cur and h["qty"] > 0 else 0
        summary[t] = {
            "qty": round(h["qty"], 4),
            "avg_price": round(avg, 4) if avg else 0,
            "current_price": round(cur, 2) if cur else None,
            "unrealized": round(unrealized, 2),
            "realized": round(h["realized"], 2),
        }
    return summary


def format_portfolio() -> str:
    s = portfolio_summary()
    if not s:
        return (
            "📭 거래 기록 없음\n\n"
            "사용법:\n"
            "  /buy TICKER QTY PRICE\n"
            "  예: /buy QQQM 1 220.50"
        )

    lines = ["<b>💼 포트폴리오 (거래 기록 기반)</b>"]
    total_unr = 0.0
    total_real = 0.0
    total_cost = 0.0
    total_value = 0.0

    for t, d in sorted(s.items()):
        if d["qty"] < 1e-6:
            if abs(d["realized"]) >= 0.01:
                lines.append(f"⚪ <b>{t}</b>  청산  실현 ${d['realized']:+,.2f}")
                total_real += d["realized"]
            continue
        if d["current_price"] is None:
            lines.append(f"⚪ <b>{t}</b>  {d['qty']}주  평단 ${d['avg_price']:.2f}  (현재가 조회 실패)")
            continue
        pct = (d["current_price"] - d["avg_price"]) / d["avg_price"] * 100 if d["avg_price"] else 0
        emoji = "🟢" if d["unrealized"] >= 0 else "🔴"
        lines.append(
            f"{emoji} <b>{t}</b>  {d['qty']}주  평단 ${d['avg_price']:.2f}  현재 ${d['current_price']:.2f}\n"
            f"   미실현 <b>${d['unrealized']:+,.2f}</b>  ({pct:+.1f}%)"
        )
        total_unr += d["unrealized"]
        total_real += d["realized"]
        total_cost += d["qty"] * d["avg_price"]
        total_value += d["qty"] * d["current_price"]

    lines.append("─────────────────")
    lines.append(f"  매수원금  ${total_cost:,.2f}")
    lines.append(f"  현재가치  ${total_value:,.2f}")
    lines.append(f"  <b>미실현  ${total_unr:+,.2f}  ({total_unr/total_cost*100:+.1f}%)</b>"
                 if total_cost > 0 else f"  <b>미실현  ${total_unr:+,.2f}</b>")
    if abs(total_real) > 0.01:
        lines.append(f"  <b>실현    ${total_real:+,.2f}</b>")
    return "\n".join(lines)


def format_history(limit: int = 10) -> str:
    records = _load()
    if not records:
        return "📭 거래 기록 없음"
    lines = [f"<b>📜 최근 거래 ({min(limit, len(records))}건)</b>"]
    for r in records[-limit:][::-1]:
        emoji = "🟢" if r["type"] == "buy" else "🔴"
        lines.append(
            f"{emoji} {r['date']}  <b>{r['ticker']}</b>  "
            f"{r['type'].upper()} {r['qty']}@${r['price']:.2f}"
        )
    return "\n".join(lines)


_IBKR_ACTION_MAP = {"BUY": "buy", "SELL": "sell"}


def sync_from_ibkr(ibkr_trades: list[dict]) -> int:
    """IBKR 체결을 병합 — dedup 키: (date, ticker, type, qty, price)."""
    existing = _load()
    existing_keys = {
        (r["date"], r["ticker"], r["type"], float(r["qty"]), float(r["price"]))
        for r in existing
    }
    added = 0
    for t in ibkr_trades:
        rec_type = _IBKR_ACTION_MAP.get(str(t.get("action", "")).upper())
        if rec_type is None:
            continue
        try:
            symbol = t["symbol"].upper()
            qty    = float(t["qty"])
            price  = float(t["price"])
            tdate  = t["date"]
        except (KeyError, ValueError, TypeError):
            continue
        key = (tdate, symbol, rec_type, qty, price)
        if key in existing_keys:
            continue
        existing.append({
            "type":   rec_type,
            "ticker": symbol,
            "qty":    qty,
            "price":  price,
            "date":   tdate,
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "source": "ibkr",
        })
        existing_keys.add(key)
        added += 1
    if added:
        existing.sort(key=lambda r: r["date"])
        _save(existing)
    return added


if __name__ == "__main__":
    print(format_portfolio())
    print()
    print(format_history())
