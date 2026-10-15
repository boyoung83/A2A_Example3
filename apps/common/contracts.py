# 파일명: contracts.py
"""K정밀웍스 ↔ K-Logistics 사이의 데이터 계약 (Ch16 확장).

회사 간 메시지는 DataPart(JSON) 하나에 {"skill": ..., ...필드} 를 담는다.
양쪽 모두 이 모델로 검증하므로, 상대가 다른 모양을 보내면 반영하지 않는다(E6).
"""

from pydantic import BaseModel, Field

# 출고 건 상태 — 앞에서 뒤로만 진행한다. CANCELLED는 ACCEPTED에서만 갈 수 있다.
SHIPMENT_FLOW = ["ACCEPTED", "PICKED_UP", "IN_TRANSIT", "DELIVERED"]
SHIPMENT_LABEL = {
    "ACCEPTED": "출고 접수",
    "PICKED_UP": "집하 완료",
    "IN_TRANSIT": "배송 중",
    "DELIVERED": "배송 완료",
    "CANCELLED": "취소",
}

SKILL_CHECK_DELIVERY = "check-delivery"
SKILL_QUOTE = "quote"
SKILL_CREATE_SHIPMENT = "create-shipment"
SKILL_CANCEL_SHIPMENT = "cancel-shipment"
SKILL_SERVICE_AREA = "service-area"


# --- 요청 ---------------------------------------------------------------

class DeliveryQuery(BaseModel):
    order_no: str = Field(min_length=1)


class QuoteRequest(BaseModel):
    """도착지 등이 비어 있어도 계약 위반이 아니다. 물류사가 INPUT_REQUIRED로 되묻는다(E2)."""
    request_id: str = Field(min_length=1)
    order_no: str = Field(min_length=1)
    shipper: str = ""
    weight_kg: float = Field(default=0, ge=0)
    origin: str = ""
    destination: str = ""
    due_date: str = ""
    express: bool = False

    def missing_fields(self) -> list[str]:
        missing = [name for name in ("origin", "destination") if not getattr(self, name).strip()]
        if self.weight_kg <= 0:
            missing.append("weight_kg")
        return missing


class ShipmentRequest(BaseModel):
    request_id: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    order_no: str = Field(min_length=1)
    shipper: str = ""
    contact: str = ""


class CancelRequest(BaseModel):
    request_id: str = Field(min_length=1)
    shipment_id: str = Field(min_length=1)
    reason: str = ""


# --- 응답 ---------------------------------------------------------------

class DeliveryEvent(BaseModel):
    status: str
    memo: str = ""
    at: str


class DeliveryInfo(BaseModel):
    found: bool
    order_no: str
    shipment_id: str | None = None
    status: str | None = None
    eta: str | None = None
    history: list[DeliveryEvent] = []

    def summary(self) -> str:
        if not self.found:
            return f"[{self.order_no}] 조회 결과 없음: 등록된 출고 건이 없습니다"
        label = SHIPMENT_LABEL.get(self.status or "", self.status)
        eta = f" · 도착 예정 {self.eta}" if self.eta else ""
        return f"[{self.order_no}] 상태: {label}{eta}"


class Quote(BaseModel):
    quote_id: str
    carrier: str
    order_no: str
    price_krw: int = Field(ge=0)
    eta_days: int = Field(ge=0)
    valid_until: str

    def summary(self) -> str:
        return f"[{self.order_no}] 견적 {self.price_krw:,}원 · {self.eta_days}일 (유효 {self.valid_until[:16]})"


class ShipmentReceipt(BaseModel):
    shipment_id: str
    order_no: str
    status: str

    def summary(self) -> str:
        return f"[{self.order_no}] 출고 {self.shipment_id}: {SHIPMENT_LABEL.get(self.status, self.status)}"


class ServiceArea(BaseModel):
    """물류사가 운행하는 도시 목록. 화주는 이 목록에서 출발지·도착지를 고른다."""
    carrier: str
    cities: list[str] = Field(min_length=1)

    def summary(self) -> str:
        return f"{self.carrier} 운행 도시 {len(self.cities)}곳: {', '.join(self.cities)}"


class Rejection(BaseModel):
    """요청은 이해했지만 업무 규칙상 받아들일 수 없다 → Task REJECTED."""
    reason: str
    detail: str = ""


class InputNeeded(BaseModel):
    """요청을 처리하려면 정보가 더 필요하다 → Task INPUT_REQUIRED."""
    missing: list[str]
    detail: str = ""


REPLY_MODELS = {
    SKILL_CHECK_DELIVERY: DeliveryInfo,
    SKILL_QUOTE: Quote,
    SKILL_CREATE_SHIPMENT: ShipmentReceipt,
    SKILL_CANCEL_SHIPMENT: ShipmentReceipt,
    SKILL_SERVICE_AREA: ServiceArea,
}


def validate_reply(skill: str, raw: dict, order_no: str | None = None) -> BaseModel:
    """완료 응답을 계약으로 검증한다. 요청한 주문과 다른 주문의 응답이면 거절한다(Ch16)."""
    reply = REPLY_MODELS[skill].model_validate(raw)
    if order_no is not None and getattr(reply, "order_no", order_no) != order_no:
        raise ValueError(f"요청한 주문({order_no})과 다른 주문({reply.order_no})의 응답이다")
    return reply
