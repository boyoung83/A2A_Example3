# 파일명: run_all.py
"""plan_2.md 3장의 시나리오를 웹 화면·관제 허브 없이 자동으로 돌려 판정한다.

    uv run apps/scenarios/run_all.py

이 스크립트가 에이전트를 직접 띄운다(K-Logistics 9860, K정밀웍스 9861, 불량 물류 9862, 임시 DB).
이미 띄워 둔 9810·9820과 겹치지 않는다. 사람 쪽 조작(구매 담당자·물류 운영자)은 CLI와 똑같이
service 함수를 직접 부른다.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

APPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APPS))

import httpx  # noqa: E402

from common.a2a_io import call_skill, call_text  # noqa: E402

KL_PORT, KP_PORT, BAD_PORT = (int(os.getenv(k, d)) for k, d in
                              (("RUN_KL_PORT", "9860"), ("RUN_KP_PORT", "9861"), ("RUN_BAD_PORT", "9862")))
KL_URL, KP_URL, BAD_URL = (f"http://127.0.0.1:{p}" for p in (KL_PORT, KP_PORT, BAD_PORT))
TMP = Path(tempfile.mkdtemp(prefix="a2a_apps_"))

ENV = {k: v for k, v in os.environ.items() if k != "A2A_OBS_URL"}  # 관제 허브 없이(E9)
ENV.update(PYTHONIOENCODING="utf-8", KL_DB=str(TMP / "kl.db"), KP_DB=str(TMP / "kp.db"),
           KL_AGENT_PORT=str(KL_PORT), KP_AGENT_PORT=str(KP_PORT), BAD_PARTNER_PORT=str(BAD_PORT),
           KL_AGENT_URL=KL_URL, KL_FAIL_ORDERS="ORDER-E8", KP_PARTNER_TIMEOUT="5")
os.environ.update({k: ENV[k] for k in ("KL_DB", "KP_DB", "KL_AGENT_URL", "KL_FAIL_ORDERS", "KP_PARTNER_TIMEOUT")})

from klogistics.service import LogisticsService  # noqa: E402
from kprecision.service import ProcurementService  # noqa: E402

procs: dict[str, subprocess.Popen] = {}
results: list[tuple[str, bool, str]] = []


def start(name: str, script: str, url: str) -> None:
    log = open(TMP / f"{name}.log", "a", encoding="utf-8")
    procs[name] = subprocess.Popen([sys.executable, str(APPS / script)], env=ENV, stdout=log, stderr=log)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/.well-known/agent-card.json", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"{name}이(가) 뜨지 않았습니다. 로그: {TMP / (name + '.log')}")


def stop(name: str) -> None:
    p = procs.pop(name, None)
    if p:
        p.terminate()
        p.wait(10)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


async def scenario(sid: str, title: str, body) -> None:
    try:
        note = await body()
        results.append((sid, True, title + (f" — {note}" if note else "")))
        print(f"  PASS  {sid:<4} {title}" + (f" — {note}" if note else ""))
    except Exception as e:
        results.append((sid, False, f"{title}: {e}"))
        print(f"  FAIL  {sid:<4} {title}: {e}")
        traceback.print_exc(limit=3)


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"임시 데이터: {TMP}")
    start("klogistics", "klogistics/agent.py", KL_URL)
    start("kprecision", "kprecision/agent.py", KP_URL)
    start("bad_partner", "scenarios/bad_partner.py", BAD_URL)
    print("에이전트 3개 기동 (웹 화면·관제 허브 없음)\n")

    kp = ProcurementService()          # 구매 담당자 (CLI·포털과 같은 함수)
    kl = LogisticsService()            # 물류 운영 담당자 (CLI·콘솔과 같은 함수)

    async def s0():
        r = await call_text(KP_URL, "ORDER-1001")
        check(r.state == "COMPLETED" and "배송 중" in r.text, f"{r.state} {r.text}")
        return r.text

    async def s1():
        r = await kp.track("ORDER-1001")
        d = r["order"]["delivery"]
        check(r["state"] == "COMPLETED" and d["status"] == "IN_TRANSIT" and d["eta"] == "2026-09-03", r["message"])
        return r["message"]

    async def e1():
        r = await kp.track("ORDER-9999")
        check(r["state"] == "COMPLETED" and "조회 결과 없음" in r["message"], r["message"])
        return "실패가 아니라 '결과 없음'으로 완료"

    async def s2():
        kp.create_order("ORDER-S2", item="기어박스", qty=10, weight_kg=120, origin="창원", due_date="2026-10-25")
        r = await kp.request_quote("ORDER-S2")
        check(r["state"] == "INPUT_REQUIRED" and r["order"]["input_required"]["missing"] == ["destination"], r["message"])
        first_task = r["order"]["requests"][0]["task_id"]
        r = await kp.provide_input("ORDER-S2", destination="평택")
        req = r["order"]["requests"][0]
        check(r["state"] == "COMPLETED" and r["order"]["status"] == "QUOTED", r["message"])
        check(req["task_id"] == first_task and req["attempts"] == 2, "같은 Task로 이어 보내지 않았다")
        kp.approve("ORDER-S2")
        r = await kp.request_shipment("ORDER-S2", contact="평택공장 010-0000-0000")
        check(r["state"] == "COMPLETED" and r["order"]["status"] == "SHIPPING_REQUESTED", r["message"])
        return f"E2 되묻기 → 같은 Task 재개 → {r['order']['shipment_id']} 접수"

    async def s3():
        sid = kp.get_order("ORDER-S2")["shipment_id"]
        kl.advance(sid, memo="창원 집하")
        await kp.sync()
        check(kp.get_order("ORDER-S2")["status"] == "IN_DELIVERY", "동기화 후 배송 중이 아니다")
        tracks = lambda: sum(1 for r in kp.list_requests(500) if r["skill"] == "check-delivery")  # noqa: E731
        before = tracks()
        await kp.sync()  # 바뀐 것이 없으면 요청 기록을 남기지 않는다
        check(tracks() == before, f"변경 없는 동기화가 기록됐다 ({before} → {tracks()})")
        kl.advance(sid)
        kl.advance(sid, "DELIVERED", memo="평택 공장 인수")
        r = await kp.track("ORDER-S2")
        check(r["order"]["status"] == "DELIVERED" and len(r["order"]["delivery"]["history"]) == 4, r["message"])
        r = kp.confirm_receipt("ORDER-S2")
        check(r["order"]["status"] == "RECEIVED", "입고 확인 실패")
        return "물류 운영자 갱신 → 화주 동기화 → 입고 완료"

    async def e3():
        kp.create_order("ORDER-E3", weight_kg=50, origin="부산", destination="대구")
        r = await kp.request_quote("ORDER-E3")
        qid = r["order"]["quotes"][0]["quote_id"]
        kp.approve("ORDER-E3")
        kl.expire_quote(qid)
        r = await kp.request_shipment("ORDER-E3")
        check(r["state"] == "REJECTED" and r["order"]["status"] == "DRAFT", r["message"])
        check(r["order"]["quotes"][0]["status"] == "EXPIRED", "견적이 만료 처리되지 않았다")
        r = await kp.request_quote("ORDER-E3")
        check(r["state"] == "COMPLETED" and r["order"]["status"] == "QUOTED", "재견적 실패")
        return "만료 견적 거절 → 작성으로 복귀 → 재견적"

    async def s4():
        a = await kp.service_area()
        check(a["fresh"] and "창원" in a["cities"] and "상주" not in a["cities"], str(a))
        return f"{len(a['cities'])}곳: {', '.join(a['cities'])}"

    async def e4():
        kp.create_order("ORDER-E4", weight_kg=200, origin="인천", destination="광주")
        stop("klogistics")
        a = await kp.service_area()
        check(not a["fresh"] and a["cities"], "물류사가 꺼졌을 때 마지막 도시 목록을 쓰지 않았다")
        r = await kp.request_quote("ORDER-E4")
        check(r["state"] == "PENDING_RETRY", r["message"])
        rid = r["order"]["requests"][0]["request_id"]
        start("klogistics", "klogistics/agent.py", KL_URL)
        r = await kp.retry(rid)
        req = r["order"]["requests"][0]
        check(r["state"] == "COMPLETED" and req["request_id"] == rid and req["attempts"] == 2, r["message"])
        return f"물류사 꺼짐 → PENDING_RETRY → 재기동 후 같은 업무키 {rid}로 완료"

    async def e5():
        kp.approve("ORDER-E4")
        r = await kp.request_shipment("ORDER-E4")
        req = next(q for q in r["order"]["requests"] if q["skill"] == "create-shipment")
        sids = set()
        for _ in range(3):  # 응답을 못 받았다고 가정하고 같은 요청을 다시 보낸다
            again = await call_skill(KL_URL, "create-shipment", req["payload"])
            sids.add(again.data["shipment_id"])
        count = sum(1 for s in kl.list_shipments() if s["order_no"] == "ORDER-E4")
        check(sids == {r["order"]["shipment_id"]} and count == 1, f"출고 건 {count}개, {sids}")
        return f"같은 업무키 4회 전송 → 출고 건 1개 ({r['order']['shipment_id']})"

    async def e6():
        bad = ProcurementService(path=TMP / "kp_bad.db", partner_url=BAD_URL)
        bad.create_order("ORDER-E6", weight_kg=10, origin="서울", destination="부산")
        r = await bad.request_quote("ORDER-E6")
        check(r["state"] == "INVALID_RESPONSE" and r["order"]["status"] == "DRAFT" and not r["order"]["quotes"],
              r["message"])
        return "계약 위반 응답은 반영하지 않고 원문만 보관"

    async def e7():
        kp.create_order("ORDER-E7", weight_kg=30, origin="대전", destination="서울")
        await kp.request_quote("ORDER-E7")
        kp.approve("ORDER-E7")
        await kp.request_shipment("ORDER-E7")
        r = await kp.cancel("ORDER-E7", "수요 취소")
        check(r["state"] == "COMPLETED" and r["order"]["status"] == "CANCELLED", r["message"])
        r = await kp.cancel("ORDER-1001", "테스트")
        check(r["state"] == "REJECTED" and r["order"]["status"] == "IN_DELIVERY", r["message"])
        return "집하 전 취소 완료 / 집하 후 취소 거절"

    async def e8():
        kp.create_order("ORDER-E8", weight_kg=10, origin="서울", destination="수원")
        r = await kp.request_quote("ORDER-E8")
        req = r["order"]["requests"][0]
        check(r["state"] == "FAILED" and "운임 시스템 오류" in (req["last_error"] or ""), r["message"])
        return "FAILED + 사유 전달"

    async def e9_e10():
        check("A2A_OBS_URL" not in ENV, "관제 허브 주소가 설정돼 있다")
        return "웹 화면(9811·9821)·관제 허브(9900)를 쓰지 않고 위 시나리오 완료"

    try:
        await scenario("S0", "기존 방식 텍스트 문의(call_router 호환)", s0)
        await scenario("S1", "배송 조회", s1)
        await scenario("S4", "물류사 운행 도시 목록 (service-area)", s4)
        await scenario("E1", "없는 주문 조회", e1)
        await scenario("S2", "견적 → 추가 정보 → 승인 → 출고 요청", s2)
        await scenario("S3", "물류사 상태 갱신 → 화주 반영 → 입고", s3)
        await scenario("E3", "만료 견적으로 출고 요청", e3)
        await scenario("E4", "물류사 꺼짐 → 재시도", e4)
        await scenario("E5", "같은 업무키 중복 전송", e5)
        await scenario("E6", "계약 위반 응답", e6)
        await scenario("E7", "출고 취소", e7)
        await scenario("E8", "물류사 내부 오류", e8)
        await scenario("E9", "웹·관제 없이 연동", e9_e10)
    finally:
        for name in list(procs):
            stop(name)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
