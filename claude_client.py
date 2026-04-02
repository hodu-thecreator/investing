"""
Anthropic API 직접 호출 유틸리티
SDK 인증 문제를 우회하여 requests로 직접 호출
"""
import os
import requests

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


def call(prompt: str, model: str = _DEFAULT_MODEL, max_tokens: int = 512) -> str:
    """Claude API 직접 호출. 실패 시 빈 문자열 반환."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[claude_client] ANTHROPIC_API_KEY 환경변수 없음")
        return ""
    try:
        resp = requests.post(
            _API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    except Exception as e:
        print(f"[claude_client] 오류: {e}")
        return ""
