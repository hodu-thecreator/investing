#!/usr/bin/env python3
"""
양방향 텔레그램 봇
- 매일 08:00 자동 브리핑 전송
- 사용자 명령에 실시간 응답

명령어:
  /report           전체 종합 판단 브리핑
  /check TICKER     특정 종목 즉시 분석
  /help             명령어 목록
"""

import time
import threading
import schedule
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from telegram_notifier import send_message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, _api
from daily_report import build_report, judge_ticker, market_score
from market_indicators import collect_all

# ── 업데이트 폴링 ──────────────────────────────────────────────

_last_update_id = 0


def _get_updates(timeout: int = 30) -> list:
    global _last_update_id
    try:
        data = _api(
            "getUpdates",
            offset=_last_update_id + 1,
            timeout=timeout,
            allowed_updates=["message"],
        )
        updates = data.get("result", [])
        if updates:
            _last_update_id = updates[-1]["update_id"]
        return updates
    except Exception as e:
        print(f"[poll] getUpdates 오류: {e}")
        return []


def _reply(chat_id: int, text: str):
    send_message(text, chat_id=str(chat_id))


# ── 명령어 핸들러 ──────────────────────────────────────────────

def handle_report(chat_id: int):
    try:
        _reply(chat_id, "⏳ 분석 중... 잠시만 기다려주세요.")
        report = build_report()
        _reply(chat_id, report)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[handle_report] 오류:\n{tb}")
        _reply(chat_id, f"❌ 오류 발생:\n<code>{e}</code>")


def handle_check(chat_id: int, ticker: str):
    try:
        ticker = ticker.upper().strip()
        if not ticker:
            _reply(chat_id, "❌ 사용법: /check TICKER  (예: /check TQQQ)")
            return

        _reply(chat_id, f"⏳ {ticker} 분석 중...")
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

        lines += [
            "",
            f"<b>판단: {action}</b>",
        ]
        if reasons:
            lines.append("<i>" + " · ".join(reasons) + "</i>")

        _reply(chat_id, "\n".join(lines))
    except Exception as e:
        import traceback
        print(f"[handle_check] 오류:\n{traceback.format_exc()}")
        _reply(chat_id, f"❌ 오류 발생:\n<code>{e}</code>")


def handle_help(chat_id: int):
    text = (
        "<b>📖 사용 가능한 명령어</b>\n\n"
        "/report — 전체 종합 판단 브리핑\n"
        "/check TICKER — 특정 종목 즉시 분석\n"
        "   예) /check TQQQ\n"
        "/help — 이 메시지\n\n"
        "<i>매일 08:00 KST 자동 브리핑이 전송됩니다.</i>"
    )
    _reply(chat_id, text)


def dispatch(message: dict):
    """메시지를 파싱해 적절한 핸들러로 라우팅"""
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    # 봇 허용 Chat ID 검증 (설정된 경우만)
    if TELEGRAM_CHAT_ID and str(chat_id) != TELEGRAM_CHAT_ID:
        print(f"[bot] 무시된 chat_id: {chat_id}")
        return

    if not text.startswith("/"):
        return  # 명령어가 아닌 메시지 무시

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # /cmd@botname 형식 대응
    arg = parts[1] if len(parts) > 1 else ""

    print(f"[bot] 명령: {cmd!r}  인자: {arg!r}  from chat_id={chat_id}")

    if cmd == "/report":
        threading.Thread(target=handle_report, args=(chat_id,), daemon=True).start()
    elif cmd == "/check":
        threading.Thread(target=handle_check, args=(chat_id, arg), daemon=True).start()
    elif cmd == "/help" or cmd == "/start":
        handle_help(chat_id)
    else:
        _reply(chat_id, f"❓ 알 수 없는 명령어: {cmd}\n/help 로 명령어 목록을 확인하세요.")


# ── 스케줄러 ───────────────────────────────────────────────────

def scheduled_report():
    print(f"[{datetime.now():%H:%M:%S}] 정기 브리핑 전송 중...")
    report = build_report()
    send_message(report)


def run_scheduler():
    schedule.every().day.at("08:00").do(scheduled_report)
    print("[scheduler] 매일 08:00 자동 브리핑 예약됨")
    while True:
        schedule.run_pending()
        time.sleep(30)


# ── 메인 폴링 루프 ─────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN이 .env에 설정되지 않았습니다.")
        return

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 양방향 봇 시작")
    send_message("🤖 <b>투자 봇 시작됨</b>\n/help 로 명령어를 확인하세요.")

    # 스케줄러 백그라운드 스레드
    threading.Thread(target=run_scheduler, daemon=True).start()

    # 메인 스레드: 폴링 루프
    print("[bot] 메시지 수신 대기 중...")
    while True:
        updates = _get_updates(timeout=30)
        for update in updates:
            msg = update.get("message")
            if msg:
                try:
                    dispatch(msg)
                except Exception as e:
                    print(f"[bot] dispatch 오류: {e}")


if __name__ == "__main__":
    main()
