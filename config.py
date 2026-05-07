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

    # ── 실제 보유 주수 (포트폴리오 변경 시 업데이트) ──────────────
    # 2026-05-07 기준
    HOLDINGS: dict[str, float] = {
        "QQQI":  600,
        "SPYI":  600,
        "SGOV":  110.24,
        "ETN":   0.1,
        "MU":    0.038,
        "VRT":   0.061,
        "AEHR":  0.22,
        "GEV":   0.0186,
        "SOXL":  0.1001,
        "UPRO":  0.1001,
        "QLD":   0.1001,
        "TQQQ":  0.1001,
        "SSO":   0.1001,
        "QQQM":  0.02,
        "SOXQ":  0.01,
        "SPYM":  0.01,
        "SCHD":  0.02,
    }

    # ── 현금 관리 ─────────────────────────────────────────────────
    TARGET_CASH_RATIO = float(os.getenv("TARGET_CASH_RATIO", "0.20"))  # 20%
    CASH_TICKERS = ["SGOV", "BIL", "SHV", "SHY"]  # 현금성 자산
    IDLE_CASH_USD = float(os.getenv("IDLE_CASH_USD", "612.19"))  # 미사용 USD 잔고

    # ── 정기 적립 스케줄 ─────────────────────────────────────────
    # interval: "biweekly" = 2주마다, "monthly" = 월 1회
    DCA_SCHEDULE: dict[str, dict] = {
        "SPYM": {"amount": 50,  "interval": "biweekly"},
        "QQQM": {"amount": 40,  "interval": "biweekly"},
        "ETN":  {"amount": 40,  "interval": "biweekly"},
        "SCHD": {"amount": 40,  "interval": "biweekly"},
        "GEV":  {"amount": 20,  "interval": "biweekly"},
        "VRT":  {"amount": 20,  "interval": "monthly"},
        "MU":   {"amount": 20,  "interval": "monthly"},
        "SOXQ": {"amount": 20,  "interval": "monthly"},
        "SGOV": {"amount": 20,  "interval": "monthly"},
    }
