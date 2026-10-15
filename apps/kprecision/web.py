# 파일명: web.py
"""K정밀웍스 구매 포털 (기본 9821). 선택 기능이다 — 꺼도 에이전트·CLI로 같은 업무를 할 수 있다.

    uv run apps/kprecision/web.py      → http://127.0.0.1:9821
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi import Body, FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from common.a2a_io import fetch_card  # noqa: E402
from kprecision.service import ORDER_LABEL, SHIPMENT_LABEL, ProcurementError, ProcurementService  # noqa: E402

HOST = os.getenv("KP_WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("KP_WEB_PORT", "9821"))
OBS_URL = os.getenv("A2A_OBS_URL", "http://127.0.0.1:9900").rstrip("/")
STATIC = Path(__file__).resolve().parent / "static"

service = ProcurementService()
app = FastAPI(title="K정밀웍스 구매 포털")


@app.exception_handler(ProcurementError)
async def rule_error(_: Request, e: ProcurementError):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(ValueError)
async def bad_input(_: Request, e: ValueError):
    return JSONResponse({"detail": f"입력값 오류: {e}"}, status_code=400)


@app.exception_handler(Exception)
async def unexpected(_: Request, e: Exception):
    return JSONResponse({"detail": f"서버 오류: {type(e).__name__}: {e}"}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(STATIC / "portal.html")


@app.get("/api/meta")
async def meta():
    return {"order_label": ORDER_LABEL, "shipment_label": SHIPMENT_LABEL, "partner_url": service.partner_url,
            "obs_url": OBS_URL}


@app.get("/api/partner")
async def partner():
    try:
        card = await fetch_card(service.partner_url)
        return {"online": True, "name": card.name, "skills": [s.id for s in card.skills]}
    except Exception as e:
        return {"online": False, "error": str(e)}


@app.get("/api/cities")
async def cities():
    """물류사 운행 도시 (A2A service-area). 물류사가 꺼져 있으면 마지막으로 받은 목록."""
    return await service.service_area()


@app.get("/api/orders")
async def orders(status: str | None = None):
    return service.list_orders(status)


@app.post("/api/orders")
async def create_order(body: dict = Body(...)):
    fields = _order_fields(body, with_no=True)
    return service.create_order(fields.pop("order_no", ""), **fields)  # 빈 번호는 service가 규칙 오류로 막는다


@app.get("/api/orders/{order_no}")
async def order(order_no: str):
    return service.get_order(order_no)


@app.patch("/api/orders/{order_no}")
async def update_order(order_no: str, body: dict = Body(...)):
    return service.update_order(order_no, **_order_fields(body))


@app.post("/api/orders/{order_no}/{action}")
async def order_action(order_no: str, action: str, body: dict = Body(default={})):
    match action:
        case "quote": return await service.request_quote(order_no)
        case "input": return await service.provide_input(order_no, **_order_fields(body))
        case "approve": return service.approve(order_no, body.get("quote_id"))
        case "ship": return await service.request_shipment(order_no, body.get("contact", ""))
        case "track": return await service.track(order_no)
        case "receive": return service.confirm_receipt(order_no)
        case "cancel": return await service.cancel(order_no, body.get("reason", ""))
    raise ProcurementError(f"알 수 없는 조작: {action}")


@app.get("/api/requests")
async def requests(limit: int = 100):
    return service.list_requests(limit)


@app.post("/api/requests/{request_id}/retry")
async def retry(request_id: str):
    return await service.retry(request_id)


@app.post("/api/sync")
async def sync():
    return await service.sync()


def _order_fields(body: dict, with_no: bool = False) -> dict:
    keys = ["item", "qty", "weight_kg", "origin", "destination", "due_date", "express", "note"]
    if with_no:
        keys.insert(0, "order_no")
    out = {k: body[k] for k in keys if k in body and body[k] not in (None, "")}
    if "qty" in out:
        out["qty"] = int(out["qty"])
    if "weight_kg" in out:
        out["weight_kg"] = float(out["weight_kg"])
    if "express" in body:
        out["express"] = bool(body["express"])
    return out


if __name__ == "__main__":
    print(f"K정밀웍스 구매 포털 http://{HOST}:{PORT}  물류사={service.partner_url}  DB={service.path}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
