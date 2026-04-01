import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    WATCH_STOCKS = os.getenv("WATCH_STOCKS", "AAPL,NVDA,TSLA,MSFT,GOOGL").split(",")
    WATCH_CRYPTO = os.getenv("WATCH_CRYPTO", "bitcoin,ethereum,solana").split(",")
    # 소액 적립(DCA) 포트폴리오 — 매일 모으기 중인 전체 목록
    ACCUMULATION_PORTFOLIO = os.getenv(
        "ACCUMULATION_PORTFOLIO",
        "QQQI,SPYI,SPYM,QQQM,SCHD,DIVO,DGRW,QDVO,"
        "BITX,ETHU,ETN,NVDA,VRT,CCJ,CEG,AVGO,XOM,"
        "COPX,SOXQ,SOXX,SOXL,QLD,SSO,TQQQ,UPRO,"
        "SLV,GLDM,ARKK,SGOV,CRCL",
    ).replace(" ", "").split(",")
