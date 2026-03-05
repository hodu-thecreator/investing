import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    WATCH_STOCKS = os.getenv("WATCH_STOCKS", "AAPL,NVDA,TSLA,MSFT,GOOGL").split(",")
    WATCH_CRYPTO = os.getenv("WATCH_CRYPTO", "bitcoin,ethereum,solana").split(",")
