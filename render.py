"""把计算结果渲染成聊天文本。"""

from __future__ import annotations

from datetime import datetime

from .portfolio import CN_TZ, Summary

DISCLAIMER = "仅供记录参考，不构成投资建议"


def money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "--"
    s = f"{v:+,.0f}" if sign else f"{v:,.0f}"
    return s


def pct(v: float | None) -> str:
    return "--" if v is None else f"{v:+.2f}%"


def render_board(
    s: Summary, show_amount: bool = True, now: datetime | None = None, title: str = "📊 持仓看板"
) -> str:
    now = now or datetime.now(CN_TZ)
    lines = [f"{title}  {now:%m-%d %H:%M}", "──────────"]
    for i, r in enumerate(s.rows, 1):
        lines.append(f"{i}. {r.name}（{r.code}）")
        if r.market_value is None:
            lines.append("   暂无行情数据")
            continue
        tag = r.price_kind + (f"{r.price_date}" if r.price_date else "")
        if show_amount:
            day_label = "当日" if r.price_date else "今日"
            lines.append(f"   {tag} {pct(r.day_pct)} ｜ 市值 {money(r.market_value)} ｜ {day_label} {money(r.day_profit, True)}")
            lines.append(f"   累计 {money(r.total_profit, True)}（{pct(r.total_pct)}）")
        else:
            lines.append(f"   {tag} {pct(r.day_pct)} ｜ 累计 {pct(r.total_pct)}")
    lines.append("──────────")
    if show_amount:
        lines.append(f"合计 市值 {money(s.market_value)} ｜ 今日 {money(s.day_profit, True)}（{pct(s.day_pct)}）")
        lines.append(f"累计 {money(s.total_profit, True)}（{pct(s.total_pct)}）")
    else:
        lines.append(f"合计 今日 {pct(s.day_pct)} ｜ 累计 {pct(s.total_pct)}")
        lines.append("（群聊中已隐藏金额，私聊可查看完整看板）")
    if s.stale:
        lines.append(f"ℹ️ {'、'.join(s.stale)} 今日净值未更新（如 QDII），其涨跌未计入今日合计")
    if s.missing:
        lines.append(f"⚠️ 以下基金暂无数据，未计入合计：{'、'.join(s.missing)}")
    lines.append(f"估=盘中估值 净=官方净值（带日期表示非今日）。{DISCLAIMER}")
    return "\n".join(lines)


def render_list(holdings, show_amount: bool = True) -> str:
    if not holdings:
        return "还没有持仓。用 /fund add 代码 持有金额 持有收益 添加，或 /fund import 发截图导入。"
    lines = ["📁 我的持仓"]
    for i, h in enumerate(holdings, 1):
        if show_amount:
            lines.append(f"{i}. {h.name}（{h.code}） 份额 {h.shares:,.2f} ｜ 成本 {h.cost:,.2f}")
        else:
            lines.append(f"{i}. {h.name}（{h.code}）")
    return "\n".join(lines)


def render_trades(trades, show_amount: bool = True) -> str:
    if not trades:
        return "还没有交易记录。用 /fund buy、/fund sell 记录加仓和赎回。"
    lines = ["🧾 最近交易"]
    for t in trades:
        day = datetime.fromtimestamp(t.ts, CN_TZ).strftime("%m-%d")
        kind = "买入" if t.kind == "buy" else "卖出"
        if show_amount:
            extra = f" ｜ 实现盈亏 {t.realized:+,.2f}" if t.kind == "sell" else ""
            lines.append(f"{day} {kind} {t.name}（{t.code}） 金额 {t.amount:,.2f} ｜ 份额 {t.shares:,.2f} ｜ 净值 {t.nav}{extra}")
        else:
            lines.append(f"{day} {kind} {t.name}（{t.code}）")
    return "\n".join(lines)


def _short(name: str, n: int = 12) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def render_overlap(a, show_amount: bool = True) -> str:
    if not a.funds:
        return "还没有持仓。"
    lines = ["🔍 重仓股穿透与重叠分析"]
    if a.report_dates:
        lines.append(f"数据来源：基金季报前十大重仓股（最新截至 {a.report_dates[0]}），覆盖组合约 {a.disclosed_pct:.0f}% 的市值")
    lines.append("")
    lines.append("【穿透后个股暴露 Top 10】")
    if not a.stocks:
        lines.append("没有取到重仓股数据（债券、货币基金没有股票持仓）")
    for k, e in enumerate(a.stocks[:10], 1):
        amt = f"{e.amount:,.0f} 元｜" if show_amount else ""
        lines.append(f"{k}. {e.name}  {amt}占组合 {e.pct:.1f}%｜{len(e.funds)} 只基金持有")
    lines.append("")
    lines.append("【行业分布（占已披露重仓股）】")
    for e in a.industries[:6]:
        lines.append(f"{e.name} {e.pct:.0f}%")
    lines.append("")
    lines.append("【基金两两重叠度】")
    if not a.pairs:
        lines.append("各基金的前十大重仓股没有重合")
    for p in a.pairs[:6]:
        lines.append(f"{_short(p.a)} × {_short(p.b)}：{p.overlap:.1f}%（共同：{'、'.join(p.common[:4])}{'等' if len(p.common) > 4 else ''}）")
    lines.append("重叠度 = 共同重仓股在两只基金里占净值比例的较小值之和。")
    return "\n".join(lines)


def render_check(a, summary, show_amount: bool = True) -> str:
    if not a.funds:
        return "还没有持仓。"
    lines = ["🩺 组合体检"]
    head = f"{len(a.funds)} 只基金｜有效基金数 {a.effective_funds:.1f}"
    if show_amount:
        head += f"｜市值 {summary.market_value:,.0f}｜累计 {summary.total_profit:+,.0f}（{pct(summary.total_pct)}）"
    lines.append(head)
    lines.append("")
    lines.append("【发现】")
    lines.extend(f"• {f}" for f in a.flags) if a.flags else lines.append("• 没有发现明显的集中或重叠情况")
    lines.append("")
    lines.append("【持仓结构】")
    lines.append("类型：" + "｜".join(f"{t} {v:.0f}%" for t, v in a.type_mix))
    if a.industries:
        lines.append("行业：" + "｜".join(f"{e.name} {e.pct:.0f}%" for e in a.industries[:4]))
    if a.stocks:
        lines.append("个股：" + "｜".join(f"{e.name} {e.pct:.1f}%" for e in a.stocks[:4]))
    lines.append("")
    lines.append("【单只基金】")
    for f in a.funds:
        parts = [f"占 {f.weight:.0f}%"]
        if f.return_1y is not None:
            parts.append(f"近1年 {f.return_1y:+.1f}%")
        if f.drawdown_1y is not None:
            parts.append(f"回撤 {f.drawdown_1y:.0f}%")
        if f.stock_ratio is not None:
            parts.append(f"股票仓位 {f.stock_ratio:.0f}%")
        if f.managers:
            name, days = max(f.managers, key=lambda m: m[1])
            parts.append(f"经理 {name} {days / 365:.1f}年")
        lines.append(f"{_short(f.name, 14)}：" + "｜".join(parts))
    lines.append("")
    lines.append("说明：穿透只基于季报前十大重仓股；回撤为近一年累计净值较高点。" + DISCLAIMER)
    return "\n".join(lines)


HELP = """📊 基金持仓看板 指令
【持仓】
/fund board        持仓看板（别名 /fund 看板）
/fund list         持仓明细
/fund add 代码 持有金额 [持有收益]
                   例：/fund add 016370 41200 -23500（照抄 App 持仓页）
/fund del 代码     删除一只
/fund clear        清空全部持仓
/fund import       截图一键导入
【交易】
/fund buy 代码 金额 [净值]     记录加仓，不填净值按最新净值
/fund sell 代码 份额|全部 [净值] 记录赎回，按平均成本计算实现盈亏
/fund trades [代码]            最近交易记录
【推送与提醒】
/fund push on|off  在当前会话开启/关闭每日推送
/fund push now     立即推送一次日报（测试用）
/fund alert        查看提醒设置
/fund alert day 3  组合单日跌幅超过 3% 时提醒（0 关闭）
/fund alert dd 20  组合或单只基金回撤超过 20% 时提醒（0 关闭）
【分析】
/fund overlap      重仓股穿透与基金重叠分析
/fund check        组合体检（集中度、行业、回撤、经理）
/fund board img    图片版看板（check img 同理）
/fund weekly       本周周报（含 AI 点评）
【其他】
/fund info 代码    单只基金净值与阶段收益
/fund help         显示本帮助
也可以直接问机器人：我哪只基金亏得最多？我的持仓集中吗？"""
