#!/usr/bin/env python3
"""
보유 종목 뉴스 헤드라인 — yfinance.Ticker(t).news 무료 API 활용.

리포트에 1~3줄 압축해서 노출.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import yfinance as yf


def _parse_news_item(item: dict) -> dict | None:
    """yfinance news 아이템(구/신 포맷 모두) → {title, url, ts, age_h} 정규화."""
    # 신 포맷: {'content': {'title': ..., 'pubDate': '2024-01-01T...Z', 'canonicalUrl': {'url': ...}, ...}}
    content = item.get("content") if isinstance(item, dict) else None
    if isinstance(content, dict):
        title = content.get("title") or ""
        pub = content.get("pubDate") or content.get("displayTime")
        ts = None
        if pub:
            try:
                ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:
                ts = None
        if not ts:
            return None
        url = (
            (content.get("canonicalUrl") or {}).get("url")
            or (content.get("clickThroughUrl") or {}).get("url")
            or ""
        )
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return {"title": title.strip(), "url": url, "ts": ts, "age_h": age_h}

    # 구 포맷: {'title': ..., 'link': 'https://...', 'providerPublishTime': 1234567890, ...}
    title = (item.get("title") or "").strip()
    pt = item.get("providerPublishTime")
    if not title or not pt:
        return None
    try:
        ts = datetime.fromtimestamp(int(pt), tz=timezone.utc)
    except Exception:
        return None
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return {"title": title, "url": item.get("link") or "", "ts": ts, "age_h": age_h}


def fetch_recent_news(ticker: str, max_age_hours: int = 48) -> dict | None:
    """티커의 최신 뉴스 1건 (max_age_hours 이내)."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return None
    parsed = []
    for it in items:
        p = _parse_news_item(it)
        if p and p["age_h"] <= max_age_hours:
            parsed.append(p)
    if not parsed:
        return None
    return sorted(parsed, key=lambda x: x["age_h"])[0]


def _format_age(age_h: float) -> str:
    if age_h < 1:
        return f"{int(age_h * 60)}분 전"
    if age_h < 24:
        return f"{int(age_h)}시간 전"
    return f"{int(age_h / 24)}일 전"


def _truncate(text: str, max_len: int = 60) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_news_section(holdings: dict, top_n: int = 3, max_age_hours: int = 48) -> str:
    """보유 비중 상위 N개 종목의 최신 뉴스 1건씩 (클릭 가능한 링크 포함)."""
    if not holdings:
        return ""
    # 포지션 큰 순서 (qty 기준 — 가격 곱하기까진 과도)
    top = sorted(
        ((t, q) for t, q in holdings.items() if q and q > 0),
        key=lambda x: -x[1],
    )[:top_n]
    if not top:
        return ""

    lines = ["<b>📰 보유 종목 뉴스</b>"]
    found = 0
    for ticker, _ in top:
        news = fetch_recent_news(ticker, max_age_hours=max_age_hours)
        if not news:
            continue
        age = _format_age(news["age_h"])
        title = _escape_html(_truncate(news["title"], 55))
        url = news.get("url")
        title_html = f'<a href="{_escape_html(url)}">{title}</a>' if url else title
        lines.append(f"  <b>{ticker}</b>  {title_html}  <i>({age})</i>")
        found += 1

    if found == 0:
        return ""
    return "\n".join(lines)


if __name__ == "__main__":
    test = {"NVDA": 100, "AAPL": 50, "TSLA": 20}
    print(build_news_section(test))
