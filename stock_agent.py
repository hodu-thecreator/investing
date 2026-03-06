"""
주식/암호화폐 브리핑 에이전트
- Yahoo Finance: 나스닥, S&P500, 관심 종목
- CoinGecko: 암호화폐 시세
- Claude API: 데이터 분석 및 요약
"""

import aiohttp
import anthropic
import pandas as pd
from datetime import datetime
import pytz
import yfinance as yf
import warnings
from config import Config

warnings.filterwarnings("ignore")

config = Config()
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

MODEL = "claude-haiku-4-5-20251001"

# daily_report.py 호환 상수
PORTFOLIO = config.WATCH_STOCKS
MA_PERIODS = [5, 20, 50, 100, 200]


# ── daily_report.py 호환 함수 ────────────────────────────────────

def fetch_stock_data(ticker: str, period: str = "1y") -> pd.DataFrame:
    """yfinance로 주가 데이터 가져오기"""
    try:
        df = yf.Ticker(ticker).history(period=period)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def calc_moving_averages(df: pd.DataFrame) -> dict:
    """이평선 현재값 계산"""
    if df.empty:
        return {}
    result = {}
    close = df["Close"]
    for period in MA_PERIODS:
        if len(close) >= period:
            result[period] = close.rolling(period).mean().iloc[-1]
    return result


def calc_drawdown_from_high(df: pd.DataFrame, lookback_days: int = 252) -> dict:
    """52주 고점 대비 현재 하락률 계산"""
    if df.empty:
        return {"current": 0, "high": 0, "drawdown_pct": 0}
    recent = df["Close"].tail(lookback_days)
    high = recent.max()
    current = recent.iloc[-1]
    drawdown_pct = (current - high) / high * 100 if high else 0
    return {"current": current, "high": high, "drawdown_pct": drawdown_pct}


# ── StockAgent 클래스 ────────────────────────────────────────────

class StockAgent:

    async def get_market_indices(self) -> dict:
        """나스닥, S&P500 데이터 수집"""
        indices = {
            "^IXIC": "나스닥",
            "^GSPC": "S&P500",
            "^DJI": "다우존스",
        }
        result = {}
        for ticker, name in indices.items():
            try:
                data = yf.Ticker(ticker)
                info = data.fast_info
                current = info.last_price
                prev_close = info.previous_close
                change = current - prev_close
                change_pct = (change / prev_close) * 100
                result[name] = {
                    "price": f"{current:,.2f}",
                    "change": f"{change:+,.2f}",
                    "change_pct": f"{change_pct:+.2f}%",
                    "emoji": "🟢" if change >= 0 else "🔴"
                }
            except Exception as e:
                result[name] = {"error": str(e)}
        return result

    async def get_watch_stocks(self) -> dict:
        """관심 종목 데이터"""
        result = {}
        for ticker in config.WATCH_STOCKS:
            try:
                data = yf.Ticker(ticker.strip())
                info = data.fast_info
                current = info.last_price
                prev_close = info.previous_close
                change_pct = ((current - prev_close) / prev_close) * 100
                result[ticker.strip()] = {
                    "price": f"${current:,.2f}",
                    "change_pct": f"{change_pct:+.2f}%",
                    "emoji": "🟢" if change_pct >= 0 else "🔴"
                }
            except Exception as e:
                result[ticker.strip()] = {"error": str(e)}
        return result

    async def get_crypto(self) -> dict:
        """암호화폐 시세 (CoinGecko 무료 API)"""
        ids = ",".join(config.WATCH_CRYPTO)
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
            result = {}
            name_map = {
                "bitcoin": "비트코인(BTC)",
                "ethereum": "이더리움(ETH)",
                "solana": "솔라나(SOL)",
                "ripple": "리플(XRP)",
            }
            for coin_id, values in data.items():
                name = name_map.get(coin_id, coin_id.upper())
                price = values.get("usd", 0)
                change = values.get("usd_24h_change", 0)
                result[name] = {
                    "price": f"${price:,.2f}",
                    "change_pct": f"{change:+.2f}%",
                    "emoji": "🟢" if change >= 0 else "🔴"
                }
            return result
        except Exception as e:
            return {"error": str(e)}

    async def generate_report(self) -> str:
        """전체 브리핑 리포트 생성"""
        kst = pytz.timezone('Asia/Seoul')
        now = datetime.now(kst)
        date_str = now.strftime("%Y년 %m월 %d일 (%a)")

        indices = await self.get_market_indices()
        stocks = await self.get_watch_stocks()
        crypto = await self.get_crypto()

        raw_data = f"""
날짜: {date_str}

[주요 지수]
{self._format_indices(indices)}

[관심 종목]
{self._format_stocks(stocks)}

[암호화폐]
{self._format_crypto(crypto)}
        """

        message = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": f"""아래 주식/암호화폐 데이터를 바탕으로 간결하고 통찰력 있는 아침 브리핑을 작성해줘.
텔레그램 메시지용으로 이모지를 적절히 사용하고 Markdown 형식으로 작성해줘.

형식:
1. 한 줄 시장 요약
2. 주요 지수 현황
3. 관심 종목 하이라이트 (눈에 띄는 종목 위주)
4. 암호화폐 현황
5. 오늘 주목할 포인트 (간단히 1-2줄)

{raw_data}
"""
            }]
        )

        header = f"📊 *{date_str} 아침 브리핑*\n\n"
        return header + message.content[0].text

    def _format_indices(self, data: dict) -> str:
        lines = []
        for name, values in data.items():
            if "error" in values:
                lines.append(f"  {name}: 데이터 오류")
            else:
                lines.append(f"  {values['emoji']} {name}: {values['price']} ({values['change_pct']})")
        return "\n".join(lines)

    def _format_stocks(self, data: dict) -> str:
        lines = []
        for ticker, values in data.items():
            if "error" in values:
                lines.append(f"  {ticker}: 데이터 오류")
            else:
                lines.append(f"  {values['emoji']} {ticker}: {values['price']} ({values['change_pct']})")
        return "\n".join(lines)

    def _format_crypto(self, data: dict) -> str:
        if "error" in data:
            return f"  데이터 오류: {data['error']}"
        lines = []
        for name, values in data.items():
            lines.append(f"  {values['emoji']} {name}: {values['price']} ({values['change_pct']})")
        return "\n".join(lines)
