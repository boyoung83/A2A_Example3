# 파일명: keys.py
"""파트너 API Key 발급·교체·폐기 도구. 키를 발급한 회사가 자기 장부(DB)를 관리한다.

    kl = K-Logistics가 화주(K정밀웍스 등)에게 발급한 키   (물류 에이전트 9810이 확인)
    kp = K정밀웍스가 사내 시스템에 발급한 키              (구매 에이전트 9820이 확인)

    uv run apps/keys.py kl list
    uv run apps/keys.py kl issue K전자 --expires-hours 720
    uv run apps/keys.py kl rotate K정밀웍스 --grace-hours 24 --deliver
    uv run apps/keys.py kl revoke KLK-002
    uv run apps/keys.py kl revoke --partner K전자 --reason "계약 종료"

에이전트는 요청마다 장부를 보므로 다시 띄울 필요가 없다. 키 원문은 발급할 때 한 번만 출력된다.
--deliver: (로컬 개발용) 새 키를 받는 쪽 키 파일 apps/data/secrets.json 에 넣어 '전달'한다.
           K정밀웍스 쪽은 요청마다 키를 다시 읽으므로 바로 새 키를 쓴다(KL_API_KEY 환경변수가 있으면 그쪽이 우선).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import db  # noqa: E402
from common.auth import KL_PARTNERS, KP_CLIENTS, deliver  # noqa: E402
from common.keystore import KeyError_, KeyRegistry  # noqa: E402

ISSUERS = {
    "kl": ("K-Logistics → 화주", "KL_DB", "klogistics.db", "KL_PARTNER_KEYS", KL_PARTNERS),
    "kp": ("K정밀웍스 → 사내 시스템", "KP_DB", "kprecision.db", "KP_CLIENT_KEYS", KP_CLIENTS),
}
STATE_LABEL = {"ACTIVE": "유효", "EXPIRING": "만료 예정", "EXPIRED": "만료", "REVOKED": "폐기"}


def registry(issuer: str) -> KeyRegistry:
    _, db_env, db_file, key_env, section = ISSUERS[issuer]
    return KeyRegistry(os.getenv(db_env) or db.DATA_DIR / db_file, issuer, key_env, section)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="keys", description="파트너 API Key 발급·교체·폐기")
    ap.add_argument("issuer", choices=ISSUERS, help="kl: K-Logistics가 발급 / kp: K정밀웍스가 발급")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="키 목록"); p.add_argument("--partner")
    p = sub.add_parser("issue", help="새 키 발급"); p.add_argument("partner")
    p.add_argument("--expires-hours", type=float, help="유효시간(생략하면 무기한)")
    p.add_argument("--note", default=""); p.add_argument("--deliver", action="store_true")
    p = sub.add_parser("rotate", help="교체: 새 키 발급 + 기존 키는 유예기간 뒤 만료"); p.add_argument("partner")
    p.add_argument("--grace-hours", type=float, default=24.0, help="기존 키 유예기간(0이면 즉시 폐기, 기본 24)")
    p.add_argument("--deliver", action="store_true")
    p = sub.add_parser("revoke", help="즉시 폐기"); p.add_argument("key_id", nargs="?")
    p.add_argument("--partner"); p.add_argument("--reason", default="")
    return ap


def show_new(key_id: str, partner: str, key: str, delivered: Path | None) -> None:
    print(f"새 키 {key_id} ({partner})")
    print(f"  {key}")
    print("  ※ 키 원문은 지금 한 번만 보입니다. 장부에는 해시만 남습니다.")
    if delivered:
        print(f"  → 받는 쪽 키 파일에 전달했습니다: {delivered}")
    else:
        print("  → 상대에게 안전한 경로로 전달하세요 (로컬 개발이면 --deliver)")


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    reg = registry(args.issuer)
    section = ISSUERS[args.issuer][4]
    try:
        if args.cmd == "list":
            rows = reg.list(args.partner)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            print(f"[{ISSUERS[args.issuer][0]}] 장부: {reg.path}")
            for r in rows:
                until = f"  만료 {r['expires_at'][:16].replace('T', ' ')}" if r["expires_at"] else ""
                used = (r["last_used_at"] or "-")[:19].replace("T", " ")
                print(f"  {r['key_id']}  {r['partner']:<8} {STATE_LABEL[r['state']]:<6} {r['hint']:<13}"
                      f" 발급 {r['created_at'][:16].replace('T', ' ')}  최근 사용 {used}{until}  {r['note'] or ''}")
            if not rows:
                print("  (없음)")
        elif args.cmd == "issue":
            key_id, key = reg.issue(args.partner, args.expires_hours, args.note)
            show_new(key_id, args.partner, key, deliver(section, args.partner, key) if args.deliver else None)
        elif args.cmd == "rotate":
            key_id, key, old = reg.rotate(args.partner, args.grace_hours)
            show_new(key_id, args.partner, key, deliver(section, args.partner, key) if args.deliver else None)
            how = "즉시 폐기" if args.grace_hours <= 0 else f"{args.grace_hours:g}시간 뒤 만료(그때까지는 함께 유효)"
            print(f"  기존 키 {', '.join(old)}: {how}")
        else:
            done = reg.revoke(args.key_id, args.partner, args.reason)
            print(f"폐기: {', '.join(done)} — 다음 요청부터 거절됩니다")
    except KeyError_ as e:
        print(f"[거부] {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
