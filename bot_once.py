#!/usr/bin/env python3
"""
GitHub Actions용 단발 봇 핸들러
미처리 텔레그램 명령을 한 번 읽고 응답한 뒤 종료합니다.
GitHub Actions에서 1분마다 실행 → 사실상 양방향 봇처럼 동작
"""

import json
import os
import requests
import claude_client
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from telegram_notifier import send_message, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, _api
from daily_report import build_report, judge_ticker, market_score
from market_indicators import collect_all
from blog_ideas import generate_blog_ideas
from config import Config
import transactions
import yfinance as yf
import ibkr_flex

_config = Config()

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

_MODEL_HAIKU  = "claude-3-haiku-20240307"
_MODEL_SONNET = "claude-3-5-sonnet-20241022"
_MODEL_OPUS   = "claude-3-5-sonnet-20241022"

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
        report = build_report()
        send_message(report, chat_id=str(chat_id))
        # Claude 구간이 빠진 경우 에러 알림
        if claude_client.last_error:
            send_message(f"⚠️ Claude API 오류 (뉴스해설/적립보고서 생략됨)\n<code>{claude_client.last_error}</code>", chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_blog_ideas(chat_id: int):
    try:
        send_message("⏳ 취향서랍 소재 생성 중...", chat_id=str(chat_id))
        result = generate_blog_ideas()
        if result:
            send_message(result, chat_id=str(chat_id))
        else:
            err = claude_client.last_error or "Claude 응답 없음"
            send_message(f"❌ 블로그 아이디어 생성 실패\n<code>{err}</code>", chat_id=str(chat_id))
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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": "당신은 주식·암호화폐 투자 전문 AI 어시스턴트입니다. 한국어로 친절하고 간결하게 답변하세요.",
                "messages": history,
            },
            timeout=90,
        )
        resp.raise_for_status()
        answer = resp.json()["content"][0]["text"]
        history.append({"role": "assistant", "content": answer})
        if len(history) > MAX_HISTORY:
            histories[key] = history[-MAX_HISTORY:]
        send_message(answer, chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ Claude 응답 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_reset(chat_id: int, state: dict):
    state.get("chat_histories", {}).pop(str(chat_id), None)
    send_message("🗑 대화 기록이 초기화되었습니다.", chat_id=str(chat_id))


def _parse_trade(arg: str):
    parts = arg.split()
    if len(parts) < 3:
        return None
    return {
        "ticker": parts[0].upper(),
        "qty": float(parts[1]),
        "price": float(parts[2]),
        "date": parts[3] if len(parts) > 3 else None,
    }


def handle_buy(chat_id: int, arg: str):
    try:
        p = _parse_trade(arg)
        if not p:
            send_message(
                "사용법: <code>/buy TICKER QTY PRICE [YYYY-MM-DD]</code>\n"
                "예: <code>/buy QQQM 1 220.50</code>",
                chat_id=str(chat_id),
            )
            return
        rec = transactions.add_buy(p["ticker"], p["qty"], p["price"], p["date"])
        send_message(
            f"✅ 매수 기록\n<b>{rec['ticker']}</b>  {rec['qty']}주 @ ${rec['price']:.2f}\n"
            f"  {rec['date']}  ·  총 ${rec['qty']*rec['price']:.2f}",
            chat_id=str(chat_id),
        )
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def _five_day_return(ticker: str) -> float | None:
    """최근 5거래일 수익률(%). 실패 시 None."""
    try:
        df = yf.Ticker(ticker).history(period="10d")
        if df is None or df.empty or len(df) < 2:
            return None
        close = df["Close"].squeeze()
        cur = float(close.iloc[-1])
        prev = float(close.iloc[max(0, len(close) - 6)])
        return (cur - prev) / prev * 100
    except Exception:
        return None


def _do_sell(chat_id: int, p: dict):
    rec = transactions.add_sell(p["ticker"], p["qty"], p["price"], p["date"])
    send_message(
        f"✅ 매도 기록\n<b>{rec['ticker']}</b>  {rec['qty']}주 @ ${rec['price']:.2f}\n"
        f"  {rec['date']}  ·  총 ${rec['qty']*rec['price']:.2f}",
        chat_id=str(chat_id),
    )


def handle_sell(chat_id: int, arg: str):
    try:
        p = _parse_trade(arg)
        if not p:
            send_message(
                "사용법: <code>/sell TICKER QTY PRICE [YYYY-MM-DD]</code>",
                chat_id=str(chat_id),
            )
            return
        pct = _five_day_return(p["ticker"])
        if pct is not None and pct <= -10:
            send_message(
                f"⚠️ <b>패닉 매도 경고</b>\n"
                f"<b>{p['ticker']}</b> 최근 5일 <b>{pct:+.1f}%</b> 하락 중\n\n"
                f"S&amp;P500 기준 -10% 이상 하락 후 1년 내 회복 확률: 역사적으로 95%+\n"
                f"지금 파는 건 바닥에서 팔고 반등 놓치는 전형적 패턴입니다.\n\n"
                f"👉 그래도 매도하려면: <code>/sell_yes {arg}</code>\n"
                f"👉 취소: 아무것도 안 하면 됩니다",
                chat_id=str(chat_id),
            )
            return
        _do_sell(chat_id, p)
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_sell_yes(chat_id: int, arg: str):
    """패닉 가드 경고 무시하고 강제 매도."""
    try:
        p = _parse_trade(arg)
        if not p:
            send_message(
                "사용법: <code>/sell_yes TICKER QTY PRICE</code>",
                chat_id=str(chat_id),
            )
            return
        _do_sell(chat_id, p)
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_portfolio(chat_id: int):
    try:
        ibkr = ibkr_flex.get_account_data()
        if ibkr["error"] is None and ibkr["positions"]:
            msg = ibkr_flex.build_account_section(ibkr["positions"], ibkr["cash_usd"])
            send_message(msg, chat_id=str(chat_id))
            return
        send_message(transactions.format_portfolio(), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_history(chat_id: int, arg: str):
    try:
        limit = int(arg) if arg.isdigit() else 10
        send_message(transactions.format_history(limit), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_undo(chat_id: int):
    try:
        last = transactions.undo_last()
        if not last:
            send_message("📭 취소할 거래 기록 없음", chat_id=str(chat_id))
            return
        send_message(
            f"↩️ 취소됨\n{last['type'].upper()} {last['ticker']} "
            f"{last['qty']}@${last['price']:.2f} ({last['date']})",
            chat_id=str(chat_id),
        )
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_rebalance(chat_id: int):
    try:
        from rebalancing import build_rebalance_section
        send_message(build_rebalance_section(), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_ibkrsync(chat_id: int):
    try:
        ibkr = ibkr_flex.get_account_data()
        if ibkr["error"] is not None:
            send_message(f"❌ IBKR 조회 실패: <code>{ibkr['error']}</code>", chat_id=str(chat_id))
            return
        trades = ibkr.get("trades", [])
        if not trades:
            send_message("📭 IBKR 체결 내역 없음 (Flex Query 기간 내)", chat_id=str(chat_id))
            return
        added = transactions.sync_from_ibkr(trades)
        send_message(
            f"✅ IBKR 동기화 완료\n"
            f"  조회된 체결: {len(trades)}건\n"
            f"  신규 추가: <b>{added}건</b>\n"
            f"  (중복 제외 후)",
            chat_id=str(chat_id),
        )
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def _parse_krw_arg(arg: str) -> float | None:
    """'200' → 200만원, '2000000' → 2,000,000원. 미입력/오류 시 None."""
    arg = arg.replace(",", "").replace("만", "").strip()
    if not arg:
        return None
    try:
        v = float(arg)
        return v * 10_000 if v < 10_000 else v
    except ValueError:
        return None


def handle_now(chat_id: int, arg: str):
    try:
        send_message("⏳ 지금 할 일 계산 중...", chat_id=str(chat_id))
        import action_plan
        holdings, idle_cash, _ = ibkr_flex.resolve_holdings_and_cash(_config)
        monthly_krw = _parse_krw_arg(arg)
        send_message(
            action_plan.build_now_message(holdings, idle_cash, monthly_krw),
            chat_id=str(chat_id),
        )
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_goal(chat_id: int, arg: str):
    try:
        import action_plan
        from rebalancing import calc_portfolio_state
        holdings, idle_cash, _ = ibkr_flex.resolve_holdings_and_cash(_config)
        total = calc_portfolio_state(holdings, idle_cash).get("total", 0)
        monthly_krw = _parse_krw_arg(arg)
        send_message(action_plan.build_goal_message(total, monthly_krw, holdings), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_tax(chat_id: int):
    try:
        send_message("⏳ 절세 플랜 계산 중...", chat_id=str(chat_id))
        import tax_korea
        send_message(tax_korea.build_tax_message(), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_idea(chat_id: int, arg: str):
    try:
        import idea_check
        send_message(idea_check.evaluate(arg), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_dip(chat_id: int):
    try:
        send_message("⏳ 저수지 수위 측정 중...", chat_id=str(chat_id))
        import reservoir
        from rebalancing import calc_portfolio_state
        holdings, idle_cash, _ = ibkr_flex.resolve_holdings_and_cash(_config)
        try:
            state = calc_portfolio_state(holdings, idle_cash)
        except Exception:
            state = None
        send_message(reservoir.build_reservoir_section(state), chat_id=str(chat_id))
    except Exception as e:
        send_message(f"❌ 오류: <code>{e}</code>", chat_id=str(chat_id))


def handle_help(chat_id: int):
    send_message(
        "<b>📖 사용 가능한 명령어</b>\n\n"
        "<b>🧭 결정 엔진</b>\n"
        "/now [만원] — 지금 할 일 한 방에 (트리거 판정 + 납입 배분)\n"
        "/goal — 마일스톤별 예상 도달 시기\n"
        "/tax — 양도세 250만원 공제 매도 플랜\n"
        "/idea TICKER — 새 종목 8문 통과제 판정\n"
        "/dip — 저수지 수위 (종목별 낙폭 → 어디에 쏠지)\n\n"
        "<b>📊 브리핑</b>\n"
        "/briefing — 투자 브리핑 + 취향서랍 소재 한 번에\n"
        "/report — 투자 판단 브리핑만\n"
        "/ideas — 취향서랍 블로그 소재만\n"
        "/check TICKER — 특정 종목 즉시 분석\n\n"
        "<b>💼 거래 기록</b>\n"
        "/buy TICKER QTY PRICE — 매수 기록 (예: /buy QQQM 1 220.50)\n"
        "/sell TICKER QTY PRICE — 매도 기록 (급락 시 경고)\n"
        "/sell_yes TICKER QTY PRICE — 경고 무시 강제 매도\n"
        "/portfolio — 보유 종목 평균단가 + 손익\n"
        "/history [N] — 최근 거래 내역 (기본 10건)\n"
        "/undo — 마지막 거래 기록 취소\n"
        "/rebalance — 카테고리별 비중 vs 목표 점검\n"
        "/ibkrsync — IBKR 체결 내역을 거래 기록에 동기화\n\n"
        "<b>⚙️ 기타</b>\n"
        "/testapi — Claude API 연결 테스트\n"
        "/reset — Claude 대화 기록 초기화\n"
        "/help — 이 메시지\n\n"
        "<i>💬 자연어도 가능 — 그 외 질문은 Claude가 답변</i>\n"
        "<i>📨 평일 미국 장 오픈 후 30분(약 22:30~23:30 KST) 자동 브리핑</i>",
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

    if cmd == "/now":
        handle_now(chat_id, arg)
    elif cmd == "/goal":
        handle_goal(chat_id, arg)
    elif cmd == "/tax":
        handle_tax(chat_id)
    elif cmd == "/idea":
        handle_idea(chat_id, arg)
    elif cmd in ("/dip", "/water"):
        handle_dip(chat_id)
    elif cmd == "/briefing":
        handle_full_briefing(chat_id)
    elif cmd == "/report":
        handle_report(chat_id)
    elif cmd == "/ideas":
        handle_blog_ideas(chat_id)
    elif cmd == "/check":
        handle_check(chat_id, arg)
    elif cmd == "/buy":
        handle_buy(chat_id, arg)
    elif cmd == "/sell":
        handle_sell(chat_id, arg)
    elif cmd == "/portfolio":
        handle_portfolio(chat_id)
    elif cmd == "/history":
        handle_history(chat_id, arg)
    elif cmd == "/sell_yes":
        handle_sell_yes(chat_id, arg)
    elif cmd == "/undo":
        handle_undo(chat_id)
    elif cmd == "/rebalance":
        handle_rebalance(chat_id)
    elif cmd == "/ibkrsync":
        handle_ibkrsync(chat_id)
    elif cmd == "/testapi":
        send_message(claude_client.test_api(), chat_id=str(chat_id))
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

    # getUpdates 실패(예: 웹훅 충돌, 네트워크 오류)가 아래 일방향 알림까지
    # 막지 않도록 별도 처리 — 명령 응답이 죽어도 장중 알림은 계속 발송.
    try:
        data = _api("getUpdates", offset=offset + 1, timeout=5, allowed_updates=["message"])
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
    except Exception as e:
        print(f"getUpdates 실패: {e}")

    # ── 장중 급락 알림 (미국장 시간 + 10분 throttle 내부에서 처리) ──
    holdings = None
    try:
        import intraday_alert
        holdings, idle_cash, _ = ibkr_flex.resolve_holdings_and_cash(_config)
        if intraday_alert.check_and_alert(state, holdings, idle_cash):
            print(f"[{datetime.now():%H:%M:%S}] 장중 급락 알림 발송")
    except Exception as e:
        print(f"[intraday] 오류: {e}")

    # ── 저수지 구간 진입 알림 (종목별 52주 고점 낙폭, 15분 throttle) ──
    try:
        import reservoir
        if reservoir.check_zone_alerts(state, holdings):
            print(f"[{datetime.now():%H:%M:%S}] 저수지 진입 알림 발송")
    except Exception as e:
        print(f"[reservoir] 오류: {e}")

    _save_state(state)


if __name__ == "__main__":
    main()
