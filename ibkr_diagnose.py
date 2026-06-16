#!/usr/bin/env python3
"""
IBKR Flex Query 연동 진단 — GitHub Actions에서 수동 실행(workflow_dispatch).

체인의 어느 단계에서 끊기는지 단계별로 확인해 텔레그램 + 로그로 보고:
  1. Secrets 주입 여부 (IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID)
  2. SendRequest 응답 (토큰 만료·IP 제한·쿼리 ID 오류 등 에러코드 해석)
  3. GetStatement 응답의 섹션 구성 (OpenPosition / CashReport / Trade 개수)
"""
import os
import xml.etree.ElementTree as ET

import requests

import ibkr_flex

# IBKR Flex 에러코드 → 원인/조치 (공식 문서 기준 주요 코드)
_ERROR_HINTS = {
    "1001": "Statement 생성 불가 (일시적) — Flex Query의 Period 설정이 'Last Business Day'나 'Today'이면 "
            "'Last 30 Days'로 변경하세요. 장 마감 전·주말엔 데이터가 없어 이 오류가 납니다.",
    "1003": "Statement 생성 불가 — Query ID가 잘못됐거나 해당 쿼리에 권한 없음",
    "1012": "토큰 만료 — IBKR 사이트에서 Flex Web Service 토큰 재발급 필요",
    "1013": "IP 제한 — 토큰 설정의 IP 화이트리스트에 걸림 (GitHub Actions는 IP가 매번 바뀌므로 IP 제한 해제 필요)",
    "1015": "토큰 무효 — IBKR_FLEX_TOKEN 값 오타/공백 포함 여부 확인",
    "1018": "요청 과다 — 잠시 후 재시도",
    "1019": "Statement 생성 중 — 잠시 후 재시도",
    "1020": "요청 검증 실패 — 토큰/쿼리 ID 조합 확인",
    "1021": "Query 무효 — IBKR_FLEX_QUERY_ID 값 확인",
}


def diagnose() -> str:
    lines = ["<b>🔧 IBKR Flex 연동 진단</b>"]

    # 1) Secrets 주입 확인
    token = os.environ.get("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.environ.get("IBKR_FLEX_QUERY_ID", "").strip()
    lines.append(
        f"  1️⃣ IBKR_FLEX_TOKEN: {'✅ 설정됨 (' + str(len(token)) + '자)' if token else '❌ 비어있음'}"
    )
    lines.append(
        f"  1️⃣ IBKR_FLEX_QUERY_ID: {'✅ 설정됨 (' + str(len(query_id)) + '자)' if query_id else '❌ 비어있음'}"
    )
    if not token or not query_id:
        lines.append("")
        lines.append("  👉 GitHub 저장소 → Settings → Secrets and variables →")
        lines.append("     Actions 에 위 이름 그대로 등록돼 있는지 확인하세요.")
        lines.append("     (이름 오타·앞뒤 공백이 가장 흔한 원인)")
        return "\n".join(lines)

    # 2) SendRequest
    try:
        r = requests.get(
            ibkr_flex._SEND_URL,
            params={"t": token, "q": query_id, "v": "3"},
            timeout=15,
        )
        r.raise_for_status()
        root = ET.fromstring(r.text)
        status = root.findtext("Status")
        if status != "Success":
            code = root.findtext("ErrorCode") or "?"
            msg = root.findtext("ErrorMessage") or ""
            hint = _ERROR_HINTS.get(code, "")
            lines.append(f"  2️⃣ SendRequest: ❌ 실패 (code {code})")
            lines.append(f"     {msg}")
            if hint:
                lines.append(f"     👉 {hint}")
            return "\n".join(lines)
        ref = root.findtext("ReferenceCode")
        lines.append("  2️⃣ SendRequest: ✅ 성공")
    except Exception as e:
        lines.append(f"  2️⃣ SendRequest: ❌ 네트워크 오류 — {e}")
        return "\n".join(lines)

    # 3) GetStatement + 섹션 구성
    try:
        xml_text = ibkr_flex._get_statement(token, ref)
        root = ET.fromstring(xml_text)
        n_pos = len(list(root.iter("OpenPosition")))
        n_cash = len(list(root.iter("CashReportCurrency")))
        n_trade = len(list(root.iter("Trade")))
        lines.append("  3️⃣ GetStatement: ✅ 성공")
        lines.append(f"     포지션 {n_pos}건 · 현금항목 {n_cash}건 · 체결 {n_trade}건")
        if n_pos == 0:
            lines.append("     ⚠️ 포지션 0건 → Flex Query 설정에서")
            lines.append("        'Open Positions' 섹션을 추가하세요.")
        if n_cash == 0:
            lines.append("     ⚠️ 현금항목 0건 → 'Cash Report' 섹션을 추가하세요.")
        else:
            for cash_el in root.iter("CashReportCurrency"):
                cur = cash_el.get("currency")
                ending = cash_el.get("endingCash")
                ending_settled = cash_el.get("endingSettledCash")
                lines.append(
                    f"     현금항목 raw: currency={cur} endingCash={ending}"
                    f" endingSettledCash={ending_settled}"
                )
        if n_pos > 0:
            positions = ibkr_flex.parse_positions(root)
            cash = ibkr_flex.parse_cash(root)
            top = sorted(positions.items(), key=lambda kv: -kv[1]["qty"])[:5]
            tickers = ", ".join(f"{s} {d['qty']:g}주" for s, d in top)
            lines.append(f"     예시: {tickers}")
            lines.append(f"     USD 현금: ${cash:,.2f}")
            lines.append("")
            lines.append("  🎉 연동 정상 — 봇이 이 데이터를 사용합니다.")
    except Exception as e:
        lines.append(f"  3️⃣ GetStatement: ❌ 실패 — {e}")

    return "\n".join(lines)


def main():
    report = diagnose()
    print(report.replace("<b>", "").replace("</b>", ""))
    try:
        from telegram_notifier import send_message
        send_message(report)
    except Exception as e:
        print(f"[diagnose] 텔레그램 발송 실패: {e}")


if __name__ == "__main__":
    main()
