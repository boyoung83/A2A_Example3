# 파일명: cli.py
"""K-Logistics 운영 CLI — 운영 콘솔 없이 출고 건 상태를 진행한다(콘솔과 같은 service 함수를 부른다).

    uv run apps/klogistics/cli.py shipment list
    uv run apps/klogistics/cli.py shipment advance SHP-0003            # 다음 단계로
    uv run apps/klogistics/cli.py shipment advance SHP-0003 DELIVERED --memo "평택 공장 인수"
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.contracts import SHIPMENT_FLOW, SHIPMENT_LABEL  # noqa: E402
from klogistics import pricing  # noqa: E402
from klogistics.service import LogisticsError, LogisticsService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="klogistics", description="K-Logistics 물류 운영")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ship = sub.add_parser("shipment", help="출고 건").add_subparsers(dest="act", required=True)
    p = ship.add_parser("list"); p.add_argument("--status", choices=[*SHIPMENT_FLOW, "CANCELLED"])
    p = ship.add_parser("show"); p.add_argument("shipment_id")
    p = ship.add_parser("advance"); p.add_argument("shipment_id")
    p.add_argument("status", nargs="?", choices=SHIPMENT_FLOW[1:]); p.add_argument("--memo", default="")
    quote = sub.add_parser("quote", help="견적").add_subparsers(dest="act", required=True)
    quote.add_parser("list")
    p = quote.add_parser("expire", help="견적을 즉시 만료(실험·운영)"); p.add_argument("quote_id")
    sub.add_parser("pricing", help="요금 규칙")
    return ap


def show_shipment(s: dict) -> None:
    print(f"{s['shipment_id']}  {s['order_no']:<12} {s['shipper']:<8} {s['origin']} → {s['destination']}"
          f"  [{SHIPMENT_LABEL.get(s['status'], s['status'])}]  도착 예정 {s['eta']}")
    for e in s.get("events", []):
        print(f"   {e['at'][:16]}  {SHIPMENT_LABEL.get(e['status'], e['status'])}  {e['memo']}")


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    svc = LogisticsService()
    try:
        match (args.cmd, getattr(args, "act", None)):
            case ("shipment", "list"): result = svc.list_shipments(args.status)
            case ("shipment", "show"): result = svc.get_shipment(args.shipment_id)
            case ("shipment", "advance"): result = svc.advance(args.shipment_id, args.status, args.memo)
            case ("quote", "list"): result = svc.list_quotes()
            case ("quote", "expire"):
                svc.expire_quote(args.quote_id)
                result = {"quote_id": args.quote_id, "status": "EXPIRED"}
            case _: result = pricing.table()
    except LogisticsError as e:
        print(f"[거부] {e}")
        return 2
    if args.json or args.cmd == "pricing" or args.act == "expire":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "shipment":
        for s in result if isinstance(result, list) else [result]:
            show_shipment(s)
    else:
        for q in result:
            print(f"{q['quote_id']}  {q['order_no']:<12} {q['origin']} → {q['destination']}  {q['weight_kg']}kg"
                  f"  {q['price_krw']:,}원 · {q['eta_days']}일  유효 {q['valid_until'][:16]}  {q['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
