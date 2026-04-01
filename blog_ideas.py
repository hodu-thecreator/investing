"""
블로그 '호두의 취향서랍' 콘텐츠 아이디어 생성기
매일 5개의 새로운 소재를 제안합니다.

취향 프로필 (조회수 상위 글 기반):
  컴팩트/필름 카메라 (Fujifilm X100VI, Leica, Ricoh GR)
  프리미엄 오디오 (Sony LinkBuds, Bang & Olufsen)
  커피 도구 (Fellow Eiden, Hario V60)
  생활 소품/알람 (발뮤다, 퀴시)
  스마트폰 (Nothing Phone)
  게임기 주변기기 (Nintendo)
  액션캠/짐벌 (DJI)
  가구·인테리어 소품
"""

import requests
import xml.etree.ElementTree as ET
import anthropic
from datetime import datetime
from config import Config

_config = Config()
_claude = anthropic.Anthropic(api_key=_config.ANTHROPIC_API_KEY)

DESIGNBOOM_RSS = "https://www.designboom.com/feed/"

_TASTE_PROFILE = """
블로그 '호두의 취향서랍' 운영자 취향 (에세이 형식, 사용기 X):
- 컴팩트 카메라: Fujifilm X100 시리즈, Leica D-Lux/Q 시리즈, Ricoh GR 시리즈
- 프리미엄 오디오: Sony LinkBuds 클립형/인이어, Bang & Olufsen Beosound
- 커피 도구: Fellow 그라인더·케틀, Hario V60, 에어로프레스
- 생활 소품: 발뮤다 알람시계, 청소 도구, 조명
- 스마트폰: Nothing Phone 시리즈 (디자인 중시)
- 미니멀 가전: Mac mini 액세서리, 소형 PC
- 액션캠: DJI 오즈모 시리즈
- 가구/인테리어 소품 (일본·북유럽 감성)
- Nintendo 주변기기 (디자인 관점 접근)
"""

_CATEGORIES = [
    "신제품",
    "비교 콘텐츠 (A vs B)",
    "designboom 픽",
    "전시회/행사 출품작",
    "계절/트렌드 소재",
]


def _fetch_designboom_recent(n: int = 12) -> list[dict]:
    """designboom RSS에서 최근 항목 수집"""
    try:
        resp = requests.get(DESIGNBOOM_RSS, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        channel = root.find("channel")
        items = []
        for item in (channel.findall("item") if channel is not None else [])[:n]:
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()[:150]
            if title:
                items.append({"title": title, "link": link, "summary": desc})
        return items
    except Exception as e:
        print(f"[blog_ideas] designboom RSS 오류: {e}")
        return []


def generate_blog_ideas() -> str:
    """오늘의 취향서랍 콘텐츠 아이디어 5개 생성"""
    today = datetime.now().strftime("%Y년 %m월 %d일")

    designboom_items = _fetch_designboom_recent()
    if designboom_items:
        db_lines = "\n".join(
            f"  • {it['title']} — {it['link']}" for it in designboom_items
        )
        designboom_section = f"\n[오늘 designboom 최신 게시물]\n{db_lines}"
    else:
        designboom_section = ""

    prompt = f"""오늘 날짜: {today}

{_TASTE_PROFILE}
{designboom_section}

위 취향 프로필을 가진 블로그 운영자를 위한 '호두의 취향서랍' 콘텐츠 아이디어 5개를 제안해주세요.

아래 5가지 카테고리에서 각 1개씩:
1. 신제품 소개 — 최근 출시·발표된 제품 (실제 존재하는 제품)
2. 비교 콘텐츠 — 취향 내 두 제품 A vs B 구도
3. designboom 픽 — 위 목록에서 취향에 부합하는 제품 (링크 포함)
4. 전시회/행사 출품작 — CES·IFA·Computex·Salone del Mobile 등 최근 행사 출품 제품
5. 계절·트렌드 소재 — 요즘 분위기에 맞는 라이프스타일 소재

각 아이디어 형식:
[카테고리] 제목 아이디어
→ 각도/방향 한 줄

한국어로 작성. 실제 존재하는 제품만 언급. 모르면 생략하고 다른 제품으로 대체."""

    try:
        resp = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        ideas_text = resp.content[0].text
    except Exception as e:
        print(f"[blog_ideas] Claude 오류: {e}")
        ideas_text = "아이디어 생성 중 오류가 발생했습니다."

    return (
        f"✏️ <b>오늘의 취향서랍 소재</b>  {today}\n"
        "━" * 20 + "\n"
        f"{ideas_text}"
    )
