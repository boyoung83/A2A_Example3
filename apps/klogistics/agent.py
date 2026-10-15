# 파일명: agent.py
"""K-Logistics 물류 에이전트 (A2A, 기본 9810). 회사 간 연동은 이 프로세스 하나면 된다 — 운영 콘솔(web.py)은 선택.

인증(Ch18): K-Logistics가 화주에게 발급한 키를 X-Partner-Key 헤더로 받는다. 키 없이 오면 REJECTED.
권한: 키로 확인된 화주는 자기 출고 건·견적만 조회·사용·취소할 수 있다.

    uv run apps/klogistics/agent.py
    uv run obs/obs_run.py apps/klogistics/agent.py      # 관제 화면으로 볼 때
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402

from common.auth import KL_PARTNERS, card_security, issued_keys  # noqa: E402
from common.a2a_io import COMPLETED, INPUT_REQUIRED, REJECTED, Outcome, SkillExecutor, build_a2a_app  # noqa: E402
from common.contracts import (  # noqa: E402
    SKILL_CANCEL_SHIPMENT, SKILL_CHECK_DELIVERY, SKILL_CREATE_SHIPMENT, SKILL_QUOTE, SKILL_SERVICE_AREA, CancelRequest,
    DeliveryQuery,
    InputNeeded, QuoteRequest, Rejection, ShipmentRequest,
)
from klogistics.service import LogisticsService, dump  # noqa: E402

HOST = os.getenv("KL_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("KL_AGENT_PORT", "9810"))


def to_outcome(result) -> Outcome:
    if isinstance(result, Rejection):
        return Outcome(REJECTED, dump(result), f"거절: {result.detail or result.reason}")
    if isinstance(result, InputNeeded):
        return Outcome(INPUT_REQUIRED, dump(result), result.detail)
    return Outcome(COMPLETED, dump(result), result.summary())


def build_handlers(service: LogisticsService) -> dict:
    async def check_delivery(payload: dict, caller: str) -> Outcome:
        return to_outcome(service.check_delivery(DeliveryQuery.model_validate(payload).order_no, shipper=caller))

    async def quote(payload: dict, caller: str) -> Outcome:
        return to_outcome(service.quote(QuoteRequest.model_validate(payload), shipper=caller))

    async def create_shipment(payload: dict, caller: str) -> Outcome:
        return to_outcome(service.create_shipment(ShipmentRequest.model_validate(payload), shipper=caller))

    async def cancel_shipment(payload: dict, caller: str) -> Outcome:
        return to_outcome(service.cancel_shipment(CancelRequest.model_validate(payload), shipper=caller))

    async def service_area(payload: dict, caller: str) -> Outcome:
        return to_outcome(service.service_area())

    return {SKILL_CHECK_DELIVERY: check_delivery, SKILL_QUOTE: quote,
            SKILL_CREATE_SHIPMENT: create_shipment, SKILL_CANCEL_SHIPMENT: cancel_shipment,
            SKILL_SERVICE_AREA: service_area}


def skill(skill_id: str, description: str) -> AgentSkill:
    return AgentSkill(id=skill_id, name=skill_id, description=description,
                      input_modes=["application/json", "text/plain"], output_modes=["application/json", "text/plain"])


agent_card = AgentCard(
    name="K-Logistics 물류",
    description="운임 견적, 출고 접수·취소, 배송 조회를 처리하는 K-Logistics 물류 에이전트",
    version="1.0.0",
    default_input_modes=["application/json", "text/plain"],
    default_output_modes=["application/json", "text/plain"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(url=f"http://{HOST}:{PORT}/", protocol_binding="JSONRPC", protocol_version="1.0")
    ],
    skills=[
        skill(SKILL_CHECK_DELIVERY, "주문번호의 출고 건 상태와 이력을 반환한다 (텍스트 주문번호도 받음)"),
        skill(SKILL_QUOTE, "중량·출발지·도착지로 운임 견적을 낸다. 정보가 비면 INPUT_REQUIRED로 되묻는다"),
        skill(SKILL_CREATE_SHIPMENT, "유효한 견적으로 출고를 접수한다. 만료 견적은 REJECTED"),
        skill(SKILL_CANCEL_SHIPMENT, "집하 전 출고 건을 취소한다. 집하 후면 REJECTED"),
        skill(SKILL_SERVICE_AREA, "운행하는 도시 목록을 반환한다 (견적 요청 전 출발지·도착지 선택용)"),
    ],
    **card_security("K-Logistics가 계약 화주에게 발급한 파트너 키를 헤더에 담아 보낸다"),
)

service = LogisticsService()
executor = SkillExecutor(build_handlers(service), default_skill=SKILL_CHECK_DELIVERY,
                         text_to_payload=lambda text: {"order_no": text}, name="K-Logistics", auth_required=True)
partner_keys = issued_keys("KL_PARTNER_KEYS", KL_PARTNERS)
app = build_a2a_app(agent_card, executor, keys=partner_keys)

if __name__ == "__main__":
    print(f"K-Logistics 물류 에이전트 http://{HOST}:{PORT}  DB={service.path}  키 발급 화주={list(partner_keys)}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
