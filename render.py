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


def render_board(s: Summary, show_amount: bool = True, now: datetime | None = None) -> str:
    now = now or datetime.now(CN_TZ)
    lines = [f"📊 持仓看板  {now:%m-%d %H:%M}", "──────────"]
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


HELP = """📊 基金持仓看板 指令
/fund board        持仓看板（别名 /fund 看板）
/fund list         查看持仓明细
/fund add 代码 持有金额 [持有收益]
                   例：/fund add 016370 41200 -23500
                   金额和收益照抄 App 持仓页即可，份额按最新净值自动换算
/fund del 代码     删除一只
/fund clear        清空全部持仓
/fund info 代码    查看单只基金净值与阶段收益
/fund import       截图一键导入（支付宝/天天基金/银行 App 持仓页）
/fund help         显示本帮助"""
