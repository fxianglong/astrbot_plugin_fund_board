"""v0.8 周边功能：费用透视、基金异动、自选、备份导出导入。纯函数。"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime

from .datasource import FundBasic, FundFees, FundMeta
from .portfolio import CN_TZ

# ---------------------------------------------------------------- 费用透视


def counterpart(name: str, funds: list[FundMeta]) -> FundMeta | None:
    """同一基金的另一类份额（A↔C）。"""
    m = re.match(r"^(.*?)([AC])$", (name or "").strip())
    if not m:
        return None
    other = m.group(1) + ("C" if m.group(2) == "A" else "A")
    return next((f for f in funds if f.name == other), None)


def breakeven_days(a_buy_rate: float | None, c_service: float | None) -> float | None:
    """A 类一次性申购费 vs C 类按年收的销售服务费：持有超过多少天 A 类更省（不计赎回费差异）。"""
    if a_buy_rate is None or not c_service or c_service <= 0:
        return None
    return a_buy_rate / c_service * 365


@dataclass
class FeeRow:
    code: str
    name: str
    mv: float
    fees: FundFees | None
    yearly_amt: float | None
    compare: str  # A/C 对比说明


def fee_rows(rows, fees: dict[str, FundFees], basics: dict[str, FundBasic], pairs: dict[str, tuple]) -> list[FeeRow]:
    """pairs: code -> (另一类的 FundMeta, 另一类的 FundFees, 另一类的 FundBasic)。"""
    out = []
    for r in rows:
        if not r.market_value:
            continue
        f = fees.get(r.code)
        yearly = r.market_value * f.yearly / 100 if f else None
        compare = ""
        pair = pairs.get(r.code)
        if f and pair and pair[1]:
            other, of, ob = pair
            me_c = bool(f.service)
            a_fee, a_basic = (of, ob) if me_c else (f, basics.get(r.code))
            c_fee = f if me_c else of
            days = breakeven_days(a_basic.buy_rate if a_basic else None, c_fee.service)
            if days is not None:
                compare = (f"对应{'A' if me_c else 'C'}类 {other.code}：A 类申购费 {a_basic.buy_rate:g}%，"
                           f"C 类销售服务费 {c_fee.service:g}%/年，持有超过约 {days:.0f} 天 A 类更省（不计赎回费差异）")
        out.append(FeeRow(r.code, r.name, r.market_value, f, yearly, compare))
    return sorted(out, key=lambda x: -(x.yearly_amt or 0))


def render_fees(rows: list[FeeRow], show_amount: bool = True) -> str:
    if not rows:
        return "还没有持仓。"
    lines = ["💸 费用透视（每年从基金资产中扣除，已体现在净值里）"]
    total = 0.0
    for r in rows:
        if not r.fees:
            lines.append(f"· {r.name}：暂时取不到费率")
            continue
        f = r.fees
        parts = [f"管理 {f.manage:g}%" if f.manage is not None else "", f"托管 {f.custody:g}%" if f.custody is not None else ""]
        if f.service:
            parts.append(f"销售服务 {f.service:g}%")
        detail = " + ".join(p for p in parts if p)
        amt = f"，约 {r.yearly_amt:,.0f} 元/年" if show_amount and r.yearly_amt is not None else ""
        lines.append(f"· {r.name}：{detail} = {f.yearly:.2f}%/年{amt}")
        if r.compare:
            lines.append(f"   {r.compare}")
        short = [t for t in f.redeem if t[1] > 0]
        if short:
            lines.append("   赎回费：" + "，".join(f"{d} {v:g}%" for d, v in short))
        total += r.yearly_amt or 0
    if show_amount:
        mv = sum(r.mv for r in rows)
        lines.append(f"合计约 {total:,.0f} 元/年（占持仓 {total / mv * 100:.2f}%）" if mv else "")
    lines.append("费率来自天天基金，以基金合同和销售平台为准。不构成投资建议。")
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------- 基金异动
def state_of(managers: list[tuple[str, int]] | None, basic: FundBasic | None, dividends: list[tuple[int, str]] | None) -> dict:
    s = {}
    if managers is not None:
        s["managers"] = sorted(n for n, _ in managers)
    if basic is not None:
        s["buy"] = basic.buy_status
        s["sell"] = basic.sell_status
        s["max_buy"] = basic.max_buy
    if dividends:
        ts, text = dividends[-1]
        s["dividend"] = [datetime.fromtimestamp(ts / 1000, CN_TZ).strftime("%Y-%m-%d"), text]
    return s


def diff_state(old: dict | None, new: dict, name: str) -> list[str]:
    """和上次记录的状态比，返回变化描述。第一次记录不报。"""
    if not old:
        return []
    out = []
    if "managers" in new and "managers" in old and new["managers"] != old["managers"]:
        gone = [m for m in old["managers"] if m not in new["managers"]]
        came = [m for m in new["managers"] if m not in old["managers"]]
        parts = []
        if came:
            parts.append(f"新任 {'、'.join(came)}")
        if gone:
            parts.append(f"离任 {'、'.join(gone)}")
        out.append(f"👤 {name} 基金经理变更：{'，'.join(parts)}")
    if new.get("buy") and old.get("buy") and new["buy"] != old["buy"]:
        out.append(f"🚪 {name} 申购状态：{old['buy']} → {new['buy']}")
    elif new.get("max_buy") and old.get("max_buy") and new["max_buy"] != old["max_buy"] and new.get("buy") == old.get("buy"):
        out.append(f"🚪 {name} 申购上限变化：{old['max_buy']} → {new['max_buy']}")
    if new.get("sell") and old.get("sell") and new["sell"] != old["sell"]:
        out.append(f"🚪 {name} 赎回状态：{old['sell']} → {new['sell']}")
    if new.get("dividend") and new.get("dividend") != old.get("dividend"):
        d, text = new["dividend"]
        out.append(f"💰 {name} {d}：{text}")
    return out


# ---------------------------------------------------------------- 自选
def watch_line(code: str, name: str, q, alert: float | None) -> str:
    if not q:
        return f"· {name}（{code}）：暂无数据"
    if q.est is not None and q.est_pct is not None:
        val = f"估 {q.est_pct:+.2f}%（{q.est_time[11:] if q.est_time else ''}）"
    elif q.nav_pct is not None:
        val = f"净 {q.nav_pct:+.2f}%（{q.nav_date[5:] if q.nav_date else ''}）"
    else:
        val = "--"
    tail = f"｜涨跌超 {alert:g}% 提醒" if alert else ""
    return f"· {q.name or name}（{code}）：{val}{tail}"


# ---------------------------------------------------------------- 备份
def pack_zip(data: dict[str, list[dict]], meta: dict) -> bytes:
    """每张表一个 CSV（UTF-8 BOM，Excel 可直接打开），外加 meta.csv。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for table, rows in data.items():
            s = io.StringIO()
            cols = list(rows[0].keys()) if rows else []
            w = csv.DictWriter(s, fieldnames=cols)
            if cols:
                w.writeheader()
                w.writerows(rows)
            z.writestr(f"{table}.csv", "﻿" + s.getvalue())
        s = io.StringIO()
        w = csv.writer(s)
        for k, v in meta.items():
            w.writerow([k, v])
        z.writestr("meta.csv", "﻿" + s.getvalue())
    return buf.getvalue()


def unpack_zip(path: str) -> tuple[dict[str, list[dict]], dict]:
    data, meta = {}, {}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "meta.csv" not in names:
            raise ValueError("不是本插件导出的备份文件")
        for n in names:
            if not n.endswith(".csv") or "/" in n or ".." in n:
                continue
            text = z.read(n).decode("utf-8-sig")
            if n == "meta.csv":
                meta = {r[0]: r[1] for r in csv.reader(io.StringIO(text)) if len(r) >= 2}
            else:
                data[n[:-4]] = list(csv.DictReader(io.StringIO(text)))
    if meta.get("plugin") != "astrbot_plugin_fund_board":
        raise ValueError("不是本插件导出的备份文件")
    return data, meta
