# 파일명: bad_partner.py
"""E6 실험용: 계약을 어기는 물류사. quote에 '다른 주문번호'와 음수 금액을 돌려준다."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill  # noqa: E402

from common.a2a_io import COMPLETED, Outcome, SkillExecutor, build_a2a_app  # noqa: E402

PORT = int(os.getenv("BAD_PARTNER_PORT", "9862"))


async def quote(payload: dict) -> Outcome:
    return Outcome(COMPLETED, {"quote_id": "BAD-1", "carrier": "불량 물류", "order_no": "SOMEONE-ELSE",
                               "price_krw": -1, "eta_days": 2, "valid_until": "2099-01-01T00:00:00+09:00"},
                   "견적 -1원 (다른 주문)")


card = AgentCard(
    name="불량 물류(실험)", description="계약 위반 응답 실험용", version="0.0.1",
    default_input_modes=["application/json"], default_output_modes=["application/json"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[AgentInterface(url=f"http://127.0.0.1:{PORT}/", protocol_binding="JSONRPC",
                                         protocol_version="1.0")],
    skills=[AgentSkill(id="quote", name="quote", description="잘못된 견적")],
)
app = build_a2a_app(card, SkillExecutor({"quote": quote}, name="불량 물류"))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
