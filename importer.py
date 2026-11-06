"""截图导入：大模型提取 → 名称匹配基金代码 → 草稿确认 → 换算份额和成本。

本文件不依赖 AstrBot，方便单独测试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .datasource import FundMeta, Quote

try:  # rapidfuzz 更快更准；没装时退回标准库 difflib
    from rapidfuzz import fuzz, process

    HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    import difflib

    HAS_RAPIDFUZZ = False

EXTRACT_PROMPT = """你是基金持仓截图解析器。图片来自支付宝、天天基金、银行等 App 的基金持仓页面，可能有多张且内容有重叠。
识别图中每一只基金，合并重复项，只输出一个 JSON 数组，不要输出任何解释或 Markdown：
[{"name": "基金名称，按图中原样，被截断就保留截断的样子",
  "code": "6位基金代码，图中没有则为 null",
  "amount": 持有金额（数字，单位元）,
  "profit": 持有收益（数字，亏损为负；注意不是昨日收益），
  "shares": 持有份额（图中没有则为 null）,
  "pending": 是否标注"确认中/待确认/交易中"（true 或 false）}]
规则：
1. 金额去掉逗号和"元"；带"万"的换算成元。
2. 看不清的字段填 null，不要猜。
3. 只识别基金，忽略余额宝、股票、理财、广告等。
4. 图中没有基金时输出 []。"""


# ---------------------------------------------------------------- 解析大模型输出
def parse_llm_json(text: str) -> list[dict]:
    if not text:
        raise ValueError("模型没有返回内容")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("模型返回的不是 JSON 数组")
    data = json.loads(t[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("模型返回的不是 JSON 数组")
    items = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        code = d.get("code")
        code = str(code).strip() if code not in (None, "", "null") else None
        if code and not re.fullmatch(r"\d{6}", code):
            code = None
        if not name and not code:
            continue
        items.append(
            {
                "name": name,
                "code": code,
                "amount": _to_money(d.get("amount")),
                "profit": _to_money(d.get("profit")),
                "shares": _to_money(d.get("shares")),
                "pending": bool(d.get("pending")),
            }
        )
    return items


def _to_money(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("，", "").replace("元", "").replace("+", "").strip()
    mult = 1.0
    if s.endswith("万"):
        mult, s = 10000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


# ---------------------------------------------------------------- 名称匹配
_FW = str.maketrans("（）［］【】　", "()[][] ")


def normalize_name(s: str) -> str:
    s = (s or "").translate(_FW)
    s = re.sub(r"\s+", "", s)
    s = s.rstrip(".…")
    return s.upper()


class FundMatcher:
    def __init__(self, funds: list[FundMeta]):
        self.funds = funds
        self.by_code = {f.code: f for f in funds}
        self.norm_names = [normalize_name(f.name) for f in funds]
        self.by_norm: dict[str, list[FundMeta]] = {}
        for f, n in zip(funds, self.norm_names):
            self.by_norm.setdefault(n, []).append(f)

    def match(self, name: str, code: str | None = None) -> tuple[FundMeta | None, list[FundMeta]]:
        """返回 (唯一确定的基金, 候选列表)。无法唯一确定时第一个值为 None。"""
        if code and code in self.by_code:
            return self.by_code[code], []
        n = normalize_name(name)
        if not n:
            return None, []
        exact = self.by_norm.get(n)
        if exact and len(exact) == 1:
            return exact[0], []
        truncated = bool(re.search(r"(\.\.\.|…)\s*$", name or ""))
        if truncated:
            pref = [f for f, fn in zip(self.funds, self.norm_names) if fn.startswith(n)]
            if len(pref) == 1:
                return pref[0], []
            if 1 < len(pref) <= 6:
                return None, pref
        scored = self._fuzzy(n)
        if not scored:
            return None, []
        top_score = scored[0][1]
        second = scored[1][1] if len(scored) > 1 else 0
        if top_score >= 92 and top_score - second >= 3:
            return scored[0][0], []
        cands = [f for f, sc in scored if sc >= max(70, top_score - 10)][:3]
        return None, cands

    def _fuzzy(self, n: str) -> list[tuple[FundMeta, float]]:
        if HAS_RAPIDFUZZ:
            res = process.extract(n, self.norm_names, scorer=fuzz.WRatio, limit=5, score_cutoff=60)
            return [(self.funds[idx], score) for _, score, idx in res]
        matches = difflib.get_close_matches(n, self.norm_names, n=5, cutoff=0.6)
        out = []
        for m in matches:
            idx = self.norm_names.index(m)
            out.append((self.funds[idx], difflib.SequenceMatcher(None, n, m).ratio() * 100))
        return out


# ---------------------------------------------------------------- 草稿
@dataclass
class DraftItem:
    raw_name: str
    amount: float | None
    profit: float | None
    shares: float | None
    pending: bool
    fund: FundMeta | None = None
    candidates: list[FundMeta] = field(default_factory=list)
    include: bool = True
    deleted: bool = False


def build_draft(items: list[dict], matcher: FundMatcher) -> list[DraftItem]:
    draft: list[DraftItem] = []
    seen: set[str] = set()
    for it in items:
        key = it.get("code") or normalize_name(it["name"])
        if key in seen:  # 多张截图重叠时去重
            continue
        seen.add(key)
        fund, cands = matcher.match(it["name"], it.get("code"))
        draft.append(
            DraftItem(
                raw_name=it["name"] or (fund.name if fund else ""),
                amount=it.get("amount"),
                profit=it.get("profit"),
                shares=it.get("shares"),
                pending=it.get("pending", False),
                fund=fund,
                candidates=cands,
                include=not it.get("pending", False),
            )
        )
    # 匹配后代码相同的也合并（例如一张图带代码、另一张不带）
    by_code: dict[str, DraftItem] = {}
    result = []
    for d in draft:
        if d.fund and d.fund.code in by_code:
            continue
        if d.fund:
            by_code[d.fund.code] = d
        result.append(d)
    return result


def resolved_codes(draft: list[DraftItem]) -> list[str]:
    return [d.fund.code for d in draft if d.fund and not d.deleted]


@dataclass
class FinalRow:
    code: str
    name: str
    shares: float
    cost: float
    amount: float


def finalize(draft: list[DraftItem], quotes: dict[str, Quote]) -> tuple[list[FinalRow], list[str]]:
    """把草稿换算成可入库的行。返回 (可导入行, 问题列表)。"""
    rows: list[FinalRow] = []
    problems: list[str] = []
    for i, d in enumerate(draft, 1):
        if d.deleted or not d.include:
            continue
        if not d.fund:
            problems.append(f"第 {i} 项还没确定是哪只基金")
            continue
        q = quotes.get(d.fund.code)
        nav = q.nav if q else None
        shares, amount = d.shares, d.amount
        if shares is None and amount is not None and nav:
            shares = amount / nav
        if amount is None and shares is not None and nav:
            amount = shares * nav
        if shares is None or amount is None:
            problems.append(f"第 {i} 项缺少金额或净值，无法换算份额")
            continue
        cost = amount - (d.profit or 0.0)
        rows.append(FinalRow(d.fund.code, d.fund.name, shares, cost, amount))
    return rows, problems


def render_draft(draft: list[DraftItem], show_amount: bool = True) -> str:
    lines = ["🧾 识别结果，请核对："]
    total_amount = total_profit = 0.0
    for i, d in enumerate(draft, 1):
        if d.deleted:
            continue
        amt = f"金额 {d.amount:,.0f}" if (show_amount and d.amount is not None) else ("金额 ?" if show_amount else "")
        prf = f"收益 {d.profit:+,.0f}" if (show_amount and d.profit is not None) else ""
        nums = " ".join(x for x in (amt, prf) if x)
        if d.fund:
            mark = "⏳" if (d.pending and not d.include) else "✅"
            lines.append(f"{i}. {mark} {d.fund.name}（{d.fund.code}） {nums}".rstrip())
            if d.pending and not d.include:
                lines.append(f"   有确认中的金额，默认不导入；回复「加 {i}」导入")
        elif d.candidates:
            lines.append(f"{i}. ❓ {d.raw_name} {nums}".rstrip())
            opts = "｜".join(f"{i}{chr(97 + k)} {c.name}（{c.code}）" for k, c in enumerate(d.candidates))
            lines.append(f"   有多只同名，请回复编号选择：{opts}")
        else:
            lines.append(f"{i}. ❌ {d.raw_name} {nums}".rstrip())
            lines.append(f"   没找到对应基金，回复「改 {i} 代码 六位代码」补充")
        if d.include and d.amount is not None:
            total_amount += d.amount
            total_profit += d.profit or 0.0
    if show_amount:
        lines.append(f"合计 金额 {total_amount:,.0f} ｜ 收益 {total_profit:+,.0f}（请和 App 总数核对）")
    lines.append(
        "回复：确认（合并到现有持仓）｜确认覆盖（清空后导入）｜取消\n"
        "修改：删 N ｜ 加 N ｜ 改 N 金额 30000 ｜ 改 N 收益 -2000 ｜ 改 N 代码 016370"
    )
    return "\n".join(lines)


def apply_edit(draft: list[DraftItem], text: str, matcher: FundMatcher) -> str | None:
    """处理用户的修改指令。识别成功返回提示文本（空串表示成功无额外提示），无法识别返回 None。"""
    t = text.strip().replace("　", " ")

    def get(idx: int) -> DraftItem | None:
        return draft[idx - 1] if 1 <= idx <= len(draft) and not draft[idx - 1].deleted else None

    m = re.fullmatch(r"(\d+)\s*([a-zA-Z])", t)
    if m:
        d = get(int(m.group(1)))
        k = ord(m.group(2).lower()) - 97
        if not d or not (0 <= k < len(d.candidates)):
            return "编号不存在"
        d.fund, d.candidates = d.candidates[k], []
        return ""
    m = re.fullmatch(r"(?:删|删除)\s*(\d+)", t)
    if m:
        d = get(int(m.group(1)))
        if not d:
            return "编号不存在"
        d.deleted = True
        return ""
    m = re.fullmatch(r"加\s*(\d+)", t)
    if m:
        d = get(int(m.group(1)))
        if not d:
            return "编号不存在"
        d.include = True
        return ""
    m = re.fullmatch(r"改\s*(\d+)\s*(金额|收益|份额|代码)\s*([-+]?[\d.,]+万?)", t)
    if m:
        d = get(int(m.group(1)))
        if not d:
            return "编号不存在"
        field_name, val = m.group(2), m.group(3)
        if field_name == "代码":
            code = val.strip()
            fund = matcher.by_code.get(code)
            if not fund:
                return f"没找到代码 {code} 对应的基金"
            d.fund, d.candidates = fund, []
            return ""
        num = _to_money(val)
        if num is None:
            return "数字格式不对"
        if field_name == "金额":
            d.amount = num
        elif field_name == "收益":
            d.profit = num
        else:
            d.shares = num
        return ""
    return None
