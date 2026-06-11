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

_FLEX_EXC = (requests.RequestException, ET.ParseError, RuntimeError, ValueError)


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


def parse_positions(root: ET.Element) -> dict:
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


def parse_cash(root: ET.Element) -> float:
    """USD 현금 잔고. 계좌가 USD 단일통화면 currency="USD" 항목이 없고
    "BASE_SUMMARY" 한 줄만 나오는 경우가 있어 그쪽도 폴백으로 확인."""
    base_summary = None
    for cash in root.iter("CashReportCurrency"):
        cur = cash.get("currency")
        ending = cash.get("endingCash") or cash.get("endingSettledCash") or 0
        try:
            ending = float(ending)
        except (ValueError, TypeError):
            ending = 0.0
        if cur == "USD":
            return ending
        if cur == "BASE_SUMMARY":
            base_summary = ending
    return base_summary or 0.0


def parse_trades(root: ET.Element) -> list[dict]:
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
    IBKR 계좌 전체 조회. 토큰 미설정/네트워크 오류 시 error 필드에 메시지.
    Returns: {positions, cash_usd, trades, error}
    """
    try:
        root = ET.fromstring(fetch_flex_xml())
        return {
            "positions": parse_positions(root),
            "cash_usd":  parse_cash(root),
            "trades":    parse_trades(root),
            "error":     None,
        }
    except _FLEX_EXC as e:
        return {
            "error":     str(e),
            "positions": {},
            "cash_usd":  0.0,
            "trades":    [],
        }


def resolve_holdings_and_cash(config) -> tuple[dict, float, dict]:
    """
    IBKR 실계좌 우선, 실패 시 config 폴백.
    Returns: (holdings, idle_cash, ibkr_data)
    ibkr_data["error"]가 None이면 IBKR 사용 중 → 호출자가 추가 섹션 빌드 가능.
    """
    data = get_account_data()
    if data["error"] is None and data["positions"]:
        holdings = {sym: d["qty"] for sym, d in data["positions"].items()}
        return holdings, data["cash_usd"], data
    if data["error"] and os.getenv("IBKR_FLEX_TOKEN"):
        print(f"[ibkr] 조회 실패 — config 폴백: {data['error']}")
    return config.HOLDINGS, config.IDLE_CASH_USD, data


def _fmt_qty(qty: float) -> str:
    return f"{qty:.0f}주" if qty.is_integer() else f"{qty:.2f}주"


def build_account_section(positions: dict, cash_usd: float) -> str:
    """아침 요약용 계좌 현황 섹션."""
    if not positions and cash_usd <= 0:
        return ""

    items = []
    for sym, d in positions.items():
        val = d["mark_price"] * d["qty"]
        items.append((sym, d, val))
    items.sort(key=lambda x: -x[2])

    total = cash_usd + sum(val for _, _, val in items)
    lines = [f"<b>💼 계좌 현황</b>  총 <b>${total:,.0f}</b>"]

    for sym, d, val in items:
        pnl     = d["unrealized_pnl"]
        cost    = d["cost_basis"] * d["qty"]
        pnl_pct = pnl / cost * 100 if cost else 0
        sign    = "+" if pnl >= 0 else ""
        emoji   = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"  {emoji} <b>{sym}</b>  {_fmt_qty(d['qty'])}  ${val:,.0f}"
            f"  {sign}${pnl:,.0f} ({sign}{pnl_pct:.1f}%)"
        )

    if cash_usd > 0:
        lines.append(f"  💵 현금  ${cash_usd:,.0f}")

    return "\n".join(lines)
