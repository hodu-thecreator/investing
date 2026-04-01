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
import anthropic

load_dotenv()

from telegram_notifier import send_message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, _api
from daily_report import build_report, judge_ticker, market_score
from market_indicators import collect_all
from blog_ideas import generate_blog_ideas
from config import Config

_config = Config()
_claude = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY)

# chat_id → 대화 히스토리 (최대 20턴 유지)
_chat_histories: dict[str, list] = {}

# ── 모델 자동 선택 ─────────────────────────────────────────────

_MODEL_HAIKU  = "claude-haiku-4-5-20251001"   # 단순 질문
_MODEL_SONNET = "claude-sonnet-4-6"            # 일반 분석
_MODEL_OPUS   = "claude-opus-4-6"              # 심층 분석

# 복잡도를 높이는 키워드 (각 +2, 최대 +6까지 누적)
_COMPLEX_KEYWORDS = [
    "포트폴리오", "최적화", "백테스트", "리밸런싱", "자산배분",
    "헷지", "파생상품", "옵션", "선물", "공매도",
    "거시경제", "금리", "인플레이션", "연준", "fed",
    "상관관계", "변동성", "샤프지수",
    "심층", "자세히", "상세히", "이유를 설명",
    "전략을 세워", "어떻게 해야", "어떻게 생각",
]

# 투자 관련 키워드 (단순하지 않음을 보장, +1)
_INVEST_KEYWORDS = [
    "나스닥", "s&p", "코스피", "코스닥", "주식", "etf", "코인",
    "암호화폐", "매수", "매도", "주가", "실적", "섹터", "종목",
    "차트", "기술적", "펀더멘털", "배당",
]

# 단순 응답 키워드 (길이가 짧을 때만 적용, -3)
_SIMPLE_KEYWORDS = [
    "안녕", "고마워", "감사", "ㅋㅋ", "ㅎㅎ", "응", "맞아", "알겠어",
]


def _select_model(text: str) -> tuple[str, int]:
    """질문 복잡도에 따라 (model_id, max_tokens) 반환"""
    import re
    length = len(text)
    lower = text.lower()
    question_marks = text.count("?") + text.count("？")
    extra_sentences = text.count(".") + text.count("。") + text.count("\n")

    score = 0
    score += min(length // 40, 4)                          # 길이: 최대 +4
    score += min(question_marks, 3)                        # 물음표: 최대 +3
    score += min(extra_sentences, 2)                       # 문장 수: 최대 +2

    # 주식 티커 감지 (2~5자 대문자, 예: NVDA, TQQQ)
    if re.search(r'\b[A-Z]{2,5}\b', text):
        score += 2

    # 투자 관련 키워드 (+1, 단순 아님 보장)
    for kw in _INVEST_KEYWORDS:
        if kw in lower:
            score += 1
            break

    # 복잡 키워드 (+2씩, 최대 +6)
    complex_bonus = 0
    for kw in _COMPLEX_KEYWORDS:
        if kw in lower:
            complex_bonus += 2
            if complex_bonus >= 6:
                break
    score += complex_bonus

    # 단순 응답 (짧은 메시지에서만 페널티)
    if length < 20:
        for kw in _SIMPLE_KEYWORDS:
            if kw in lower:
                score -= 3
                break

    if score <= 1:
        return _MODEL_HAIKU, 512
    elif score <= 4:
        return _MODEL_SONNET, 1024
    else:
        return _MODEL_OPUS, 2048

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


def handle_chat(chat_id: int, text: str):
    """일반 텍스트 메시지를 Claude에게 전달하고 응답 반환"""
    key = str(chat_id)
    history = _chat_histories.setdefault(key, [])

    model, max_tokens = _select_model(text)
    model_label = {
        _MODEL_HAIKU:  "Haiku",
        _MODEL_SONNET: "Sonnet",
        _MODEL_OPUS:   "Opus",
    }[model]
    print(f"[chat] chat_id={chat_id}  model={model_label}  max_tokens={max_tokens}  len={len(text)}")

    history.append({"role": "user", "content": text})

    try:
        resp = _claude.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=(
                "당신은 주식·암호화폐 투자 전문 AI 어시스턴트입니다. "
                "한국어로 친절하고 간결하게 답변하세요. "
                "투자 관련 질문에는 데이터와 근거를 바탕으로 답변하고, "
                "일반 질문도 성실히 답변하세요."
            ),
            messages=history,
        )
        answer = resp.content[0].text
        history.append({"role": "assistant", "content": answer})

        # 히스토리 최대 20턴(40개 메시지) 유지
        if len(history) > 40:
            _chat_histories[key] = history[-40:]

        _reply(chat_id, answer)
    except Exception as e:
        import traceback
        print(f"[handle_chat] 오류:\n{traceback.format_exc()}")
        _reply(chat_id, f"❌ Claude 응답 오류: {e}")


def handle_reset(chat_id: int):
    """대화 히스토리 초기화"""
    _chat_histories.pop(str(chat_id), None)
    _reply(chat_id, "🗑 대화 기록이 초기화되었습니다.")


def handle_help(chat_id: int):
    text = (
        "<b>📖 사용 가능한 명령어</b>\n\n"
        "/report — 전체 종합 판단 브리핑\n"
        "/check TICKER — 특정 종목 즉시 분석\n"
        "   예) /check TQQQ\n"
        "/reset — Claude와의 대화 기록 초기화\n"
        "/help — 이 메시지\n\n"
        "<i>명령어 외 일반 메시지는 Claude AI가 직접 답변합니다.</i>\n"
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
        # 일반 메시지 → Claude 자유 대화
        threading.Thread(target=handle_chat, args=(chat_id, text), daemon=True).start()
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # /cmd@botname 형식 대응
    arg = parts[1] if len(parts) > 1 else ""

    print(f"[bot] 명령: {cmd!r}  인자: {arg!r}  from chat_id={chat_id}")

    if cmd == "/report":
        threading.Thread(target=handle_report, args=(chat_id,), daemon=True).start()
    elif cmd == "/check":
        threading.Thread(target=handle_check, args=(chat_id, arg), daemon=True).start()
    elif cmd == "/reset":
        handle_reset(chat_id)
    elif cmd == "/help" or cmd == "/start":
        handle_help(chat_id)
    else:
        _reply(chat_id, f"❓ 알 수 없는 명령어: {cmd}\n/help 로 명령어 목록을 확인하세요.")


# ── 스케줄러 ───────────────────────────────────────────────────

def scheduled_report():
    print(f"[{datetime.now():%H:%M:%S}] 정기 브리핑 전송 중...")
    report = build_report()
    send_message(report)

    print(f"[{datetime.now():%H:%M:%S}] 블로그 아이디어 생성 중...")
    try:
        ideas = generate_blog_ideas()
        send_message(ideas)
    except Exception as e:
        print(f"[scheduled_report] 블로그 아이디어 오류: {e}")


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
