#!/usr/bin/env python3
"""
IBKR Flex Query API — 포지션/현금/체결 조회 (읽기 전용).
두 단계: SendRequest → GetStatement (XML 파싱).
환경변수: IBKR_FLEX_TOKEN, IBKR_FLEX_QUERY_ID
"""
import os
import time
import xml.etree.ElementTree as ET

import requests

_SEND_URL = "https://ndcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
_GET_URL  = "https://ndcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"


def _send_request(token: str, query_id: str) -> str:
    r = requests.get(_SEND_URL, params={"t": token, "q": query_id, "v": "3"}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.findtext("Status") != "Success":
        raise RuntimeError(f"SendRequest 실패: {root.findtext('ErrorMessage')}")
    return root.findtext("ReferenceCode")


def _get_statement(token: str, ref_code: str) -> str:
    for attempt in range(5):
        time.sleep(2 + attempt * 2)
        r = requests.get(_GET_URL, params={"t": token, "q": ref_code, "v": "3"}, timeout=30)
        r.raise_for_status()
        if "<FlexStatements" in r.text:
            return r.text
    raise RuntimeError("GetStatement 타임아웃 (5회 시도)")


def fetch_flex_xml() -> str:
    token    = os.environ.get("IBKR_FLEX_TOKEN", "")
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID", "")
    if not token or not query_id:
        raise RuntimeError("IBKR_FLEX_TOKEN 또는 IBKR_FLEX_QUERY_ID 미설정")
    ref = _send_request(token, query_id)
    return _get_statement(token, ref)


def parse_positions(xml_text: str) -> dict:
    """Returns {symbol: {qty, cost_basis, mark_price, unrealized_pnl}}"""
    root = ET.fromstring(xml_text)
    out: dict = {}
    for pos in root.iter("OpenPosition"):
        sym = pos.get("symbol")
        qty_str = pos.get("position") or pos.get("quantity", "0")
        if not sym:
            continue
        try:
            qty = float(qty_str)
            if qty <= 0:
                continue
            out[sym] = {
                "qty":            qty,
                "cost_basis":     float(pos.get("costBasisPrice") or 0),
                "mark_price":     float(pos.get("markPrice") or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnL") or 0),
            }
        except (ValueError, TypeError):
            continue
    return out


def parse_cash(xml_text: str) -> float:
    """Returns USD ending cash balance."""
    root = ET.fromstring(xml_text)
    for cash in root.iter("CashReportCurrency"):
        if cash.get("currency") == "USD":
            try:
                return float(cash.get("endingCash") or 0)
            except (ValueError, TypeError):
                pass
    return 0.0


def parse_trades(xml_text: str) -> list[dict]:
    """Returns list of trades as {symbol, date, action, qty, price}."""
    root = ET.fromstring(xml_text)
    trades = []
    for t in root.iter("Trade"):
        sym    = t.get("symbol")
        date_s = t.get("tradeDate")
        action = t.get("buySell")
        qty    = t.get("quantity")
        price  = t.get("tradePrice")
        if not all([sym, date_s, action, qty, price]):
            continue
        try:
            trades.append({
                "symbol": sym,
                "date":   date_s,
                "action": action,
                "qty":    abs(float(qty)),
                "price":  float(price),
            })
        except (ValueError, TypeError):
            continue
    return trades


def get_account_data() -> dict:
    """
    IBKR 계좌 전체 조회.
    토큰 미설정 시 error 반환 → 호출자가 config 폴백 처리.
    Returns: {holdings, positions, cash_usd, trades, error}
    """
    try:
        xml       = fetch_flex_xml()
        positions = parse_positions(xml)
        cash      = parse_cash(xml)
        trades    = parse_trades(xml)
        holdings  = {sym: d["qty"] for sym, d in positions.items()}
        return {
            "holdings":  holdings,
            "positions": positions,
            "cash_usd":  cash,
            "trades":    trades,
            "error":     None,
        }
    except Exception as e:
        return {
            "error":     str(e),
            "holdings":  {},
            "positions": {},
            "cash_usd":  0.0,
            "trades":    [],
        }


def build_account_section(positions: dict, cash_usd: float) -> str:
    """아침 요약용 계좌 현황 섹션."""
    if not positions and cash_usd <= 0:
        return ""

    total = cash_usd + sum(
        d["mark_price"] * d["qty"] for d in positions.values()
    )
    lines = [f"<b>💼 계좌 현황</b>  총 <b>${total:,.0f}</b>"]

    for sym, d in sorted(positions.items(), key=lambda x: -x[1]["mark_price"] * x[1]["qty"]):
        val     = d["mark_price"] * d["qty"]
        pnl     = d["unrealized_pnl"]
        cost    = d["cost_basis"] * d["qty"]
        pnl_pct = pnl / cost * 100 if cost else 0
        sign    = "+" if pnl >= 0 else ""
        emoji   = "🟢" if pnl >= 0 else "🔴"
        qty_str = f"{d['qty']:.0f}주" if d["qty"] == int(d["qty"]) else f"{d['qty']:.2f}주"
        lines.append(
            f"  {emoji} <b>{sym}</b>  {qty_str}  ${val:,.0f}"
            f"  {sign}${pnl:,.0f} ({sign}{pnl_pct:.1f}%)"
        )

    if cash_usd > 0:
        lines.append(f"  💵 현금  ${cash_usd:,.0f}")

    return "\n".join(lines)
