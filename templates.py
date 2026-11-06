"""图片版看板与体检报告的 HTML 模板（Jinja2），由 AstrBot 的 html_render 渲染成图片。

配色遵循 A 股习惯：红涨绿跌。
"""

from __future__ import annotations

from datetime import datetime

from .portfolio import CN_TZ

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #f2f3f5; font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
       color: #1f2329; padding: 24px; width: 760px; margin: 0 auto; }
html { background: #f2f3f5; }
.card { background: #fff; border-radius: 14px; padding: 22px 24px; margin-bottom: 16px; }
.head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 14px; }
.title { font-size: 26px; font-weight: 700; }
.time { font-size: 15px; color: #8f959e; }
.kpis { display: flex; gap: 12px; }
.kpi { flex: 1; background: #f7f8fa; border-radius: 10px; padding: 12px 14px; }
.kpi .l { font-size: 13px; color: #8f959e; }
.kpi .v { font-size: 24px; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }
.kpi .s { font-size: 14px; margin-top: 2px; font-variant-numeric: tabular-nums; }
.up { color: #e5383b; } .down { color: #16a34a; } .flat { color: #646a73; }
.sec { font-size: 17px; font-weight: 700; margin-bottom: 12px; }
.row { display: flex; align-items: center; padding: 11px 0; border-top: 1px solid #f0f1f3; }
.row:first-of-type { border-top: none; }
.name { flex: 1; min-width: 0; }
.name .n { font-size: 16px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.name .c { font-size: 12px; color: #8f959e; margin-top: 2px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #eef0f3; color: #646a73; margin-left: 6px; font-weight: 400; }
.num { width: 112px; text-align: right; font-variant-numeric: tabular-nums; }
.num .a { font-size: 16px; font-weight: 600; }
.num .b { font-size: 12px; margin-top: 2px; }
.colhead { display: flex; font-size: 12px; color: #8f959e; padding-bottom: 6px; }
.colhead .name { font-size: 12px; }
.tbl .num { width: 92px; }
.foot { font-size: 12px; color: #8f959e; line-height: 1.7; }
.flag { font-size: 15px; line-height: 1.6; padding: 8px 12px; background: #fff7e6; border-left: 3px solid #f59e0b; border-radius: 6px; margin-bottom: 8px; }
.ok { background: #f0fdf4; border-left-color: #16a34a; }
.bar { display: flex; align-items: center; margin-bottom: 9px; font-size: 14px; }
.bar .l { width: 150px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar .t { flex: 1; height: 12px; background: #f0f1f3; border-radius: 6px; margin: 0 10px; overflow: hidden; }
.bar .f { height: 100%; background: #3b6fd8; border-radius: 6px; }
.grid2 .bar .l { width: 76px; }
.bar .v { width: 64px; text-align: right; font-variant-numeric: tabular-nums; color: #646a73; }
.grid2 { display: flex; gap: 16px; }
.grid2 > .card { flex: 1; }
.pair { font-size: 14px; padding: 8px 0; border-top: 1px solid #f0f1f3; line-height: 1.5; }
.pair:first-of-type { border-top: none; }
.pair .p { font-weight: 700; color: #3b6fd8; }
.small { font-size: 12px; color: #8f959e; }
"""

BOARD_TMPL = (
    "<html><head><meta charset='utf-8'><style>"
    + BASE_CSS
    + """</style></head><body>
<div class="card">
  <div class="head"><div class="title">{{ title }}</div><div class="time">{{ time }}</div></div>
  <div class="kpis">
    {% if show_amount %}
    <div class="kpi"><div class="l">总市值</div><div class="v">{{ total.mv }}</div></div>
    <div class="kpi"><div class="l">今日盈亏</div><div class="v {{ total.day_cls }}">{{ total.day }}</div><div class="s {{ total.day_cls }}">{{ total.day_pct }}</div></div>
    <div class="kpi"><div class="l">累计盈亏</div><div class="v {{ total.tot_cls }}">{{ total.tot }}</div><div class="s {{ total.tot_cls }}">{{ total.tot_pct }}</div></div>
    {% else %}
    <div class="kpi"><div class="l">今日涨跌</div><div class="v {{ total.day_cls }}">{{ total.day_pct }}</div></div>
    <div class="kpi"><div class="l">累计收益率</div><div class="v {{ total.tot_cls }}">{{ total.tot_pct }}</div></div>
    {% endif %}
  </div>
</div>
<div class="card">
  <div class="colhead"><div class="name">基金</div><div class="num">当日涨跌</div>
    {% if show_amount %}<div class="num">市值 / 今日</div>{% endif %}<div class="num">{% if show_amount %}累计盈亏{% else %}累计收益率{% endif %}</div></div>
  {% for r in rows %}
  <div class="row">
    <div class="name"><div class="n">{{ r.name }}</div><div class="c">{{ r.code }}<span class="tag">{{ r.tag }}</span></div></div>
    {% if r.missing %}
    <div class="num flat">暂无数据</div>
    {% else %}
    <div class="num"><div class="a {{ r.day_cls }}">{{ r.day_pct }}</div></div>
    {% if show_amount %}<div class="num"><div class="a">{{ r.mv }}</div><div class="b {{ r.day_cls }}">{{ r.day }}</div></div>{% endif %}
    <div class="num"><div class="a {{ r.tot_cls }}">{% if show_amount %}{{ r.tot }}{% else %}{{ r.tot_pct }}{% endif %}</div>
      {% if show_amount %}<div class="b {{ r.tot_cls }}">{{ r.tot_pct }}</div>{% endif %}</div>
    {% endif %}
  </div>
  {% endfor %}
</div>
<div class="foot">{% for n in notes %}{{ n }}<br>{% endfor %}</div>
</body></html>"""
)

CHECK_TMPL = (
    "<html><head><meta charset='utf-8'><style>"
    + BASE_CSS
    + """</style></head><body>
<div class="card">
  <div class="head"><div class="title">🩺 组合体检</div><div class="time">{{ time }}</div></div>
  <div class="kpis">
    <div class="kpi"><div class="l">基金数 / 有效基金数</div><div class="v">{{ n_funds }} / {{ effective }}</div></div>
    {% if show_amount %}<div class="kpi"><div class="l">总市值</div><div class="v">{{ mv }}</div></div>{% endif %}
    <div class="kpi"><div class="l">累计收益率</div><div class="v {{ tot_cls }}">{{ tot_pct }}</div></div>
  </div>
</div>
<div class="card">
  <div class="sec">发现</div>
  {% for f in flags %}<div class="flag">{{ f }}</div>{% else %}<div class="flag ok">没有发现明显的集中或重叠情况</div>{% endfor %}
</div>
<div class="grid2">
  <div class="card"><div class="sec">行业分布 <span class="small">占已披露重仓股</span></div>
    {% for b in industries %}<div class="bar"><div class="l">{{ b.name }}</div><div class="t"><div class="f" style="width: {{ b.w }}%"></div></div><div class="v">{{ b.v }}</div></div>{% endfor %}
  </div>
  <div class="card"><div class="sec">基金类型</div>
    {% for b in types %}<div class="bar"><div class="l">{{ b.name }}</div><div class="t"><div class="f" style="width: {{ b.w }}%"></div></div><div class="v">{{ b.v }}</div></div>{% endfor %}
  </div>
</div>
<div class="card"><div class="sec">穿透后个股暴露 <span class="small">占组合市值</span></div>
  {% for b in stocks %}<div class="bar"><div class="l">{{ b.name }}</div><div class="t"><div class="f" style="width: {{ b.w }}%"></div></div><div class="v">{{ b.v }}</div></div>{% else %}<div class="small">没有重仓股数据</div>{% endfor %}
</div>
<div class="card"><div class="sec">基金两两重叠度</div>
  {% for p in pairs %}<div class="pair"><span class="p">{{ p.overlap }}</span>　{{ p.a }} × {{ p.b }}<div class="small">共同重仓：{{ p.common }}</div></div>{% else %}<div class="small">各基金前十大重仓股没有重合</div>{% endfor %}
</div>
<div class="card tbl"><div class="sec">单只基金</div>
  <div class="colhead"><div class="name">基金</div><div class="num">占组合</div><div class="num">近1年</div><div class="num">近1年回撤</div><div class="num">现任经理</div></div>
  {% for f in funds %}
  <div class="row"><div class="name"><div class="n">{{ f.name }}</div><div class="c">{{ f.code }}<span class="tag">{{ f.type }}</span>{% if f.stock_ratio %}<span class="tag">股票仓位 {{ f.stock_ratio }}</span>{% endif %}</div></div>
    <div class="num"><div class="a">{{ f.weight }}</div></div>
    <div class="num"><div class="a {{ f.r1y_cls }}">{{ f.r1y }}</div></div>
    <div class="num"><div class="a">{{ f.dd }}</div></div>
    <div class="num"><div class="a" style="font-size:14px">{{ f.mgr }}</div></div>
  </div>
  {% endfor %}
</div>
<div class="foot">{% for n in notes %}{{ n }}<br>{% endfor %}</div>
</body></html>"""
)


def _cls(v: float | None) -> str:
    if v is None or abs(v) < 1e-9:
        return "flat"
    return "up" if v > 0 else "down"


def _pct(v: float | None, sign: bool = True) -> str:
    if v is None:
        return "--"
    return f"{v:+.2f}%" if sign else f"{v:.1f}%"


def _money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "--"
    return f"{v:+,.0f}" if sign else f"{v:,.0f}"


def board_data(summary, show_amount: bool, title: str, now: datetime | None = None, realized: float = 0.0) -> dict:
    now = now or datetime.now(CN_TZ)
    rows = []
    for r in summary.rows:
        tag = "估值" if r.price_kind == "估" else (f"净值 {r.price_date}" if r.price_date else "净值")
        rows.append(
            dict(
                name=r.name, code=r.code, tag=tag, missing=r.market_value is None,
                day_pct=_pct(r.day_pct), day_cls=_cls(r.day_pct),
                mv=_money(r.market_value), day=_money(r.day_profit, True),
                tot=_money(r.total_profit, True), tot_pct=_pct(r.total_pct), tot_cls=_cls(r.total_profit),
            )
        )
    notes = []
    if summary.stale:
        notes.append(f"{'、'.join(summary.stale)} 今日净值未更新（如 QDII），涨跌未计入今日合计")
    if summary.missing:
        notes.append(f"{'、'.join(summary.missing)} 暂无行情数据，未计入合计")
    if realized and show_amount:
        notes.append(f"已实现盈亏（卖出记录合计）：{realized:+,.2f}")
    notes.append("估值=盘中估值，净值=官方净值。仅供记录参考，不构成投资建议。")
    return dict(
        title=title, time=now.strftime("%Y-%m-%d %H:%M"), show_amount=show_amount, rows=rows, notes=notes,
        total=dict(
            mv=_money(summary.market_value), day=_money(summary.day_profit, True),
            day_pct=_pct(summary.day_pct), day_cls=_cls(summary.day_profit),
            tot=_money(summary.total_profit, True), tot_pct=_pct(summary.total_pct), tot_cls=_cls(summary.total_profit),
        ),
    )


def _bars(items: list[tuple[str, float]], fmt: str = "{:.0f}%") -> list[dict]:
    top = max((v for _, v in items), default=0) or 1
    return [dict(name=n, w=round(v / top * 100, 1), v=fmt.format(v)) for n, v in items]


def check_data(a, summary, show_amount: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(CN_TZ)
    funds = []
    for f in a.funds:
        mgr = "--"
        if f.managers:
            name, days = max(f.managers, key=lambda m: m[1])
            mgr = f"{name} {days / 365:.1f}年"
        funds.append(
            dict(
                name=f.name, code=f.code, type=f.ftype, weight=f"{f.weight:.0f}%",
                r1y=(f"{f.return_1y:+.1f}%" if f.return_1y is not None else "--"), r1y_cls=_cls(f.return_1y),
                dd=f"{f.drawdown_1y:.0f}%" if f.drawdown_1y is not None else "--",
                stock_ratio=f"{f.stock_ratio:.0f}%" if f.stock_ratio is not None else "",
                mgr=mgr,
            )
        )
    pairs = [
        dict(a=p.a, b=p.b, overlap=f"{p.overlap:.1f}%",
             common="、".join(p.common[:5]) + ("等" if len(p.common) > 5 else ""))
        for p in a.pairs[:5]
    ]
    notes = []
    if a.report_dates:
        notes.append(f"穿透数据来自基金季报前十大重仓股（最新截至 {a.report_dates[0]}），覆盖组合约 {a.disclosed_pct:.0f}% 的市值。")
    notes.append("重叠度 = 共同重仓股在两只基金中占净值比例的较小值之和；回撤为近一年累计净值较最高点的跌幅。")
    notes.append("有效基金数 = 1 ÷ Σ(各基金占比²)，越接近基金只数说明资金越分散。仅供参考，不构成投资建议。")
    return dict(
        time=now.strftime("%Y-%m-%d %H:%M"), show_amount=show_amount,
        n_funds=len(a.funds), effective=f"{a.effective_funds:.1f}",
        mv=_money(summary.market_value), tot_pct=_pct(summary.total_pct), tot_cls=_cls(summary.total_profit),
        flags=a.flags,
        industries=_bars([(e.name, e.pct) for e in a.industries[:6]]),
        types=_bars(a.type_mix),
        stocks=_bars([(e.name, e.pct) for e in a.stocks[:8]], "{:.1f}%"),
        pairs=pairs, funds=funds, notes=notes,
    )


# ---------------------------------------------------------------- 收益表现
PERF_TMPL = (
    "<html><head><meta charset='utf-8'><style>"
    + BASE_CSS
    + """
.legend { display: flex; gap: 18px; font-size: 13px; color: #646a73; margin-top: 8px; }
.legend i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
.brow { display: flex; justify-content: space-between; font-size: 15px; padding: 9px 0; border-top: 1px solid #f0f1f3; }
.brow:first-of-type { border-top: none; }
</style></head><body>
<div class="card">
  <div class="head"><div class="title">📈 收益表现 · {{ range }}</div><div class="time">{{ time }}</div></div>
  <div class="kpis">
    <div class="kpi"><div class="l">组合涨跌（时间加权）</div><div class="v {{ twr_cls }}">{{ twr }}</div></div>
    <div class="kpi"><div class="l">年化（XIRR）</div><div class="v {{ xirr_cls }}">{{ xirr }}</div></div>
    <div class="kpi"><div class="l">区间最大回撤</div><div class="v">{{ mdd }}</div></div>
  </div>
</div>
{% if svg %}
<div class="card"><div class="sec">组合与基准走势 <span class="small">区间起点 = 1</span></div>
  {{ svg | safe }}
  <div class="legend">{% for l in legend %}<span><i style="background: {{ l.color }}"></i>{{ l.name }}</span>{% endfor %}</div>
</div>
{% endif %}
<div class="card"><div class="sec">同期基准</div>
  {% for b in benches %}<div class="brow"><span>{{ b.name }}<span class="small">　{{ b.code }}</span></span><span class="{{ b.cls }}">{{ b.ret }}　<span class="small">{{ b.diff }}</span></span></div>{% endfor %}
  {% if show_amount and profit %}<div class="brow"><span>区间盈亏</span><span class="{{ profit_cls }}">{{ profit }}</span></div>{% endif %}
</div>
<div class="foot">{% for n in notes %}{{ n }}<br>{% endfor %}</div>
</body></html>"""
)

LINE_COLORS = ["#3b6fd8", "#f59e0b", "#8b5cf6", "#10b981"]


def _svg_lines(series: list[list[float]], width: int = 680, height: int = 220) -> str:
    vals = [v for s in series for v in s]
    if not vals or max(len(s) for s in series) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi, lo = hi + 0.01, lo - 0.01
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    n = max(len(s) for s in series)

    def xy(i, v):
        return 40 + i * (width - 50) / (n - 1), 10 + (hi - v) / (hi - lo) * (height - 30)

    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    for k in range(5):  # 横向网格和刻度
        v = lo + (hi - lo) * k / 4
        _, y = xy(0, v)
        parts.append(f'<line x1="40" x2="{width - 10}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eef0f3"/>')
        parts.append(f'<text x="0" y="{y + 4:.1f}" font-size="11" fill="#8f959e">{v:.2f}</text>')
    _, y1 = xy(0, 1.0)
    if lo < 1.0 < hi:
        parts.append(f'<line x1="40" x2="{width - 10}" y1="{y1:.1f}" y2="{y1:.1f}" stroke="#c9cdd4" stroke-dasharray="4 4"/>')
    for idx, s in enumerate(series):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, v) for i, v in enumerate(s)))
        w = 3 if idx == 0 else 2
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{LINE_COLORS[idx % 4]}" stroke-width="{w}" stroke-linejoin="round"/>')
    parts.append("</svg>")
    return "".join(parts)


def perf_data(p, show_amount: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(CN_TZ)
    series = [[v for _, v in p.curve]] if p.curve else []
    legend = [dict(name="我的组合", color=LINE_COLORS[0])] if p.curve else []
    for k, (name, curve) in enumerate(p.bench_curves.items(), 1):
        series.append([v for _, v in curve])
        legend.append(dict(name=name, color=LINE_COLORS[k % 4]))
    benches = []
    for b in p.benches:
        diff = ""
        if b.ret is not None and p.twr is not None:
            d = p.twr - b.ret
            diff = f"组合{'跑赢' if d >= 0 else '跑输'} {abs(d):.2f} 个百分点"
        benches.append(dict(name=b.name, code=b.code, ret=_pct(b.ret), cls=_cls(b.ret), diff=diff))
    notes = []
    if p.start:
        notes.append(f"区间 {p.start} 起，共 {p.days} 个记录日。年化需要至少 30 天的记录。")
    notes.append("基准以指数联接基金的累计净值近似。收益从开始在插件里记录时算起。仅供参考，不构成投资建议。")
    return dict(
        range=p.range_name, time=now.strftime("%Y-%m-%d %H:%M"), show_amount=show_amount,
        twr=_pct(p.twr), twr_cls=_cls(p.twr), xirr=_pct(p.xirr), xirr_cls=_cls(p.xirr),
        mdd=f"{p.mdd:.2f}%" if p.mdd is not None else "--",
        svg=_svg_lines(series) if series else "", legend=legend, benches=benches,
        profit=_money(p.profit, True) if p.profit is not None else "", profit_cls=_cls(p.profit), notes=notes,
    )


# ---------------------------------------------------------------- 目标配置
TARGET_TMPL = (
    "<html><head><meta charset='utf-8'><style>"
    + BASE_CSS
    + """
.trow { padding: 12px 0; border-top: 1px solid #f0f1f3; }
.trow:first-of-type { border-top: none; }
.tl { display: flex; justify-content: space-between; font-size: 15px; margin-bottom: 6px; }
.track { position: relative; height: 14px; background: #f0f1f3; border-radius: 7px; }
.fill { position: absolute; left: 0; top: 0; height: 100%; border-radius: 7px; background: #3b6fd8; }
.fill.off { background: #f59e0b; }
.mark { position: absolute; top: -4px; width: 3px; height: 22px; background: #1f2329; border-radius: 2px; }
</style></head><body>
<div class="card">
  <div class="head"><div class="title">🎯 目标配置</div><div class="time">{{ time }}</div></div>
  {% for r in rows %}
  <div class="trow">
    <div class="tl"><span><b>{{ r.group }}</b>　<span class="small">{{ r.funds }}</span></span>
      <span>{{ r.pct }}{% if r.target %}<span class="small">　目标 {{ r.target }}</span>　<span class="{{ r.cls }}">{{ r.diff }}</span>{% endif %}</span></div>
    <div class="track"><div class="fill {{ 'off' if r.off else '' }}" style="width: {{ r.w }}%"></div>
      {% if r.tw is not none %}<div class="mark" style="left: calc({{ r.tw }}% - 1px)"></div>{% endif %}</div>
  </div>
  {% endfor %}
</div>
<div class="foot">{% for n in notes %}{{ n }}<br>{% endfor %}</div>
</body></html>"""
)


def target_data(alloc, tolerance: float, now: datetime | None = None) -> dict:
    now = now or datetime.now(CN_TZ)
    rows = []
    for a in alloc:
        off = a.diff is not None and abs(a.diff) >= tolerance
        rows.append(dict(
            group=a.group,
            funds="、".join(f if len(f) <= 10 else f[:9] + "…" for f in a.funds[:2]) + ("等" if len(a.funds) > 2 else ""),
            pct=f"{a.pct:.1f}%", w=round(min(a.pct, 100), 1),
            target=f"{a.target:g}%" if a.target is not None else "",
            tw=round(min(a.target, 100), 1) if a.target is not None else None,
            diff=f"{a.diff:+.1f}" if a.diff is not None else "", off=off,
            cls="up" if off and a.diff > 0 else ("down" if off else "flat"),
        ))
    notes = [f"黑色竖线为你设定的目标位置，偏离超过 ±{tolerance:g} 个百分点显示为橙色。",
             "偏离只对照你自己设定的目标，不构成操作建议。"]
    return dict(time=now.strftime("%Y-%m-%d %H:%M"), rows=rows, notes=notes)
