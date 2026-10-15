# 파일명: cli.py
"""K정밀웍스 구매 업무 CLI — 웹 포털 없이 같은 업무를 한다(포털과 같은 service 함수를 부른다).

    uv run apps/kprecision/cli.py order create ORDER-2001 --item 기어박스 --weight 120 --from 창원 --to 평택
    uv run apps/kprecision/cli.py quote request ORDER-2001
    uv run apps/kprecision/cli.py order approve ORDER-2001
    uv run apps/kprecision/cli.py shipment request ORDER-2001
    uv run apps/kprecision/cli.py delivery check ORDER-2001
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kprecision.service import ORDER_LABEL, SHIPMENT_LABEL, ProcurementError, ProcurementService  # noqa: E402


def order_fields(p: argparse.ArgumentParser) -> None:
    p.add_argument("--item")
    p.add_argument("--qty", type=int)
    p.add_argument("--weight", dest="weight_kg", type=float)
    p.add_argument("--from", dest="origin")
    p.add_argument("--to", dest="destination")
    p.add_argument("--due", dest="due_date")
    p.add_argument("--express", action="store_true", default=None)
    p.add_argument("--note")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kprecision", description="K정밀웍스 구매 업무")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    sub = ap.add_subparsers(dest="cmd", required=True)

    order = sub.add_parser("order", help="발주").add_subparsers(dest="act", required=True)
    p = order.add_parser("list"); p.add_argument("--status")
    p = order.add_parser("show"); p.add_argument("order_no")
    p = order.add_parser("create"); p.add_argument("order_no", nargs="?", help="생략하면 자동 채번"); order_fields(p)
    order.add_parser("next-no", help="다음 발주번호 제안")
    p = order.add_parser("update"); p.add_argument("order_no"); order_fields(p)
    p = order.add_parser("approve"); p.add_argument("order_no"); p.add_argument("--quote", dest="quote_id")
    p = order.add_parser("receive", help="입고 확인"); p.add_argument("order_no")
    p = order.add_parser("cancel"); p.add_argument("order_no"); p.add_argument("--reason", default="")

    quote = sub.add_parser("quote", help="운임 견적").add_subparsers(dest="act", required=True)
    p = quote.add_parser("request"); p.add_argument("order_no")
    p = quote.add_parser("input", help="물류사가 되물은 항목을 채워 이어 보내기"); p.add_argument("order_no")
    order_fields(p)

    ship = sub.add_parser("shipment", help="출고 요청").add_subparsers(dest="act", required=True)
    p = ship.add_parser("request"); p.add_argument("order_no"); p.add_argument("--contact", default="")

    dl = sub.add_parser("delivery", help="배송 조회").add_subparsers(dest="act", required=True)
    p = dl.add_parser("check"); p.add_argument("order_no")

    rq = sub.add_parser("request", help="물류사에 보낸 요청").add_subparsers(dest="act", required=True)
    p = rq.add_parser("list"); p.add_argument("--limit", type=int, default=20)
    p = rq.add_parser("retry"); p.add_argument("request_id")

    sub.add_parser("sync", help="대기 요청 재전송 + 배송 중 발주 상태 동기화")
    sub.add_parser("cities", help="물류사 운행 도시 목록 (A2A service-area)")
    pk = sub.add_parser("partner-key", help="K-Logistics가 발급한 키").add_subparsers(dest="act", required=True)
    pk.add_parser("check", help="지금 키가 물류사에서 통하는지 확인")
    p = pk.add_parser("set", help="받은 새 키 등록(물류사에 확인 후 저장)"); p.add_argument("key")
    return ap


def fields_of(args) -> dict:
    keys = ("item", "qty", "weight_kg", "origin", "destination", "due_date", "express", "note")
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


async def run(args, svc: ProcurementService):
    match (args.cmd, getattr(args, "act", None)):
        case ("order", "list"): return svc.list_orders(args.status)
        case ("order", "show"): return svc.get_order(args.order_no)
        case ("order", "create"): return svc.create_order(args.order_no or svc.next_order_no(), **fields_of(args))
        case ("order", "next-no"): return {"order_no": svc.next_order_no()}
        case ("order", "update"): return svc.update_order(args.order_no, **fields_of(args))
        case ("order", "approve"): return svc.approve(args.order_no, args.quote_id)
        case ("order", "receive"): return svc.confirm_receipt(args.order_no)
        case ("order", "cancel"): return await svc.cancel(args.order_no, args.reason)
        case ("quote", "request"): return await svc.request_quote(args.order_no)
        case ("quote", "input"): return await svc.provide_input(args.order_no, **fields_of(args))
        case ("shipment", "request"): return await svc.request_shipment(args.order_no, args.contact)
        case ("delivery", "check"): return await svc.track(args.order_no)
        case ("request", "list"): return svc.list_requests(args.limit)
        case ("request", "retry"): return await svc.retry(args.request_id)
        case ("sync", _): return await svc.sync()
        case ("cities", _): return await svc.service_area()
        case ("partner-key", "check"): return await svc.partner_key_status()
        case ("partner-key", "set"): return await svc.set_partner_key(args.key)


def show_order(o: dict) -> None:
    print(f"{o['order_no']}  [{ORDER_LABEL.get(o['status'], o['status'])}]  {o['item'] or '-'} x{o['qty']}"
          f"  {o['weight_kg']}kg  {o['origin'] or '?'} → {o['destination'] or '?'}  납기 {o['due_date'] or '-'}"
          + (f"  출고 {o['shipment_id']}" if o.get("shipment_id") else ""))
    for q in o.get("quotes", []):
        print(f"   견적 {q['quote_id']}  {q['price_krw']:,}원 · {q['eta_days']}일  유효 {q['valid_until'][:16]}  {q['status']}")
    if o.get("input_required"):
        print(f"   ! 물류사 추가 정보 요청: {', '.join(o['input_required'].get('missing', []))}")
    if d := o.get("delivery"):
        if d.get("found"):
            print(f"   배송 {SHIPMENT_LABEL.get(d['status'], d['status'])}  도착 예정 {d.get('eta') or '-'}"
                  f"  (조회 {d['fetched_at'][11:19]})")
            for e in d.get("history", []):
                print(f"     {e['at'][:16]}  {SHIPMENT_LABEL.get(e['status'], e['status'])}  {e.get('memo', '')}")
    for r in o.get("requests", [])[:5]:
        print(f"   요청 {r['request_id']}  {r['skill']:<16} {r['state']:<15} 시도 {r['attempts']}"
              f"  task {(r['task_id'] or '-')[:8]}" + (f"  {r['last_error']}" if r['last_error'] else ""))


def show(result) -> None:
    if isinstance(result, dict) and "hint" in result and "source" in result:
        mark = {True: "인증됨", False: "거절됨", None: "확인 불가"}[result["ok"]]
        print(f"[{mark}] 물류사 키 {result['hint'] or '(없음)'} — {result['detail']}")
    elif isinstance(result, dict) and "cities" in result:
        note = "" if result["fresh"] else f"  (물류사 응답 없음 — 마지막 목록. {result['error']})"
        print(f"{result['carrier'] or '물류사'} 운행 도시: {', '.join(result['cities']) or '(없음)'}{note}")
    elif isinstance(result, dict) and "state" in result and "message" in result:
        mark = "OK" if result["ok"] else "!!"
        print(f"[{mark} {result['state']}] {result['message']}")
        if result.get("order"):
            show_order(result["order"])
    elif isinstance(result, dict) and set(result) == {"order_no"}:
        print(f"다음 발주번호: {result['order_no']}")
    elif isinstance(result, dict) and "order_no" in result:
        show_order(result)
    elif isinstance(result, list):
        if not result:
            print("(없음)")
        for item in result:
            if "request_id" in item and "skill" in item:
                print(f"{item['sent_at'][:19]}  {item['request_id']}  {item['order_no']:<12} {item['skill']:<16}"
                      f" {item['state']:<15} 시도 {item['attempts']}" + (f"  {item['last_error']}" if item['last_error'] else ""))
            else:
                show(item)


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run(args, ProcurementService()))
    except ProcurementError as e:
        print(f"[거부] {e}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        show(result)
    if isinstance(result, dict) and result.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
