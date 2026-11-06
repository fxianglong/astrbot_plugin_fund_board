"""SQLite 存储：持仓、交易记录、推送设置、组合净值指数、提醒状态。

数据量很小，直接用标准库 sqlite3。
"""

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


@dataclass
class Settings:
    user_id: str
    umo: str = ""  # 推送目标会话
    private: bool = True  # 推送目标是否私聊（决定是否显示金额）
    push_on: bool = False
    day_alert: float | None = None  # 单日跌幅提醒阈值 %，None 表示用默认值，<=0 表示关闭
    dd_alert: float | None = None  # 回撤提醒阈值 %
    last_close: str = ""  # 最近一次收盘推送日期
    last_nav: str = ""  # 最近一次净值日报日期
    last_weekly: str = ""  # 最近一次周报对应的周（如 2026-W45）


@dataclass
class Trade:
    id: int
    user_id: str
    code: str
    name: str
    kind: str  # buy / sell
    amount: float  # 买入金额或卖出所得
    shares: float
    nav: float
    realized: float  # 卖出实现盈亏，买入为 0
    ts: float


_UPSERT_HOLDING = """INSERT INTO holdings (user_id, code, name, shares, cost, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(user_id, code) DO UPDATE SET
      name=excluded.name, shares=excluded.shares,
      cost=excluded.cost, updated_at=excluded.updated_at"""


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                user_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                shares REAL NOT NULL, cost REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, code));
            CREATE TABLE IF NOT EXISTS settings (
                user_id TEXT PRIMARY KEY, umo TEXT NOT NULL DEFAULT '', private INTEGER NOT NULL DEFAULT 1,
                push_on INTEGER NOT NULL DEFAULT 0, day_alert REAL, dd_alert REAL,
                last_close TEXT NOT NULL DEFAULT '', last_nav TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, amount REAL NOT NULL,
                shares REAL NOT NULL, nav REAL NOT NULL, realized REAL NOT NULL DEFAULT 0, ts REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id, ts);
            -- 组合净值指数：按每日涨跌连乘，不受加仓、赎回影响，用于计算组合回撤
            CREATE TABLE IF NOT EXISTS pindex (
                user_id TEXT NOT NULL, date TEXT NOT NULL, idx REAL NOT NULL,
                PRIMARY KEY (user_id, date));
            -- 回撤提醒的已提醒档位，避免每天重复提醒同一档
            CREATE TABLE IF NOT EXISTS alert_state (
                user_id TEXT NOT NULL, scope TEXT NOT NULL, level INTEGER NOT NULL,
                PRIMARY KEY (user_id, scope));
            """
        )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧版本数据库补字段。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(settings)").fetchall()}
        if "last_weekly" not in cols:
            self.conn.execute("ALTER TABLE settings ADD COLUMN last_weekly TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------------ 持仓
    def upsert(self, user_id: str, code: str, name: str, shares: float, cost: float) -> None:
        self.conn.execute(_UPSERT_HOLDING, (user_id, code, name, round(shares, 4), round(cost, 2), time.time()))
        self.conn.commit()

    def upsert_many(self, user_id: str, rows: list[tuple[str, str, float, float]], replace_all: bool = False) -> None:
        """rows: [(code, name, shares, cost)]。replace_all=True 时先清空该用户的持仓。"""
        with self.conn:
            if replace_all:
                self.conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
            now = time.time()
            self.conn.executemany(
                _UPSERT_HOLDING, [(user_id, c, n, round(s, 4), round(cost, 2), now) for c, n, s, cost in rows]
            )

    def get(self, user_id: str, code: str) -> Holding | None:
        row = self.conn.execute(
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings WHERE user_id=? AND code=?",
            (user_id, code),
        ).fetchone()
        return Holding(*row) if row else None

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
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings WHERE user_id=? ORDER BY cost DESC",
            (user_id,),
        )
        return [Holding(*row) for row in cur.fetchall()]

    # ------------------------------------------------------------ 交易
    def buy(self, user_id: str, code: str, name: str, amount: float, nav: float) -> tuple[Holding, Trade]:
        """按金额买入：份额 = 金额 ÷ 净值，成本增加买入金额。"""
        shares = amount / nav
        with self.conn:
            h = self.get(user_id, code)
            new_shares = (h.shares if h else 0) + shares
            new_cost = (h.cost if h else 0) + amount
            self.conn.execute(
                _UPSERT_HOLDING, (user_id, code, name, round(new_shares, 4), round(new_cost, 2), time.time())
            )
            tid = self._add_trade(user_id, code, name, "buy", amount, shares, nav, 0.0)
        return self.get(user_id, code), self.get_trade(tid)

    def sell(self, user_id: str, code: str, shares: float, nav: float) -> tuple[Holding | None, Trade]:
        """按份额卖出，平均成本法：成本按卖出份额比例扣减，差额记为实现盈亏。"""
        h = self.get(user_id, code)
        if not h:
            raise ValueError("持仓里没有这只基金")
        if shares <= 0:
            raise ValueError("卖出份额必须大于 0")
        if shares > h.shares + 1e-6:
            raise ValueError(f"卖出份额超过持有份额（{h.shares:,.2f}）")
        shares = min(shares, h.shares)
        cost_out = h.cost * shares / h.shares if h.shares else 0.0
        amount = shares * nav
        realized = amount - cost_out
        with self.conn:
            left = h.shares - shares
            if left < 0.01:  # 卖光了
                self.conn.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, code))
            else:
                self.conn.execute(
                    _UPSERT_HOLDING, (user_id, code, h.name, round(left, 4), round(h.cost - cost_out, 2), time.time())
                )
            tid = self._add_trade(user_id, code, h.name, "sell", amount, shares, nav, realized)
        return self.get(user_id, code), self.get_trade(tid)

    def _add_trade(self, user_id, code, name, kind, amount, shares, nav, realized) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (user_id, code, name, kind, amount, shares, nav, realized, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, code, name, kind, round(amount, 2), round(shares, 4), nav, round(realized, 2), time.time()),
        )
        return cur.lastrowid

    def get_trade(self, tid: int) -> Trade:
        row = self.conn.execute(
            "SELECT id, user_id, code, name, kind, amount, shares, nav, realized, ts FROM trades WHERE id=?", (tid,)
        ).fetchone()
        return Trade(*row)

    def trades(self, user_id: str, code: str | None = None, limit: int = 10) -> list[Trade]:
        sql = "SELECT id, user_id, code, name, kind, amount, shares, nav, realized, ts FROM trades WHERE user_id=?"
        args: list = [user_id]
        if code:
            sql += " AND code=?"
            args.append(code)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [Trade(*r) for r in self.conn.execute(sql, args).fetchall()]

    def trades_since(self, user_id: str, ts: float) -> list[Trade]:
        rows = self.conn.execute(
            "SELECT id, user_id, code, name, kind, amount, shares, nav, realized, ts FROM trades "
            "WHERE user_id=? AND ts>=? ORDER BY ts",
            (user_id, ts),
        ).fetchall()
        return [Trade(*r) for r in rows]

    def realized_total(self, user_id: str) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(realized), 0) FROM trades WHERE user_id=?", (user_id,)).fetchone()
        return float(row[0])

    # ------------------------------------------------------------ 设置
    def get_settings(self, user_id: str) -> Settings:
        row = self.conn.execute(
            "SELECT user_id, umo, private, push_on, day_alert, dd_alert, last_close, last_nav, last_weekly "
            "FROM settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return Settings(user_id)
        return Settings(row[0], row[1], bool(row[2]), bool(row[3]), row[4], row[5], row[6], row[7], row[8])

    def save_settings(self, s: Settings) -> None:
        self.conn.execute(
            """INSERT INTO settings (user_id, umo, private, push_on, day_alert, dd_alert, last_close, last_nav, last_weekly)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET umo=excluded.umo, private=excluded.private,
                 push_on=excluded.push_on, day_alert=excluded.day_alert, dd_alert=excluded.dd_alert,
                 last_close=excluded.last_close, last_nav=excluded.last_nav, last_weekly=excluded.last_weekly""",
            (s.user_id, s.umo, int(s.private), int(s.push_on), s.day_alert, s.dd_alert, s.last_close, s.last_nav,
             s.last_weekly),
        )
        self.conn.commit()

    def subscribers(self) -> list[Settings]:
        rows = self.conn.execute("SELECT user_id FROM settings WHERE push_on=1 AND umo<>''").fetchall()
        return [self.get_settings(r[0]) for r in rows]

    # ------------------------------------------------------------ 组合指数
    def record_index(self, user_id: str, date: str, day_pct: float) -> tuple[float, float]:
        """按当日涨跌更新组合指数（起点 1.0），返回 (当前指数, 历史最高)。同一天重复调用会覆盖。"""
        prev = self.conn.execute(
            "SELECT idx FROM pindex WHERE user_id=? AND date<? ORDER BY date DESC LIMIT 1", (user_id, date)
        ).fetchone()
        idx = (prev[0] if prev else 1.0) * (1 + day_pct / 100)
        with self.conn:
            self.conn.execute(
                "INSERT INTO pindex (user_id, date, idx) VALUES (?,?,?) "
                "ON CONFLICT(user_id, date) DO UPDATE SET idx=excluded.idx",
                (user_id, date, idx),
            )
        peak = self.conn.execute("SELECT MAX(idx) FROM pindex WHERE user_id=?", (user_id,)).fetchone()[0]
        return idx, max(peak, 1.0)

    def latest_index(self, user_id: str) -> tuple[float, float] | None:
        row = self.conn.execute(
            "SELECT idx FROM pindex WHERE user_id=? ORDER BY date DESC LIMIT 1", (user_id,)
        ).fetchone()
        if not row:
            return None
        peak = self.conn.execute("SELECT MAX(idx) FROM pindex WHERE user_id=?", (user_id,)).fetchone()[0]
        return row[0], max(peak, 1.0)

    def index_series(self, user_id: str, start: str, end: str) -> list[tuple[str, float]]:
        """[start, end] 区间内每天的组合指数，另外带上 start 之前最近的一天作为基准。"""
        base = self.conn.execute(
            "SELECT date, idx FROM pindex WHERE user_id=? AND date<? ORDER BY date DESC LIMIT 1", (user_id, start)
        ).fetchall()
        rows = self.conn.execute(
            "SELECT date, idx FROM pindex WHERE user_id=? AND date>=? AND date<=? ORDER BY date", (user_id, start, end)
        ).fetchall()
        return [(d, i) for d, i in base + rows]

    def index_days(self, user_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pindex WHERE user_id=?", (user_id,)).fetchone()[0]

    # ------------------------------------------------------------ 提醒档位
    def get_alert_level(self, user_id: str, scope: str) -> int:
        row = self.conn.execute(
            "SELECT level FROM alert_state WHERE user_id=? AND scope=?", (user_id, scope)
        ).fetchone()
        return row[0] if row else 0

    def set_alert_level(self, user_id: str, scope: str, level: int) -> None:
        self.conn.execute(
            "INSERT INTO alert_state (user_id, scope, level) VALUES (?,?,?) "
            "ON CONFLICT(user_id, scope) DO UPDATE SET level=excluded.level",
            (user_id, scope, level),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
