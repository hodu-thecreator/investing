#!/usr/bin/env python3
"""
거래 기록 백업 — GitHub Gist (비공개) 으로 .transactions.json 업로드.

GitHub Actions에서 GIST_TOKEN (Personal Access Token, gist scope) 시크릿이 필요합니다.
최초 실행 시 새 Gist 생성 후 ID를 .bot_state.json 에 저장, 이후 업데이트.
"""
import json
import os
from pathlib import Path

import requests

TRANSACTIONS_FILE = Path(__file__).parent / ".transactions.json"
BOT_STATE_FILE = Path(__file__).parent / ".bot_state.json"
GIST_DESCRIPTION = "investing-bot-transactions"


def _load_state() -> dict:
    try:
        return json.loads(BOT_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    BOT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def backup(verbose: bool = True) -> bool:
    token = os.getenv("GIST_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        if verbose:
            print("[backup] GIST_TOKEN 미설정 — 스킵")
        return False

    if not TRANSACTIONS_FILE.exists():
        if verbose:
            print("[backup] .transactions.json 없음 — 스킵")
        return False

    content = TRANSACTIONS_FILE.read_text().strip()
    if not content or content == "[]":
        if verbose:
            print("[backup] 거래 기록 없음 — 스킵")
        return False

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "files": {
            "transactions.json": {"content": content},
        }
    }

    state = _load_state()
    gist_id = state.get("gist_id")

    try:
        if gist_id:
            r = requests.patch(
                f"https://api.github.com/gists/{gist_id}",
                json=payload, headers=headers, timeout=15,
            )
        else:
            payload["description"] = GIST_DESCRIPTION
            payload["public"] = False
            r = requests.post(
                "https://api.github.com/gists",
                json=payload, headers=headers, timeout=15,
            )
            if r.ok:
                gist_id = r.json()["id"]
                gist_url = r.json()["html_url"]
                state["gist_id"] = gist_id
                _save_state(state)
                if verbose:
                    print(f"[backup] Gist 생성: {gist_url}")
                # 새로 만든 Gist URL을 텔레그램으로 알림
                try:
                    from telegram_notifier import send_message
                    send_message(
                        f"📦 <b>거래 기록 백업 Gist 생성</b>\n"
                        f"<a href='{gist_url}'>북마크 해두세요</a>\n"
                        f"<i>이후 자동으로 업데이트됩니다</i>"
                    )
                except Exception:
                    pass

        if r.ok:
            records = json.loads(content)
            if verbose:
                print(f"[backup] OK — {len(records)}건 백업")
            return True
        else:
            if verbose:
                print(f"[backup] FAILED {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        if verbose:
            print(f"[backup] 오류: {e}")
        return False


if __name__ == "__main__":
    backup()
