"""决策日志（交易理由、到期复盘）与定投计划。纯函数。

复盘只列事实：记录时的理由、之后这只基金涨跌了多少、同期基准涨跌了多少，不评价对错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .perf import value_at, xirr
from .portfolio import CN_TZ

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


# ---------------------------------------------------------------- 定投规则
@dataclass
class Rule:
    kind: str  # daily / weekly / biweekly / monthly
    weekday: int = 0
    day: int = 1

    def text(self) -> str:
        names = "一二三四五六日"
        if self.kind == "daily":
            return "每个交易日"
        if self.kind == "weekly":
            return f"每周{names[self.weekday]}"
        if self.kind == "biweekly":
            return f"每两周周{names[self.weekday]}"
        return f"每月{self.day}日"

    def dump(self) -> str:
        return f"{self.kind}:{self.weekday}:{self.day}"

    @staticmethod
    def load(s: str) -> "Rule":
        k, w, d = s.split(":")
        return Rule(k, int(w), int(d))


def parse_rule(text: str) -> Rule | None:
    t = (text or "").strip().replace("星期", "周").replace("礼拜", "周")
    if t in ("每日", "每天", "每个交易日", "每交易日"):
        return Rule("daily")
    m = re.fullmatch(r"每(?:两|双|2)周(?:周)?([一二三四五六日天])", t)
    if m:
        return Rule("biweekly", weekday=WEEKDAY_CN[m.group(1)])
    m = re.fullmatch(r"每周([一二三四五六日天])", t)
    if m:
        return Rule("weekly", weekday=WEEKDAY_CN[m.group(1)])
    m = re.fullmatch(r"每月(\d{1,2})[日号]?", t)
    if m and 1 <= int(m.group(1)) <= 31:
        return Rule("monthly", day=int(m.group(1)))
    return None


def scheduled_dates(rule: Rule, start: date, end: date, anchor: date) -> list[date]:
    """[start, end] 之间按规则应该扣款的日历日期（遇周末/节假日由调用方顺延）。

    月定投日大于当月天数时取当月最后一天；双周以创建日所在周为第一周。
    """
    out = []
    d = start
    while d <= end:
        if rule.kind == "daily":
            if d.weekday() < 5:
                out.append(d)
        elif rule.kind == "weekly":
            if d.weekday() == rule.weekday:
                out.append(d)
        elif rule.kind == "biweekly":
            weeks = ((d - timedelta(days=d.weekday())) - (anchor - timedelta(days=anchor.weekday()))).days // 7
            if d.weekday() == rule.weekday and weeks % 2 == 0:
                out.append(d)
        else:
            nxt = d + timedelta(days=1)
            last_day = nxt.month != d.month
            if d.day == rule.day or (last_day and d.day < rule.day):
                out.append(d)
        d += timedelta(days=1)
    return out


def effective_trade_date(trend: list[tuple[int, float]], sched: date) -> tuple[date, float] | None:
    """扣款日之后（含当天）第一个有净值的交易日，以及当天的单位净值。"""
    start_ms = datetime(sched.year, sched.month, sched.day, tzinfo=CN_TZ).timestamp() * 1000
    for ts, nav in trend:
        if ts >= start_ms:
            return datetime.fromtimestamp(ts / 1000, CN_TZ).date(), nav
    return None


@dataclass
class SipStat:
    plan_id: int
    name: str
    code: str
    rule: str
    periods: int
    invested: float
    shares: float
    avg_cost: float | None
    market_value: float | None
    ret: float | None  # 收益率 %
    xirr: float | None  # 年化 %
    lump_value: float | None  # 第一期一次性投入全部金额的现值


def sip_stat(plan, trades, nav_now: float | None, now: datetime) -> SipStat:
    """trades: 该计划生成的买入记录（kind、amount、shares、nav、ts）。"""
    invested = sum(t.amount for t in trades)
    shares = sum(t.shares for t in trades)
    mv = shares * nav_now if nav_now else None
    ret = (mv / invested - 1) * 100 if mv is not None and invested > 0 else None
    flows = [(t.ts, -t.amount) for t in trades]
    rate = None
    if mv is not None and trades and (now.timestamp() - trades[0].ts) >= 30 * 86400:
        r = xirr(flows + [(now.timestamp(), mv)])
        rate = r * 100 if r is not None else None
    lump = invested / trades[0].nav * nav_now if trades and trades[0].nav and nav_now else None
    return SipStat(
        plan_id=plan.id, name=plan.name, code=plan.code, rule=Rule.load(plan.rule).text(), periods=len(trades),
        invested=invested, shares=shares, avg_cost=invested / shares if shares else None,
        market_value=mv, ret=ret, xirr=rate, lump_value=lump,
    )


def render_sip_list(plans) -> str:
    if not plans:
        return "还没有定投计划。例如：/fund sip add 110020 500 每周三"
    lines = ["📆 定投计划"]
    for p in plans:
        state = "进行中" if p.active else "已暂停"
        fee = f"，费率 {p.fee_rate:g}%" if p.fee_rate else ""
        last = f"，最近记账 {p.last_date}" if p.last_date else ""
        lines.append(f"{p.id}. {p.name}（{p.code}） {Rule.load(p.rule).text()} {p.amount:,.0f} 元{fee}｜{state}{last}")
    lines.append("管理：/fund sip pause N ｜ resume N ｜ del N ｜ stats")
    return "\n".join(lines)


def render_sip_stats(stats: list[SipStat], show_amount: bool = True) -> str:
    if not stats:
        return "还没有定投计划。"
    lines = ["📆 定投统计"]
    for s in stats:
        lines.append(f"【{s.plan_id}. {s.name}】{s.rule}，已记账 {s.periods} 期")
        if not s.periods:
            continue
        parts = []
        if show_amount:
            parts.append(f"投入 {s.invested:,.0f}")
            if s.market_value is not None:
                parts.append(f"市值 {s.market_value:,.0f}")
        if s.ret is not None:
            parts.append(f"收益率 {s.ret:+.2f}%")
        if s.xirr is not None:
            parts.append(f"年化 {s.xirr:+.2f}%")
        if s.avg_cost:
            parts.append(f"平均成本 {s.avg_cost:.4f}")
        lines.append("   " + "｜".join(parts))
        if s.lump_value is not None and s.market_value is not None:
            gap = s.market_value - s.lump_value
            if show_amount:
                lines.append(f"   对照：第一期就一次性投入同样的钱，现在约 {s.lump_value:,.0f}（定投{'多' if gap >= 0 else '少'} {abs(gap):,.0f}）")
            else:
                lines.append(f"   对照：定投比第一期一次性投入{'多' if gap >= 0 else '少'} {abs(gap) / s.lump_value * 100:.1f}%")
    lines.append("定投记账只是插件里的记录，不会真实扣款。不构成投资建议。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 决策日志与复盘
KIND_CN = {"buy": "买入", "sell": "卖出", "note": "记录想法"}


@dataclass
class Review:
    note_id: int
    days: int
    code: str
    name: str
    kind: str
    text: str
    when: str  # 记录日期
    fund_ret: float | None  # 之后基金涨跌 %
    bench: list[tuple[str, float | None]]


def review_of(note, days: int, trend, benches: dict, now: datetime) -> Review:
    start_ms = note.ts * 1000
    base = value_at(trend, start_ms) or (trend[0][1] if trend else None)
    fund_ret = (trend[-1][1] / base - 1) * 100 if trend and base else None
    bench = []
    for name, (_code, tr) in benches.items():
        b = value_at(tr, start_ms)
        bench.append((name, (tr[-1][1] / b - 1) * 100 if tr and b else None))
    when = datetime.fromtimestamp(note.ts, CN_TZ).strftime("%Y-%m-%d")
    return Review(note.id, days, note.code, note.name, note.kind, note.text, when, fund_ret, bench)


def render_review(r: Review, show_amount: bool = True, amount: float | None = None) -> str:
    what = KIND_CN.get(r.kind, r.kind)
    amt = f" {amount:,.0f} 元" if show_amount and amount else ""
    head = f"🗒️ {r.days} 天前（{r.when}）你{what} {r.name}{amt}" if r.days else f"🗒️ {r.when} {what} {r.name}{amt}"
    lines = [head, f"   当时的理由：{r.text}"]
    if r.fund_ret is not None:
        after = "卖出后这只基金" if r.kind == "sell" else "之后这只基金"
        lines.append(f"   {after} {r.fund_ret:+.2f}%")
    bt = "｜".join(f"{n} {v:+.2f}%" for n, v in r.bench if v is not None)
    if bt:
        lines.append(f"   同期 {bt}")
    return "\n".join(lines)


def due_reviews(notes, now: datetime, windows: list[int]) -> list[tuple[object, int]]:
    """到期需要复盘的 (笔记, 天数)。笔记需有 ts 和 reviewed（已复盘过的天数集合）。"""
    out = []
    for n in notes:
        age = (now.timestamp() - n.ts) / 86400
        for w in windows:
            if age >= w and w not in n.reviewed:
                out.append((n, w))
    return out
