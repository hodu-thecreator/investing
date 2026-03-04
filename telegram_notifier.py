#!/usr/bin/env python3
"""
Telegram Bot Notifier
텔레그램으로 보고서를 전송합니다.
Bot Token과 Chat ID는 .env 파일에서 읽습니다.
"""

import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str, **kwargs) -> dict:
    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    r = requests.post(url, json=kwargs, timeout=15)
    r.raise_for_status()
    return r.json()


def send_message(text: str, chat_id: str = "") -> bool:
    """마크다운 형식으로 텔레그램 메시지 전송"""
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        print("[Telegram] BOT_TOKEN 또는 CHAT_ID 미설정 — .env 파일을 확인하세요.")
        return False
    try:
        # 4096자 초과 시 분할 전송
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            _api(
                "sendMessage",
                chat_id=cid,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        return True
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")
        return False


def get_my_chat_id() -> None:
    """봇에게 아무 메시지를 보낸 후 이 함수로 Chat ID를 확인하세요."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN을 .env에 설정하세요.")
        return
    try:
        data = _api("getUpdates")
        updates = data.get("result", [])
        if not updates:
            print("봇에게 먼저 메시지를 보내주세요 (텔레그램 앱에서).")
            return
        for update in updates[-5:]:
            msg = update.get("message", {})
            chat = msg.get("chat", {})
            print(f"Chat ID: {chat.get('id')}  |  이름: {chat.get('first_name')} {chat.get('last_name','')}")
    except Exception as e:
        print(f"오류: {e}")


if __name__ == "__main__":
    import sys
    if "--get-chat-id" in sys.argv:
        get_my_chat_id()
    else:
        ok = send_message(f"✅ 텔레그램 알림 테스트 성공!\n{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("전송 성공" if ok else "전송 실패")
