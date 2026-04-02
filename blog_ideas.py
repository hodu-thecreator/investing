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
_claude = anthropic.Anthropic()

DESIGNBOOM_RSS = "https://www.designboom.com/feed/"
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blog_history.json")
MAX_HISTORY = 300  # 최대 보관 항목 수

_TASTE_PROFILE = """
'호두의 취향서랍' 취향 — 핵심 기준: 일단 예뻐야 한다.

✅ 들어올 수 있는 제품 (실제 작성된 글 기반):
  카메라: Fujifilm X100·X-E·GFX 시리즈, Leica Q·M·D-Lux·라이츠폰, Ricoh GR, Canon PowerShot V1, Sigma BF, Instax Mini Evo처럼 디자인 정체성이 강한 카메라
  오디오: Sony LinkBuds 클립형·인이어, Bang & Olufsen, Teenage Engineering EP-133/EP-1320, LP 레코드플레이어, ENSA P1처럼 물리 미디어 감성 오디오
  조명: Artemide Tolomeo·Tizio 같은 클래식 디자인 조명, 베르판 VP Globe Pendant, 무인양품 LED 소품
  커피: Fellow Eiden, Hario V60 메탈, 미니멀하고 예쁜 커피 도구
  스마트폰: Nothing Phone, Punkt MC03, 라이카 라이츠폰처럼 디자인·철학이 있는 폰
  인테리어·소품: 하마다 유이치 미니어처 가구, 티볼리 송버드, 발뮤다 더 클락, NOS 캘린더
  향·인센스: 이숨, 헤트라스, 아르테미데 계열
  레고 아트·콜렉터 에디션
  기타 디자인 오브제: Tembo(자석 드럼머신), DECOKEE Quake(스마트보드), Stream Deck XL처럼 기능보다 디자인 포인트가 있는 기술 제품, 몰스킨 스마트 라이팅

❌ 들어오기 어려운 제품:
  디자인이 평범한 가성비 제품
  대형 가전 (세탁기·냉장고 등)
  기능에만 집중된 무개성 제품

에세이 형식 — 직접 사용기 X, 제조사 정보 + 사용자 후기 조합 + 운영자 관점
"""

# 이미 작성된 글 목록 — 최초 1회 이력 파일 초기화에 사용
_WRITTEN_ARTICLES = [
    "TiLink", "마이케 안첸 폴딩 큐브", "미니폰 울트라", "페라리 루체",
    "NOS 캘린더", "DECOKEE Quake", "ENSA P1", "ARIA 조립식 전기차",
    "토폴리노 XS", "EntoPedia", "레고 스누피 도그하우스",
    "Nothing Phone 4a Pro", "낫씽폰 4a 프로",
    "Tembo 나무 드럼머신", "소니 PS-LX5BT",
    "하마다 유이치 미니어처 가구", "Stream Deck XL",
    "발뮤다 더 클락", "캐논 파워샷 G7Xm3 30주년 한정판",
    "Punkt MC03", "소니 링크버즈 클립", "Sony LinkBuds Clip",
    "인스탁스 미니 에보 시네마", "Instax Mini Evo Cinema",
    "라이카 라이츠폰", "Leica Laiphone",
    "리코 GR 4 모노크롬", "Ricoh GR IV Monochrome",
    "맥북 네오", "MacBook Neo",
    "이숨 카케로우 아로마틱 인센스", "콜린스 인센스",
    "베르판 VP 글로브 펜던트", "Verpan VP Globe Pendant",
    "아르떼미데 톨로메오", "Artemide Tolomeo",
    "아르떼미데 티지오", "Artemide Tizio",
    "헤트라스 프리미엄 디퓨저 데이지향",
    "리코 GR 3x", "Ricoh GR IIIx",
    "맥북 에어 M4", "MacBook Air M4",
    "캐논 파워샷 V1", "Canon PowerShot V1",
    "몰스킨 스마트 라이팅 세트", "Moleskine Smart Writing Set",
    "후지필름 X-E5", "Fujifilm X-E5",
    "틴에이지 엔지니어링 EP-1320 미디블", "Teenage Engineering EP-1320",
    "티볼리 송버드 맥스", "Tivoli Songbird Max",
    "펠로우 에이든 프리시전 커피 메이커", "Fellow Eiden Precision Coffee Maker",
    "닌텐도 사운드 클락 알라모", "Nintendo Sound Clock Alarmo",
    "라이카 D-Lux 8", "Leica D-Lux 8",
    "라이카 M11-D", "Leica M11-D",
    "라이카 Q3 43", "Leica Q3 43",
    "시그마 BF", "Sigma BF",
    "후지필름 GFX100RF", "Fujifilm GFX100RF",
    "하리오 V60 메탈 매트블랙 드리퍼", "Hario V60 Metal Matte Black",
    "헤이 소은 주전자", "에어팟 4 ANC", "AirPods 4 ANC",
    "무인양품 LED 랜턴 화이트",
    "틴에이지 엔지니어링 EP-133 K.O. II", "Teenage Engineering EP-133 K.O. II",
    "낫씽폰 4a", "Nothing Phone 4a",
    "뱅앤올룹슨 베오사운드 A5", "Bang & Olufsen Beosound A5",
    "라이카 M11 모노크롬", "Leica M11 Monochrome",
    "후지필름 X100VI", "Fujifilm X100VI",
    "맥 미니", "Mac mini",
    "DJI 오즈모 포켓 3",
    "소니 링크버즈 클립", "쿼시 스티키 점착식 청소포",
    "캐논 파워샷 V1",
]


# ── 이력 관리 ──────────────────────────────────────────────────

def _load_history() -> list[str]:
    """이미 추천/작성한 제품명 목록 로드. 없으면 기작성 글로 초기화."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("suggested", [])
    except (FileNotFoundError, json.JSONDecodeError):
        # 최초 실행: 기작성 글 목록으로 이력 초기화
        _save_history([], _WRITTEN_ARTICLES)
        return list(_WRITTEN_ARTICLES)


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
            model="claude-3-5-sonnet-20241022",
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
