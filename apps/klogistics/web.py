# 파일명: web.py
"""K-Logistics 운영 콘솔 (기본 9811). 선택 기능이다 — 꺼도 에이전트·CLI로 같은 업무를 할 수 있다.

    uv run apps/klogistics/web.py      → http://127.0.0.1:9811
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi import Body, FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

from common.contracts import SHIPMENT_FLOW, SHIPMENT_LABEL  # noqa: E402
from klogistics import pricing  # noqa: E402
from klogistics.service import LogisticsError, LogisticsService  # noqa: E402

HOST = os.getenv("KL_WEB_HOST", "127.0.0.1")
PORT = int(os.getenv("KL_WEB_PORT", "9811"))
OBS_URL = os.getenv("A2A_OBS_URL", "http://127.0.0.1:9900").rstrip("/")
STATIC = Path(__file__).resolve().parent / "static"

service = LogisticsService()
app = FastAPI(title="K-Logistics 운영 콘솔")


@app.exception_handler(LogisticsError)
async def rule_error(_: Request, e: LogisticsError):
    return JSONResponse({"detail": str(e)}, status_code=409)


@app.exception_handler(Exception)
async def unexpected(_: Request, e: Exception):
    return JSONResponse({"detail": f"서버 오류: {type(e).__name__}: {e}"}, status_code=500)


@app.get("/")
async def index():
    return FileResponse(STATIC / "console.html")


@app.get("/api/meta")
async def meta():
    return {"flow": SHIPMENT_FLOW, "label": SHIPMENT_LABEL, "pricing": pricing.table(), "obs_url": OBS_URL}


@app.get("/api/shipments")
async def shipments(status: str | None = None):
    return service.list_shipments(status)


@app.get("/api/shipments/{shipment_id}")
async def shipment(shipment_id: str):
    return service.get_shipment(shipment_id)


@app.post("/api/shipments/{shipment_id}/advance")
async def advance(shipment_id: str, body: dict = Body(default={})):
    return service.advance(shipment_id, body.get("status") or None, body.get("memo", ""))


@app.get("/api/quotes")
async def quotes():
    return service.list_quotes()


@app.post("/api/quotes/{quote_id}/expire")
async def expire(quote_id: str):
    service.expire_quote(quote_id)
    return {"quote_id": quote_id, "status": "EXPIRED"}


if __name__ == "__main__":
    print(f"K-Logistics 운영 콘솔 http://{HOST}:{PORT}  DB={service.path}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
