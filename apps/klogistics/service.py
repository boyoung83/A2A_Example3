# 파일명: service.py
"""K-Logistics 업무 로직. A2A·HTTP를 모른다 — 에이전트·운영 콘솔·CLI가 모두 이 클래스를 부른다.

회사 간 요청(견적·출고·취소)은 request_id(업무키)로 한 번만 처리한다. 같은 request_id가 다시 오면
처음 결과를 그대로 돌려준다(Ch15, E5).
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from common import db
from common.contracts import (
    SHIPMENT_FLOW, SHIPMENT_LABEL, CancelRequest, DeliveryEvent, DeliveryInfo, InputNeeded, Quote,
    QuoteRequest, Rejection, ServiceArea, ShipmentReceipt, ShipmentRequest,
)
from klogistics import pricing

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes(
  quote_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, order_no TEXT, shipper TEXT, weight_kg REAL,
  origin TEXT, destination TEXT, express INTEGER, price_krw INTEGER, eta_days INTEGER,
  valid_until TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS shipments(
  shipment_id TEXT PRIMARY KEY, request_id TEXT UNIQUE, quote_id TEXT UNIQUE, order_no TEXT, shipper TEXT,
  contact TEXT, origin TEXT, destination TEXT, status TEXT, eta TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS shipment_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id TEXT, status TEXT, memo TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS processed_requests(
  request_id TEXT PRIMARY KEY, skill TEXT, kind TEXT, body TEXT, at TEXT);
"""

DEFAULT_DB = db.DATA_DIR / "klogistics.db"
_KINDS = {m.__name__: m for m in (Quote, ShipmentReceipt, Rejection)}


class LogisticsError(ValueError):
    """운영자 조작(상태 진행 등)이 규칙에 맞지 않는다."""


class LogisticsService:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.getenv("KL_DB") or DEFAULT_DB)
        db.init(self.path, SCHEMA)
        # 실험용: 이 주문번호들은 내부 오류를 낸다(E8). 예) set KL_FAIL_ORDERS=ORDER-E8
        self.fail_orders = {o.strip() for o in os.getenv("KL_FAIL_ORDERS", "").split(",") if o.strip()}
        if os.getenv("KL_SEED", "1") != "0":
            self._seed()

    # --- 업무키·권한 ---------------------------------------------------------
    @staticmethod
    def _key(shipper: str | None, request_id: str) -> str:
        """업무키는 화주별로 따로 센다 — 다른 화주가 같은 request_id를 보내도 남의 결과를 받지 못한다."""
        return f"{shipper}|{request_id}" if shipper else request_id

    @staticmethod
    def _owned(row: dict | None, shipper: str | None) -> bool:
        """shipper가 None이면 내부 조작(운영 콘솔·CLI), 아니면 인증된 화주 — 자기 건만 본다."""
        return row is not None and (shipper is None or row["shipper"] == shipper)

    def _recall(self, conn, request_id: str):
        hit = db.row(conn.execute("SELECT kind, body FROM processed_requests WHERE request_id=?", (request_id,)))
        return _KINDS[hit["kind"]].model_validate_json(hit["body"]) if hit else None

    def _remember(self, conn, request_id: str, skill: str, result) -> None:
        conn.execute("INSERT INTO processed_requests VALUES(?,?,?,?,?)",
                     (request_id, skill, type(result).__name__, result.model_dump_json(), db.now()))

    def _check_fault(self, order_no: str) -> None:
        if order_no in self.fail_orders:
            raise RuntimeError(f"운임 시스템 오류(실험): {order_no}")

    @staticmethod
    def _next_id(conn, table: str, column: str, prefix: str) -> str:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
        while conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (f"{prefix}-{n:04d}",)).fetchone():
            n += 1
        return f"{prefix}-{n:04d}"

    # --- 회사 간 스킬 ---------------------------------------------------------
    def service_area(self) -> ServiceArea:
        return ServiceArea(carrier=pricing.CARRIER, cities=list(pricing.CITY_XY_KM))

    def check_delivery(self, order_no: str, shipper: str | None = None) -> DeliveryInfo:
        self._check_fault(order_no)
        with db.session(self.path) as conn:
            ship = db.row(conn.execute(
                "SELECT * FROM shipments WHERE order_no=? AND (? IS NULL OR shipper=?)"
                " ORDER BY (status='CANCELLED'), created_at DESC LIMIT 1", (order_no, shipper, shipper)))
            if ship is None:  # 남의 화물이면 '없음'과 똑같이 답한다(존재 여부도 알려 주지 않는다)
                return DeliveryInfo(found=False, order_no=order_no)
            events = db.rows(conn.execute(
                "SELECT status, memo, at FROM shipment_events WHERE shipment_id=? ORDER BY id", (ship["shipment_id"],)))
        return DeliveryInfo(found=True, order_no=order_no, shipment_id=ship["shipment_id"], status=ship["status"],
                            eta=ship["eta"], history=[DeliveryEvent(**e) for e in events])

    def quote(self, req: QuoteRequest, shipper: str | None = None) -> Quote | Rejection | InputNeeded:
        self._check_fault(req.order_no)
        if shipper:  # 화주는 요청 본문이 아니라 인증된 키로 정한다
            req = req.model_copy(update={"shipper": shipper})
        rid = self._key(shipper, req.request_id)
        with db.session(self.path) as conn:
            if (done := self._recall(conn, rid)) is not None:
                return done
            if missing := req.missing_fields():
                return InputNeeded(missing=missing, detail="견적에 필요한 정보가 비어 있습니다: " + ", ".join(missing))
            km = pricing.distance_km(req.origin, req.destination)
            if km is None:
                result = Rejection(reason="unsupported_route",
                                   detail=f"운행하지 않는 구간입니다: {req.origin} → {req.destination}")
            else:
                krw, days = pricing.price(km, req.weight_kg, req.express)
                valid_until = (datetime.now().astimezone() + timedelta(hours=pricing.QUOTE_TTL_HOURS)).isoformat(
                    timespec="seconds")
                result = Quote(quote_id=self._next_id(conn, "quotes", "quote_id", "Q"), carrier=pricing.CARRIER,
                               order_no=req.order_no, price_krw=krw, eta_days=days, valid_until=valid_until)
                conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (result.quote_id, rid, req.order_no, req.shipper, req.weight_kg, req.origin,
                              req.destination, int(req.express), krw, days, valid_until, "OPEN", db.now()))
            self._remember(conn, rid, "quote", result)
            return result

    def create_shipment(self, req: ShipmentRequest, shipper: str | None = None) -> ShipmentReceipt | Rejection:
        self._check_fault(req.order_no)
        if shipper:
            req = req.model_copy(update={"shipper": shipper})
        rid = self._key(shipper, req.request_id)
        with db.session(self.path) as conn:
            if (done := self._recall(conn, rid)) is not None:
                return done
            q = db.row(conn.execute("SELECT * FROM quotes WHERE quote_id=?", (req.quote_id,)))
            q = q if self._owned(q, shipper) else None  # 남의 견적은 없는 견적으로
            existing = db.row(conn.execute("SELECT * FROM shipments WHERE quote_id=?", (req.quote_id,)))
            if q is None:
                result = Rejection(reason="unknown_quote", detail=f"없는 견적입니다: {req.quote_id}")
            elif q["order_no"] != req.order_no:
                result = Rejection(reason="order_mismatch", detail=f"견적 {req.quote_id}는 {q['order_no']}의 견적입니다")
            elif existing is not None:
                # 다른 request_id라도 같은 견적으로 이미 접수됐으면 같은 출고 건을 돌려준다(업무키 = quote_id).
                result = ShipmentReceipt(shipment_id=existing["shipment_id"], order_no=req.order_no,
                                         status=existing["status"])
            elif q["status"] == "EXPIRED" or datetime.fromisoformat(q["valid_until"]) < datetime.now().astimezone():
                conn.execute("UPDATE quotes SET status='EXPIRED' WHERE quote_id=?", (req.quote_id,))
                result = Rejection(reason="quote_expired", detail=f"견적 {req.quote_id}의 유효기간이 지났습니다. 다시 견적을 요청하세요")
            else:
                sid = self._next_id(conn, "shipments", "shipment_id", "SHP")
                eta = (date.today() + timedelta(days=q["eta_days"])).isoformat()
                now = db.now()
                conn.execute("INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (sid, rid, req.quote_id, req.order_no, req.shipper or q["shipper"],
                              req.contact, q["origin"], q["destination"], "ACCEPTED", eta, now, now))
                conn.execute("INSERT INTO shipment_events(shipment_id,status,memo,at) VALUES(?,?,?,?)",
                             (sid, "ACCEPTED", f"{q['origin']} → {q['destination']} 출고 접수", now))
                conn.execute("UPDATE quotes SET status='USED' WHERE quote_id=?", (req.quote_id,))
                result = ShipmentReceipt(shipment_id=sid, order_no=req.order_no, status="ACCEPTED")
            self._remember(conn, rid, "create-shipment", result)
            return result

    def cancel_shipment(self, req: CancelRequest, shipper: str | None = None) -> ShipmentReceipt | Rejection:
        rid = self._key(shipper, req.request_id)
        with db.session(self.path) as conn:
            if (done := self._recall(conn, rid)) is not None:
                return done
            ship = db.row(conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (req.shipment_id,)))
            ship = ship if self._owned(ship, shipper) else None  # 남의 출고 건은 없는 건으로
            if ship is None:
                result = Rejection(reason="unknown_shipment", detail=f"없는 출고 건입니다: {req.shipment_id}")
            elif ship["status"] == "CANCELLED":
                result = ShipmentReceipt(shipment_id=ship["shipment_id"], order_no=ship["order_no"], status="CANCELLED")
            elif ship["status"] != "ACCEPTED":
                label = SHIPMENT_LABEL[ship["status"]]
                result = Rejection(reason="already_picked_up", detail=f"이미 {label} 상태라 취소할 수 없습니다")
            else:
                self._set_status(conn, ship["shipment_id"], "CANCELLED", f"화주 취소: {req.reason}".strip())
                result = ShipmentReceipt(shipment_id=ship["shipment_id"], order_no=ship["order_no"], status="CANCELLED")
            self._remember(conn, rid, "cancel-shipment", result)
            return result

    # --- 운영자 조작 ---------------------------------------------------------
    @staticmethod
    def _set_status(conn, shipment_id: str, status: str, memo: str) -> None:
        now = db.now()
        conn.execute("UPDATE shipments SET status=?, updated_at=? WHERE shipment_id=?", (status, now, shipment_id))
        conn.execute("INSERT INTO shipment_events(shipment_id,status,memo,at) VALUES(?,?,?,?)",
                     (shipment_id, status, memo, now))

    def advance(self, shipment_id: str, status: str | None = None, memo: str = "") -> dict:
        """출고 건을 다음 단계(또는 지정한 뒤 단계)로 진행한다."""
        with db.session(self.path) as conn:
            ship = db.row(conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)))
            if ship is None:
                raise LogisticsError(f"없는 출고 건입니다: {shipment_id}")
            if ship["status"] not in SHIPMENT_FLOW or ship["status"] == SHIPMENT_FLOW[-1]:
                raise LogisticsError(f"{SHIPMENT_LABEL[ship['status']]} 상태에서는 진행할 수 없습니다")
            cur = SHIPMENT_FLOW.index(ship["status"])
            target = status or SHIPMENT_FLOW[cur + 1]
            if target not in SHIPMENT_FLOW or SHIPMENT_FLOW.index(target) <= cur:
                raise LogisticsError(f"{ship['status']} → {target} 로는 진행할 수 없습니다")
            self._set_status(conn, shipment_id, target, memo)
        return self.get_shipment(shipment_id)

    def expire_quote(self, quote_id: str) -> None:
        with db.session(self.path) as conn:
            if conn.execute("UPDATE quotes SET status='EXPIRED', valid_until=? WHERE quote_id=? AND status='OPEN'",
                            (db.now(), quote_id)).rowcount == 0:
                raise LogisticsError(f"만료시킬 수 있는 견적이 아닙니다: {quote_id}")

    def list_shipments(self, status: str | None = None) -> list[dict]:
        with db.session(self.path) as conn:
            sql, args = "SELECT * FROM shipments", ()
            if status:
                sql, args = sql + " WHERE status=?", (status,)
            return db.rows(conn.execute(sql + " ORDER BY created_at DESC, shipment_id DESC", args))

    def get_shipment(self, shipment_id: str) -> dict:
        with db.session(self.path) as conn:
            ship = db.row(conn.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)))
            if ship is None:
                raise LogisticsError(f"없는 출고 건입니다: {shipment_id}")
            ship["events"] = db.rows(conn.execute(
                "SELECT status, memo, at FROM shipment_events WHERE shipment_id=? ORDER BY id", (shipment_id,)))
            return ship

    def list_quotes(self) -> list[dict]:
        with db.session(self.path) as conn:
            return db.rows(conn.execute("SELECT * FROM quotes ORDER BY created_at DESC, quote_id DESC"))

    # --- 시드 -----------------------------------------------------------------
    def _seed(self) -> None:
        """기존 실습과 같은 주문(ORDER-1001·1002)을 출고 건으로 넣어 둔다. 비어 있을 때만."""
        with db.session(self.path) as conn:
            if conn.execute("SELECT COUNT(*) FROM shipments").fetchone()[0]:
                return
            seed = [
                ("SHP-0001", "ORDER-1001", "창원", "평택", "IN_TRANSIT", "2026-09-03",
                 [("ACCEPTED", "창원 → 평택 출고 접수"), ("PICKED_UP", "출고 완료"), ("IN_TRANSIT", "간선 운송 중")]),
                ("SHP-0002", "ORDER-1002", "부산", "인천", "PICKED_UP", "2026-09-05",
                 [("ACCEPTED", "부산 → 인천 출고 접수"), ("PICKED_UP", "통관 대기 · 영업일 기준 2일 추가 소요")]),
            ]
            for sid, order_no, o, d, status, eta, events in seed:
                at = "2026-09-01T09:00:00+09:00"
                conn.execute("INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (sid, None, None, order_no, "K정밀웍스", "", o, d, status, eta, at, at))  # 견적 없이 등록된 기존 건
                for st, memo in events:
                    conn.execute("INSERT INTO shipment_events(shipment_id,status,memo,at) VALUES(?,?,?,?)",
                                 (sid, st, memo, at))


def dump(model) -> dict:
    return json.loads(model.model_dump_json())
