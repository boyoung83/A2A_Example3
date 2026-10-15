# 파일명: pricing.py
"""K-Logistics 요금 규칙. 거리는 서울 기준 대략적인 좌표(km)로 계산한다."""

import math

CARRIER = "K-Logistics"
BASE_KRW = 30_000
PER_KM_KRW = 300
PER_KG_KRW = 150
EXPRESS_RATE = 1.5
QUOTE_TTL_HOURS = 24

CITY_XY_KM = {
    "서울": (0, 0), "인천": (-30, 0), "수원": (5, -35), "평택": (10, -60), "천안": (25, -80),
    "대전": (40, -140), "청주": (55, -110), "대구": (150, -210), "구미": (110, -180),
    "광주": (-40, -270), "울산": (230, -250), "창원": (150, -300), "부산": (200, -300),
}


def distance_km(origin: str, destination: str) -> int | None:
    a, b = CITY_XY_KM.get(origin.strip()), CITY_XY_KM.get(destination.strip())
    if a is None or b is None:
        return None
    return round(math.dist(a, b) * 1.3)  # 직선거리 → 도로거리 보정


def price(km: int, weight_kg: float, express: bool) -> tuple[int, int]:
    """(금액, 소요일)"""
    krw = BASE_KRW + km * PER_KM_KRW + weight_kg * PER_KG_KRW
    days = 1 if km < 150 else 2 if km < 300 else 3
    if express:
        krw *= EXPRESS_RATE
        days = max(1, days - 1)
    return int(round(krw, -2)), days


def table() -> dict:
    return {
        "carrier": CARRIER, "base_krw": BASE_KRW, "per_km_krw": PER_KM_KRW, "per_kg_krw": PER_KG_KRW,
        "express_rate": EXPRESS_RATE, "quote_ttl_hours": QUOTE_TTL_HOURS, "cities": sorted(CITY_XY_KM),
    }
