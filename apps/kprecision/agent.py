# 파일명: agent.py
"""K정밀웍스 구매 에이전트 (A2A, 기본 9820). 외부(사내 다른 시스템, 기존 call_router.py)에서 들어온
배송 문의를 받아 물류사에 위임한다. 구매 담당자의 업무(발주·견적·출고 요청)는 포털·CLI에서 한다.

인증(Ch18): 이 에이전트를 부르는 사내 시스템도 K정밀웍스가 발급한 키(X-Partner-Key)가 있어야 한다.
물류사로 넘길 때는 K-Logistics가 발급한 다른 키를 쓴다 — 받은 키를 그대로 넘기지 않는다.

    uv run apps/kprecision/agent.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402

from common.a2a_io import COMPLETED, Outcome, SkillExecutor, build_a2a_app  # noqa: E402
from common.auth import KP_CLIENTS, card_security  # noqa: E402
from common.keystore import KeyRegistry  # noqa: E402
from common.contracts import DeliveryQuery  # noqa: E402
from kprecision.service import ProcurementService  # noqa: E402

HOST = os.getenv("KP_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("KP_AGENT_PORT", "9820"))
SKILL_ROUTE = "route-delivery-inquiry"

service = ProcurementService()


async def route_delivery_inquiry(payload: dict, caller: str) -> Outcome:
    order_no = DeliveryQuery.model_validate(payload).order_no
    print(f"[구매 에이전트] {caller}의 배송 문의: {order_no}")
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
    **card_security("K정밀웍스가 사내 시스템에 발급한 키를 헤더에 담아 보낸다"),
)

executor = SkillExecutor({SKILL_ROUTE: route_delivery_inquiry}, default_skill=SKILL_ROUTE,
                         text_to_payload=lambda text: {"order_no": text}, name="K정밀웍스", auth_required=True)
keys = KeyRegistry(service.path, "kp", "KP_CLIENT_KEYS", KP_CLIENTS)  # 사내 시스템에 발급한 키 장부(구매 DB)
app = build_a2a_app(agent_card, executor, verify=keys.verify)

if __name__ == "__main__":
    print(f"K정밀웍스 구매 에이전트 http://{HOST}:{PORT}  물류사={service.partner_url}  DB={service.path}"
          f"  유효 키={[(k['key_id'], k['partner']) for k in keys.list() if k['state'] in ('ACTIVE', 'EXPIRING')]}  물류사 키={'있음' if service.api_key else '없음'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
