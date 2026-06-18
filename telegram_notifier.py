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


def _split_chunks(text: str, limit: int = 4000) -> list[str]:
    """텔레그램 4096자 제한 대응 — 줄 단위로 안전하게 분할.

    글자 수로 무작정 자르면 <a href>·<b> 같은 HTML 태그 한가운데가 잘려
    parse_mode=HTML 전송이 400 에러로 통째 실패함. 줄 경계(\n)에서만 잘라
    각 조각의 태그가 항상 닫혀 있도록 보장한다.
    """
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # 한 줄 자체가 limit을 넘는 비정상 케이스 — 최후 수단으로 강제 분할
        if len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i+limit])
            continue
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def send_message(text: str, chat_id: str = "") -> bool:
    """HTML 형식으로 텔레그램 메시지 전송 (4096자 초과 시 줄 단위 분할)."""
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not cid:
        print("[Telegram] BOT_TOKEN 또는 CHAT_ID 미설정 — .env 파일을 확인하세요.")
        return False
    ok = True
    for chunk in _split_chunks(text):
        try:
            _api(
                "sendMessage",
                chat_id=cid,
                text=chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            # HTML 파싱 실패(400 등) — 해당 조각만 태그 제거 후 평문으로 재전송
            print(f"[Telegram] HTML 전송 실패, 평문 재시도: {e}")
            try:
                import re
                plain = re.sub(r"<[^>]+>", "", chunk)
                _api("sendMessage", chat_id=cid, text=plain,
                     disable_web_page_preview=True)
            except Exception as e2:
                print(f"[Telegram] 평문 재시도도 실패: {e2}")
                ok = False
    return ok


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
