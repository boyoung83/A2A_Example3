# 파일명: keystore.py
"""발급한 API Key 장부. 키를 발급한 회사가 자기 DB에 둔다(K-Logistics → 화주 키, K정밀웍스 → 사내 시스템 키).

- 키 원문은 저장하지 않는다. SHA-256 해시와 앞뒤 몇 글자(hint)만 둔다. 원문은 발급할 때 한 번만 보여 준다.
- 에이전트는 요청마다 verify()로 장부를 확인한다 → 교체·폐기가 재시작 없이 바로 반영된다.
- 상태: ACTIVE(유효) · EXPIRING(교체 후 유예기간) · EXPIRED(유예기간 끝) · REVOKED(폐기)
- 처음 실행할 때 장부가 비어 있으면 환경변수(KL_PARTNER_KEYS 등) 또는 개발용 secrets.json의 키를 들여온다.
  한 번 들여온 키는 폐기해도 다시 들여오지 않는다(같은 해시는 건너뛴다).
"""

from __future__ import annotations  # 클래스 안의 list() 메서드가 타입 표기 list[...]를 가리지 않게

import hashlib
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from common import db
from common.auth import _parse_env, load_secrets

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys(
  key_id TEXT PRIMARY KEY, partner TEXT, key_hash TEXT UNIQUE, hint TEXT, status TEXT,
  created_at TEXT, expires_at TEXT, revoked_at TEXT, last_used_at TEXT, note TEXT);
"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _hint(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}"


def _now() -> datetime:
    return datetime.now().astimezone()


class KeyError_(ValueError):
    """장부 조작이 규칙에 맞지 않는다."""


class KeyRegistry:
    def __init__(self, path: str | Path, prefix: str, env_var: str, section: str) -> None:
        """prefix: 키와 key_id 머리글(kl/kp), env_var·section: 처음 들여올 키의 출처."""
        self.path, self.prefix, self.env_var, self.section = Path(path), prefix, env_var, section
        db.init(self.path, SCHEMA)
        self._import_initial()

    # --- 확인 ------------------------------------------------------------------
    def verify(self, presented: str) -> str | None:
        """유효한 키면 파트너 이름, 아니면 None. 마지막 사용 시각을 남긴다."""
        if not presented:
            return None
        with db.session(self.path) as conn:
            row = db.row(conn.execute("SELECT * FROM api_keys WHERE key_hash=?", (_hash(presented),)))
            if row is None or self._state(row) not in ("ACTIVE", "EXPIRING"):
                return None
            conn.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (db.now(), row["key_id"]))
            return row["partner"]

    @staticmethod
    def _state(row: dict) -> str:
        if row["status"] == "REVOKED":
            return "REVOKED"
        if row["expires_at"]:
            return "EXPIRING" if datetime.fromisoformat(row["expires_at"]) > _now() else "EXPIRED"
        return "ACTIVE"

    def list(self, partner: str | None = None) -> list[dict]:
        with db.session(self.path) as conn:
            sql, args = "SELECT * FROM api_keys", ()
            if partner:
                sql, args = sql + " WHERE partner=?", (partner,)
            rows = db.rows(conn.execute(sql + " ORDER BY partner, created_at", args))
        for r in rows:
            r["state"] = self._state(r)
            r.pop("key_hash")
        return rows

    # --- 발급·교체·폐기 --------------------------------------------------------
    def issue(self, partner: str, expires_hours: float | None = None, note: str = "") -> tuple[str, str]:
        """새 키를 발급한다. (key_id, 키 원문) — 원문은 이때 한 번만 알 수 있다."""
        partner = partner.strip()
        if not partner:
            raise KeyError_("파트너 이름이 필요합니다")
        key = f"{self.prefix}-{secrets.token_urlsafe(24)}"
        expires = (_now() + timedelta(hours=expires_hours)).isoformat(timespec="seconds") if expires_hours else None
        with db.session(self.path) as conn:
            key_id = self._insert(conn, partner, key, expires, note or "발급")
        return key_id, key

    def rotate(self, partner: str, grace_hours: float = 24.0) -> tuple[str, str, list[str]]:
        """새 키를 발급하고, 지금 유효한 키들은 유예기간 뒤 만료되게 한다(0이면 즉시 폐기).
        (새 key_id, 새 키 원문, 영향받은 기존 key_id 목록)"""
        current = [r for r in self.list(partner) if r["state"] in ("ACTIVE", "EXPIRING")]
        if not current:
            raise KeyError_(f"{partner}에게 유효한 키가 없습니다. 교체 대신 발급(issue)을 쓰세요")
        new_id, new_key = self.issue(partner, note="교체 발급")
        until = (_now() + timedelta(hours=grace_hours)).isoformat(timespec="seconds")
        with db.session(self.path) as conn:
            for r in current:
                if grace_hours <= 0:
                    conn.execute("UPDATE api_keys SET status='REVOKED', revoked_at=?, note=? WHERE key_id=?",
                                 (db.now(), f"교체로 폐기 → {new_id}", r["key_id"]))
                elif not r["expires_at"] or r["expires_at"] > until:
                    conn.execute("UPDATE api_keys SET expires_at=?, note=? WHERE key_id=?",
                                 (until, f"교체 유예 → {new_id}", r["key_id"]))
        return new_id, new_key, [r["key_id"] for r in current]

    def revoke(self, key_id: str | None = None, partner: str | None = None, reason: str = "") -> list[str]:
        """키 하나(key_id) 또는 파트너의 키 전부를 즉시 폐기한다."""
        if not key_id and not partner:
            raise KeyError_("key_id 또는 파트너 이름이 필요합니다")
        with db.session(self.path) as conn:
            if key_id:
                targets = db.rows(conn.execute("SELECT key_id FROM api_keys WHERE key_id=? AND status!='REVOKED'",
                                               (key_id,)))
            else:
                targets = db.rows(conn.execute("SELECT key_id FROM api_keys WHERE partner=? AND status!='REVOKED'",
                                               (partner,)))
            if not targets:
                raise KeyError_(f"폐기할 키가 없습니다: {key_id or partner}")
            for t in targets:
                conn.execute("UPDATE api_keys SET status='REVOKED', revoked_at=?, note=? WHERE key_id=?",
                             (db.now(), f"폐기: {reason}".strip(": "), t["key_id"]))
        return [t["key_id"] for t in targets]

    # --- 내부 ------------------------------------------------------------------
    def _insert(self, conn, partner: str, key: str, expires: str | None, note: str) -> str:
        n = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] + 1
        key_id = f"{self.prefix.upper()}K-{n:03d}"
        while conn.execute("SELECT 1 FROM api_keys WHERE key_id=?", (key_id,)).fetchone():
            n += 1
            key_id = f"{self.prefix.upper()}K-{n:03d}"
        conn.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (key_id, partner, _hash(key), _hint(key), "ACTIVE", db.now(), expires, None, None, note))
        return key_id

    def _import_initial(self) -> None:
        """장부가 비어 있을 때 한 번만 들여온다. 이후에는 장부만 믿는다 — 환경변수·파일이 바뀌어도
        (또는 다른 설정으로 도구를 실행해도) 키가 저절로 유효해지지 않는다. 키를 더하려면 issue를 쓴다."""
        with db.session(self.path) as conn:
            if conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]:
                return
            source = _parse_env(os.environ[self.env_var]) if os.getenv(self.env_var) else load_secrets()[self.section]
            for partner, key in source.items():
                if not conn.execute("SELECT 1 FROM api_keys WHERE key_hash=?", (_hash(key),)).fetchone():
                    self._insert(conn, partner, key, None, f"들여옴({self.env_var if os.getenv(self.env_var) else 'secrets.json'})")
