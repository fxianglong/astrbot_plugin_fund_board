"""阈值提醒：单日跌幅、组合回撤、单只基金回撤。

回撤提醒按档位去重：超过阈值后每再加深 5 个百分点才再提醒一次，
回撤回到阈值以下时重置。
"""

from __future__ import annotations

from datetime import datetime

from .portfolio import CN_TZ, Summary
from .storage import Storage

STEP = 5.0  # 回撤提醒档位间隔（百分点）
NOTE = "以上提醒只基于你设置的阈值，不构成操作建议。"


def dd_level(dd: float, threshold: float) -> int:
    if threshold <= 0 or dd < threshold:
        return 0
    return 1 + int((dd - threshold) // STEP)


def evaluate(
    storage: Storage,
    user_id: str,
    summary: Summary,
    day_th: float,
    dd_th: float,
    portfolio: tuple[float, float] | None,
    fund_dds: dict[str, tuple[float, float, int]],
    dry: bool = False,
) -> list[str]:
    """返回需要推送的提醒行。

    portfolio: (组合指数, 历史最高)；fund_dds: code -> (近一年回撤%, 最高累计净值, 最高点时间戳)。
    dry=True 时不读写提醒档位，列出当前所有超过阈值的项（用于手动查看）。
    """

    def old_level(scope: str) -> int:
        return 0 if dry else storage.get_alert_level(user_id, scope)

    def save_level(scope: str, lvl: int) -> None:
        if not dry:
            storage.set_alert_level(user_id, scope, lvl)

    lines: list[str] = []
    if day_th > 0 and summary.day_pct is not None and summary.day_pct <= -day_th:
        lines.append(f"⚠️ 组合今日下跌 {abs(summary.day_pct):.2f}%，超过你设置的 {day_th:g}% 提醒线。")

    if dd_th > 0 and portfolio:
        idx, peak = portfolio
        dd = (1 - idx / peak) * 100 if peak > 0 else 0.0
        lvl, old = dd_level(dd, dd_th), old_level("portfolio")
        if lvl > old:
            lines.append(f"📉 组合较记录以来最高点回撤 {dd:.1f}%，超过你设置的 {dd_th:g}% 提醒线。")
        if lvl != old:
            save_level("portfolio", lvl)

    if dd_th > 0:
        names = {r.code: r.name for r in summary.rows}
        hit = []
        for code, (dd, _peak, peak_ts) in fund_dds.items():
            lvl, old = dd_level(dd, dd_th), old_level(f"fund:{code}")
            if lvl > old:
                peak_day = datetime.fromtimestamp(peak_ts / 1000, CN_TZ).strftime("%Y-%m-%d")
                hit.append(f"   {names.get(code, code)}：较近一年高点（{peak_day}）回撤 {dd:.1f}%")
            if lvl != old:
                save_level(f"fund:{code}", lvl)
        if hit:
            lines.append(f"📉 以下基金回撤超过 {dd_th:g}% 提醒线：")
            lines.extend(hit)

    if lines:
        lines.append(NOTE)
    return lines
