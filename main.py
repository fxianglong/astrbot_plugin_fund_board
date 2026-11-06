import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import SessionController, session_waiter

from .datasource import DataSourceError, FundDataSource
from .importer import (
    EXTRACT_PROMPT,
    FundMatcher,
    apply_edit,
    build_draft,
    finalize,
    parse_llm_json,
    render_draft,
    render_changes,
    resolved_codes,
    diff_holdings,
)
from .importer import _to_money as to_money
from . import alerts
from .portfolio import CN_TZ, build_summary, drawdown_from_trend, has_trading_today, nav_readiness
from .analysis import analyze
from .render import DISCLAIMER, HELP, render_board, render_check, render_list, render_overlap, render_trades
from .storage import Holding, Settings, Storage
from .journal import (
    Rule,
    due_reviews,
    effective_trade_date,
    parse_rule,
    render_review,
    render_sip_list,
    render_sip_stats,
    review_of,
    scheduled_dates,
    sip_stat,
)
from .perf import RANGE_ALIASES, RANGES, compute_perf, render_perf
from .planning import (
    PRESETS,
    allocation,
    drift_lines,
    group_of,
    parse_scenario,
    render_alloc,
    render_stress,
    stress,
)
from .templates import (
    BOARD_TMPL,
    CHECK_TMPL,
    PERF_TMPL,
    TARGET_TMPL,
    board_data,
    check_data,
    perf_data,
    target_data,
)
from .weekly import (
    SYSTEM_PROMPT,
    build_week,
    fund_week_return,
    llm_prompt,
    render_weekly,
    sanitize_commentary,
    week_info,
)

TOOL_NOTE = "（以上为用户在基金持仓看板插件中记录的数据。回答时只陈述数据，不要给出买入、卖出等操作建议。）"

PLUGIN_NAME = "astrbot_plugin_fund_board"
CONFIRM_WORDS = {"确认", "确认合并", "确认覆盖", "确认更新"}
DONE_WORDS = {"完成", "好了", "好", "ok", "识别", "开始"}
CANCEL_WORDS = {"取消", "退出", "算了"}
PRIVACY_NOTE = "提示：截图会发送给当前配置的大模型服务商识别，请确认你接受。识别结果只保存在本机 AstrBot 的数据库里。"


REPORT_TITLES = {"close": "🔔 收盘估值", "nav": "🌙 净值日报", "manual": "📊 持仓日报", "preclose": "⏰ 收盘前估值"}


@register(PLUGIN_NAME, "fxianglong", "基金持仓看板：记录持仓、查看盈亏、截图导入、每日推送、回撤提醒、组合体检、AI 周报与自然语言查询", "0.7.0")
class FundBoard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # 兼容旧版本
            data_dir = Path("data") / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)

        self.storage = Storage(data_dir / "fund_board.db")
        self.ds = FundDataSource(data_dir, timeout=int(config.get("request_timeout", 10) or 10))
        self._matcher: FundMatcher | None = None
        self._nav_next_try: datetime | None = None
        self._qdii_next: datetime | None = None
        self._intra_next: datetime | None = None
        self._intra_levels: dict[tuple[str, str, str], int] = {}
        self._last_gather: dict[str, tuple] = {}
        self._sip_next: datetime | None = None
        self._sip_done: dict[str, list[tuple[str, str]]] = {}
        self._task = asyncio.create_task(self._scheduler_loop())

    # ------------------------------------------------------------ 工具方法
    def _cfg_time(self, key: str, default: str) -> str:
        v = str(self.config.get(key, default) or default).strip()
        return v if re.fullmatch(r"\d{2}:\d{2}", v) else default

    def _thresholds(self, st: Settings) -> tuple[float, float]:
        day = st.day_alert if st.day_alert is not None else float(self.config.get("default_day_alert", 3) or 0)
        dd = st.dd_alert if st.dd_alert is not None else float(self.config.get("default_dd_alert", 20) or 0)
        return day, dd

    async def _quote_one(self, code: str):
        try:
            return (await self.ds.get_quotes([code])).get(code)
        except DataSourceError:
            return None

    @staticmethod
    def _tail_after(event: AstrMessageEvent, code: str, skip: int) -> str:
        """取消息里基金代码之后、跳过 skip 个参数的剩余文字（用于理由、笔记，允许包含空格）。"""
        tokens = (event.message_str or "").split()
        if code not in tokens:
            return ""
        i = tokens.index(code) + 1 + skip
        return " ".join(tokens[i:]).strip()

    def _split_nav_reason(self, event: AstrMessageEvent, code: str, nav: str) -> tuple[str, str]:
        """buy/sell 的第三个参数可能是净值，也可能是理由的第一个词。"""
        if nav and to_money(nav) is not None:
            return nav, self._tail_after(event, code, 2)
        return "", self._tail_after(event, code, 1)

    @staticmethod
    def _base(event: AstrMessageEvent) -> str:
        """用户本人（不区分账户），推送设置、提醒、分析都按这个 ID。"""
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    def _uid(self, event: AstrMessageEvent) -> str:
        """当前账户的存储 ID，持仓和交易按这个 ID 存。"""
        return self._account_uid(self._base(event))

    def _account_uid(self, base: str) -> str:
        return base  # v0.8 多账户时改为当前选中的账户

    def _account_uids(self, base: str) -> list[str]:
        return [base]  # v0.8 多账户时返回该用户的全部账户

    def _merged_holdings(self, base: str):
        """该用户全部账户的持仓，同一基金合并份额和成本。"""
        merged: dict[str, Holding] = {}
        for u in self._account_uids(base):
            for h in self.storage.list(u):
                if h.code in merged:
                    m = merged[h.code]
                    merged[h.code] = Holding(base, h.code, m.name, m.shares + h.shares, m.cost + h.cost,
                                             max(m.updated_at, h.updated_at))
                else:
                    merged[h.code] = Holding(base, h.code, h.name, h.shares, h.cost, h.updated_at)
        return sorted(merged.values(), key=lambda h: -h.cost)

    def _merged_trades(self, base: str, since: float = 0.0):
        out = []
        for u in self._account_uids(base):
            out.extend(self.storage.trades_since(u, since))
        return sorted(out, key=lambda t: (t.ts, t.id))

    def _merged_realized(self, base: str) -> float:
        return sum(self.storage.realized_total(u) for u in self._account_uids(base))

    def _show_amount(self, event: AstrMessageEvent) -> bool:
        return event.is_private_chat() or bool(self.config.get("group_show_amount", False))

    async def _matcher_get(self) -> FundMatcher:
        if self._matcher is None:
            funds = await self.ds.get_fund_list()
            self._matcher = await asyncio.to_thread(FundMatcher, funds)
        return self._matcher

    @staticmethod
    async def _collect_images(event: AstrMessageEvent) -> list[str]:
        paths = []
        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                try:
                    paths.append(await comp.convert_to_file_path())
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[fund_board] 图片下载失败: {e}")
        return paths

    # ------------------------------------------------------------ 指令
    @filter.command_group("fund", alias={"基金"})
    def fund(self):
        """基金持仓看板"""

    @fund.command("help", alias={"帮助"})
    async def help_(self, event: AstrMessageEvent):
        """显示帮助"""
        yield event.plain_result(HELP)

    @fund.command("board", alias={"看板"})
    async def board(self, event: AstrMessageEvent, mode: str = ""):
        """持仓看板：/fund board [img|text]"""
        uid = self._uid(event)
        holdings = self.storage.list(uid)
        if not holdings:
            yield event.plain_result(render_list([]))
            return
        try:
            quotes = await self.ds.get_quotes([h.code for h in holdings])
        except DataSourceError as e:
            logger.error(f"[fund_board] 获取行情失败: {e}")
            yield event.plain_result("获取行情失败，请稍后再试。")
            return
        summary = build_summary(holdings, quotes)
        show = self._show_amount(event)
        realized = self.storage.realized_total(uid)
        if self._want_image(mode):
            path = await self._render_img(
                BOARD_TMPL, board_data(summary, show, "📊 持仓看板", realized=realized), event.unified_msg_origin
            )
            if path:
                yield event.image_result(path)
                return
        text = render_board(summary, show)
        if realized and show:
            text += f"\n已实现盈亏（卖出记录合计）：{realized:+,.2f}"
        yield event.plain_result(text)

    # ------------------------------------------------------------ 分析
    def _want_image(self, mode: str) -> bool:
        m = (mode or "").strip().lower()
        if m in ("img", "image", "图", "图片"):
            return True
        if m in ("text", "txt", "文字"):
            return False
        return bool(self.config.get("image_mode", False))

    async def _render_img(self, tmpl: str, data: dict, umo: str | None = None) -> str | None:
        """渲染图片，返回本地路径；失败返回 None，由调用方退回文字。"""
        try:
            return await self.html_render(tmpl, data, return_url=False, umo=umo)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[fund_board] 图片渲染失败，改发文字: {e}")
            return None

    async def _analyze(self, uid: str):
        """取重仓股、经理、走势等数据并做穿透分析（合并全部账户）。返回 (summary, analysis)，没有持仓时返回 None。"""
        data = await self._gather(uid)
        if not data:
            return None
        summary, hs, ps, dds, types = data
        self._last_gather[uid] = data
        return summary, await asyncio.to_thread(analyze, summary, hs, ps, dds, types)

    async def _ftypes(self, codes: list[str]) -> dict[str, str]:
        out = {}
        for c in codes:
            try:
                meta = await self.ds.get_meta(c)
            except DataSourceError:
                meta = None
            out[c] = meta.ftype if meta else ""
        return out

    async def _gather(self, uid: str):
        """取分析需要的全部数据：(summary, 重仓股, 资料, 近一年回撤, 类型)。"""
        holdings = self._merged_holdings(uid)
        if not holdings:
            return None
        codes = [h.code for h in holdings]
        quotes = await self.ds.get_quotes(codes)
        summary = build_summary(holdings, quotes)
        sem = asyncio.Semaphore(4)

        async def one(code: str):
            async with sem:
                h = p = dd = None
                try:
                    h = await self.ds.get_holdings(code)
                except DataSourceError as e:
                    logger.warning(f"[fund_board] {code} 重仓股获取失败: {e}")
                try:
                    p = await self.ds.get_profile(code)
                    r = drawdown_from_trend(await self.ds.get_ac_trend(code))
                    dd = r[0] if r else None
                except DataSourceError as e:
                    logger.warning(f"[fund_board] {code} 详情获取失败: {e}")
                meta = None
                try:
                    meta = await self.ds.get_meta(code)
                except DataSourceError:
                    pass
                return code, h, p, dd, (meta.ftype if meta else "")

        results = await asyncio.gather(*(one(c) for c in codes))
        hs = {c: h for c, h, _, _, _ in results if h}
        ps = {c: p for c, _, p, _, _ in results if p}
        dds = {c: dd for c, _, _, dd, _ in results if dd is not None}
        types = {c: t for c, _, _, _, t in results}
        return summary, hs, ps, dds, types

    @fund.command("overlap", alias={"重叠", "穿透"})
    async def overlap(self, event: AstrMessageEvent):
        """重仓股穿透与基金重叠分析"""
        yield event.plain_result("正在获取各基金重仓股，请稍候…")
        try:
            res = await self._analyze(self._base(event))
        except DataSourceError as e:
            logger.error(f"[fund_board] 分析失败: {e}")
            yield event.plain_result("获取数据失败，请稍后再试。")
            return
        if not res:
            yield event.plain_result(render_list([]))
            return
        yield event.plain_result(render_overlap(res[1], self._show_amount(event)))

    @fund.command("check", alias={"体检"})
    async def check(self, event: AstrMessageEvent, mode: str = ""):
        """组合体检：/fund check [img|text]"""
        yield event.plain_result("正在体检，需要获取每只基金的重仓股和历史走势，请稍候…")
        try:
            res = await self._analyze(self._base(event))
        except DataSourceError as e:
            logger.error(f"[fund_board] 体检失败: {e}")
            yield event.plain_result("获取数据失败，请稍后再试。")
            return
        if not res:
            yield event.plain_result(render_list([]))
            return
        summary, a = res
        show = self._show_amount(event)
        if self._want_image(mode):
            path = await self._render_img(CHECK_TMPL, check_data(a, summary, show), event.unified_msg_origin)
            if path:
                yield event.image_result(path)
                return
        text = render_check(a, summary, show)
        try:
            p = await self._perf(self._base(event), "全部", datetime.now(CN_TZ))
        except DataSourceError:
            p = None
        if p and (p.twr is not None or p.xirr is not None):
            parts = []
            if p.twr is not None:
                parts.append(f"记录以来组合 {p.twr:+.2f}%")
            if p.xirr is not None:
                parts.append(f"年化 {p.xirr:+.2f}%")
            parts += [f"{b.name} {b.ret:+.2f}%" for b in p.benches if b.ret is not None]
            text += "\n收益：" + "｜".join(parts) + "（详见 /fund perf）"
        g = self._last_gather.get(self._base(event))
        if g:
            gs, hs, ps, _dds, types = g
            worst = min((stress(sc, gs.rows, types, hs, ps) for sc in PRESETS), key=lambda r: r.loss)
            if worst.loss < 0:
                text += f"\n压力测试：最不利的预设情景「{worst.scenario.name}」下组合约 {worst.pct:+.1f}%（详见 /fund stress）"
        if self.storage.targets(self._base(event)):
            alloc = await self._alloc(self._base(event), summary)
            off = [a2 for a2 in alloc if a2.diff is not None and abs(a2.diff) >= self._tolerance()]
            if off:
                text += "\n目标偏离：" + "｜".join(f"{x.group} {x.pct:.0f}%/{x.target:g}%" for x in off)
        yield event.plain_result(text)

    @fund.command("list", alias={"持仓"})
    async def list_(self, event: AstrMessageEvent):
        """持仓明细"""
        holdings = self.storage.list(self._uid(event))
        yield event.plain_result(render_list(holdings, self._show_amount(event)))

    @fund.command("add", alias={"添加"})
    async def add(self, event: AstrMessageEvent, code: str, amount: str, profit: str = "0"):
        """添加或更新一只基金：/fund add 代码 持有金额 [持有收益]"""
        code = code.strip()
        if not re.fullmatch(r"\d{6}", code):
            yield event.plain_result("基金代码应为 6 位数字，例如 /fund add 016370 41200 -23500")
            return
        amt, prf = to_money(amount), to_money(profit)
        if amt is None or amt <= 0 or prf is None:
            yield event.plain_result("金额或收益格式不对，例如 /fund add 016370 41200 -23500（支持 4.12万）")
            return
        try:
            q = (await self.ds.get_quotes([code])).get(code)
        except DataSourceError:
            q = None
        if not q or not q.nav:
            yield event.plain_result(f"没查到 {code} 的净值，请确认代码是否正确，或稍后再试。")
            return
        shares = amt / q.nav
        cost = amt - prf
        existed = any(h.code == code for h in self.storage.list(self._uid(event)))
        self.storage.upsert(self._uid(event), code, q.name, shares, cost, nav=q.nav)
        verb = "已更新" if existed else "已添加"
        if self._show_amount(event):
            msg = (
                f"{verb} {q.name}（{code}）\n"
                f"份额 {shares:,.2f}（按 {q.nav_date} 净值 {q.nav} 换算） ｜ 成本 {cost:,.2f}"
            )
        else:
            msg = f"{verb} {q.name}（{code}）"
        yield event.plain_result(msg)

    @fund.command("del", alias={"删除"})
    async def delete(self, event: AstrMessageEvent, code: str):
        """删除一只基金"""
        q = await self._quote_one(code.strip())
        ok = self.storage.delete(self._uid(event), code.strip(), nav=q.nav if q else None)
        yield event.plain_result(f"已删除 {code}" if ok else f"持仓里没有 {code}")

    @fund.command("clear", alias={"清空"})
    async def clear(self, event: AstrMessageEvent, confirm: str = ""):
        """清空全部持仓：/fund clear 确认"""
        if confirm != "确认":
            yield event.plain_result("将清空你的全部持仓，确定的话请发送：/fund clear 确认")
            return
        uid = self._uid(event)
        try:
            quotes = await self.ds.get_quotes([h.code for h in self.storage.list(uid)])
        except DataSourceError:
            quotes = {}
        n = self.storage.clear(uid, navs={c: q.nav for c, q in quotes.items() if q.nav})
        yield event.plain_result(f"已清空 {n} 只基金。")

    @fund.command("info", alias={"查询"})
    async def info(self, event: AstrMessageEvent, code: str):
        """查看单只基金"""
        code = code.strip()
        try:
            q = (await self.ds.get_quotes([code])).get(code)
        except DataSourceError:
            q = None
        if not q:
            yield event.plain_result(f"没查到 {code}，请确认代码是否正确。")
            return
        try:
            meta = await self.ds.get_meta(code)
        except DataSourceError:
            meta = None
        lines = [f"📈 {q.name}（{code}）"]
        if meta:
            lines.append(f"类型：{meta.ftype}")
        if q.nav is not None:
            nav_pct = f"{q.nav_pct:+.2f}%" if q.nav_pct is not None else "--"
            lines.append(f"净值：{q.nav}（{q.nav_date}，{nav_pct}）")
        if q.est is not None and q.est_pct is not None:
            lines.append(f"估值：{q.est}（{q.est_time}，{q.est_pct:+.2f}%）")
        else:
            lines.append("估值：暂无（QDII 等基金没有盘中估值）")
        try:
            periods = await self.ds.get_period_returns(code)
            ps = "  ".join(f"{k} {v:+.2f}%" for k, v in periods.items() if v is not None)
            if ps:
                lines.append(ps)
        except DataSourceError:
            pass
        lines.append(DISCLAIMER)
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 收益表现（v0.5）
    def _benchmarks(self) -> dict[str, str]:
        """配置里的基准：'沪深300:110020,纳指100:270042' -> {名称: 代码}。"""
        raw = str(self.config.get("benchmarks", "沪深300:110020,纳指100:270042") or "")
        out = {}
        for part in re.split(r"[,，;；\s]+", raw):
            if ":" in part or "：" in part:
                name, code = re.split(r"[:：]", part, maxsplit=1)
                if re.fullmatch(r"\d{6}", code.strip()) and name.strip():
                    out[name.strip()] = code.strip()
        return out

    async def _bench_trends(self) -> dict[str, tuple[str, list[tuple[int, float]]]]:
        out = {}
        for name, code in self._benchmarks().items():
            try:
                out[name] = (code, await self.ds.get_ac_trend(code))
            except DataSourceError as e:
                logger.warning(f"[fund_board] 基准 {name}({code}) 获取失败: {e}")
                out[name] = (code, [])
        return out

    async def _perf(self, base: str, range_name: str, now: datetime):
        holdings = self._merged_holdings(base)
        if not holdings:
            return None
        quotes = await self.ds.get_quotes([h.code for h in holdings])
        summary = build_summary(holdings, quotes, now)
        return compute_perf(
            range_name,
            now,
            self._merged_trades(base),
            summary.market_value,
            self.storage.index_all(base),
            self.storage.snapshots(base),
            await self._bench_trends(),
        )

    @staticmethod
    def _parse_range(text: str) -> str | None:
        t = (text or "").strip().lower()
        if not t:
            return "全部"
        if t in RANGES:
            return t
        return RANGE_ALIASES.get(t)

    @fund.command("perf", alias={"收益", "表现"})
    async def perf(self, event: AstrMessageEvent, range_: str = "", mode: str = ""):
        """收益表现与基准对比：/fund perf [近1月|近3月|近6月|近1年|今年|全部] [img]"""
        if range_.strip().lower() in ("img", "image", "图", "图片", "text", "文字"):
            range_, mode = "", range_
        rn = self._parse_range(range_)
        if not rn:
            yield event.plain_result("区间可选：近1月、近3月、近6月、近1年、今年、全部")
            return
        try:
            p = await self._perf(self._base(event), rn, datetime.now(CN_TZ))
        except DataSourceError as e:
            logger.error(f"[fund_board] 收益计算失败: {e}")
            yield event.plain_result("获取数据失败，请稍后再试。")
            return
        if not p:
            yield event.plain_result(render_list([]))
            return
        show = self._show_amount(event)
        if self._want_image(mode):
            path = await self._render_img(PERF_TMPL, perf_data(p, show), event.unified_msg_origin)
            if path:
                yield event.image_result(path)
                return
        yield event.plain_result(render_perf(p, show))

    async def _tick_snapshot(self, now: datetime) -> None:
        """每个交易日晚上给所有有持仓的用户记一次市值快照和组合指数（不依赖是否开启推送）。"""
        if now.weekday() >= 5 or now.strftime("%H:%M") < self._cfg_time("snapshot_time", "23:40"):
            return
        today = now.strftime("%Y-%m-%d")
        if self.storage.get_meta("snapshot_date") == today:
            return
        bases = sorted({u.split("#", 1)[0] for u in self.storage.all_user_ids()})
        for base in bases:
            holdings = self._merged_holdings(base)
            if not holdings:
                continue
            try:
                quotes = await self.ds.get_quotes([h.code for h in holdings])
            except DataSourceError as e:
                logger.warning(f"[fund_board] 快照取行情失败，稍后重试: {e}")
                return
            if not any(q.nav_date == today for q in quotes.values()):
                continue  # 今天不是交易日，或净值都没出
            summary = build_summary(holdings, quotes, now)
            self.storage.record_snapshot(base, today, summary.market_value, summary.cost)
            if summary.day_pct is not None:
                self.storage.record_index(base, today, summary.day_pct)
        self.storage.set_meta("snapshot_date", today)

    # ------------------------------------------------------------ 盘中估值监控（v0.5）
    def _intraday_level(self, value: float, threshold: float) -> int:
        if threshold <= 0 or value < threshold:
            return 0
        return 1 + int((value - threshold) // 2)  # 每再跌 2 个百分点再提醒一次

    async def _tick_intraday(self, now: datetime) -> None:
        if not self.config.get("intraday_alert", True) or now.weekday() >= 5:
            return
        hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        if not ("09:45" <= hm <= "14:57"):
            return
        if self._intra_next and now < self._intra_next:
            return
        self._intra_next = now + timedelta(minutes=max(int(self.config.get("intraday_minutes", 15) or 15), 5))
        fund_th = float(self.config.get("intraday_fund_alert", 5) or 0)
        for st in self.storage.subscribers():
            holdings = self._merged_holdings(st.user_id)
            if not holdings:
                continue
            try:
                quotes = await self.ds.get_quotes([h.code for h in holdings])
            except DataSourceError:
                return
            if not any(q.est_time and q.est_time[:10] == today for q in quotes.values()):
                continue
            summary = build_summary(holdings, quotes, now)
            day_th, _ = self._thresholds(st)
            show = st.private or bool(self.config.get("group_show_amount", False))
            lines = []

            def hit(scope: str, drop: float, th: float) -> bool:
                key = (st.user_id, today, scope)
                lvl = self._intraday_level(drop, th)
                if lvl > self._intra_levels.get(key, 0):
                    self._intra_levels[key] = lvl
                    return True
                return False

            if summary.day_pct is not None and hit("portfolio", -summary.day_pct, day_th):
                amt = f"（约 {summary.day_profit:+,.0f} 元）" if show else ""
                lines.append(f"组合估算 {summary.day_pct:+.2f}%{amt}，超过 {day_th:g}% 提醒线")
            for r in summary.rows:
                if r.price_kind == "估" and r.day_pct is not None and hit(f"fund:{r.code}", -r.day_pct, fund_th):
                    lines.append(f"{r.name} 估算 {r.day_pct:+.2f}%，超过 {fund_th:g}% 提醒线")
            if lines:
                text = "\n".join([f"⏱️ 盘中估值提醒 {now:%H:%M}"] + [f"· {x}" for x in lines])
                text += "\n估值按季报持仓推算，常与当晚净值有偏差，以净值为准。不构成投资建议。"
                await self._send(st, text)
        # 清掉前一天的记录
        self._intra_levels = {k: v for k, v in self._intra_levels.items() if k[1] == today}

    async def _tick_preclose(self, now: datetime) -> None:
        t = str(self.config.get("preclose_push_time", "14:45") or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", t) or now.weekday() >= 5:
            return
        hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        if not (t <= hm < "15:00"):
            return
        for st in self.storage.subscribers():
            key = f"preclose:{st.user_id}"
            if self.storage.get_meta(key) == today:
                continue
            status, text, img, extra = await self._build_report(st.user_id, st, "preclose", now)
            if status == "wait":
                continue
            if status == "ok":
                await self._send(st, text, img, extra)
            self.storage.set_meta(key, today)

    # ------------------------------------------------------------ 标签、目标配置、压力测试（v0.6）
    def _tolerance(self) -> float:
        return float(self.config.get("target_tolerance", 5) or 0)

    async def _alloc(self, base: str, summary) -> list:
        codes = [r.code for r in summary.rows]
        return allocation(
            summary.rows, await self._ftypes(codes), self.storage.tags(base), self.storage.targets(base)
        )

    @fund.command("tag", alias={"标签"})
    async def tag(self, event: AstrMessageEvent, code: str = "", tag: str = ""):
        """基金分组标签：/fund tag [代码] [标签|删除]"""
        base = self._base(event)
        code = code.strip()
        if code and not re.fullmatch(r"\d{6}", code):
            yield event.plain_result("用法：/fund tag 016370 科技主题 ｜ /fund tag 016370 删除 ｜ /fund tag 查看全部")
            return
        if code and tag.strip():
            t = tag.strip()
            if t in ("删除", "清除", "默认"):
                self.storage.set_tag(base, code, None)
                yield event.plain_result(f"已删除 {code} 的自定义标签，恢复按基金类型分组。")
                return
            self.storage.set_tag(base, code, t[:12])
            yield event.plain_result(f"已把 {code} 归入「{t[:12]}」。用 /fund target {t[:12]} 30 可以给这个分组设目标比例。")
            return
        holdings = self._merged_holdings(base)
        if not holdings:
            yield event.plain_result(render_list([]))
            return
        tags = self.storage.tags(base)
        ftypes = await self._ftypes([h.code for h in holdings])
        lines = ["🏷️ 基金分组"]
        for h in holdings:
            g = group_of(h.code, ftypes.get(h.code, ""), tags)
            src = "自定义" if h.code in tags else "按类型"
            lines.append(f"· {h.name}（{h.code}）：{g}（{src}）")
        lines.append("自定义：/fund tag 代码 标签 ｜ 恢复默认：/fund tag 代码 删除")
        yield event.plain_result("\n".join(lines))

    @fund.command("target", alias={"目标"})
    async def target(self, event: AstrMessageEvent, group: str = "", pct: str = "", mode: str = ""):
        """目标配置：/fund target [分组 比例|删除] [img]"""
        base = self._base(event)
        if group.strip().lower() in ("img", "image", "图", "图片", "text", "文字"):
            group, mode = "", group
        if group.strip() and pct.strip():
            g = group.strip()[:12]
            if pct.strip() in ("删除", "清除"):
                self.storage.set_target(base, g, None)
                yield event.plain_result(f"已删除「{g}」的目标。")
                return
            v = to_money(pct.strip().rstrip("%"))
            if v is None or not 0 <= v <= 100:
                yield event.plain_result("比例请填 0～100 的数字，例如 /fund target 科技主题 30")
                return
            self.storage.set_target(base, g, v)
            total = sum(self.storage.targets(base).values())
            msg = f"已设定「{g}」目标 {v:g}%。"
            if total > 100.01:
                msg += f"\n⚠️ 当前所有目标合计 {total:g}%，超过了 100%。"
            yield event.plain_result(msg)
        holdings = self._merged_holdings(base)
        if not holdings:
            yield event.plain_result(render_list([]))
            return
        try:
            quotes = await self.ds.get_quotes([h.code for h in holdings])
        except DataSourceError:
            yield event.plain_result("获取行情失败，请稍后再试。")
            return
        summary = build_summary(holdings, quotes)
        alloc = await self._alloc(base, summary)
        show = self._show_amount(event)
        if self._want_image(mode):
            path = await self._render_img(TARGET_TMPL, target_data(alloc, self._tolerance()), event.unified_msg_origin)
            if path:
                yield event.image_result(path)
                return
        yield event.plain_result(render_alloc(alloc, self._tolerance(), show))

    @fund.command("stress", alias={"压力", "压力测试"})
    async def stress_cmd(self, event: AstrMessageEvent, target: str = "", shock: str = ""):
        """情景压力测试：/fund stress [行业|美股|A股|债市 幅度]"""
        if target.strip():
            sc = parse_scenario(target, shock or "-20")
            if not sc:
                yield event.plain_result("用法：/fund stress 通信 -30 ｜ /fund stress 电子,计算机 -20 ｜ /fund stress 美股 -15")
                return
            scenarios = [sc]
        else:
            scenarios = PRESETS
        yield event.plain_result("正在获取持仓结构，请稍候…")
        try:
            data = await self._gather(self._base(event))
        except DataSourceError:
            yield event.plain_result("获取数据失败，请稍后再试。")
            return
        if not data:
            yield event.plain_result(render_list([]))
            return
        summary, hs, ps, _dds, types = data
        results = [stress(sc, summary.rows, types, hs, ps) for sc in scenarios]
        yield event.plain_result(render_stress(results, self._show_amount(event)))

    # ------------------------------------------------------------ 决策日志（v0.7）
    def _review_windows(self) -> list[int]:
        raw = str(self.config.get("review_days", "30,90") or "")
        days = sorted({int(x) for x in re.findall(r"\d+", raw) if 0 < int(x) <= 3650})
        return days or [30, 90]

    async def _reviews(self, notes, now: datetime, days_of=lambda n: 0):
        benches = await self._bench_trends()
        out = []
        for n in notes:
            try:
                trend = await self.ds.get_ac_trend(n.code)
            except DataSourceError:
                trend = []
            out.append((n, review_of(n, days_of(n), trend, benches, now)))
        return out

    @fund.command("note", alias={"笔记", "记录"})
    async def note(self, event: AstrMessageEvent, code: str = ""):
        """记一笔想法：/fund note 代码 内容"""
        code = code.strip()
        text = self._tail_after(event, code, 0) if code else ""
        if not re.fullmatch(r"\d{6}", code) or not text:
            yield event.plain_result("用法：/fund note 016370 跌到 0.8 以下也不动。到期会推送复盘。")
            return
        q = await self._quote_one(code)
        name = (q.name if q else "") or code
        self.storage.add_note(self._base(event), code, name, "note", text)
        days = "、".join(str(d) for d in self._review_windows())
        yield event.plain_result(f"已记下关于 {name} 的想法。{days} 天后会推送复盘（需开启每日推送），也可以随时 /fund review 查看。")

    @fund.command("review", alias={"复盘"})
    async def review(self, event: AstrMessageEvent):
        """查看决策日志和之后的表现"""
        notes = self.storage.notes(self._base(event), limit=10)
        if not notes:
            yield event.plain_result("还没有决策日志。买卖时在指令末尾加一句理由，或用 /fund note 代码 内容 记录想法。")
            return
        now = datetime.now(CN_TZ)
        reviews = await self._reviews(notes, now)
        show = self._show_amount(event)
        parts = ["🗒️ 决策日志（最近 10 条）"] + [render_review(r, show, n.amount) for n, r in reviews]
        parts.append("只列出记录时的理由和之后的涨跌，不评价对错。")
        yield event.plain_result("\n".join(parts))

    async def _tick_review(self, now: datetime) -> None:
        t = self._cfg_time("review_time", "20:00")
        if now.strftime("%H:%M") < t:
            return
        today = now.strftime("%Y-%m-%d")
        if self.storage.get_meta("review_date") == today:
            return
        windows = self._review_windows()
        for st in self.storage.subscribers():
            due = due_reviews(self.storage.notes(st.user_id), now, windows)
            if not due:
                continue
            days_of = {n.id: d for n, d in due}
            reviews = await self._reviews([n for n, _ in due], now, lambda n: days_of[n.id])
            show = st.private or bool(self.config.get("group_show_amount", False))
            text = "\n".join(
                ["🗒️ 到期复盘"] + [render_review(r, show, n.amount) for n, r in reviews]
                + ["只列出记录时的理由和之后的涨跌，不评价对错。"]
            )
            await self._send(st, text)
            for n, d in due:
                self.storage.mark_reviewed(n.id, d)
        self.storage.set_meta("review_date", today)

    # ------------------------------------------------------------ 定投（v0.7）
    @fund.command("sip", alias={"定投"})
    async def sip(self, event: AstrMessageEvent, action: str = "", a1: str = "", a2: str = "", a3: str = "", a4: str = ""):
        """定投计划：/fund sip add 代码 金额 规则 [费率] ｜ list ｜ pause/resume/del N ｜ stats"""
        uid = self._uid(event)
        act = action.strip().lower()
        if act in ("add", "新增", "添加"):
            code, amt, rule, fee = a1.strip(), to_money(a2), parse_rule(a3), to_money((a4 or "0").rstrip("%"))
            if not re.fullmatch(r"\d{6}", code) or not amt or amt <= 0 or not rule or fee is None or not 0 <= fee < 5:
                yield event.plain_result(
                    "用法：/fund sip add 代码 金额 规则 [费率%]\n"
                    "规则：每周三、每两周周一、每月15日、每个交易日\n例如：/fund sip add 110020 500 每周三 0.12"
                )
                return
            q = await self._quote_one(code)
            if not q:
                yield event.plain_result(f"没查到基金 {code}。")
                return
            pid = self.storage.add_sip(uid, code, q.name or code, amt, rule.dump(), fee)
            yield event.plain_result(
                f"已创建定投计划 {pid}：{q.name}（{code}）{rule.text()} {amt:,.0f} 元。\n"
                "扣款日遇到非交易日顺延，当天净值公布后自动记一笔买入；只是插件里的记录，不会真实扣款。"
            )
            return
        if act in ("pause", "暂停", "resume", "恢复", "del", "删除"):
            pid = int(a1) if a1.strip().isdigit() else -1
            plan = next((p for p in self.storage.sips(uid) if p.id == pid), None)
            if not plan:
                yield event.plain_result("没有这个计划编号，发送 /fund sip list 查看。")
                return
            if act in ("del", "删除"):
                self.storage.del_sip(pid)
                yield event.plain_result(f"已删除定投计划 {pid}，已经记账的买入记录保留。")
            else:
                active = act in ("resume", "恢复")
                fields = {"active": int(active)}
                if active:  # 恢复时从今天起算，暂停期间不补记
                    fields["last_date"] = (datetime.now(CN_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
                self.storage.update_sip(pid, **fields)
                yield event.plain_result(f"已{'恢复' if active else '暂停'}定投计划 {pid}。")
            return
        if act in ("stats", "统计"):
            plans = self.storage.sips(uid)
            codes = list({p.code for p in plans})
            try:
                quotes = await self.ds.get_quotes(codes) if codes else {}
            except DataSourceError:
                quotes = {}
            now = datetime.now(CN_TZ)
            stats = [
                sip_stat(p, self.storage.sip_trades(p.id), quotes[p.code].nav if p.code in quotes else None, now)
                for p in plans
            ]
            yield event.plain_result(render_sip_stats(stats, self._show_amount(event)))
            return
        yield event.plain_result(render_sip_list(self.storage.sips(uid)))

    async def _tick_sip(self, now: datetime) -> None:
        """按计划补记定投：每个扣款日取「当天或之后第一个交易日」的单位净值记一笔买入。"""
        if self._sip_next and now < self._sip_next:
            return
        self._sip_next = now + timedelta(minutes=10)
        today = now.date()
        plans = self.storage.sips(active_only=True)
        if not plans:
            return
        try:
            quotes = await self.ds.get_quotes(list({p.code for p in plans}))
        except DataSourceError:
            return
        for p in plans:
            rule = Rule.load(p.rule)
            created = datetime.fromtimestamp(p.created, CN_TZ)
            if p.last_date:
                start = datetime.strptime(p.last_date, "%Y-%m-%d").date() + timedelta(days=1)
            else:  # 15:00 之后创建的，当天的扣款赶不上，从第二天算
                start = created.date() + timedelta(days=1 if created.hour >= 15 else 0)
            dates = scheduled_dates(rule, start, today, created.date())
            if not dates:
                continue
            try:
                trend = await self.ds.get_unit_trend(p.code)
            except DataSourceError:
                trend = []
            q = quotes.get(p.code)
            for d in dates:
                eff = effective_trade_date(trend, d)
                if eff is None and q and q.nav and q.nav_date == today.strftime("%Y-%m-%d") and d <= today:
                    eff = (today, q.nav)
                if eff is None:
                    break  # 净值还没出，下次再试
                eff_date, nav = eff
                ts = datetime(eff_date.year, eff_date.month, eff_date.day, 15, 0, tzinfo=CN_TZ).timestamp()
                self.storage.buy(p.user_id, p.code, p.name, p.amount, nav, fee_rate=p.fee_rate, ts=ts, sip_id=p.id)
                self.storage.update_sip(p.id, last_date=d.strftime("%Y-%m-%d"))
                base = p.user_id.split("#", 1)[0]
                self._sip_done.setdefault(base, []).append(
                    (today.strftime("%Y-%m-%d"), f"📆 定投已记账：{p.name} {p.amount:,.0f} 元（{eff_date:%m-%d} 净值 {nav}）")
                )

    # ------------------------------------------------------------ 周报
    async def _commentary(self, prompt: str, umo: str | None) -> str:
        """让大模型写一段中性点评；没有可用模型或出错时返回空串。"""
        if not self.config.get("weekly_ai", True):
            return ""
        try:
            pid = (self.config.get("weekly_provider_id") or "").strip()
            if not pid and umo:
                pid = await self.context.get_current_chat_provider_id(umo)
            if not pid:
                return ""
            resp = await asyncio.wait_for(
                self.context.llm_generate(chat_provider_id=pid, prompt=prompt, system_prompt=SYSTEM_PROMPT),
                timeout=60,
            )
            text = sanitize_commentary(resp.completion_text)
            return text[:300]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[fund_board] 周报点评生成失败: {e}")
            return ""

    async def _weekly_text(self, uid: str, show_amount: bool, umo: str | None, now: datetime) -> str:
        res = await self._analyze(uid)
        if not res:
            return ""
        summary, a = res
        monday, _ = week_info(now)
        codes = [r.code for r in summary.rows if r.market_value]
        trends = {}
        for c in codes:
            try:
                trends[c] = await self.ds.get_ac_trend(c)
            except DataSourceError:
                trends[c] = []
        series = self.storage.index_series(uid, monday.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
        trades = self._merged_trades(uid, monday.timestamp())
        report = build_week(summary, trends, series, trades, a.flags, now)
        for name, (_code, trend) in (await self._bench_trends()).items():
            report.benches.append((name, fund_week_return(trend, monday)[0]))
        commentary = ""
        if report.ret is not None:
            commentary = await self._commentary(llm_prompt(report, show_amount), umo)
        text = render_weekly(report, show_amount, commentary)
        due = [
            (n, w) for n in self.storage.notes(uid) for w in self._review_windows()
            if monday.timestamp() <= n.ts + w * 86400 <= now.timestamp()
        ]
        if due:
            names = "、".join(dict.fromkeys(n.name for n, _ in due))
            text += f"\n🗒️ 本周到期复盘 {len(due)} 条（{names}），发送 /fund review 查看。"
        return text

    @fund.command("weekly", alias={"周报"})
    async def weekly(self, event: AstrMessageEvent):
        """本周周报（含 AI 点评）"""
        yield event.plain_result("正在生成本周周报，请稍候…")
        try:
            text = await self._weekly_text(
                self._base(event), self._show_amount(event), event.unified_msg_origin, datetime.now(CN_TZ)
            )
        except DataSourceError as e:
            logger.error(f"[fund_board] 周报失败: {e}")
            yield event.plain_result("获取数据失败，请稍后再试。")
            return
        yield event.plain_result(text or render_list([]))

    # ------------------------------------------------------------ 自然语言查询（LLM 工具）
    def _tools_on(self) -> bool:
        return bool(self.config.get("enable_llm_tools", True))

    @filter.llm_tool(name="fund_get_portfolio")
    async def tool_portfolio(self, event: AstrMessageEvent, scope: str = "all"):
        """查询当前用户在"基金持仓看板"插件里记录的基金持仓：每只基金的当日涨跌、市值、今日盈亏、累计盈亏和合计。用户问到自己的基金、持仓、收益、盈亏、哪只亏得最多等问题时调用。

        Args:
            scope(string): 填 all 返回全部持仓；填 6 位基金代码只返回这一只
        """
        if not self._tools_on():
            return "管理员已关闭基金持仓查询工具。"
        holdings = self._merged_holdings(self._base(event))
        if not holdings:
            return "用户还没有在插件里记录基金持仓。可以告诉用户用 /fund add 或 /fund import 添加。"
        code = (scope or "").strip()
        if re.fullmatch(r"\d{6}", code):
            holdings = [h for h in holdings if h.code == code]
            if not holdings:
                return f"用户的持仓里没有基金 {code}。"
        try:
            quotes = await self.ds.get_quotes([h.code for h in holdings])
        except DataSourceError:
            return "暂时取不到行情数据，请稍后再试。"
        summary = build_summary(holdings, quotes)
        show = self._show_amount(event)
        text = render_board(summary, show)
        if not show:
            text += "\n（当前是群聊，只提供百分比，不要透露金额。）"
        return text + "\n" + TOOL_NOTE

    @filter.llm_tool(name="fund_lookup")
    async def tool_lookup(self, event: AstrMessageEvent, code: str):
        """查询任意一只公募基金的名称、类型、最新净值、盘中估值和近 1/3/6/12 个月收益。用户问某只基金（给出 6 位代码）的情况时调用。

        Args:
            code(string): 6 位基金代码
        """
        if not self._tools_on():
            return "管理员已关闭基金查询工具。"
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            return "需要 6 位基金代码。"
        q = await self._quote_one(code)
        if not q:
            return f"没查到基金 {code}。"
        lines = [f"{q.name}（{code}）"]
        try:
            meta = await self.ds.get_meta(code)
            if meta:
                lines.append(f"类型：{meta.ftype}")
        except DataSourceError:
            pass
        if q.nav is not None:
            lines.append(f"最新净值 {q.nav}（{q.nav_date}，当日 {q.nav_pct if q.nav_pct is not None else '--'}%）")
        if q.est is not None:
            lines.append(f"盘中估值 {q.est}（{q.est_time}，{q.est_pct}%）")
        try:
            periods = await self.ds.get_period_returns(code)
            lines.append("阶段收益：" + "，".join(f"{k} {v:+.2f}%" for k, v in periods.items() if v is not None))
        except DataSourceError:
            pass
        return "\n".join(lines) + "\n" + TOOL_NOTE

    @filter.llm_tool(name="fund_performance")
    async def tool_performance(self, event: AstrMessageEvent, period: str = "全部"):
        """查询当前用户基金组合的收益表现：时间加权收益、XIRR 年化、最大回撤，以及与沪深300、纳指100等基准的同期对比。用户问"我今年收益多少""跑赢大盘了吗""年化多少"时调用。

        Args:
            period(string): 区间，可选 近1月、近3月、近6月、近1年、今年、全部
        """
        if not self._tools_on():
            return "管理员已关闭基金收益查询工具。"
        rn = self._parse_range(period) or "全部"
        try:
            p = await self._perf(self._base(event), rn, datetime.now(CN_TZ))
        except DataSourceError:
            return "暂时取不到数据，请稍后再试。"
        if not p:
            return "用户还没有在插件里记录基金持仓。"
        return render_perf(p, self._show_amount(event)) + "\n" + TOOL_NOTE

    @filter.llm_tool(name="fund_checkup")
    async def tool_checkup(self, event: AstrMessageEvent, focus: str = "overview"):
        """对当前用户记录的基金组合做体检：穿透重仓股后的个股和行业暴露、基金之间的持仓重叠、集中度、近一年回撤、经理任职。用户问持仓是否集中、基金是否重复、风险在哪里、买了哪些股票等问题时调用。

        Args:
            focus(string): overview 返回体检全貌；overlap 返回重仓股穿透和重叠细节
        """
        if not self._tools_on():
            return "管理员已关闭基金体检工具。"
        try:
            res = await self._analyze(self._base(event))
        except DataSourceError:
            return "暂时取不到数据，请稍后再试。"
        if not res:
            return "用户还没有在插件里记录基金持仓。"
        summary, a = res
        show = self._show_amount(event)
        text = render_overlap(a, show) if (focus or "").strip() == "overlap" else render_check(a, summary, show)
        return text + "\n" + TOOL_NOTE

    # ------------------------------------------------------------ 交易记录
    @fund.command("buy", alias={"买入", "加仓"})
    async def buy(self, event: AstrMessageEvent, code: str, amount: str, nav: str = ""):
        """记录加仓：/fund buy 代码 金额 [净值] [理由]"""
        code = code.strip()
        nav, reason = self._split_nav_reason(event, code, nav)
        amt = to_money(amount)
        if not re.fullmatch(r"\d{6}", code) or amt is None or amt <= 0:
            yield event.plain_result("格式：/fund buy 代码 金额 [净值]，例如 /fund buy 016370 1000")
            return
        q = await self._quote_one(code)
        price = to_money(nav) if nav else (q.nav if q else None)
        if not price or price <= 0:
            yield event.plain_result(f"没查到 {code} 的净值，可以手动指定：/fund buy {code} {amount} 净值")
            return
        name = (q.name if q else "") or code
        h, t = self.storage.buy(self._uid(event), code, name, amt, price)
        if reason:
            self.storage.add_note(self._base(event), code, name, "buy", reason, t.id, amt)
        msg = f"已记录买入 {name}（{code}）" + ("，理由已记下" if reason else "")
        if self._show_amount(event):
            msg += (
                f"\n金额 {amt:,.2f} ｜ 按净值 {price} 计 {t.shares:,.2f} 份"
                f"\n现持有 {h.shares:,.2f} 份 ｜ 成本 {h.cost:,.2f}"
            )
        if not nav:
            msg += "\n（按最新净值估算份额，实际以基金公司确认为准；确认后可用 /fund add 校准）"
        if not reason:
            msg += "\n可以在指令末尾加一句理由，30 天和 90 天后会推送复盘。"
        yield event.plain_result(msg)

    @fund.command("sell", alias={"卖出", "赎回"})
    async def sell(self, event: AstrMessageEvent, code: str, shares: str, nav: str = ""):
        """记录赎回：/fund sell 代码 份额|全部 [净值] [理由]"""
        code = code.strip()
        nav, reason = self._split_nav_reason(event, code, nav)
        uid = self._uid(event)
        h = self.storage.get(uid, code)
        if not h:
            yield event.plain_result(f"持仓里没有 {code}")
            return
        n = h.shares if shares.strip() in ("全部", "all", "ALL") else to_money(shares)
        if n is None or n <= 0:
            yield event.plain_result("格式：/fund sell 代码 份额|全部 [净值]，例如 /fund sell 016370 全部")
            return
        q = await self._quote_one(code)
        price = to_money(nav) if nav else (q.nav if q else None)
        if not price or price <= 0:
            yield event.plain_result(f"没查到 {code} 的净值，可以手动指定：/fund sell {code} {shares} 净值")
            return
        try:
            left, t = self.storage.sell(uid, code, n, price)
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        if reason:
            self.storage.add_note(self._base(event), code, h.name, "sell", reason, t.id, t.amount)
        msg = f"已记录卖出 {h.name}（{code}）" + ("，理由已记下" if reason else "")
        if self._show_amount(event):
            msg += f"\n{t.shares:,.2f} 份 × 净值 {price} = {t.amount:,.2f} ｜ 实现盈亏 {t.realized:+,.2f}"
            msg += f"\n剩余 {left.shares:,.2f} 份 ｜ 成本 {left.cost:,.2f}" if left else "\n已全部卖出，持仓已移除"
        elif not left:
            msg += "，已全部卖出"
        yield event.plain_result(msg)

    @fund.command("trades", alias={"交易"})
    async def trades(self, event: AstrMessageEvent, code: str = ""):
        """最近交易记录"""
        rows = self.storage.trades(self._uid(event), code.strip() or None, limit=15)
        yield event.plain_result(render_trades(rows, self._show_amount(event)))

    # ------------------------------------------------------------ 推送与提醒
    @fund.command("push", alias={"推送"})
    async def push(self, event: AstrMessageEvent, action: str = ""):
        """每日推送：/fund push on|off|now"""
        uid = self._base(event)
        st = self.storage.get_settings(uid)
        close_t, nav_t = self._cfg_time("close_push_time", "15:05"), self._cfg_time("nav_push_time", "21:30")
        a = action.strip().lower()
        if a in ("on", "开", "开启"):
            st.umo, st.private, st.push_on = event.unified_msg_origin, event.is_private_chat(), True
            self.storage.save_settings(st)
            where = "当前私聊" if st.private else "当前群聊（只显示百分比）"
            yield event.plain_result(
                f"已开启每日推送，发到{where}。\n交易日 {close_t} 推送收盘估值，{nav_t} 起推送净值日报和回撤提醒。"
            )
        elif a in ("off", "关", "关闭"):
            st.push_on = False
            self.storage.save_settings(st)
            yield event.plain_result("已关闭每日推送。")
        elif a in ("now", "test", "测试"):
            st_tmp = Settings(uid, event.unified_msg_origin, event.is_private_chat(), True, st.day_alert, st.dd_alert)
            status, text, img, extra = await self._build_report(uid, st_tmp, "manual", datetime.now(CN_TZ))
            if status != "ok":
                yield event.plain_result("还没有持仓，或暂时取不到行情。")
            elif img:
                yield event.image_result(img)
                if extra:
                    yield event.plain_result(extra)
            else:
                yield event.plain_result(text)
        else:
            state = "已开启" if st.push_on else "未开启"
            yield event.plain_result(
                f"每日推送：{state}\n交易日 {close_t} 收盘估值，{nav_t} 起净值日报。\n"
                "开启：/fund push on（发到当前会话） ｜ 关闭：/fund push off ｜ 立即推送一次：/fund push now"
            )

    @fund.command("alert", alias={"提醒"})
    async def alert(self, event: AstrMessageEvent, kind: str = "", value: str = ""):
        """提醒阈值：/fund alert day 3 ｜ /fund alert dd 20"""
        uid = self._base(event)
        st = self.storage.get_settings(uid)
        k = kind.strip().lower()
        if k in ("day", "dd", "单日", "回撤"):
            v = to_money(value)
            if v is None or v < 0 or v >= 100:
                yield event.plain_result("请输入 0～99 之间的数字，0 表示关闭。例如 /fund alert dd 20")
                return
            if k in ("day", "单日"):
                st.day_alert = v
            else:
                st.dd_alert = v
            self.storage.save_settings(st)
        elif k in ("off", "关闭"):
            st.day_alert = st.dd_alert = 0
            self.storage.save_settings(st)
        elif k:
            yield event.plain_result("用法：/fund alert day 3 ｜ /fund alert dd 20 ｜ /fund alert off")
            return
        day, dd = self._thresholds(st)
        lines = [
            "🔔 提醒设置",
            f"组合单日跌幅：{f'超过 {day:g}% 提醒' if day > 0 else '关闭'}",
            f"回撤（组合及单只基金）：{f'超过 {dd:g}% 提醒，之后每加深 5% 再提醒一次' if dd > 0 else '关闭'}",
            "提醒随每日净值日报发送，需要先 /fund push on 开启推送。",
        ]
        if not st.push_on:
            lines.append("⚠️ 你还没有开启每日推送。")
        yield event.plain_result("\n".join(lines))

    async def _fund_drawdowns(self, codes: list[str]) -> dict[str, tuple[float, float, int]]:
        out = {}
        for c in codes:
            try:
                r = drawdown_from_trend(await self.ds.get_ac_trend(c))
            except DataSourceError as e:
                logger.warning(f"[fund_board] 获取 {c} 走势失败: {e}")
                continue
            if r:
                out[c] = r
        return out

    async def _build_report(
        self, uid: str, st: Settings, mode: str, now: datetime
    ) -> tuple[str, str, str | None, str]:
        """生成推送内容。

        返回 (状态, 完整文字, 图片路径或 None, 提醒文字)。
        状态：ok / wait（净值未出齐，稍后再试）/ skip（非交易日或无持仓）。
        开启图片模式时，看板渲染成图片，提醒仍以文字附在后面。
        """
        holdings = self._merged_holdings(uid)
        if not holdings:
            return "skip", "", None, ""
        codes = [h.code for h in holdings]
        try:
            quotes = await self.ds.get_quotes(codes)
        except DataSourceError as e:
            logger.error(f"[fund_board] 推送取行情失败: {e}")
            return "wait", "", None, ""
        today = now.strftime("%Y-%m-%d")
        if mode in ("close", "nav", "preclose") and not has_trading_today(quotes, today):
            return "skip", "", None, ""
        retry_until = self._cfg_time("nav_retry_until", "23:30")
        if mode == "nav":
            ready, pending = nav_readiness(quotes, codes, today)
            late = now.strftime("%H:%M") >= retry_until
            if (pending or not ready) and not late:
                return "wait", "", None, ""
            if not ready:
                return "skip", "", None, ""

        summary = build_summary(holdings, quotes, now)
        show = st.private or bool(self.config.get("group_show_amount", False))
        extra: list[str] = []
        day_th, dd_th = self._thresholds(st)

        if mode in ("close", "preclose"):
            if day_th > 0 and summary.day_pct is not None and summary.day_pct <= -day_th:
                extra.append(f"⚠️ 按盘中估值，组合今日约下跌 {abs(summary.day_pct):.2f}%，超过 {day_th:g}% 提醒线。")
        else:
            portfolio = None
            if mode == "nav" and summary.day_pct is not None:
                portfolio = self.storage.record_index(uid, today, summary.day_pct)
            elif mode == "manual":
                portfolio = self.storage.latest_index(uid)
            fund_dds = await self._fund_drawdowns(codes) if dd_th > 0 else {}
            if mode == "nav":  # 记下推送过的净值日期，QDII 补推据此判断有没有新净值
                self.storage.set_reported(uid, {c: q.nav_date for c, q in quotes.items() if q.nav_date})
            alert_lines = alerts.evaluate(
                self.storage, uid, summary, day_th, dd_th, portfolio, fund_dds, dry=(mode == "manual")
            )
            extra.extend(alert_lines)
            if mode == "nav":
                today_s = now.strftime("%Y-%m-%d")
                extra.extend(t for d, t in self._sip_done.get(uid, []) if d == today_s)
                self._sip_done.pop(uid, None)
            if self.storage.targets(uid) and self._tolerance() > 0:
                dry = mode == "manual"
                drift = drift_lines(
                    await self._alloc(uid, summary),
                    self._tolerance(),
                    (lambda sc: 0) if dry else (lambda sc: self.storage.get_alert_level(uid, sc)),
                    (lambda sc, lv: None) if dry else (lambda sc, lv: self.storage.set_alert_level(uid, sc, lv)),
                )
                if drift:
                    extra.extend(drift + ["偏离只对照你自己设定的目标，不构成操作建议。"])
        extra_text = "\n".join(extra)
        text = render_board(summary, show, now, REPORT_TITLES[mode]) + (f"\n\n{extra_text}" if extra else "")
        img = None
        if self._want_image(""):
            realized = self._merged_realized(uid)
            data = board_data(summary, show, REPORT_TITLES[mode], now, realized if show else 0.0)
            img = await self._render_img(BOARD_TMPL, data, st.umo)
        return "ok", text, img, extra_text

    async def _send(self, st: Settings, text: str, img: str | None = None, extra: str = "") -> None:
        chain = MessageChain()
        if img:
            chain.file_image(img)
            if extra:
                chain.message(extra)
        else:
            chain.message(text)
        try:
            ok = await self.context.send_message(st.umo, chain)
            if not ok:
                logger.warning(f"[fund_board] 推送失败，找不到会话 {st.umo}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fund_board] 推送出错 {st.umo}: {e}")

    async def _tick_weekly(self, now: datetime) -> None:
        if not self.config.get("weekly_report", True):
            return
        wd = int(self.config.get("weekly_weekday", 6) or 6)
        if now.isoweekday() != wd or now.strftime("%H:%M") < self._cfg_time("weekly_time", "10:00"):
            return
        _, key = week_info(now)
        for st in self.storage.subscribers():
            if st.last_weekly == key:
                continue
            try:
                text = await self._weekly_text(st.user_id, st.private, st.umo, now)
            except DataSourceError as e:
                logger.error(f"[fund_board] 周报取数失败，稍后重试: {e}")
                return
            if text:
                await self._send(st, text)
            st.last_weekly = key
            self.storage.save_settings(st)

    async def _tick_qdii(self, now: datetime) -> None:
        """没有盘中估值的基金（主要是 QDII）净值往往晚 1～2 天公布，出了新净值就补推一条。"""
        if not self.config.get("qdii_push", True):
            return
        if self._qdii_next and now < self._qdii_next:
            return
        interval = int(self.config.get("qdii_check_minutes", 60) or 60)
        self._qdii_next = now + timedelta(minutes=max(interval, 10))
        if not ("08:00" <= now.strftime("%H:%M") <= "23:30"):
            return
        for st in self.storage.subscribers():
            holdings = self._merged_holdings(st.user_id)
            if not holdings:
                continue
            try:
                quotes = await self.ds.get_quotes([h.code for h in holdings])
            except DataSourceError as e:
                logger.warning(f"[fund_board] QDII 补推取行情失败: {e}")
                return
            reported = self.storage.get_reported(st.user_id)
            fresh, init = [], {}
            for h in holdings:
                q = quotes.get(h.code)
                if not q or q.est is not None or not q.nav_date or q.nav is None:
                    continue
                last = reported.get(h.code)
                if last is None:
                    init[h.code] = q.nav_date  # 第一次见到这只基金，只记录不推送
                elif q.nav_date > last:
                    fresh.append((h, q))
            if init:
                self.storage.set_reported(st.user_id, init)
            if not fresh:
                continue
            show = st.private or bool(self.config.get("group_show_amount", False))
            lines = ["🌏 净值更新（QDII 等延迟公布的基金）"]
            for h, q in fresh:
                pct = f"{q.nav_pct:+.2f}%" if q.nav_pct is not None else "--"
                line = f"· {q.name or h.name}：{q.nav_date[5:]} 净值 {q.nav}，当日 {pct}"
                if show and q.nav_pct is not None and q.nav_pct > -100:
                    mv = h.shares * q.nav
                    line += f"，约 {mv - mv / (1 + q.nav_pct / 100):+,.0f} 元"
                lines.append(line)
            lines.append(DISCLAIMER)
            await self._send(st, "\n".join(lines))
            self.storage.set_reported(st.user_id, {h.code: q.nav_date for h, q in fresh})

    async def _tick(self, now: datetime) -> None:
        await self._tick_sip(now)
        await self._tick_review(now)
        await self._tick_weekly(now)
        await self._tick_qdii(now)
        await self._tick_snapshot(now)
        await self._tick_intraday(now)
        await self._tick_preclose(now)
        if now.weekday() >= 5:  # 周末不推送日报；节假日由"今天有没有估值/净值"判断
            return
        hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        close_t, nav_t = self._cfg_time("close_push_time", "15:05"), self._cfg_time("nav_push_time", "21:30")
        subs = self.storage.subscribers()

        if close_t <= hm < nav_t:
            for st in subs:
                if st.last_close == today:
                    continue
                status, text, img, extra = await self._build_report(st.user_id, st, "close", now)
                if status == "wait":
                    continue
                if status == "ok":
                    await self._send(st, text, img, extra)
                st.last_close = today
                self.storage.save_settings(st)

        if hm >= nav_t:
            if self._nav_next_try and now < self._nav_next_try:
                return
            waiting = False
            for st in subs:
                if st.last_nav == today:
                    continue
                status, text, img, extra = await self._build_report(st.user_id, st, "nav", now)
                if status == "wait":
                    waiting = True
                    continue
                if status == "ok":
                    await self._send(st, text, img, extra)
                st.last_nav = today
                self.storage.save_settings(st)
            self._nav_next_try = now + timedelta(minutes=30) if waiting else None

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(15)  # 等 AstrBot 各平台加载完
        while True:
            try:
                await self._tick(datetime.now(CN_TZ))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[fund_board] 定时任务出错: {e}")
            await asyncio.sleep(30)

    # ------------------------------------------------------------ 截图导入
    async def _recognize(self, event: AstrMessageEvent, images: list[str]):
        pid = (self.config.get("vision_provider_id") or "").strip()
        if not pid:
            pid = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        prompt = EXTRACT_PROMPT
        items = None
        for attempt in range(2):
            resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt, image_urls=images)
            try:
                items = parse_llm_json(resp.completion_text)
                break
            except ValueError as e:
                logger.warning(f"[fund_board] 识别结果解析失败({attempt + 1}): {e}")
                prompt = EXTRACT_PROMPT + "\n\n上一次输出无法解析。这次只输出 JSON 数组本身，以 [ 开头、以 ] 结尾。"
        if items is None:
            raise ValueError("模型输出无法解析为 JSON")
        matcher = await self._matcher_get()
        return await asyncio.to_thread(build_draft, items, matcher)

    @fund.command("import", alias={"导入"})
    async def import_(self, event: AstrMessageEvent):
        """截图一键导入持仓"""
        uid = self._uid(event)
        max_images = int(self.config.get("max_images", 6) or 6)
        state = {"stage": "collect", "images": await self._collect_images(event), "draft": None}
        show_amount = self._show_amount(event)

        intro = ["请发送基金 App 持仓页截图（支付宝/天天基金/银行均可），可分多条发送，发完回复「完成」，回复「取消」退出。"]
        if state["images"]:
            intro.append(f"已收到 {len(state['images'])} 张。")
        if not await self.get_kv_data(f"privacy_ack:{uid}", False):
            intro.append(PRIVACY_NOTE)
            await self.put_kv_data(f"privacy_ack:{uid}", True)
        yield event.plain_result("\n".join(intro))

        @session_waiter(timeout=300, record_history_chains=False)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            text = (ev.message_str or "").strip()
            if text in CANCEL_WORDS:
                await ev.send(ev.plain_result("已取消导入。"))
                controller.stop()
                return

            # ---- 阶段三：给截图生成的交易补理由
            if state["stage"] == "reason":
                m = re.fullmatch(r"理由\s*(\d+)\s+(.+)", text, re.S)
                if not m:
                    await ev.send(ev.plain_result("好的，已结束。"))
                    controller.stop()
                    return
                k = int(m.group(1))
                if not 1 <= k <= len(state["reasons"]):
                    await ev.send(ev.plain_result("编号不对，请看上面的列表。"))
                    return
                c, tid = state["reasons"][k - 1]
                kind = "sell" if c.kind == "sell" else "buy"
                self.storage.add_note(self._base(ev), c.code, c.name, kind, m.group(2).strip(), tid, c.amount)
                await ev.send(ev.plain_result(f"已记下第 {k} 笔的理由。可以继续，或回复其他内容结束。"))
                controller.keep(timeout=300, reset_timeout=True)
                return

            # ---- 阶段一：收图
            if state["stage"] == "collect":
                new_imgs = await self._collect_images(ev)
                if new_imgs:
                    state["images"].extend(new_imgs)
                    state["images"] = state["images"][:max_images]
                    await ev.send(ev.plain_result(f"已收到 {len(state['images'])} 张，继续发或回复「完成」。"))
                    controller.keep(timeout=300, reset_timeout=True)
                    return
                if text.lower() in DONE_WORDS:
                    if not state["images"]:
                        await ev.send(ev.plain_result("还没收到截图哦。"))
                        return
                    await ev.send(ev.plain_result("正在识别，请稍候…"))
                    try:
                        draft = await self._recognize(ev, state["images"])
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[fund_board] 识别失败: {e}")
                        await ev.send(
                            ev.plain_result(
                                "识别失败：请确认当前模型支持图片输入（或在插件配置里指定视觉模型），"
                                "也可以用 /fund add 手动添加。"
                            )
                        )
                        controller.stop()
                        return
                    if not draft:
                        await ev.send(ev.plain_result("没有在截图里识别到基金持仓。"))
                        controller.stop()
                        return
                    state.update(stage="confirm", draft=draft)
                    await ev.send(ev.plain_result(render_draft(draft, show_amount)))
                    controller.keep(timeout=300, reset_timeout=True)
                    return
                await ev.send(ev.plain_result("请继续发截图，或回复「完成」开始识别、「取消」退出。"))
                return

            # ---- 阶段二：核对与确认
            draft = state["draft"]
            if text in CONFIRM_WORDS:
                try:
                    quotes = await self.ds.get_quotes(resolved_codes(draft))
                except DataSourceError:
                    await ev.send(ev.plain_result("获取净值失败，请稍后再回复「确认」。"))
                    return
                rows, problems = finalize(draft, quotes)
                if problems:
                    await ev.send(ev.plain_result("还有问题需要处理：\n" + "\n".join(problems)))
                    return
                if not rows:
                    await ev.send(ev.plain_result("没有可导入的基金，已结束。"))
                    controller.stop()
                    return
                existing = {h.code: h for h in self.storage.list(uid)}
                if text == "确认更新" and existing:
                    navs = {c: q.nav for c, q in quotes.items() if q.nav}
                    changes, missing = diff_holdings(existing, rows, navs)
                    ids = self.storage.apply_changes(uid, changes)
                    moved = [c for c in changes if c.kind != "same"]
                    text_out = render_changes(changes, missing, show_amount)
                    if ids:
                        state.update(stage="reason", reasons=list(zip(moved, ids)))
                        kinds = {"new": "新买入", "buy": "加仓", "sell": "减仓"}
                        text_out += "\n\n要给这些交易记一句理由吗？30 天和 90 天后会推送复盘。\n" + "\n".join(
                            f"{i}. {kinds[c.kind]} {c.name}" for i, (c, _) in enumerate(state["reasons"], 1)
                        ) + "\n回复「理由 1 内容」，回复其他内容结束。"
                        await ev.send(ev.plain_result(text_out))
                        controller.keep(timeout=300, reset_timeout=True)
                        return
                    await ev.send(ev.plain_result(text_out))
                    controller.stop()
                    return
                replace_all = text == "确认覆盖"
                navs = {c: q.nav for c, q in quotes.items() if q.nav}
                others = [c for c in existing if c not in navs]
                if replace_all and others:
                    try:
                        navs.update({c: q.nav for c, q in (await self.ds.get_quotes(others)).items() if q.nav})
                    except DataSourceError:
                        pass
                self.storage.upsert_many(
                    uid, [(r.code, r.name, r.shares, r.cost) for r in rows], replace_all, navs=navs
                )
                total = sum(r.amount for r in rows)
                cost = sum(r.cost for r in rows)
                msg = f"✅ 已{'覆盖' if replace_all else '合并'}导入 {len(rows)} 只基金。"
                if show_amount:
                    msg += f"\n导入金额 {total:,.0f} ｜ 累计收益 {total - cost:+,.0f}"
                msg += "\n发送 /fund board 查看看板。"
                await ev.send(ev.plain_result(msg))
                controller.stop()
                return

            res = apply_edit(draft, text, await self._matcher_get())
            if res is None:
                await ev.send(ev.plain_result("没看懂，请按提示回复：确认 / 确认覆盖 / 取消 / 删 N / 改 N 金额 xxx"))
                return
            if res:
                await ev.send(ev.plain_result(res))
                return
            await ev.send(ev.plain_result(render_draft(draft, show_amount)))
            controller.keep(timeout=300, reset_timeout=True)

        try:
            await waiter(event)
        except TimeoutError:
            yield event.plain_result("导入已超时结束，需要的话请重新发送 /fund import。")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fund_board] 导入出错: {e}")
            yield event.plain_result("导入过程出错了，请稍后重试。")
        finally:
            event.stop_event()

    async def terminate(self):
        self._task.cancel()
        await self.ds.close()
        self.storage.close()
