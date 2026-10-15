# 파일명: agent.py
"""K정밀웍스 구매 에이전트 (A2A, 기본 9820). 외부(사내 다른 시스템, 기존 call_router.py)에서 들어온
배송 문의를 받아 물류사에 위임한다. 구매 담당자의 업무(발주·견적·출고 요청)는 포털·CLI에서 한다.

    uv run apps/kprecision/agent.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402

from common.a2a_io import COMPLETED, Outcome, SkillExecutor, build_a2a_app  # noqa: E402
from common.contracts import DeliveryQuery  # noqa: E402
from kprecision.service import ProcurementService  # noqa: E402

HOST = os.getenv("KP_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("KP_AGENT_PORT", "9820"))
SKILL_ROUTE = "route-delivery-inquiry"

service = ProcurementService()


async def route_delivery_inquiry(payload: dict) -> Outcome:
    order_no = DeliveryQuery.model_validate(payload).order_no
    summary, data = await service.route_inquiry(order_no)
    return Outcome(COMPLETED, data, f"[구매 에이전트 -> K-Logistics] {summary}")


agent_card = AgentCard(
    name="K정밀웍스 구매 에이전트",
    description="배송 문의를 물류 파트너(K-Logistics)에게 위임해 결과를 돌려준다",
    version="1.0.0",
    default_input_modes=["text/plain", "application/json"],
    default_output_modes=["text/plain", "application/json"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(url=f"http://{HOST}:{PORT}/", protocol_binding="JSONRPC", protocol_version="1.0")
    ],
    skills=[
        AgentSkill(id=SKILL_ROUTE, name=SKILL_ROUTE, description="주문번호의 배송 상태를 물류 파트너에게 물어 반환한다",
                   input_modes=["text/plain", "application/json"], output_modes=["text/plain", "application/json"])
    ],
)

executor = SkillExecutor({SKILL_ROUTE: route_delivery_inquiry}, default_skill=SKILL_ROUTE,
                         text_to_payload=lambda text: {"order_no": text}, name="K정밀웍스")
app = build_a2a_app(agent_card, executor)

if __name__ == "__main__":
    print(f"K정밀웍스 구매 에이전트 http://{HOST}:{PORT}  물류사={service.partner_url}  DB={service.path}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
