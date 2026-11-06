"""盈亏计算。纯函数，不做任何网络或数据库操作，方便测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .datasource import Quote
from .storage import Holding

# 基金净值和估值都按北京时间，服务器时区可能是 UTC，这里固定时区
CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class Row:
    code: str
    name: str
    price_kind: str  # "估" 盘中估值 / "净" 官方净值 / "?" 无数据
    price_date: str | None  # 价格对应日期（MM-DD），净值不是今天时用于提示
    day_pct: float | None  # 当日涨跌 %
    market_value: float | None
    day_profit: float | None
    total_profit: float | None
    total_pct: float | None
    cost: float


@dataclass
class Summary:
    rows: list[Row]
    market_value: float = 0.0
    cost: float = 0.0
    day_profit: float = 0.0
    day_base: float = 0.0  # 当日盈亏的基数（昨日市值）
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # 净值不是今天的基金

    @property
    def total_profit(self) -> float:
        return self.market_value - self.cost

    @property
    def total_pct(self) -> float | None:
        return self.total_profit / self.cost * 100 if self.cost > 0 else None

    @property
    def day_pct(self) -> float | None:
        return self.day_profit / self.day_base * 100 if self.day_base > 0 else None


def pick_price(q: Quote, today: str) -> tuple[float | None, float | None, str, str | None]:
    """选出用于计算的价格。

    返回 (价格, 当日涨跌%, 类型, 日期)。规则：
    1. 今天的官方净值已公布 → 用净值；
    2. 否则有今天的盘中估值 → 用估值；
    3. 否则用最近一次净值（例如周末、QDII 净值延迟），并标出日期。
    """
    if q.nav is not None and q.nav_date == today:
        return q.nav, q.nav_pct, "净", None
    if q.est is not None and q.est_time and q.est_time[:10] == today:
        return q.est, q.est_pct, "估", None
    if q.nav is not None:
        return q.nav, q.nav_pct, "净", (q.nav_date[5:] if q.nav_date else None)
    return None, None, "?", None


def build_summary(holdings: list[Holding], quotes: dict[str, Quote], now: datetime | None = None) -> Summary:
    today = (now or datetime.now(CN_TZ)).strftime("%Y-%m-%d")
    s = Summary(rows=[])
    for h in holdings:
        q = quotes.get(h.code)
        price, pct, kind, pdate = pick_price(q, today) if q else (None, None, "?", None)
        name = (q.name if q and q.name else h.name) or h.code
        s.cost += h.cost
        if price is None:
            s.missing.append(h.code)
            s.rows.append(Row(h.code, name, "?", None, None, None, None, None, None, h.cost))
            continue
        mv = h.shares * price
        day_profit = None
        if pct is not None and pct > -100:
            prev = mv / (1 + pct / 100)
            day_profit = mv - prev
            if pdate is None:  # 只有今天的涨跌才计入"今日"合计；QDII 等净值滞后的不计入
                s.day_profit += day_profit
                s.day_base += prev
            else:
                s.stale.append(h.code)
        total = mv - h.cost
        s.market_value += mv
        s.rows.append(
            Row(
                code=h.code,
                name=name,
                price_kind=kind,
                price_date=pdate,
                day_pct=pct,
                market_value=mv,
                day_profit=day_profit,
                total_profit=total,
                total_pct=(total / h.cost * 100) if h.cost > 0 else None,
                cost=h.cost,
            )
        )
    # 缺数据的基金无法计市值，把它们的成本从合计里扣掉，避免合计收益被拉低
    for r in s.rows:
        if r.market_value is None:
            s.cost -= r.cost
    s.rows.sort(key=lambda r: (r.market_value is None, -(r.market_value or 0)))
    return s


def drawdown_from_trend(trend: list[tuple[int, float]], days: int = 365) -> tuple[float, float, int] | None:
    """近 N 天累计净值走势里，最新值较最高点的回撤。

    返回 (回撤%, 最高值, 最高点时间戳毫秒)，回撤为正数；数据不足返回 None。
    """
    if not trend:
        return None
    last_ts = trend[-1][0]
    window = [p for p in trend if p[0] >= last_ts - days * 86400 * 1000]
    if len(window) < 2:
        return None
    peak_ts, peak = max(window, key=lambda p: p[1])
    latest = window[-1][1]
    if peak <= 0:
        return None
    return (1 - latest / peak) * 100, peak, peak_ts


def nav_readiness(quotes: dict[str, Quote], codes: list[str], today: str) -> tuple[list[str], list[str]]:
    """判断今天的净值是否已公布。

    返回 (已公布今日净值的代码, 预计今天会公布但还没出的代码)。
    今天有盘中估值、但净值日期不是今天的，视为"还没出"；QDII 这类没有估值的不等待。
    """
    ready, pending = [], []
    for c in codes:
        q = quotes.get(c)
        if not q:
            continue
        if q.nav_date == today:
            ready.append(c)
        elif q.est_time and q.est_time[:10] == today:
            pending.append(c)
    return ready, pending


def has_trading_today(quotes: dict[str, Quote], today: str) -> bool:
    """今天是否交易日：只要有一只基金有今天的估值或净值即可判断。"""
    return any(
        (q.est_time and q.est_time[:10] == today) or q.nav_date == today for q in quotes.values()
    )
