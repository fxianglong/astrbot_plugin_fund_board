"""组合穿透分析与体检。纯函数，输入为已取好的数据，方便测试。

注意：基金只披露季报前十大重仓股，穿透结果只覆盖这部分持仓，且有 1～3 个月滞后。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .datasource import FundHoldings, FundProfile
from .portfolio import Summary

# 体检提示的阈值（描述性，不是建议）
FUND_WEIGHT_HIGH = 40.0  # 单只基金占组合比例
INDUSTRY_HIGH = 40.0  # 单一行业占已披露持仓比例
STOCK_HIGH = 10.0  # 单只股票占组合市值比例
PAIR_OVERLAP_HIGH = 30.0  # 两只基金重仓股重叠度
EFFECTIVE_FUNDS_LOW = 2.5  # 有效基金数
DRAWDOWN_HIGH = 30.0  # 单只基金近一年回撤
MANAGER_NEW_DAYS = 365  # 现任经理任职不足一年


@dataclass
class Exposure:
    name: str
    code: str
    amount: float  # 穿透金额（元）
    pct: float  # 占组合市值 %
    funds: list[str] = field(default_factory=list)  # 持有它的基金名


@dataclass
class OverlapPair:
    a: str
    b: str
    overlap: float  # 重叠度 %：共同重仓股在两只基金中占净值比例的较小值之和
    common: list[str]


@dataclass
class FundInfo:
    code: str
    name: str
    mv: float
    weight: float  # 占组合 %
    ftype: str
    report_date: str | None
    top10: float  # 前十大合计占净值 %
    stock_ratio: float | None
    return_1y: float | None
    drawdown_1y: float | None
    managers: list[tuple[str, int]]


@dataclass
class Analysis:
    total_mv: float
    funds: list[FundInfo]
    stocks: list[Exposure]
    industries: list[Exposure]
    pairs: list[OverlapPair]
    disclosed_pct: float  # 已披露（前十大）持仓占组合市值 %
    effective_funds: float
    type_mix: list[tuple[str, float]]
    report_dates: list[str]
    flags: list[str] = field(default_factory=list)


def short_type(ftype: str) -> str:
    t = ftype or "未知"
    if "海外" in t:  # 天天基金新分类把部分 QDII 记为「指数型-海外股票」
        return "QDII"
    for key in ("QDII", "FOF", "货币", "债券", "指数", "股票", "混合"):
        if key in t:
            return {"指数": "指数型", "股票": "股票型", "混合": "混合型", "债券": "债券型", "货币": "货币型"}.get(key, key)
    return t.split("-")[0] or "未知"


def pair_overlap(a: FundHoldings, b: FundHoldings) -> tuple[float, list[str]]:
    wa = {s.code or s.name: s for s in a.stocks}
    wb = {s.code or s.name: s for s in b.stocks}
    common = [k for k in wa if k in wb]
    common.sort(key=lambda k: -min(wa[k].weight, wb[k].weight))
    return sum(min(wa[k].weight, wb[k].weight) for k in common), [wa[k].name for k in common]


def analyze(
    summary: Summary,
    holdings: dict[str, FundHoldings],
    profiles: dict[str, FundProfile],
    drawdowns: dict[str, float],
    ftypes: dict[str, str],
) -> Analysis:
    rows = [r for r in summary.rows if r.market_value]
    total = sum(r.market_value for r in rows) or 0.0
    funds: list[FundInfo] = []
    stock_map: dict[str, Exposure] = {}
    ind_map: dict[str, Exposure] = {}
    disclosed = 0.0
    type_amt: dict[str, float] = {}

    for r in rows:
        h = holdings.get(r.code)
        p = profiles.get(r.code)
        weight = r.market_value / total * 100 if total else 0.0
        t = short_type(ftypes.get(r.code, ""))
        type_amt[t] = type_amt.get(t, 0.0) + r.market_value
        top10 = sum(s.weight for s in h.stocks) if h else 0.0
        funds.append(
            FundInfo(
                code=r.code,
                name=r.name,
                mv=r.market_value,
                weight=weight,
                ftype=t,
                report_date=h.report_date if h else None,
                top10=top10,
                stock_ratio=p.stock_ratio if p else None,
                return_1y=p.return_1y if p else None,
                drawdown_1y=drawdowns.get(r.code),
                managers=p.managers if p else [],
            )
        )
        if not h:
            continue
        for s in h.stocks:
            amt = r.market_value * s.weight / 100
            disclosed += amt
            key = s.code or s.name
            e = stock_map.setdefault(key, Exposure(s.name, s.code, 0.0, 0.0))
            e.amount += amt
            if r.name not in e.funds:
                e.funds.append(r.name)
            ie = ind_map.setdefault(s.industry, Exposure(s.industry, "", 0.0, 0.0))
            ie.amount += amt
            if r.name not in ie.funds:
                ie.funds.append(r.name)

    for e in stock_map.values():
        e.pct = e.amount / total * 100 if total else 0.0
    for e in ind_map.values():  # 行业占比按"已披露持仓"计算，更能反映方向集中度
        e.pct = e.amount / disclosed * 100 if disclosed else 0.0
    stocks = sorted(stock_map.values(), key=lambda e: -e.amount)
    industries = sorted(ind_map.values(), key=lambda e: -e.amount)

    pairs: list[OverlapPair] = []
    codes = [f.code for f in funds if holdings.get(f.code) and holdings[f.code].stocks]
    names = {f.code: f.name for f in funds}
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            ov, common = pair_overlap(holdings[codes[i]], holdings[codes[j]])
            if common:
                pairs.append(OverlapPair(names[codes[i]], names[codes[j]], ov, common))
    pairs.sort(key=lambda p: -p.overlap)

    shares = [f.weight / 100 for f in funds]
    effective = 1 / sum(s * s for s in shares) if shares and sum(shares) > 0 else 0.0
    type_mix = sorted(((t, a / total * 100) for t, a in type_amt.items()), key=lambda x: -x[1]) if total else []
    dates = sorted({f.report_date for f in funds if f.report_date}, reverse=True)

    a = Analysis(
        total_mv=total,
        funds=sorted(funds, key=lambda f: -f.mv),
        stocks=stocks,
        industries=industries,
        pairs=pairs,
        disclosed_pct=disclosed / total * 100 if total else 0.0,
        effective_funds=effective,
        type_mix=type_mix,
        report_dates=dates,
    )
    a.flags = make_flags(a)
    return a


def make_flags(a: Analysis) -> list[str]:
    """体检发现：只陈述数据事实，不给买卖建议。"""
    flags = []
    if a.funds and a.funds[0].weight >= FUND_WEIGHT_HIGH and len(a.funds) > 1:
        f = a.funds[0]
        flags.append(f"单只基金占比高：{f.name} 占组合 {f.weight:.0f}%")
    if len(a.funds) > 1 and a.effective_funds < EFFECTIVE_FUNDS_LOW:
        flags.append(f"有效基金数 {a.effective_funds:.1f}（{len(a.funds)} 只基金，但资金集中在少数几只）")
    if a.industries and a.industries[0].pct >= INDUSTRY_HIGH:
        i = a.industries[0]
        flags.append(f"行业集中：{i.name} 占已披露重仓股的 {i.pct:.0f}%，涉及 {len(i.funds)} 只基金")
    big = [s for s in a.stocks if s.pct >= STOCK_HIGH]
    if big:
        flags.append("个股集中：" + "、".join(f"{s.name} 穿透后占组合 {s.pct:.1f}%" for s in big[:3]))
    high = [p for p in a.pairs if p.overlap >= PAIR_OVERLAP_HIGH]
    if high:
        flags.append(f"持仓重叠：{len(high)} 对基金的重仓股重叠度超过 {PAIR_OVERLAP_HIGH:.0f}%")
    dd = [f for f in a.funds if f.drawdown_1y is not None and f.drawdown_1y >= DRAWDOWN_HIGH]
    if dd:
        flags.append("近一年回撤较深：" + "、".join(f"{f.name} {f.drawdown_1y:.0f}%" for f in dd[:3]))
    new_mgr = [f for f in a.funds if f.managers and max(d for _, d in f.managers) < MANAGER_NEW_DAYS]
    if new_mgr:
        flags.append("现任经理任职不足一年：" + "、".join(f.name for f in new_mgr[:3]))
    return flags
