import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    WATCH_STOCKS = os.getenv("WATCH_STOCKS", "QQQM,SPYM,GLDM,IBIT,SGOV,QLD,TQQQ,SSO,UPRO").split(",")
    WATCH_CRYPTO = os.getenv("WATCH_CRYPTO", "bitcoin,ethereum,solana").split(",")
    # 적립·모니터링 대상 포트폴리오
    ACCUMULATION_PORTFOLIO = os.getenv(
        "ACCUMULATION_PORTFOLIO",
        "QQQM,SPYM,GLDM,IBIT,SGOV,"       # 코어 5종목 (GLDM·IBIT 미보유 → 목표)
        "QLD,TQQQ,SSO,UPRO,"              # 레버리지 (조정 시 전술)
        "QQQI,SPYI",                       # 레거시 (정리 중)
    ).replace(" ", "").split(",")

    # ── 실제 보유 주수 (IBKR 연결 실패 시 폴백) ──────────────────
    # IBKR Flex Query로 자동 업데이트됨. 이 값은 비상 폴백용.
    HOLDINGS: dict[str, float] = {
        "QQQI":  600,
        "SPYI":  600,
        "SGOV":  110.24,
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
    # 헌법 5조: SGOV 목표 29%
    TARGET_CASH_RATIO = float(os.getenv("TARGET_CASH_RATIO", "0.29"))
    CASH_TICKERS = ["SGOV", "BIL", "SHV", "SHY"]  # 현금성 자산
    IDLE_CASH_USD = float(os.getenv("IDLE_CASH_USD", "612.19"))  # 미사용 USD 잔고

    # ── 정기 적립 스케줄 (자동 매수 비활성화) ───────────────────
    DCA_SCHEDULE: dict[str, dict] = {}

    # ════════════════════════════════════════════════════════════
    # 호두 투자 헌법 (Constitution) — CONSTITUTION.md 참조
    # 코드는 이 상수를 단일 진실 소스로 사용한다.
    # ════════════════════════════════════════════════════════════

    # 헌법 5조 — 코어 5종목 목표 배분
    CORE_ALLOCATION: dict[str, float] = {
        "QQQM": 0.30,   # Nasdaq 100 성장
        "SPYM": 0.30,   # S&P 500 닻
        "GLDM": 0.07,   # 금 (인플레 헤지)
        "IBIT": 0.03,   # 비트코인 (통화 절하 헤지)
        "SGOV": 0.29,   # 현금 + 매수 탄약
    }

    # 레버리지 → 코어 버킷 매핑 (조정 시 임시 포지션, 노출 합산용)
    LEVERAGE_BUCKET: dict[str, str] = {
        "QLD": "QQQM", "TQQQ": "QQQM",     # Nasdaq 노출
        "SSO": "SPYM", "UPRO": "SPYM",     # S&P 노출
    }

    # 청산 예정 레거시 종목 (헌법 5종목 외 — 신규 매수 금지, 세금 룰 따라 정리)
    LEGACY_TICKERS: list[str] = ["QQQI", "SPYI", "SOXQ", "SOXL", "SOXX",
                                 "SCHD", "DIVO", "DGRW", "QDVO", "ETN",
                                 "MU", "VRT", "AEHR", "GEV", "NVDA", "AVGO",
                                 "CCJ", "CEG", "XOM", "COPX", "BITX", "ETHU",
                                 "SLV", "ARKK", "CRCL"]

    # 헌법 6조 — S&P500 ATH 대비 조정 트리거
    # drop: ATH 대비 낙폭(%), fire: SGOV 탄약 발사 비율,
    # action: core(코어만) / core+lev(코어+레버) / all-in(비상금 외 전액)
    # lev: 매수할 레버리지 ETF, cap: 총자산 대비 레버 포지션 상한
    CORRECTION_TRIGGERS: list[dict] = [
        {"drop": -5,  "fire": 0.25, "action": "core",     "lev": [],              "cap": 0.00},
        {"drop": -10, "fire": 0.50, "action": "core+lev", "lev": ["SSO"],         "cap": 0.02},
        {"drop": -20, "fire": 1.00, "action": "core+lev", "lev": ["UPRO", "TQQQ"], "cap": 0.05},
        {"drop": -30, "fire": 1.00, "action": "all-in",   "lev": ["UPRO", "TQQQ"], "cap": 0.05},
    ]

    # QQQ·QLD·TQQQ 평균 MDD 기준 (1999-2026 실측)
    # 출처: 겨울잠(@gyeoul_jam) — 평균 MDD 구간 진입 시 기대수익률
    MDD_REFERENCE: dict[str, dict] = {
        "QQQ":  {"avg_mdd": -20.24, "entry_return": 43.3,  "label": "나스닥100 1배"},
        "QLD":  {"avg_mdd": -30.28, "entry_return": 95.0,  "label": "나스닥100 2배"},
        "TQQQ": {"avg_mdd": -39.79, "entry_return": 198.0, "label": "나스닥100 3배"},
    }
    # 레버리지 분할 익절 타겟 (gain_pct, 설명)
    LEV_HARVEST_TARGETS: list[tuple] = [
        (30,  "1/3 익절  수익 부분 확보"),
        (50,  "1/3 추가  원금 초과 수익 확보"),
        (100, "잔여 전량  원금 2배 달성 시 청산"),
    ]

    # 헌법 3조 — 자산 마일스톤 (USD, 설명)
    MILESTONES: list[tuple] = [
        (200_000,   "1~2개월 휴직 가능"),
        (500_000,   "1년 사바티칼 ★자유 시작점★"),
        (750_000,   "자산이 본업 연봉만큼 일함"),
        (1_500_000, "호주 Full FIRE"),
        (2_000_000, "1억 연봉 완전 대체"),
    ]

    # 헌법 9조 — 한국 phase 양도세 (2026.5~2027.11)
    KR_CGT_DEDUCTION_KRW = 2_500_000      # 연 250만원 공제
    KR_PHASE_END = "2027-11"

    # 비상금 (-30% 총동원에서도 제외)
    EMERGENCY_FUND_USD = float(os.getenv("EMERGENCY_FUND_USD", "10000"))

    # ── 결정 엔진 (/now, /goal, /tax) ────────────────────────────
    # 헌법 1조: 월 납입 가능 ₩150~200만 → 기본값 중간치
    MONTHLY_DEPOSIT_KRW = float(os.getenv("MONTHLY_DEPOSIT_KRW", "1750000"))
    # 마일스톤 ETA 계산용 장기 기대수익률 (보수적 가정)
    EXPECTED_ANNUAL_RETURN = float(os.getenv("EXPECTED_ANNUAL_RETURN", "0.07"))
    # USD/KRW 환율 조회 실패 시 폴백
    FX_USDKRW_FALLBACK = float(os.getenv("FX_USDKRW_FALLBACK", "1400"))

