#!/usr/bin/env python3
"""
GitHub Actions용 단발 봇 핸들러
미처리 텔레그램 명령을 한 번 읽고 응답한 뒤 종료합니다.
GitHub Actions에서 1분마다 실행 → 사실상 양방향 봇처럼 동작
"""

import json
import os
import anthropic
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from telegram_notifier import send_message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, _api
from daily_report import build_report, judge_ticker, market_score
from market_indicators import collect_all
from blog_ideas import generate_blog_ideas
from config import Config

_config = Config()
_claude = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY or None)

# ── 상태 파일 (update offset + 대화 이력) ─────────────────────
STATE_FILE = Path(__file__).parent / ".bot_state.json"
MAX_HISTORY = 40  # 메시지 최대 보관 수


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"offset": 0, "chat_histories": {}}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False))


# ── 모델 자동 선택 ────────────────────────────────────────────

_MODEL_HAIKU  = "claude-haiku-4-5-20251001"
_MODEL_SONNET = "claude-sonnet-4-6"
_MODEL_OPUS   = "claude-opus-4-6"

_COMPLEX_KEYWORDS = [
    "포트폴리오", "최적화", "백테스트", "리밸런싱", "자산배분",
    "헷지", "파생상품", "옵션", "선물", "공매도",
    "거시경제", "금리", "인플레이션", "연준", "fed",
    "상관관계", "변동성", "샤프지수",
    "심층", "자세히", "상세히", "이유를 설명",
    "전략을 세워", "어떻게 해야", "어떻게 생각",
]
_INVEST_KEYWORDS = [
    "나스닥", "s&p", "코스피", "코스닥", "주식", "etf", "코인",
    "암호화폐", "매수", "매도", "주가", "실적", "섹터", "종목",
    "차트", "기술적", "펀더멘털", "배당",
]
_SIMPLE_KEYWORDS = [
    "안녕", "고마워", "감사", "ㅋㅋ", "ㅎㅎ", "응", "맞아", "알겠어",
]


def _select_model(text: str) -> tuple[str, int]:
    import re
    length = len(text)
    lower = text.lower()
    score = 0
    score += min(length // 40, 4)
    score += min(text.count("?") + text.count("？"), 3)
    score += min(text.count(".") + text.count("。") + text.count("\n"), 2)
    if re.search(r'\b[A-Z]{2,5}\b', text):
        score += 2
    for kw in _INVEST_KEYWORDS:
        if kw in lower:
            score += 1
            break
    complex_bonus = 0
    for kw in _COMPLEX_KEYWORDS:
        if kw in lower:
            complex_bonus += 2
            if complex_bonus >= 6:
                break
    score += complex_bonus
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


# ── 인텐트 감지 ──────────────────────────────────────────────

_INTENT_REPORT = {"브리핑", "리포트", "주식", "투자", "포트폴리오", "report"}
_INTENT_BLOG   = {"콘텐츠", "블로그", "취향서랍", "아이디어", "소재"}


def _detect_intent(text: str) -> str | None:
    lower = text.lower()
    want_report = any(kw in lower for kw in _INTENT_REPORT)
    want_blog   = any(kw in lower for kw in _INTENT_BLOG)
    if want_report and want_blog:
        return "both"
    if want_report:
        return "report"
    if want_blog:
        return "blog"
    return None


# ── 핸들러 ───────────────────────────────────────────────────

def handle_report(chat_id: int):
    try:
        send_message("⏳ 분석 중... 잠시만 기다려주세요.", chat_id=str(chat_id))
        send_message(build_report(), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_blog_ideas(chat_id: int):
    try:
        send_message("⏳ 취향서랍 소재 생성 중...", chat_id=str(chat_id))
        send_message(generate_blog_ideas(), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_full_briefing(chat_id: int):
    handle_report(chat_id)
    handle_blog_ideas(chat_id)


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


def handle_chat(chat_id: int, text: str, state: dict):
    key = str(chat_id)
    histories = state.setdefault("chat_histories", {})
    history = histories.setdefault(key, [])
    model, max_tokens = _select_model(text)
    model_label = {_MODEL_HAIKU: "Haiku", _MODEL_SONNET: "Sonnet", _MODEL_OPUS: "Opus"}[model]
    print(f"[chat] model={model_label} max_tokens={max_tokens} len={len(text)}")
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
        if len(history) > MAX_HISTORY:
            histories[key] = history[-MAX_HISTORY:]
        send_message(answer, chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ Claude 응답 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_reset(chat_id: int, state: dict):
    state.get("chat_histories", {}).pop(str(chat_id), None)
    send_message("🗑 대화 기록이 초기화되었습니다.", chat_id=str(chat_id))


def handle_help(chat_id: int):
    send_message(
        "<b>📖 사용 가능한 명령어</b>\n\n"
        "/briefing — 투자 브리핑 + 취향서랍 소재 한 번에\n"
        "/report — 투자 판단 브리핑만\n"
        "/ideas — 취향서랍 블로그 소재만\n"
        "/check TICKER — 특정 종목 즉시 분석 (예: /check NVDA)\n"
        "/reset — Claude 대화 기록 초기화\n"
        "/help — 이 메시지\n\n"
        "<i>💬 자연어도 됩니다:</i>\n"
        "  '오늘 주식 브리핑 해줘' → 투자 리포트\n"
        "  '블로그 소재 줘' → 취향서랍 아이디어\n"
        "  '주식이랑 콘텐츠 브리핑 해줘' → 둘 다\n"
        "  그 외 질문 → Claude가 직접 답변\n\n"
        "<i>매일 08:00 KST 자동 브리핑 전송됩니다.</i>",
        chat_id=str(chat_id),
    )


# ── 디스패처 ─────────────────────────────────────────────────

def dispatch(message: dict, state: dict):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if TELEGRAM_CHAT_ID and str(chat_id) != TELEGRAM_CHAT_ID.strip():
        print(f"[bot] 무시: chat_id={chat_id}")
        return

    if not text.startswith("/"):
        intent = _detect_intent(text)
        if intent == "both":
            handle_full_briefing(chat_id)
        elif intent == "report":
            handle_report(chat_id)
        elif intent == "blog":
            handle_blog_ideas(chat_id)
        else:
            handle_chat(chat_id, text, state)
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    print(f"[bot] {cmd!r} {arg!r} from {chat_id}")

    if cmd == "/briefing":
        handle_full_briefing(chat_id)
    elif cmd == "/report":
        handle_report(chat_id)
    elif cmd == "/ideas":
        handle_blog_ideas(chat_id)
    elif cmd == "/check":
        handle_check(chat_id, arg)
    elif cmd == "/reset":
        handle_reset(chat_id, state)
    elif cmd in ("/help", "/start"):
        handle_help(chat_id)
    else:
        send_message(f"❓ 알 수 없는 명령어: {cmd}\n/help 로 목록 확인", chat_id=str(chat_id))


# ── 메인 ─────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN 미설정")
        return

    state = _load_state()
    offset = state.get("offset", 0)
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
                dispatch(msg, state)
            except Exception as e:
                print(f"dispatch 오류: {e}")
        offset = update["update_id"]

    if updates:
        state["offset"] = offset
        _save_state(state)


if __name__ == "__main__":
    main()
