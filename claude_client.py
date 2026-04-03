"""
Anthropic API 직접 호출 유틸리티
SDK 인증 문제를 우회하여 requests로 직접 호출
"""
import os
import requests

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

# 마지막 에러 메시지 (외부에서 참조 가능)
last_error: str = ""


def call(prompt: str, model: str = _DEFAULT_MODEL, max_tokens: int = 512) -> str:
    """Claude API 직접 호출. 실패 시 빈 문자열 반환."""
    global last_error
    last_error = ""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        last_error = "ANTHROPIC_API_KEY 환경변수 없음"
        print(f"[claude_client] {last_error}")
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
        if not resp.ok:
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            print(f"[claude_client] 오류: {last_error}")
            return ""
        return resp.json()["content"][0]["text"]
    except Exception as e:
        last_error = str(e)
        print(f"[claude_client] 오류: {last_error}")
        return ""


def test_api() -> str:
    """API 연결 테스트. 성공 시 'OK', 실패 시 에러 메시지 반환."""
    result = call("'API 정상' 이라고만 답해.", max_tokens=20)
    if result:
        return f"✅ API 정상: {result.strip()}"
    return f"❌ API 실패: {last_error or '알 수 없는 오류'}"
