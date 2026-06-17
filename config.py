import os
from dotenv import load_dotenv
load_dotenv()

class Config:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    WATCH_STOCKS = os.getenv("WATCH_STOCKS", "QQQM,SPYM,GLDM,IBIT,SGOV,SPMO,SOXQ,QLD,TQQQ,SSO,UPRO").split(",")
    WATCH_CRYPTO = os.getenv("WATCH_CRYPTO", "bitcoin,ethereum,solana").split(",")
    # 적립·모니터링 대상 포트폴리오
    ACCUMULATION_PORTFOLIO = os.getenv(
        "ACCUMULATION_PORTFOLIO",
        "QQQM,SPYM,GLDM,IBIT,SGOV,"       # 코어 5종목 (GLDM 미보유 → 목표)
        "SPMO,SOXQ,"                       # 위성 (각 상한 10%, 저수지 구간 매수)
        "QLD,TQQQ,SSO,UPRO,"              # 레버리지 (조정 시 전술)
        "QQQI,SPYI",                       # 레거시 (정리 중)
    ).replace(" ", "").split(",")

    # ── 실제 보유 주수 (IBKR 연결 실패 시 폴백) ──────────────────
    # IBKR Flex Query로 자동 업데이트됨. 이 값은 비상 폴백용.
    HOLDINGS: dict[str, float] = {
        "QQQI":  600,
        "SPYI":  600,
        "SGOV":  115,
        "SOXL":  1,
        "UPRO":  0.1001,
        "QLD":   0.1001,
        "TQQQ":  0.1001,
        "SSO":   0.1001,
        "QQQM":  0.2896,
        "SOXQ":  1,
        "SPYM":  1.1511,
        "IBIT":  1,
        "USD":   4,      # ProShares Ultra Semiconductors (2x) — 레거시
    }


    # ── 현금 관리 ─────────────────────────────────────────────────
    # 헌법 5조: SGOV 목표 20% (2026.6 개정 — 기존 29%)
    TARGET_CASH_RATIO = float(os.getenv("TARGET_CASH_RATIO", "0.20"))
    CASH_TICKERS = ["SGOV", "BIL", "SHV", "SHY"]  # 현금성 자산
    IDLE_CASH_USD = float(os.getenv("IDLE_CASH_USD", "50.65"))  # 미사용 USD 잔고
    # 배당이 USD로 이만큼 이상 쌓여 있으면 "노는 돈" 알림 (DRIP 대신 모아서 웅덩이에 투입)
    IDLE_CASH_ALERT_USD = float(os.getenv("IDLE_CASH_ALERT_USD", "150"))

    # ── 정기 적립 스케줄 (자동 매수 비활성화) ───────────────────
    DCA_SCHEDULE: dict[str, dict] = {}

    # ════════════════════════════════════════════════════════════
    # 호두 투자 헌법 (Constitution) — CONSTITUTION.md 참조
    # 코드는 이 상수를 단일 진실 소스로 사용한다.
    # ════════════════════════════════════════════════════════════

    # 헌법 5조 — 목표 배분 (2026.6 개정: 현금 29→20%, 반도체 슬라이스 신설)
    # 비중은 대략적 파이 가이드 — 다소 어긋나도 OK (±10%p 드리프트만 경고).
    # 반도체 10%는 위성 SOXQ 슬라이스 (SOXL/USD 레버 합산) — 합계 90% + 반도체 10%.
    CORE_ALLOCATION: dict[str, float] = {
        "QQQM": 0.30,   # Nasdaq 100 성장
        "SPYM": 0.30,   # S&P 500 닻
        "GLDM": 0.07,   # 금 (인플레 헤지)
        "IBIT": 0.03,   # 비트코인 (통화 절하 헤지)
        "SGOV": 0.20,   # 현금 + 매수 탄약
    }

    # 레버리지 → 버킷 매핑 (조정 시 임시 포지션, 노출 합산용)
    LEVERAGE_BUCKET: dict[str, str] = {
        "QLD": "QQQM", "TQQQ": "QQQM",     # Nasdaq 노출
        "SSO": "SPYM", "UPRO": "SPYM",     # S&P 노출
        "SOXL": "SOXQ", "USD": "SOXQ",     # 반도체 노출 (3x/2x)
    }

    # ── 위성(satellite) 지수 ETF — 2026.6 헌법 개정 ──────────────
    # 개별주 금지는 유지. 지수 ETF는 트랙레코드 5년+로 완화.
    # 위성은 코어를 대체하지 않음: 해당 버킷 안에서 상한까지만,
    # 매수는 저수지(웅덩이) 구간에서 신규자금·배당·레거시 정리 대금으로만.
    SATELLITE_TICKERS: dict[str, float] = {"SPMO": 0.10, "SOXQ": 0.10}   # 총자산 대비 상한
    SATELLITE_BUCKET: dict[str, str] = {"SPMO": "SPYM"}    # SPMO는 S&P 버킷 합산, SOXQ는 자체 반도체 슬라이스

    # ── 저수지(웅덩이) 매수 구간 — 52주 고점 대비 종목별 낙폭 ────
    # 역할 분담: 얼마 쏠지 = 헌법 6조 S&P ATH 트리거 / 어디에 쏠지 = 종목별 수위
    # scale: 변동성 큰 자산은 같은 의미의 낙폭이 더 깊음 (IBIT ≈ 주식 3배)
    # fire: 탄약(SGOV 평가액 + 유휴 현금) 중 이 종목에 투입할 비율
    RESERVOIR_ZONES: list[dict] = [
        {"dd": -3,  "label": "1단계", "fire": 0.0,  "action": "이번 달 납입·배당 매수를 앞당겨 실행"},
        {"dd": -7,  "label": "2단계", "fire": 0.25, "action": "탄약 25% 투입"},
        {"dd": -15, "label": "3단계", "fire": 0.50, "action": "탄약 50% 투입"},
        {"dd": -25, "label": "4단계", "fire": 1.00, "action": "탄약 전량 투입"},
    ]
    RESERVOIR_SCALE: dict[str, float] = {"IBIT": 3.0, "SOXQ": 1.5}   # 반도체는 낙폭 1.5배 보정
    RESERVOIR_WATCH: list[str] = ["QQQM", "SPYM", "SPMO", "SOXQ", "GLDM", "IBIT"]

    # ── 개별주 워치리스트 — 정보용 (이 계좌는 매수 안 함, 헌법 4조 그대로) ──
    # 아내가 별도 계좌에서 실제로 보유 중인 개별주 — 저수지 구간으로 매수 타이밍만 안내
    INDIVIDUAL_WATCHLIST: list[str] = ["SPCX", "NVDA", "GOOG", "AEHR", "TSLA", "MU"]

    # ── 코어 과열 부분 익절 (헌법 7조 예외, 2026.6 신설) ─────────
    # "많이 오르고 현금이 필요하면 판다" — RSI 과열 + 현금 부족 + S&P ATH 근처일 때만
    CORE_TRIM_RSI = 70           # 이 RSI 이상이면 과열
    CORE_TRIM_CASH_GAP = 0.03    # 현금 비중이 목표보다 이만큼(%p) 부족하면 트리거
    CORE_TRIM_PCT = 0.05         # 과열 종목의 5%만 부분 익절
    # 현금이 사실상 0%면 ATH 근접 조건 없이도 트림 검토 (2026.6 신설)
    CORE_TRIM_CASH_FLOOR = 0.01
    # 과열이어도 신고가 행진 중(자체 고점 근처)이면 안 팖 — 고점에서 이만큼 꺾여야 제안
    CORE_TRIM_PULLBACK = -1.5

    # 청산 예정 레거시 종목 (헌법 5종목 외 — 신규 매수 금지, 세금 룰 따라 정리)
    # SOXQ는 2026.6 위성 승격, SOXL/USD는 반도체 레버 노출로 분류 → 레거시 제외
    LEGACY_TICKERS: list[str] = ["QQQI", "SPYI", "SOXX",
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

    # 헌법 1·9조 — 거주국 phase 전환 일정 (NZ Transitional 시작 = KR_PHASE_END)
    NZ_FIF_START = "2031-11"   # NZ Transitional → FIF (FDR 5% deemed income, 1년)
    AU_MOVE = "2032-11"        # NZ → 호주 (영구 정착)
    # 거래기록(.transactions.json) 미동기화 시 실현차익 하한 — 사용자가 직접 갱신.
    # 올해 250만원 공제를 이미 소진했다면 250만원으로 설정해 매도 플랜이 막히게 함.
    KR_CGT_REALIZED_KRW_OVERRIDE = float(os.getenv("KR_CGT_REALIZED_KRW_OVERRIDE", "2500000"))

    # 비상금 (-30% 총동원에서도 제외)
    EMERGENCY_FUND_USD = float(os.getenv("EMERGENCY_FUND_USD", "10000"))

    # ── 결정 엔진 (/now, /goal, /tax) ────────────────────────────
    # 헌법 1조: 월 납입 가능 ₩150~200만. 신규 납입 계획 없을 땐 0 —
    # 이 경우 /now·/goal은 배당 재투자 기준으로 전환.
    MONTHLY_DEPOSIT_KRW = float(os.getenv("MONTHLY_DEPOSIT_KRW", "0"))
    # 마일스톤 ETA 계산용 장기 기대수익률 (보수적 가정)
    EXPECTED_ANNUAL_RETURN = float(os.getenv("EXPECTED_ANNUAL_RETURN", "0.07"))
    # USD/KRW 환율 조회 실패 시 폴백
    FX_USDKRW_FALLBACK = float(os.getenv("FX_USDKRW_FALLBACK", "1400"))

