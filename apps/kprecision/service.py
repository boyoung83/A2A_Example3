# 파일명: service.py
"""K정밀웍스 구매 업무 로직. 구매 포털(web.py)·CLI·구매 에이전트가 모두 이 클래스를 부른다.

물류사와는 A2A로만 주고받는다(partner_requests 표에 요청마다 업무키·taskId·상태를 남긴다).
연결이 안 되면 PENDING_RETRY로 남기고, 다시 보낼 때는 같은 request_id를 쓴다(Ch15).
"""

import json
import os
import uuid
from pathlib import Path

from pydantic import ValidationError

from common import db
from common.a2a_io import COMPLETED, INPUT_REQUIRED, REJECTED, PartnerUnavailable, SkillNotOffered, call_skill
from common.contracts import (
    SHIPMENT_LABEL, SKILL_CANCEL_SHIPMENT, SKILL_CHECK_DELIVERY, SKILL_CREATE_SHIPMENT, SKILL_QUOTE,
    SKILL_SERVICE_AREA, validate_reply,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_orders(
  order_no TEXT PRIMARY KEY, item TEXT, qty INTEGER, weight_kg REAL, origin TEXT, destination TEXT,
  due_date TEXT, express INTEGER, status TEXT, shipment_id TEXT, note TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS quotes(
  quote_id TEXT PRIMARY KEY, order_no TEXT, carrier TEXT, price_krw INTEGER, eta_days INTEGER,
  valid_until TEXT, status TEXT, received_at TEXT);
CREATE TABLE IF NOT EXISTS partner_requests(
  request_id TEXT PRIMARY KEY, order_no TEXT, partner TEXT, skill TEXT, payload TEXT, task_id TEXT,
  context_id TEXT, state TEXT, attempts INTEGER, last_error TEXT, result TEXT, sent_at TEXT, done_at TEXT);
CREATE TABLE IF NOT EXISTS delivery_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT, status TEXT, eta TEXT, body TEXT, fetched_at TEXT);
"""

DEFAULT_DB = db.DATA_DIR / "kprecision.db"
SHIPPER = "K정밀웍스"

ORDER_LABEL = {
    "DRAFT": "작성", "QUOTED": "견적 수신", "APPROVED": "승인", "SHIPPING_REQUESTED": "출고 요청",
    "IN_DELIVERY": "배송 중", "DELIVERED": "배송 완료", "RECEIVED": "입고 완료", "CANCELLED": "취소",
}
TRACKED = ("SHIPPING_REQUESTED", "IN_DELIVERY", "DELIVERED")
OPEN_REQUEST = ("PENDING_RETRY", INPUT_REQUIRED)  # 같은 업무키로 이어 보낼 요청
EDITABLE = ("item", "qty", "weight_kg", "origin", "destination", "due_date", "express", "note")


class ProcurementError(ValueError):
    """업무 규칙상 지금 할 수 없는 조작."""


class ProcurementService:
    def __init__(self, path: str | Path | None = None, partner_url: str | None = None) -> None:
        self.path = Path(path or os.getenv("KP_DB") or DEFAULT_DB)
        self.partner_url = (partner_url or os.getenv("KL_AGENT_URL") or "http://127.0.0.1:9810").rstrip("/")
        self.timeout = float(os.getenv("KP_PARTNER_TIMEOUT", "15"))
        self._area: dict | None = None  # 물류사 운행 도시 — 마지막으로 받은 것
        db.init(self.path, SCHEMA)
        if os.getenv("KP_SEED", "1") != "0":
            self._seed()

    # --- 조회 -----------------------------------------------------------------
    def list_orders(self, status: str | None = None) -> list[dict]:
        with db.session(self.path) as conn:
            sql, args = "SELECT * FROM purchase_orders", ()
            if status:
                sql, args = sql + " WHERE status=?", (status,)
            return db.rows(conn.execute(sql + " ORDER BY updated_at DESC", args))

    def get_order(self, order_no: str) -> dict:
        with db.session(self.path) as conn:
            order = db.row(conn.execute("SELECT * FROM purchase_orders WHERE order_no=?", (order_no,)))
            if order is None:
                raise ProcurementError(f"없는 발주입니다: {order_no}")
            order["quotes"] = db.rows(conn.execute(
                "SELECT * FROM quotes WHERE order_no=? ORDER BY received_at DESC", (order_no,)))
            order["requests"] = [_decode(r) for r in db.rows(conn.execute(
                "SELECT * FROM partner_requests WHERE order_no=? ORDER BY sent_at DESC", (order_no,)))]
            snap = db.row(conn.execute(
                "SELECT * FROM delivery_snapshots WHERE order_no=? ORDER BY id DESC LIMIT 1", (order_no,)))
            order["delivery"] = {**json.loads(snap["body"]), "fetched_at": snap["fetched_at"]} if snap else None
            pending = next((r for r in order["requests"] if r["state"] == INPUT_REQUIRED), None)
            order["input_required"] = pending["result"] if pending else None
            return order

    def list_requests(self, limit: int = 100) -> list[dict]:
        with db.session(self.path) as conn:
            return [_decode(r) for r in db.rows(conn.execute(
                "SELECT * FROM partner_requests ORDER BY sent_at DESC LIMIT ?", (limit,)))]

    # --- 사내 업무 ------------------------------------------------------------
    def create_order(self, order_no: str, item: str = "", qty: int = 1, weight_kg: float = 0, origin: str = "",
                     destination: str = "", due_date: str = "", express: bool = False, note: str = "") -> dict:
        order_no = order_no.strip()
        if not order_no:
            raise ProcurementError("발주번호가 필요합니다")
        now = db.now()
        with db.session(self.path) as conn:
            if conn.execute("SELECT 1 FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone():
                raise ProcurementError(f"이미 있는 발주번호입니다: {order_no}")
            conn.execute("INSERT INTO purchase_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order_no, item, qty, weight_kg, origin, destination, due_date, int(express), "DRAFT",
                          None, note, now, now))
        return self.get_order(order_no)

    def update_order(self, order_no: str, **fields) -> dict:
        fields = {k: v for k, v in fields.items() if k in EDITABLE and v is not None}
        order = self.get_order(order_no)
        if order["status"] not in ("DRAFT", "QUOTED") and set(fields) - {"note"}:
            raise ProcurementError(f"{ORDER_LABEL[order['status']]} 상태에서는 발주 내용을 바꿀 수 없습니다")
        if fields:
            with db.session(self.path) as conn:
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(f"UPDATE purchase_orders SET {sets}, updated_at=? WHERE order_no=?",
                             (*fields.values(), db.now(), order_no))
        return self.get_order(order_no)

    def approve(self, order_no: str, quote_id: str | None = None) -> dict:
        order = self._require(order_no, "QUOTED")
        offered = [q for q in order["quotes"] if q["status"] == "OFFERED"]
        quote = next((q for q in offered if q["quote_id"] == quote_id), None) if quote_id else (offered or [None])[0]
        if quote is None:
            raise ProcurementError("승인할 견적이 없습니다")
        with db.session(self.path) as conn:
            conn.execute("UPDATE quotes SET status='CHOSEN' WHERE quote_id=?", (quote["quote_id"],))
            self._set_status(conn, order_no, "APPROVED")
        return self._done(order_no, "LOCAL", f"견적 {quote['quote_id']} ({quote['price_krw']:,}원) 승인")

    def confirm_receipt(self, order_no: str) -> dict:
        self._require(order_no, "DELIVERED")
        with db.session(self.path) as conn:
            self._set_status(conn, order_no, "RECEIVED")
        return self._done(order_no, "LOCAL", "입고 확인 완료")

    # --- 물류사와 주고받는 업무 ------------------------------------------------
    async def request_quote(self, order_no: str) -> dict:
        order = self._require(order_no, "DRAFT", "QUOTED")
        payload = {"order_no": order_no, "shipper": SHIPPER, "weight_kg": order["weight_kg"] or 0,
                   "origin": order["origin"] or "", "destination": order["destination"] or "",
                   "due_date": order["due_date"] or "", "express": bool(order["express"])}
        state, reply, msg = await self._dispatch(order_no, SKILL_QUOTE, payload)
        if state == COMPLETED:
            with db.session(self.path) as conn:
                conn.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?,?,?)",
                             (reply.quote_id, order_no, reply.carrier, reply.price_krw, reply.eta_days,
                              reply.valid_until, "OFFERED", db.now()))
                self._set_status(conn, order_no, "QUOTED")
        return self._done(order_no, state, msg)

    async def provide_input(self, order_no: str, **fields) -> dict:
        """물류사가 되물은 항목(INPUT_REQUIRED)을 채워 같은 Task로 이어 보낸다(E2)."""
        self.update_order(order_no, **fields)
        return await self.request_quote(order_no)

    async def request_shipment(self, order_no: str, contact: str = "") -> dict:
        order = self._require(order_no, "APPROVED")
        chosen = next((q for q in order["quotes"] if q["status"] == "CHOSEN"), None)
        if chosen is None:
            raise ProcurementError("승인된 견적이 없습니다")
        payload = {"quote_id": chosen["quote_id"], "order_no": order_no, "shipper": SHIPPER, "contact": contact}
        state, reply, msg = await self._dispatch(order_no, SKILL_CREATE_SHIPMENT, payload)
        with db.session(self.path) as conn:
            if state == COMPLETED:
                conn.execute("UPDATE purchase_orders SET shipment_id=? WHERE order_no=?", (reply.shipment_id, order_no))
                self._set_status(conn, order_no, "SHIPPING_REQUESTED")
            elif state == REJECTED and reply.get("reason") == "quote_expired":
                # 만료된 견적 — 견적을 버리고 작성 단계로 돌린다(E3)
                conn.execute("UPDATE quotes SET status='EXPIRED' WHERE quote_id=?", (chosen["quote_id"],))
                self._set_status(conn, order_no, "DRAFT")
                msg += " → 발주를 '작성'으로 돌렸습니다. 다시 견적을 요청하세요"
        return self._done(order_no, state, msg)

    async def service_area(self) -> dict:
        """물류사가 운행하는 도시 목록을 A2A로 받아 온다. 물류사가 꺼져 있으면 마지막으로 받은 목록을 쓴다.
        참고 정보 조회라 partner_requests에는 남기지 않는다."""
        try:
            reply = await call_skill(self.partner_url, SKILL_SERVICE_AREA, {}, timeout=self.timeout)
            if reply.state != COMPLETED:
                raise RuntimeError(f"{reply.state}: {reply.text}")
            area = validate_reply(SKILL_SERVICE_AREA, reply.data)
            self._area = {"carrier": area.carrier, "cities": area.cities, "fresh": True, "error": None}
        except Exception as e:
            if self._area is None:
                return {"carrier": None, "cities": [], "fresh": False, "error": str(e)}
            self._area = {**self._area, "fresh": False, "error": str(e)}
        return self._area

    async def track(self, order_no: str, quiet: bool = False) -> dict:
        """quiet=True(동기화)이면 결과가 직전 조회와 같을 때 요청 기록을 남기지 않는다."""
        last = None
        if quiet:
            with db.session(self.path) as conn:
                last = db.row(conn.execute(
                    "SELECT body FROM delivery_snapshots WHERE order_no=? ORDER BY id DESC LIMIT 1", (order_no,)))
        state, reply, msg = await self._dispatch(order_no, SKILL_CHECK_DELIVERY, {"order_no": order_no},
                                                 reuse=False, with_request_id=False)
        if quiet and state == COMPLETED and last and _same_delivery(last["body"], reply):
            with db.session(self.path) as conn:
                conn.execute("DELETE FROM partner_requests WHERE request_id=?", (self._last_request_id,))
            return self._done(order_no, state, msg + " (변경 없음)")
        if state == COMPLETED:
            with db.session(self.path) as conn:
                conn.execute("INSERT INTO delivery_snapshots(order_no,status,eta,body,fetched_at) VALUES(?,?,?,?,?)",
                             (order_no, reply.status, reply.eta, reply.model_dump_json(), db.now()))
                order = db.row(conn.execute("SELECT status FROM purchase_orders WHERE order_no=?", (order_no,)))
                new = {"PICKED_UP": "IN_DELIVERY", "IN_TRANSIT": "IN_DELIVERY", "DELIVERED": "DELIVERED",
                       "CANCELLED": "CANCELLED"}.get(reply.status or "")
                if order and order["status"] in TRACKED and new and new != order["status"]:
                    self._set_status(conn, order_no, new)
        exists = self._exists(order_no)
        return self._done(order_no, state, msg) if exists else {"ok": state == COMPLETED, "state": state,
                                                                  "message": msg, "order": None}

    async def cancel(self, order_no: str, reason: str = "") -> dict:
        order = self.get_order(order_no)
        if order["status"] in ("RECEIVED", "CANCELLED", "DELIVERED"):
            raise ProcurementError(f"{ORDER_LABEL[order['status']]} 상태에서는 취소할 수 없습니다")
        if not order["shipment_id"]:
            with db.session(self.path) as conn:
                self._set_status(conn, order_no, "CANCELLED")
            return self._done(order_no, "LOCAL", "발주 취소 (물류사에 보낸 출고 요청 없음)")
        payload = {"shipment_id": order["shipment_id"], "reason": reason}
        state, reply, msg = await self._dispatch(order_no, SKILL_CANCEL_SHIPMENT, payload)
        if state == COMPLETED:
            with db.session(self.path) as conn:
                self._set_status(conn, order_no, "CANCELLED")
        return self._done(order_no, state, msg)

    async def retry(self, request_id: str) -> dict:
        with db.session(self.path) as conn:
            req = db.row(conn.execute("SELECT * FROM partner_requests WHERE request_id=?", (request_id,)))
        if req is None:
            raise ProcurementError(f"없는 요청입니다: {request_id}")
        if req["state"] not in ("PENDING_RETRY", "FAILED", "INVALID_RESPONSE"):
            raise ProcurementError(f"{req['state']} 상태의 요청은 다시 보낼 수 없습니다")
        order_no = req["order_no"]
        if req["skill"] == SKILL_QUOTE:
            return await self.request_quote(order_no)
        if req["skill"] == SKILL_CREATE_SHIPMENT:
            return await self.request_shipment(order_no)
        if req["skill"] == SKILL_CANCEL_SHIPMENT:
            return await self.cancel(order_no, json.loads(req["payload"]).get("reason", ""))
        return await self.track(order_no)

    async def sync(self) -> list[dict]:
        """다시 보낼 요청을 보내고, 배송 중인 발주의 상태를 물류사에서 끌어온다(S3 1차 방식)."""
        results = []
        with db.session(self.path) as conn:
            pending = db.rows(conn.execute(
                "SELECT request_id FROM partner_requests WHERE state='PENDING_RETRY' ORDER BY sent_at"))
        for r in pending:
            try:
                results.append(await self.retry(r["request_id"]))
            except ProcurementError as e:
                results.append({"ok": False, "state": "SKIPPED", "message": str(e), "order": None})
        for order in self.list_orders():
            if order["status"] in ("SHIPPING_REQUESTED", "IN_DELIVERY"):
                results.append(await self.track(order["order_no"], quiet=True))
        return results

    async def route_inquiry(self, order_no: str) -> tuple[str, dict]:
        """구매 에이전트가 외부 문의(기존 call_router.py)를 받아 물류사에 위임할 때 쓴다."""
        reply = await call_skill(self.partner_url, SKILL_CHECK_DELIVERY, {"order_no": order_no}, timeout=self.timeout)
        if reply.state != COMPLETED:
            raise RuntimeError(f"물류사 배송 조회 {reply.state}: {reply.text}")
        info = validate_reply(SKILL_CHECK_DELIVERY, reply.data, order_no)
        return info.summary(), json.loads(info.model_dump_json())

    # --- 내부 -----------------------------------------------------------------
    async def _dispatch(self, order_no: str, skill: str, payload: dict, *, reuse: bool = True,
                        with_request_id: bool = True):
        """요청 한 건을 보내고 partner_requests에 결과를 남긴다. (상태, 검증된 응답 또는 dict, 안내문)"""
        open_req = None
        if reuse:
            with db.session(self.path) as conn:
                open_req = db.row(conn.execute(
                    f"SELECT * FROM partner_requests WHERE order_no=? AND skill=? AND state IN {OPEN_REQUEST}"
                    " ORDER BY sent_at DESC LIMIT 1", (order_no, skill)))
        request_id = open_req["request_id"] if open_req else f"KP-{uuid.uuid4().hex[:12]}"
        self._last_request_id = request_id
        resume = open_req and open_req["state"] == INPUT_REQUIRED
        task_id, context_id = (open_req["task_id"], open_req["context_id"]) if resume else ("", "")
        body = {"request_id": request_id, **payload} if with_request_id else dict(payload)
        now = db.now()
        with db.session(self.path) as conn:
            conn.execute(
                "INSERT INTO partner_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET"
                " payload=excluded.payload, state='SENDING', attempts=attempts+1, sent_at=excluded.sent_at",
                (request_id, order_no, self.partner_url, skill, json.dumps(body, ensure_ascii=False), task_id,
                 context_id, "SENDING", 1, None, None, now, None))

        error, reply = None, None
        try:
            try:
                reply = await call_skill(self.partner_url, skill, body, task_id=task_id or "",
                                         context_id=context_id or "", timeout=self.timeout)
            except PartnerUnavailable:
                raise
            except Exception:
                if not resume:
                    raise
                # 물류사가 재시작해 이어 보낼 Task가 없어졌다 — 같은 업무키로 새 Task를 연다.
                reply = await call_skill(self.partner_url, skill, body, timeout=self.timeout)
        except PartnerUnavailable as e:
            state, error = "PENDING_RETRY", str(e)
        except SkillNotOffered as e:
            state, error = "FAILED", str(e)
        except Exception as e:
            state, error = "FAILED", f"{type(e).__name__}: {e}"

        result, parsed = None, None
        if reply is not None:
            state, result = reply.state, reply.data
            if state == COMPLETED:
                try:
                    parsed = validate_reply(skill, reply.data, order_no)
                except (ValidationError, ValueError) as e:
                    state, error = "INVALID_RESPONSE", f"계약 위반 응답: {e}"
                    result = {"raw": reply.raw, "text": reply.text}
            elif state not in (INPUT_REQUIRED, REJECTED):
                error = reply.text or state

        final = state not in ("PENDING_RETRY", INPUT_REQUIRED)
        with db.session(self.path) as conn:
            conn.execute("UPDATE partner_requests SET task_id=?, context_id=?, state=?, last_error=?, result=?,"
                         " done_at=? WHERE request_id=?",
                         (reply.task_id if reply else task_id, reply.context_id if reply else context_id, state, error,
                          json.dumps(result, ensure_ascii=False) if result is not None else None,
                          db.now() if final else None, request_id))
        return state, parsed if parsed is not None else (result or {}), _message(skill, state, parsed, result, error)

    def _require(self, order_no: str, *statuses: str) -> dict:
        order = self.get_order(order_no)
        if order["status"] not in statuses:
            need = " 또는 ".join(ORDER_LABEL[s] for s in statuses)
            raise ProcurementError(f"{order_no}는 '{ORDER_LABEL[order['status']]}' 상태입니다 ('{need}' 상태여야 합니다)")
        return order

    def _exists(self, order_no: str) -> bool:
        with db.session(self.path) as conn:
            return conn.execute("SELECT 1 FROM purchase_orders WHERE order_no=?", (order_no,)).fetchone() is not None

    @staticmethod
    def _set_status(conn, order_no: str, status: str) -> None:
        conn.execute("UPDATE purchase_orders SET status=?, updated_at=? WHERE order_no=?", (status, db.now(), order_no))

    def _done(self, order_no: str, state: str, message: str) -> dict:
        return {"ok": state in (COMPLETED, "LOCAL"), "state": state, "message": message,
                "order": self.get_order(order_no)}

    def _seed(self) -> None:
        with db.session(self.path) as conn:
            if conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]:
                return
            at = "2026-09-01T09:00:00+09:00"
            for row in [
                ("ORDER-1001", "정밀 베어링 하우징", 40, 320, "창원", "평택", "2026-09-04", 0, "IN_DELIVERY", "SHP-0001"),
                ("ORDER-1002", "CNC 스핀들 부품", 12, 85, "부산", "인천", "2026-09-06", 0, "IN_DELIVERY", "SHP-0002"),
            ]:
                conn.execute("INSERT INTO purchase_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*row, "", at, at))


def _same_delivery(last_body: str, reply) -> bool:
    before = json.loads(last_body)
    return (before.get("status"), before.get("eta"), len(before.get("history", []))) == (
        reply.status, reply.eta, len(reply.history))


def _decode(r: dict) -> dict:
    for key in ("payload", "result"):
        if r.get(key):
            r[key] = json.loads(r[key])
    return r


def _message(skill: str, state: str, parsed, result, error) -> str:
    if state == COMPLETED and parsed is not None:
        return parsed.summary()
    if state == INPUT_REQUIRED:
        return f"물류사가 추가 정보를 요청했습니다: {', '.join((result or {}).get('missing', []))}"
    if state == REJECTED:
        return f"물류사가 거절했습니다: {(result or {}).get('detail') or (result or {}).get('reason')}"
    if state == "PENDING_RETRY":
        return f"물류사 응답 없음 — 다시 보내기 대기 ({error})"
    return f"{skill} {state}: {error}"


__all__ = ["ProcurementService", "ProcurementError", "ORDER_LABEL", "SHIPMENT_LABEL"]
