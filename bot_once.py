#!/usr/bin/env python3
"""
GitHub Actions용 단발 봇 핸들러
미처리 텔레그램 명령을 한 번 읽고 응답한 뒤 종료합니다.

GitHub Actions에서 1분마다 실행 → 사실상 양방향 봇처럼 동작
"""

import json
import os
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from telegram_notifier import send_message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, _api
from daily_report import build_report, judge_ticker, market_score
from market_indicators import collect_all

# 마지막으로 처리한 update_id를 파일에 저장 (GitHub Actions cache로 유지)
STATE_FILE = Path(__file__).parent / ".bot_state.json"


def load_offset() -> int:
    try:
        return json.loads(STATE_FILE.read_text()).get("offset", 0)
    except Exception:
        return 0


def save_offset(offset: int):
    STATE_FILE.write_text(json.dumps({"offset": offset}))


def handle_report(chat_id: int):
    try:
        send_message("⏳ 분석 중... 잠시만 기다려주세요.", chat_id=str(chat_id))
        report = build_report()
        send_message(report, chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_check(chat_id: int, ticker: str):
    try:
        ticker = ticker.upper().strip()
        if not ticker:
            send_message("❌ 사용법: /check TICKER  (예: /check TQQQ)", chat_id=str(chat_id))
            return

        send_message(f"⏳ {ticker} 분석 중...", chat_id=str(chat_id))
        indicators = collect_all()
        mkt_s, mkt_reasons = market_score(indicators)
        result = judge_ticker(ticker, mkt_s)

        action = result["action"]
        price  = result["price"]
        dd     = result["drawdown"]
        reasons = result["reasons"]
        score  = result["score"]

        mkt_label = (
            "🟢 매수 우호적" if mkt_s >= 4 else
            "🟡 중립" if mkt_s >= 2 else
            "🔴 리스크 높음" if mkt_s <= -2 else
            "⚪ 중립"
        )

        lines = [
            f"<b>{ticker} 즉시 분석</b>  {datetime.now().strftime('%H:%M')}",
            "",
            f"현재가   : <b>${price:.2f}</b>",
            f"고점 대비: <b>{dd:+.1f}%</b>",
            f"종합점수 : {score:+d}",
            "",
            f"시장 환경: {mkt_label}",
        ]
        if mkt_reasons:
            lines.append("  " + " · ".join(mkt_reasons))
        lines += ["", f"<b>판단: {action}</b>"]
        if reasons:
            lines.append("<i>" + " · ".join(reasons) + "</i>")

        send_message("\n".join(lines), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_help(chat_id: int):
    send_message(
        "<b>📖 사용 가능한 명령어</b>\n\n"
        "/report — 전체 종합 판단 브리핑\n"
        "/check TICKER — 특정 종목 즉시 분석\n"
        "   예) /check TQQQ\n"
        "/help — 이 메시지\n\n"
        "<i>매일 08:00 KST 자동 브리핑이 전송됩니다.</i>",
        chat_id=str(chat_id),
    )


def dispatch(message: dict):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if TELEGRAM_CHAT_ID and str(chat_id) != TELEGRAM_CHAT_ID.strip():
        print(f"[bot] 무시: chat_id={chat_id} (허용={TELEGRAM_CHAT_ID})")
        return

    if not text.startswith("/"):
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""

    print(f"[bot] {cmd!r} {arg!r} from {chat_id}")

    if cmd == "/report":
        handle_report(chat_id)
    elif cmd == "/check":
        handle_check(chat_id, arg)
    elif cmd in ("/help", "/start"):
        handle_help(chat_id)
    else:
        send_message(f"❓ 알 수 없는 명령어: {cmd}\n/help 로 목록 확인", chat_id=str(chat_id))


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN 미설정")
        return

    offset = load_offset()
    print(f"[{datetime.now():%H:%M:%S}] 미처리 명령 확인 (offset={offset})")

    try:
        data = _api("getUpdates", offset=offset + 1, timeout=5, allowed_updates=["message"])
    except Exception as e:
        print(f"getUpdates 실패: {e}")
        return

    updates = data.get("result", [])
    print(f"  {len(updates)}개 업데이트")

    for update in updates:
        msg = update.get("message")
        if msg:
            try:
                dispatch(msg)
            except Exception as e:
                print(f"dispatch 오류: {e}")
        # 처리 여부와 무관하게 offset 갱신
        offset = update["update_id"]

    if updates:
        save_offset(offset)


if __name__ == "__main__":
    main()
