"""收益计算：XIRR（资金加权年化）、时间加权收益、基准对比、最大回撤。纯函数。

现金流约定：投入为负、取出为正；期末市值作为最后一笔正现金流。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .portfolio import CN_TZ

DAY_MS = 86400 * 1000
RANGES = {"近1月": 30, "近3月": 91, "近6月": 182, "近1年": 365, "今年": -1, "全部": 0}
RANGE_ALIASES = {"1m": "近1月", "3m": "近3月", "6m": "近6月", "1y": "近1年", "ytd": "今年", "all": "全部",
                 "近一月": "近1月", "近三月": "近3月", "近半年": "近6月", "近一年": "近1年"}


# ---------------------------------------------------------------- 现金流与 XIRR
def flow_of(kind: str, amount: float) -> float:
    """交易记录 → 现金流（投入为负）。"""
    if kind in ("buy", "init"):
        return -abs(amount)
    if kind in ("sell", "remove"):
        return abs(amount)
    if kind == "adjust":  # amount 为成本变化，正数表示多投入
        return -amount
    return 0.0


def xirr(flows: list[tuple[float, float]]) -> float | None:
    """flows: [(时间戳秒, 金额)]。返回年化收益率（小数），无法计算返回 None。"""
    flows = [(t, a) for t, a in flows if abs(a) > 1e-9]
    if len(flows) < 2 or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = min(t for t, _ in flows)
    ys = [((t - t0) / 86400 / 365, a) for t, a in flows]
    if max(y for y, _ in ys) < 1e-6:
        return None

    def f(r: float) -> float:
        return sum(a / (1 + r) ** y for y, a in ys)

    def df(r: float) -> float:
        return sum(-y * a / (1 + r) ** (y + 1) for y, a in ys)

    r = 0.1
    for _ in range(100):
        try:
            v, d = f(r), df(r)
        except (OverflowError, ZeroDivisionError):
            break
        if abs(v) < 1e-7:
            return r
        if d == 0:
            break
        nr = r - v / d
        if nr <= -0.9999 or nr > 100:
            break
        if abs(nr - r) < 1e-10:
            return nr
        r = nr
    # 牛顿法失败时用二分法
    lo, hi = -0.9999, 10.0
    try:
        flo, fhi = f(lo), f(hi)
    except OverflowError:
        return None
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


# ---------------------------------------------------------------- 区间
def range_start(name: str, now: datetime) -> datetime | None:
    """区间起点（北京时间 00:00）；「全部」返回 None。"""
    days = RANGES[name]
    now = now.astimezone(CN_TZ)
    if days == 0:
        return None
    if days == -1:
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)


def value_at(series: list[tuple[int, float]], ts_ms: float) -> float | None:
    """走势里时间不晚于 ts_ms 的最后一个值。"""
    v = None
    for t, x in series:
        if t <= ts_ms:
            v = x
        else:
            break
    return v


def series_return(series: list[tuple[int, float]], start_ms: float | None) -> float | None:
    """区间收益 %：最新值 ÷ 区间起点前最后一个值 − 1。"""
    if len(series) < 2:
        return None
    base = series[0][1] if start_ms is None else value_at(series, start_ms - 1)
    if base is None and start_ms is not None:  # 走势从区间中间才开始，取第一个点
        base = next((x for t, x in series if t >= start_ms), None)
    if not base or base <= 0:
        return None
    return (series[-1][1] / base - 1) * 100


def max_drawdown(values: list[float]) -> float:
    peak, mdd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (1 - v / peak) * 100)
    return mdd


def date_ms(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=CN_TZ).timestamp() * 1000)


@dataclass
class Bench:
    name: str
    code: str
    ret: float | None  # 区间收益 %
    same_flow_value: float | None  # 同样现金流投入基准的期末市值（仅「全部」）


@dataclass
class PerfResult:
    range_name: str
    start: str | None  # 区间起点日期
    days: int  # 区间覆盖的记录天数
    twr: float | None  # 时间加权收益 %
    xirr: float | None  # 年化 %
    mdd: float | None  # 区间内组合最大回撤 %
    profit: float | None  # 区间内盈亏（元）
    market_value: float
    invested: float  # 区间内净投入（元）
    benches: list[Bench]
    curve: list[tuple[str, float]]  # 组合指数（区间起点归一为 1）
    bench_curves: dict[str, list[tuple[str, float]]]


def bench_same_flow(flows: list[tuple[float, float]], trend: list[tuple[int, float]]) -> float | None:
    """假设每一笔现金流都改为买卖基准，期末能有多少市值。"""
    if not trend:
        return None
    units = 0.0
    for t, a in sorted(flows):
        p = value_at(trend, t * 1000) or trend[0][1]
        if p <= 0:
            return None
        units += -a / p
    return units * trend[-1][1]


def compute_perf(
    range_name: str,
    now: datetime,
    trades: list,  # 需有 kind、amount、ts
    market_value: float,
    index_series: list[tuple[str, float]],  # 组合指数 [(日期, 指数)]，按日期升序
    snapshots: list[tuple[str, float]],  # [(日期, 市值)]
    benches: dict[str, tuple[str, list[tuple[int, float]]]],  # 名称 -> (代码, 累计净值走势)
) -> PerfResult:
    start_dt = range_start(range_name, now)
    start_ms = start_dt.timestamp() * 1000 if start_dt else None
    start_s = start_dt.strftime("%Y-%m-%d") if start_dt else None
    now_ts = now.timestamp()

    # 组合指数区间
    if start_s:
        before = [x for x in index_series if x[0] < start_s]
        inside = [x for x in index_series if x[0] >= start_s]
        seg = before[-1:] + inside
    else:
        seg = list(index_series)
    twr = mdd = None
    curve: list[tuple[str, float]] = []
    if len(seg) >= 2 and seg[0][1] > 0:
        twr = (seg[-1][1] / seg[0][1] - 1) * 100
        curve = [(d, v / seg[0][1]) for d, v in seg]
        mdd = max_drawdown([v for _, v in curve])

    # 现金流
    all_flows = [(t.ts, flow_of(t.kind, t.amount)) for t in trades]
    if start_ms is None:
        flows = list(all_flows)
        invested = -sum(a for _, a in flows)
    else:
        snap_ms = [(date_ms(d) + DAY_MS - 1, v) for d, v in snapshots]  # 快照在当天收盘后记录，按当天结束计
        start_mv = value_at(snap_ms, start_ms - 1)
        if start_mv is None:  # 快照从区间中间才开始：以第一条快照为起点
            first = next(((t, v) for t, v in snap_ms if t >= start_ms), None)
            if first:
                start_ms, start_mv = first[0] + 1, first[1]
        inner = [(t, a) for t, a in all_flows if t * 1000 >= start_ms]
        flows = ([(start_ms / 1000, -start_mv)] if start_mv else []) + inner
        invested = -sum(a for _, a in inner)
        if not start_mv:
            flows = []  # 没有区间起点市值，算不了区间 XIRR
    flows_end = flows + [(now_ts, market_value)] if flows else []
    first_ts = min((t for t, _ in flows), default=None)
    span_days = (now_ts - first_ts) / 86400 if first_ts else 0
    rate = xirr(flows_end) if span_days >= 30 else None
    profit = (market_value + sum(a for _, a in flows)) if flows else None

    bench_res, bench_curves = [], {}
    for name, (code, trend) in benches.items():
        ret = series_return(trend, start_ms)
        same = bench_same_flow(all_flows, trend) if start_ms is None and all_flows else None
        bench_res.append(Bench(name, code, ret, same))
        if curve:
            c0 = date_ms(curve[0][0])
            base = value_at(trend, c0) or next((x for t, x in trend if t >= c0), None)
            if base:
                bench_curves[name] = [
                    (d, (value_at(trend, date_ms(d)) or base) / base) for d, _ in curve
                ]
    return PerfResult(
        range_name=range_name,
        start=start_s or (seg[0][0] if seg else None),
        days=len(seg),
        twr=twr,
        xirr=rate * 100 if rate is not None else None,
        mdd=mdd,
        profit=profit,
        market_value=market_value,
        invested=invested,
        benches=bench_res,
        curve=curve,
        bench_curves=bench_curves,
    )


def render_perf(p: PerfResult, show_amount: bool = True) -> str:
    lines = [f"📈 收益表现（{p.range_name}）"]
    if p.start:
        lines.append(f"区间：{p.start} 起，共 {p.days} 个记录日")
    if p.twr is None and p.xirr is None:
        lines.append("记录的数据还不够。每个交易日 23:40 自动记录一次，过几天再来看。")
    if p.twr is not None:
        lines.append(f"组合涨跌（时间加权）：{p.twr:+.2f}%")
    if p.xirr is not None:
        lines.append(f"资金加权年化（XIRR）：{p.xirr:+.2f}%")
    elif p.twr is not None:
        lines.append("年化：记录不足 30 天，暂不计算")
    if p.mdd is not None:
        lines.append(f"区间最大回撤：{p.mdd:.2f}%")
    if show_amount and p.profit is not None:
        lines.append(f"区间盈亏：{p.profit:+,.0f}（净投入 {p.invested:+,.0f}，当前市值 {p.market_value:,.0f}）")
    if p.benches:
        lines.append("──────────")
        for b in p.benches:
            if b.ret is None:
                lines.append(f"{b.name}：暂无数据")
                continue
            line = f"{b.name}：{b.ret:+.2f}%"
            if p.twr is not None:
                diff = p.twr - b.ret
                line += f"（组合{'跑赢' if diff >= 0 else '跑输'} {abs(diff):.2f} 个百分点）"
            lines.append(line)
            if show_amount and b.same_flow_value is not None:
                gap = p.market_value - b.same_flow_value
                lines.append(f"   同样的钱同样的时间投入{b.name}，现在约 {b.same_flow_value:,.0f}（差 {gap:+,.0f}）")
        names = "、".join(f"{b.name}={b.code}" for b in p.benches)
        lines.append(f"基准以指数联接基金的累计净值近似（{names}）。")
    lines.append("收益从开始在插件里记录时算起。仅供记录参考，不构成投资建议。")
    return "\n".join(lines)
