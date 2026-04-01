"""
블로그 '호두의 취향서랍' 콘텐츠 아이디어 생성기

- designboom.com RSS에서 실제 신제품만 수집
- 이미 추천한 제품은 blog_history.json으로 관리해 반복 방지
- 각 아이디어에 아티클 흐름 개요 포함
- 존재하지 않는 제품 환각 금지 (RSS 기반 + 확실한 제품만)
"""

import json
import os
import requests
import xml.etree.ElementTree as ET
import anthropic
from datetime import datetime
from config import Config

_config = Config()
_claude = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY)

DESIGNBOOM_RSS = "https://www.designboom.com/feed/"
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blog_history.json")
MAX_HISTORY = 300  # 최대 보관 항목 수

_TASTE_PROFILE = """
블로그 '호두의 취향서랍' 취향 (조회수 상위 글 기반):
  - 컴팩트 카메라: Fujifilm X100·GFX 시리즈, Leica Q·M·D-Lux, Ricoh GR 시리즈
  - 프리미엄 오디오: Sony LinkBuds, Bang & Olufsen Beosound 시리즈
  - 커피 도구: Fellow 그라인더·케틀, Hario V60, 에어로프레스
  - 생활 소품·알람: 발뮤다, 퀴시 청소포
  - 스마트폰: Nothing Phone (디자인 중시)
  - 미니멀 가전·PC: Mac mini 액세서리
  - 액션캠·짐벌: DJI 오즈모 시리즈
  - 가구·인테리어: 일본·북유럽 감성 소품
  - Nintendo 주변기기 (디자인 관점)

에세이 형식 — 직접 사용기 X, 제조사 정보 + 사용자 후기 조합 + 운영자 관점
"""


# ── 이력 관리 ──────────────────────────────────────────────────

def _load_history() -> list[str]:
    """이미 추천한 제품명 목록 로드"""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("suggested", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_history(history: list[str], new_items: list[str]):
    """이력 저장 (최신순 MAX_HISTORY개 유지)"""
    updated = new_items + history
    updated = list(dict.fromkeys(updated))[:MAX_HISTORY]  # 중복 제거 + 최대치
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"suggested": updated, "last_updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)


# ── designboom RSS 수집 ────────────────────────────────────────

def _fetch_designboom_recent(n: int = 20) -> list[dict]:
    """designboom RSS 최근 게시물 수집"""
    try:
        resp = requests.get(DESIGNBOOM_RSS, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        items = []
        for item in (channel.findall("item") if channel is not None else [])[:n]:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()[:200]
            if title:
                items.append({"title": title, "link": link, "summary": desc})
        return items
    except Exception as e:
        print(f"[blog_ideas] designboom RSS 오류: {e}")
        return []


# ── 아이디어 생성 ──────────────────────────────────────────────

def generate_blog_ideas() -> str:
    """신제품 중심 블로그 아이디어 생성 (이력 반복 방지, 아티클 개요 포함)"""
    today = datetime.now().strftime("%Y년 %m월 %d일")
    history = _load_history()

    # designboom RSS
    db_items = _fetch_designboom_recent(20)
    if db_items:
        db_lines = "\n".join(f"  • [{i+1}] {it['title']} — {it['link']}" for i, it in enumerate(db_items))
        db_section = f"\n[오늘 designboom 최신 게시물 — 실제 존재하는 제품만 포함]\n{db_lines}"
    else:
        db_section = "\n[designboom RSS 수집 실패 — 지식 기반 제품만 사용]"

    history_section = ""
    if history:
        history_section = f"\n[이미 추천한 제품 — 절대 반복 금지]\n" + "\n".join(f"  - {h}" for h in history[:60])

    prompt = f"""오늘 날짜: {today}

{_TASTE_PROFILE}
{db_section}
{history_section}

위 취향 프로필에 맞는 '호두의 취향서랍' 블로그 아이디어를 신제품 중심으로 최대한 많이 제안해주세요 (목표 7~10개).

⚠️ 엄격한 규칙:
1. 반드시 실제로 존재하는 제품만 포함 — 확실하지 않으면 제외
2. 이미 추천한 제품 목록에 있는 것은 절대 포함 금지
3. designboom 목록에 있는 제품은 링크를 반드시 포함
4. 취향에 맞지 않는 제품은 포함하지 말 것

각 아이디어 형식 (이 형식을 정확히 따를 것):

━━━
🆕 [제품명]
취향 포인트: (이 독자에게 왜 어울리는지 한 줄)
아티클 개요:
  1. 도입 — (어떤 훅으로 시작할지)
  2. 제품 소개 — (핵심 특징·스펙 포인트)
  3. 사용자 반응 분석 — (호불호 포인트)
  4. 마무리 — (어떤 메시지로 끝낼지)
━━━

한국어로 작성."""

    try:
        resp = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        ideas_text = resp.content[0].text

        # 이력 추출: 🆕 뒤에 나오는 제품명 파싱
        import re
        new_products = re.findall(r"🆕\s+\[?(.+?)\]?(?:\n|$)", ideas_text)
        if new_products:
            _save_history(history, [p.strip() for p in new_products])

    except Exception as e:
        print(f"[blog_ideas] Claude 오류: {e}")
        ideas_text = "아이디어 생성 중 오류가 발생했습니다."

    return (
        f"✏️ <b>오늘의 취향서랍 소재</b>  {today}\n"
        + "━" * 20 + "\n"
        + ideas_text
    )
