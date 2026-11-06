"""目标配置（标签、目标比例、偏离）与情景压力测试。纯函数。

只陈述事实：偏离提醒说「离你自己定的目标差多少」，压力测试是「假设情景下的算术」，都不给操作建议。
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis import short_type
from .datasource import FundHoldings, FundProfile

DRIFT_STEP = 5.0  # 偏离超过容忍度后，每再偏 5 个百分点再提醒一次


# ---------------------------------------------------------------- 标签与目标
def group_of(code: str, ftype: str, tags: dict[str, str]) -> str:
    return tags.get(code) or short_type(ftype)


@dataclass
class AllocRow:
    group: str
    mv: float
    pct: float  # 当前占比 %
    target: float | None  # 目标占比 %
    funds: list[str]

    @property
    def diff(self) -> float | None:
        return None if self.target is None else self.pct - self.target


def allocation(rows, ftypes: dict[str, str], tags: dict[str, str], targets: dict[str, float]) -> list[AllocRow]:
    """rows: Summary.rows（有市值的）。设了目标但当前没有持仓的标签也列出来（占 0%）。"""
    total = sum(r.market_value for r in rows if r.market_value) or 0.0
    groups: dict[str, AllocRow] = {}
    for r in rows:
        if not r.market_value:
            continue
        g = group_of(r.code, ftypes.get(r.code, ""), tags)
        a = groups.setdefault(g, AllocRow(g, 0.0, 0.0, None, []))
        a.mv += r.market_value
        a.funds.append(r.name)
    for g, t in targets.items():
        groups.setdefault(g, AllocRow(g, 0.0, 0.0, None, []))
    for a in groups.values():
        a.pct = a.mv / total * 100 if total else 0.0
        a.target = targets.get(a.group)
    return sorted(groups.values(), key=lambda a: (a.target is None, -a.mv))


def drift_level(diff: float, tolerance: float) -> int:
    d = abs(diff)
    if tolerance <= 0 or d < tolerance:
        return 0
    return 1 + int((d - tolerance) // DRIFT_STEP)


def drift_lines(alloc: list[AllocRow], tolerance: float, old_level, save_level) -> list[str]:
    """偏离提醒。old_level/save_level 用于按档位去重（scope = target:标签）。"""
    out = []
    for a in alloc:
        if a.diff is None:
            continue
        scope = f"target:{a.group}"
        lvl, old = drift_level(a.diff, tolerance), old_level(scope)
        if lvl > old:
            word = "高出" if a.diff > 0 else "低于"
            out.append(f"🎯 {a.group} 当前占 {a.pct:.0f}%，你设定的目标是 {a.target:g}%，{word} {abs(a.diff):.0f} 个百分点")
        if lvl != old:
            save_level(scope, lvl)
    return out


def render_alloc(alloc: list[AllocRow], tolerance: float, show_amount: bool = True) -> str:
    if not alloc:
        return "还没有持仓。"
    lines = ["🎯 目标配置"]
    has_target = any(a.target is not None for a in alloc)
    for a in alloc:
        amt = f"｜{a.mv:,.0f}" if show_amount and a.mv else ""
        if a.target is None:
            lines.append(f"· {a.group}：{a.pct:.1f}%{amt}（未设目标）")
        else:
            mark = "⚠️" if abs(a.diff) >= tolerance else "✅"
            lines.append(f"{mark} {a.group}：{a.pct:.1f}% / 目标 {a.target:g}%（{a.diff:+.1f}）{amt}")
        if a.funds:
            lines.append(f"   {'、'.join(a.funds[:4])}{'等' if len(a.funds) > 4 else ''}")
    if not has_target:
        lines.append("还没有设定目标。例如：/fund target 科技主题 30")
    total_t = sum(a.target for a in alloc if a.target is not None)
    if has_target and abs(total_t - 100) > 0.01:
        lines.append(f"目标合计 {total_t:g}%，没设目标的分组不参与偏离判断。")
    lines.append(f"偏离超过 ±{tolerance:g} 个百分点标 ⚠️，并在净值日报里提醒。分组默认按基金类型，可用 /fund tag 自定义。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 压力测试
@dataclass
class Scenario:
    name: str
    kind: str  # industry / qdii / a_share / bond
    shock: float  # 冲击幅度 %，负数表示下跌
    industries: frozenset[str] = frozenset()


PRESETS = [
    Scenario("科技回调", "industry", -20, frozenset({"电子", "通信", "计算机"})),
    Scenario("美股下跌", "qdii", -15),
    Scenario("A股普跌", "a_share", -10),
    Scenario("债市调整", "bond", -3),
]

KEYWORDS = {"美股": "qdii", "海外": "qdii", "QDII": "qdii", "A股": "a_share", "全市场": "a_share", "债市": "bond", "债券": "bond"}
DEFAULT_STOCK_RATIO = {"股票型": 90.0, "混合型": 80.0, "指数型": 95.0, "QDII": 90.0, "FOF": 60.0}


def parse_scenario(target: str, shock: str) -> Scenario | None:
    try:
        v = float(str(shock).replace("%", ""))
    except ValueError:
        return None
    if v > 0:
        v = -v  # 压力测试默认是下跌
    t = (target or "").strip()
    if not t or v <= -100:
        return None
    if t in KEYWORDS:
        return Scenario(f"{t} {v:g}%", KEYWORDS[t], v)
    inds = frozenset(x for x in t.replace("，", ",").replace("、", ",").split(",") if x)
    return Scenario(f"{'、'.join(sorted(inds))} {v:g}%", "industry", v, inds)


def fund_shock(sc: Scenario, ftype: str, holdings: FundHoldings | None, profile: FundProfile | None) -> float:
    """单只基金在情景下的估算涨跌 %。"""
    t = short_type(ftype)
    ratio = profile.stock_ratio if profile and profile.stock_ratio is not None else DEFAULT_STOCK_RATIO.get(t, 0.0)
    if sc.kind == "bond":
        return sc.shock if t == "债券型" else 0.0
    if sc.kind == "qdii":
        return ratio / 100 * sc.shock if t == "QDII" else 0.0
    if sc.kind == "a_share":
        return ratio / 100 * sc.shock if t not in ("QDII", "债券型", "货币型") else 0.0
    # 行业情景：未披露仓位假设与已披露部分行业分布相同
    if not holdings or not holdings.stocks:
        return 0.0
    disclosed = sum(s.weight for s in holdings.stocks)
    hit = sum(s.weight for s in holdings.stocks if s.industry in sc.industries)
    if disclosed <= 0:
        return 0.0
    return ratio / 100 * (hit / disclosed) * sc.shock


@dataclass
class StressResult:
    scenario: Scenario
    loss: float  # 组合估算盈亏（元，负数为亏损）
    pct: float  # 占组合 %
    funds: list[tuple[str, float, float]]  # [(基金名, 涨跌%, 金额)]


def stress(sc: Scenario, rows, ftypes, holdings, profiles) -> StressResult:
    total = sum(r.market_value for r in rows if r.market_value) or 0.0
    funds, loss = [], 0.0
    for r in rows:
        if not r.market_value:
            continue
        pct = fund_shock(sc, ftypes.get(r.code, ""), holdings.get(r.code), profiles.get(r.code))
        amt = r.market_value * pct / 100
        loss += amt
        if abs(pct) > 1e-9:
            funds.append((r.name, pct, amt))
    funds.sort(key=lambda x: x[2])
    return StressResult(sc, loss, loss / total * 100 if total else 0.0, funds)


def render_stress(results: list[StressResult], show_amount: bool = True) -> str:
    lines = ["🧪 情景压力测试", "以下是假设情景下的算术，不是对行情的预测。"]
    for r in results:
        head = f"【{r.scenario.name}】组合 {r.pct:+.2f}%"
        if show_amount:
            head += f"（约 {r.loss:+,.0f} 元）"
        lines.append("")
        lines.append(head)
        if not r.funds:
            lines.append("   持仓不受这个情景影响")
        for name, pct, amt in r.funds[:6]:
            lines.append(f"   {name}：{pct:+.2f}%" + (f"（{amt:+,.0f}）" if show_amount else ""))
    lines.append("")
    lines.append(
        "算法：基金涨跌 = 股票仓位 × 受影响行业在已披露重仓股中的占比 × 冲击幅度；未披露仓位假设行业分布与已披露部分相同。"
        "自定义：/fund stress 通信 -30、/fund stress 美股 -20。"
    )
    return "\n".join(lines)
