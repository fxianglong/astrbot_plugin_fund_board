"""SQLite 持仓存储。数据量很小，直接用标准库 sqlite3。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Holding:
    user_id: str
    code: str
    name: str
    shares: float  # 持有份额
    cost: float  # 投入成本（元）
    updated_at: float


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS holdings (
                user_id    TEXT NOT NULL,
                code       TEXT NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                shares     REAL NOT NULL,
                cost       REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, code)
            )"""
        )
        self.conn.commit()

    def upsert(self, user_id: str, code: str, name: str, shares: float, cost: float) -> None:
        self.conn.execute(
            """INSERT INTO holdings (user_id, code, name, shares, cost, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, code) DO UPDATE SET
                 name=excluded.name, shares=excluded.shares,
                 cost=excluded.cost, updated_at=excluded.updated_at""",
            (user_id, code, name, round(shares, 4), round(cost, 2), time.time()),
        )
        self.conn.commit()

    def upsert_many(self, user_id: str, rows: list[tuple[str, str, float, float]], replace_all: bool = False) -> None:
        """rows: [(code, name, shares, cost)]。replace_all=True 时先清空该用户的持仓。"""
        with self.conn:
            if replace_all:
                self.conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            now = time.time()
            self.conn.executemany(
                """INSERT INTO holdings (user_id, code, name, shares, cost, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, code) DO UPDATE SET
                     name=excluded.name, shares=excluded.shares,
                     cost=excluded.cost, updated_at=excluded.updated_at""",
                [(user_id, c, n, round(s, 4), round(cost, 2), now) for c, n, s, cost in rows],
            )

    def delete(self, user_id: str, code: str) -> bool:
        cur = self.conn.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, code))
        self.conn.commit()
        return cur.rowcount > 0

    def clear(self, user_id: str) -> int:
        cur = self.conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
        self.conn.commit()
        return cur.rowcount

    def list(self, user_id: str) -> list[Holding]:
        cur = self.conn.execute(
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings "
            "WHERE user_id=? ORDER BY cost DESC",
            (user_id,),
        )
        return [Holding(*row) for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
