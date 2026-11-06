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
    kind: str  # buy / sell / init（录入）/ adjust（成本调整）/ remove（移除）
    amount: float  # 买入金额或卖出所得
    shares: float
    nav: float
    realized: float  # 卖出实现盈亏，买入为 0
    ts: float


@dataclass
class Note:
    id: int
    user_id: str
    code: str
    name: str
    kind: str  # buy / sell / note
    text: str
    trade_id: int | None
    amount: float
    ts: float
    reviewed: set[int]

    @staticmethod
    def from_row(r) -> "Note":
        return Note(*r[:9], {int(x) for x in r[9].split(",") if x})


@dataclass
class SipPlan:
    id: int
    user_id: str
    code: str
    name: str
    amount: float
    rule: str
    fee_rate: float
    active: int
    created: float
    last_date: str


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
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- v0.7 决策日志与定投
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, text TEXT NOT NULL, trade_id INTEGER,
                amount REAL NOT NULL DEFAULT 0, ts REAL NOT NULL, reviewed TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS sip_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', amount REAL NOT NULL, rule TEXT NOT NULL,
                fee_rate REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL, last_date TEXT NOT NULL DEFAULT '');
            -- v0.6 自定义标签与目标比例
            CREATE TABLE IF NOT EXISTS tags (
                user_id TEXT NOT NULL, code TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (user_id, code));
            CREATE TABLE IF NOT EXISTS targets (
                user_id TEXT NOT NULL, tag TEXT NOT NULL, pct REAL NOT NULL, PRIMARY KEY (user_id, tag));
            -- 每日快照：每个交易日记录一次组合市值和成本，用于计算区间收益和 XIRR
            CREATE TABLE IF NOT EXISTS snapshots (
                user_id TEXT NOT NULL, date TEXT NOT NULL, mv REAL NOT NULL, cost REAL NOT NULL,
                PRIMARY KEY (user_id, date));
            -- 每只基金最近一次推送过的净值日期，用于 QDII 等延迟公布净值的补推
            CREATE TABLE IF NOT EXISTS reported (
                user_id TEXT NOT NULL, code TEXT NOT NULL, nav_date TEXT NOT NULL,
                PRIMARY KEY (user_id, code));
            CREATE TABLE IF NOT EXISTS alert_state (
                user_id TEXT NOT NULL, scope TEXT NOT NULL, level INTEGER NOT NULL,
                PRIMARY KEY (user_id, scope));
            """
        )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧版本数据库补字段、补数据。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(settings)").fetchall()}
        if "last_weekly" not in cols:
            self.conn.execute("ALTER TABLE settings ADD COLUMN last_weekly TEXT NOT NULL DEFAULT ''")
        tcols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "sip_id" not in tcols:
            self.conn.execute("ALTER TABLE trades ADD COLUMN sip_id INTEGER")
        if not self.get_meta("flows_v1"):
            self._backfill_init_flows()
            self.set_meta("flows_v1", "1")

    def _backfill_init_flows(self) -> None:
        """v0.5 之前录入的持仓没有现金流记录，按「成本 − 买入 + 卖出扣减的成本」补一笔 init。"""
        for h in self.conn.execute(
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings"
        ).fetchall():
            h = Holding(*h)
            rows = self.conn.execute(
                "SELECT kind, amount, realized, ts FROM trades WHERE user_id=? AND code=?", (h.user_id, h.code)
            ).fetchall()
            buys = sum(a for k, a, _, _ in rows if k == "buy")
            sold_cost = sum(a - r for k, a, r, _ in rows if k == "sell")
            amount = h.cost - buys + sold_cost
            if amount <= 0.01:
                continue
            ts = min([h.updated_at] + [t for *_, t in rows]) - 1
            self.conn.execute(
                "INSERT INTO trades (user_id, code, name, kind, amount, shares, nav, realized, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (h.user_id, h.code, h.name, "init", round(amount, 2), 0.0, 0.0, 0.0, ts),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 每日快照
    def record_snapshot(self, user_id: str, date: str, market_value: float, cost: float) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (user_id, date, mv, cost) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET mv=excluded.mv, cost=excluded.cost",
            (user_id, date, round(market_value, 2), round(cost, 2)),
        )
        self.conn.commit()

    def snapshots(self, user_id: str) -> list[tuple[str, float]]:
        return self.conn.execute(
            "SELECT date, mv FROM snapshots WHERE user_id=? ORDER BY date", (user_id,)
        ).fetchall()

    def index_all(self, user_id: str) -> list[tuple[str, float]]:
        return self.conn.execute(
            "SELECT date, idx FROM pindex WHERE user_id=? ORDER BY date", (user_id,)
        ).fetchall()

    def all_user_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT user_id FROM holdings").fetchall()]

    # ------------------------------------------------------------ 持仓
    def _flow_for_set(self, user_id: str, code: str, name: str, cost: float, nav: float | None) -> None:
        """直接设置持仓时补一笔现金流：新持仓记 init（金额=成本），已有持仓成本变化记 adjust。"""
        old = self.get(user_id, code)
        if old is None:
            if cost > 0.01:
                self._add_trade(user_id, code, name, "init", cost, 0.0, nav or 0.0, 0.0)
        elif abs(cost - old.cost) > 0.01:
            self._add_trade(user_id, code, name, "adjust", cost - old.cost, 0.0, nav or 0.0, 0.0)

    def _flow_for_remove(self, h: Holding, nav: float | None) -> None:
        """移除持仓视为按当前市值取出（没有净值时按成本）。"""
        amount = h.shares * nav if nav else h.cost
        self._add_trade(h.user_id, h.code, h.name, "remove", amount, h.shares, nav or 0.0, 0.0)

    def upsert(self, user_id: str, code: str, name: str, shares: float, cost: float, nav: float | None = None) -> None:
        with self.conn:
            self._flow_for_set(user_id, code, name, cost, nav)
            self.conn.execute(_UPSERT_HOLDING, (user_id, code, name, round(shares, 4), round(cost, 2), time.time()))

    def upsert_many(
        self,
        user_id: str,
        rows: list[tuple[str, str, float, float]],
        replace_all: bool = False,
        navs: dict[str, float] | None = None,
    ) -> None:
        """rows: [(code, name, shares, cost)]。replace_all=True 时先移除截图里没有的持仓。"""
        navs = navs or {}
        with self.conn:
            if replace_all:
                keep = {r[0] for r in rows}
                for h in self.list(user_id):
                    if h.code not in keep:
                        self._flow_for_remove(h, navs.get(h.code))
                        self.conn.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, h.code))
            now = time.time()
            for c, n, sh, cost in rows:
                self._flow_for_set(user_id, c, n, cost, navs.get(c))
                self.conn.execute(_UPSERT_HOLDING, (user_id, c, n, round(sh, 4), round(cost, 2), now))

    def get(self, user_id: str, code: str) -> Holding | None:
        row = self.conn.execute(
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings WHERE user_id=? AND code=?",
            (user_id, code),
        ).fetchone()
        return Holding(*row) if row else None

    def delete(self, user_id: str, code: str, nav: float | None = None) -> bool:
        h = self.get(user_id, code)
        if not h:
            return False
        with self.conn:
            self._flow_for_remove(h, nav)
            self.conn.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, code))
        return True

    def clear(self, user_id: str, navs: dict[str, float] | None = None) -> int:
        navs = navs or {}
        hs = self.list(user_id)
        with self.conn:
            for h in hs:
                self._flow_for_remove(h, navs.get(h.code))
            self.conn.execute("DELETE FROM holdings WHERE user_id=?", (user_id,))
        return len(hs)

    def list(self, user_id: str) -> list[Holding]:
        cur = self.conn.execute(
            "SELECT user_id, code, name, shares, cost, updated_at FROM holdings WHERE user_id=? ORDER BY cost DESC",
            (user_id,),
        )
        return [Holding(*row) for row in cur.fetchall()]

    # ------------------------------------------------------------ 交易
    def buy(
        self,
        user_id: str,
        code: str,
        name: str,
        amount: float,
        nav: float,
        fee_rate: float = 0.0,
        ts: float | None = None,
        sip_id: int | None = None,
    ) -> tuple[Holding, Trade]:
        """按金额买入：份额 = 金额 ÷ (1 + 费率) ÷ 净值，成本增加买入金额。"""
        shares = amount / (1 + fee_rate / 100) / nav
        with self.conn:
            h = self.get(user_id, code)
            new_shares = (h.shares if h else 0) + shares
            new_cost = (h.cost if h else 0) + amount
            self.conn.execute(
                _UPSERT_HOLDING, (user_id, code, name, round(new_shares, 4), round(new_cost, 2), time.time())
            )
            tid = self._add_trade(user_id, code, name, "buy", amount, shares, nav, 0.0, ts=ts, sip_id=sip_id)
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

    def apply_changes(self, user_id: str, changes) -> list[int]:
        """按截图对比结果写入交易记录，并把持仓同步成截图里的数字。返回生成的买卖记录 ID（按顺序）。"""
        ids = []
        with self.conn:
            for c in changes:
                old = self.get(user_id, c.code)
                expected = old.cost if old else 0.0
                if c.kind in ("new", "buy"):
                    ids.append(self._add_trade(user_id, c.code, c.name, "buy", c.amount, c.shares, c.nav, 0.0))
                    expected += c.amount
                elif c.kind == "sell":
                    ids.append(
                        self._add_trade(user_id, c.code, c.name, "sell", c.amount, c.shares, c.nav, c.realized)
                    )
                    expected -= c.amount - c.realized
                if abs(c.new_cost - expected) > 0.01:  # 截图里的成本和推算的不一致，差额记为调整
                    self._add_trade(user_id, c.code, c.name, "adjust", c.new_cost - expected, 0.0, c.nav, 0.0)
                self.conn.execute(
                    _UPSERT_HOLDING,
                    (user_id, c.code, c.name, round(c.new_shares, 4), round(c.new_cost, 2), time.time()),
                )
        return ids

    def _add_trade(self, user_id, code, name, kind, amount, shares, nav, realized, ts=None, sip_id=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (user_id, code, name, kind, amount, shares, nav, realized, ts, sip_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, code, name, kind, round(amount, 2), round(shares, 4), nav, round(realized, 2),
             ts if ts is not None else time.time(), sip_id),
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

    # ------------------------------------------------------------ 净值补推
    def get_reported(self, user_id: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT code, nav_date FROM reported WHERE user_id=?", (user_id,)).fetchall()
        return dict(rows)

    def set_reported(self, user_id: str, items: dict[str, str]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO reported (user_id, code, nav_date) VALUES (?,?,?) "
                "ON CONFLICT(user_id, code) DO UPDATE SET nav_date=excluded.nav_date",
                [(user_id, c, d) for c, d in items.items()],
            )

    # ------------------------------------------------------------ 决策日志
    def add_note(self, user_id, code, name, kind, text, trade_id=None, amount=0.0, ts=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO notes (user_id, code, name, kind, text, trade_id, amount, ts) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, code, name, kind, text[:200], trade_id, amount, ts if ts is not None else time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def notes(self, user_id: str, limit: int | None = None) -> list[Note]:
        sql = ("SELECT id, user_id, code, name, kind, text, trade_id, amount, ts, reviewed FROM notes "
               "WHERE user_id=? ORDER BY ts DESC")
        args: list = [user_id]
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [Note.from_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def all_note_users(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT user_id FROM notes").fetchall()]

    def mark_reviewed(self, note_id: int, days: int) -> None:
        row = self.conn.execute("SELECT reviewed FROM notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            return
        done = {x for x in row[0].split(",") if x}
        done.add(str(days))
        self.conn.execute("UPDATE notes SET reviewed=? WHERE id=?", (",".join(sorted(done, key=int)), note_id))
        self.conn.commit()

    # ------------------------------------------------------------ 定投计划
    def add_sip(self, user_id, code, name, amount, rule, fee_rate=0.0, created=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sip_plans (user_id, code, name, amount, rule, fee_rate, created) VALUES (?,?,?,?,?,?,?)",
            (user_id, code, name, amount, rule, fee_rate, created if created is not None else time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def sips(self, user_id: str | None = None, active_only: bool = False) -> list[SipPlan]:
        sql = "SELECT id, user_id, code, name, amount, rule, fee_rate, active, created, last_date FROM sip_plans"
        cond, args = [], []
        if user_id:
            cond.append("user_id=?")
            args.append(user_id)
        if active_only:
            cond.append("active=1")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        return [SipPlan(*r) for r in self.conn.execute(sql + " ORDER BY id", args).fetchall()]

    def update_sip(self, plan_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sip_plans SET {sets} WHERE id=?", (*fields.values(), plan_id))
        self.conn.commit()

    def del_sip(self, plan_id: int) -> None:
        self.conn.execute("DELETE FROM sip_plans WHERE id=?", (plan_id,))
        self.conn.commit()

    def sip_trades(self, plan_id: int) -> list[Trade]:
        rows = self.conn.execute(
            "SELECT id, user_id, code, name, kind, amount, shares, nav, realized, ts FROM trades "
            "WHERE sip_id=? ORDER BY ts",
            (plan_id,),
        ).fetchall()
        return [Trade(*r) for r in rows]

    # ------------------------------------------------------------ 标签与目标
    def set_tag(self, user_id: str, code: str, tag: str | None) -> None:
        with self.conn:
            if tag:
                self.conn.execute(
                    "INSERT INTO tags (user_id, code, tag) VALUES (?,?,?) "
                    "ON CONFLICT(user_id, code) DO UPDATE SET tag=excluded.tag",
                    (user_id, code, tag),
                )
            else:
                self.conn.execute("DELETE FROM tags WHERE user_id=? AND code=?", (user_id, code))

    def tags(self, user_id: str) -> dict[str, str]:
        return dict(self.conn.execute("SELECT code, tag FROM tags WHERE user_id=?", (user_id,)).fetchall())

    def set_target(self, user_id: str, tag: str, pct: float | None) -> None:
        with self.conn:
            if pct is None:
                self.conn.execute("DELETE FROM targets WHERE user_id=? AND tag=?", (user_id, tag))
            else:
                self.conn.execute(
                    "INSERT INTO targets (user_id, tag, pct) VALUES (?,?,?) "
                    "ON CONFLICT(user_id, tag) DO UPDATE SET pct=excluded.pct",
                    (user_id, tag, pct),
                )

    def targets(self, user_id: str) -> dict[str, float]:
        return dict(self.conn.execute("SELECT tag, pct FROM targets WHERE user_id=?", (user_id,)).fetchall())

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
