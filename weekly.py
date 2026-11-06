"""周报：本周每只基金的涨跌和盈亏贡献、组合每日涨跌、本周交易，以及大模型写的中性点评。

计算部分是纯函数；点评由 main.py 调用大模型生成后，用 sanitize_commentary 过滤掉操作建议。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .portfolio import CN_TZ, Summary
from .storage import Trade

WEEKDAYS = "一二三四五六日"


@dataclass
class FundWeek:
    code: str
    name: str
    mv: float
    ret: float | None  # 本周涨跌 %
    pnl: float | None  # 本周盈亏（元，按当前持仓估算）


@dataclass
class WeekReport:
    key: str  # 2026-W45
    start: str  # 周一 YYYY-MM-DD
    end: str  # 本周最后一个有净值的日期
    funds: list[FundWeek]
    pnl: float
    ret: float | None
    daily: list[tuple[str, float]]  # [(日期, 组合当日涨跌%)]，来自组合指数
    trades: list[Trade]
    realized: float
    total_mv: float
    total_profit: float
    total_pct: float | None
    flags: list[str] = field(default_factory=list)
    benches: list[tuple[str, float | None]] = field(default_factory=list)  # [(基准名, 本周涨跌%)]


def week_info(now: datetime) -> tuple[datetime, str]:
    """返回 (本周一 00:00 北京时间, 周编号)。"""
    now = now.astimezone(CN_TZ)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    y, w, _ = monday.isocalendar()
    return monday, f"{y}-W{w:02d}"


def fund_week_return(trend: list[tuple[int, float]], monday: datetime) -> tuple[float | None, int | None]:
    """用累计净值算本周涨跌：本周最后一个净值 ÷ 上周最后一个净值 − 1。

    返回 (涨跌%, 本周最后净值的时间戳毫秒)；本周还没有净值或缺少上周数据时返回 (None, None)。
    """
    if not trend:
        return None, None
    m_ms = monday.timestamp() * 1000
    this_week = [p for p in trend if p[0] >= m_ms]
    before = [p for p in trend if p[0] < m_ms]
    if not this_week or not before or before[-1][1] <= 0:
        return None, None
    return (this_week[-1][1] / before[-1][1] - 1) * 100, this_week[-1][0]


def build_week(
    summary: Summary,
    trends: dict[str, list[tuple[int, float]]],
    index_series: list[tuple[str, float]],
    trades: list[Trade],
    flags: list[str],
    now: datetime,
) -> WeekReport:
    monday, key = week_info(now)
    funds: list[FundWeek] = []
    pnl = base = 0.0
    last_ts = None
    for r in summary.rows:
        if r.market_value is None:
            continue
        ret, ts = fund_week_return(trends.get(r.code, []), monday)
        fp = None
        if ret is not None and ret > -100:
            fp = r.market_value * ret / (100 + ret)
            pnl += fp
            base += r.market_value - fp
            last_ts = max(last_ts or ts, ts)
        funds.append(FundWeek(r.code, r.name, r.market_value, ret, fp))
    funds.sort(key=lambda f: (f.pnl is None, f.pnl or 0))

    daily = []
    for (d0, i0), (d1, i1) in zip(index_series, index_series[1:]):
        if d1 >= monday.strftime("%Y-%m-%d") and i0 > 0:
            daily.append((d1, (i1 / i0 - 1) * 100))

    end = datetime.fromtimestamp(last_ts / 1000, CN_TZ).strftime("%Y-%m-%d") if last_ts else now.strftime("%Y-%m-%d")
    return WeekReport(
        key=key,
        start=monday.strftime("%Y-%m-%d"),
        end=end,
        funds=funds,
        pnl=pnl,
        ret=pnl / base * 100 if base > 0 else None,
        daily=daily,
        trades=trades,
        realized=sum(t.realized for t in trades),
        total_mv=summary.market_value,
        total_profit=summary.total_profit,
        total_pct=summary.total_pct,
        flags=flags,
    )


# ---------------------------------------------------------------- 大模型点评
SYSTEM_PROMPT = """你是基金持仓周报的点评助手。根据给出的数据，用中文写一段不超过 150 字的点评。
要求：
1. 只描述数据里的事实：本周组合表现、贡献最大和拖累最大的基金、持仓集中或重叠、回撤等。
2. 语气平和客观，不渲染情绪，不说"恭喜""别慌"之类的话。
3. 不预测后市，不评价基金好坏。
4. 绝对不要给出任何操作建议，不要出现买入、卖出、加仓、减仓、止损、止盈、抄底、调仓、建议等词。
5. 不编造数据里没有的信息，提到的数字必须来自数据。
直接输出点评正文，不要标题、不要列表。"""

BANNED = re.compile(r"建议|加仓|减仓|止损|止盈|抄底|调仓|买入|卖出|赎回|清仓|满仓|补仓|割肉|应该|不妨|可以考虑")


def sanitize_commentary(text: str) -> str:
    """去掉含有操作建议字眼的句子，避免大模型越界。"""
    text = (text or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    kept = [p for p in parts if p.strip() and not BANNED.search(p)]
    return "".join(kept).strip()


def llm_prompt(r: WeekReport, show_amount: bool) -> str:
    lines = [f"周期：{r.start} 至 {r.end}"]
    if r.ret is not None:
        lines.append(f"组合本周涨跌：{r.ret:+.2f}%" + (f"，盈亏 {r.pnl:+,.0f} 元" if show_amount else ""))
    if r.daily:
        lines.append("每日：" + "，".join(f"{d[5:]} {v:+.2f}%" for d, v in r.daily))
    for n, v in r.benches:
        if v is not None:
            lines.append(f"同期{n}：{v:+.2f}%")
    for f in r.funds:
        if f.ret is None:
            lines.append(f"- {f.name}：本周暂无净值")
            continue
        weight = f.mv / r.total_mv * 100 if r.total_mv else 0
        extra = f"，盈亏 {f.pnl:+,.0f} 元" if show_amount and f.pnl is not None else ""
        lines.append(f"- {f.name}：占组合 {weight:.0f}%，本周 {f.ret:+.2f}%{extra}")
    if r.total_pct is not None:
        lines.append(f"持仓累计收益率：{r.total_pct:+.2f}%")
    if r.trades:
        lines.append(f"本周交易 {len(r.trades)} 笔")
    for fl in r.flags:
        lines.append(f"体检发现：{fl}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文字输出
def render_weekly(r: WeekReport, show_amount: bool, commentary: str = "") -> str:
    lines = [f"📅 基金周报  {r.start[5:]} ~ {r.end[5:]}（{r.key[-3:]}）"]
    if r.ret is None:
        lines.append("本周还没有净值数据。")
    else:
        head = f"本周组合 {r.ret:+.2f}%"
        if show_amount:
            head += f"｜盈亏 {r.pnl:+,.0f}（按当前持仓估算）"
        lines.append(head)
    bench_txt = "｜".join(f"{n} {v:+.2f}%" for n, v in r.benches if v is not None)
    if bench_txt:
        lines.append(f"同期基准：{bench_txt}")
    if r.daily:
        days = []
        for d, v in r.daily:
            wd = datetime.strptime(d, "%Y-%m-%d").weekday()
            days.append(f"周{WEEKDAYS[wd]} {v:+.2f}%")
        lines.append("每日：" + "  ".join(days))
    lines.append("──────────")
    has_ret = [f for f in r.funds if f.ret is not None]
    if has_ret:
        best = max(has_ret, key=lambda f: f.pnl or 0)
        worst = min(has_ret, key=lambda f: f.pnl or 0)
        if worst.pnl is not None and worst.pnl < 0:
            lines.append(f"拖累最大：{worst.name} {worst.ret:+.2f}%" + (f"（{worst.pnl:+,.0f}）" if show_amount else ""))
        if best.pnl is not None and best.pnl > 0:
            lines.append(f"贡献最大：{best.name} {best.ret:+.2f}%" + (f"（{best.pnl:+,.0f}）" if show_amount else ""))
    for f in r.funds:
        if f.ret is None:
            lines.append(f"· {f.name}：本周暂无净值")
        else:
            lines.append(f"· {f.name}：{f.ret:+.2f}%" + (f"｜{f.pnl:+,.0f}" if show_amount and f.pnl is not None else ""))
    if r.trades:
        lines.append("──────────")
        lines.append(f"本周交易 {len(r.trades)} 笔" + (f"，已实现盈亏 {r.realized:+,.2f}" if show_amount and r.realized else ""))
        for t in r.trades[:5]:
            kind = {"buy": "买入", "sell": "卖出", "init": "录入", "adjust": "成本调整", "remove": "移除"}.get(t.kind, t.kind)
            lines.append(f"· {kind} {t.name}" + (f" {t.amount:,.0f}" if show_amount else ""))
    if r.flags:
        lines.append("──────────")
        lines.append("体检提示：")
        lines.extend(f"· {fl}" for fl in r.flags[:4])
    if commentary:
        lines.append("──────────")
        lines.append(f"🤖 点评：{commentary}")
    lines.append("──────────")
    tail = f"持仓累计 {r.total_pct:+.2f}%" if r.total_pct is not None else "持仓累计 --"
    if show_amount:
        tail = f"持仓市值 {r.total_mv:,.0f}｜累计盈亏 {r.total_profit:+,.0f}（{r.total_pct:+.2f}%）" if r.total_pct is not None else tail
    lines.append(tail)
    lines.append("本周涨跌按累计净值计算，盈亏按当前持仓估算；点评由 AI 生成。仅供记录参考，不构成投资建议。")
    return "\n".join(lines)
