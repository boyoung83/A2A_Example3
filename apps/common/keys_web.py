# 파일명: keys_web.py
"""키 장부 화면용 API — 운영 콘솔(K-Logistics)과 구매 포털(K정밀웍스)이 함께 쓴다. apps/keys.py와 같은 일을 한다.

    GET  /api/keys                      목록 (+ 발급 대상 안내)
    POST /api/keys/issue                {partner, expires_hours?, deliver?}
    POST /api/keys/rotate               {partner, grace_hours?, deliver?}
    POST /api/keys/{key_id}/revoke      {reason?}
    POST /api/keys/revoke-partner       {partner, reason?}
    GET  /keys.js                       화면 스크립트

키 원문은 발급·교체 응답에 한 번만 담긴다. 이 화면들은 각 회사 내부용(127.0.0.1)이라 로그인이 없다 —
외부에 열 때는 반드시 관리자 로그인을 먼저 붙여야 한다.
"""

from pathlib import Path

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, JSONResponse

from common.auth import deliver
from common.keystore import KeyError_, KeyRegistry

STATIC = Path(__file__).resolve().parent / "static"


def keys_router(registry: KeyRegistry, section: str, title: str, partner_hint: str) -> APIRouter:
    router = APIRouter()

    def fail(e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @router.get("/keys.js")
    async def script():
        return FileResponse(STATIC / "keys.js", media_type="text/javascript")

    @router.get("/api/keys")
    async def list_keys():
        return {"title": title, "partner_hint": partner_hint, "ledger": str(registry.path), "keys": registry.list()}

    @router.post("/api/keys/issue")
    async def issue(body: dict = Body(...)):
        try:
            hours = float(body["expires_hours"]) if body.get("expires_hours") not in (None, "") else None
            key_id, key = registry.issue(body.get("partner", ""), hours, body.get("note", ""))
        except (KeyError_, ValueError) as e:
            return fail(e)
        delivered = str(deliver(section, body["partner"].strip(), key)) if body.get("deliver") else None
        return {"key_id": key_id, "partner": body["partner"].strip(), "key": key, "delivered": delivered, "old": []}

    @router.post("/api/keys/rotate")
    async def rotate(body: dict = Body(...)):
        try:
            grace = float(body.get("grace_hours", 24) if body.get("grace_hours") not in (None, "") else 24)
            key_id, key, old = registry.rotate(body.get("partner", ""), grace)
        except (KeyError_, ValueError) as e:
            return fail(e)
        delivered = str(deliver(section, body["partner"], key)) if body.get("deliver") else None
        return {"key_id": key_id, "partner": body["partner"], "key": key, "delivered": delivered, "old": old,
                "grace_hours": grace}

    @router.post("/api/keys/revoke-partner")
    async def revoke_partner(body: dict = Body(...)):
        try:
            return {"revoked": registry.revoke(partner=body.get("partner", ""), reason=body.get("reason", ""))}
        except KeyError_ as e:
            return fail(e)

    @router.post("/api/keys/{key_id}/revoke")
    async def revoke(key_id: str, body: dict = Body(default={})):
        try:
            return {"revoked": registry.revoke(key_id=key_id, reason=body.get("reason", ""))}
        except KeyError_ as e:
            return fail(e)

    return router
