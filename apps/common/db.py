# 파일명: db.py
"""회사마다 자기 SQLite 파일 하나. 에이전트·웹·CLI가 서로 다른 프로세스에서 같은 파일을 연다(WAL)."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def init(path: str | Path, schema: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # WAL 전환은 트랜잭션 밖에서
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(schema)
    finally:
        conn.close()


@contextmanager
def session(path: str | Path):
    """with 블록 하나가 트랜잭션 하나다. 예외가 나면 되돌린다."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def row(cursor) -> dict | None:
    r = cursor.fetchone()
    return dict(r) if r else None
