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
    resolved_codes,
)
from .importer import _to_money as to_money
from . import alerts
from .portfolio import CN_TZ, build_summary, drawdown_from_trend, has_trading_today, nav_readiness
from .render import DISCLAIMER, HELP, render_board, render_list, render_trades
from .storage import Settings, Storage

PLUGIN_NAME = "astrbot_plugin_fund_board"
CONFIRM_WORDS = {"确认", "确认合并", "确认覆盖"}
DONE_WORDS = {"完成", "好了", "好", "ok", "识别", "开始"}
CANCEL_WORDS = {"取消", "退出", "算了"}
PRIVACY_NOTE = "提示：截图会发送给当前配置的大模型服务商识别，请确认你接受。识别结果只保存在本机 AstrBot 的数据库里。"


REPORT_TITLES = {"close": "🔔 收盘估值", "nav": "🌙 净值日报", "manual": "📊 持仓日报"}


@register(PLUGIN_NAME, "fxianglong", "基金持仓看板：记录持仓、查看盈亏、截图导入、每日推送与回撤提醒", "0.2.0")
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
    def _uid(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

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
    async def board(self, event: AstrMessageEvent):
        """持仓看板"""
        holdings = self.storage.list(self._uid(event))
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
        text = render_board(summary, self._show_amount(event))
        realized = self.storage.realized_total(self._uid(event))
        if realized and self._show_amount(event):
            text += f"\n已实现盈亏（卖出记录合计）：{realized:+,.2f}"
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
        self.storage.upsert(self._uid(event), code, q.name, shares, cost)
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
        ok = self.storage.delete(self._uid(event), code.strip())
        yield event.plain_result(f"已删除 {code}" if ok else f"持仓里没有 {code}")

    @fund.command("clear", alias={"清空"})
    async def clear(self, event: AstrMessageEvent, confirm: str = ""):
        """清空全部持仓：/fund clear 确认"""
        if confirm != "确认":
            yield event.plain_result("将清空你的全部持仓，确定的话请发送：/fund clear 确认")
            return
        n = self.storage.clear(self._uid(event))
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

    # ------------------------------------------------------------ 交易记录
    @fund.command("buy", alias={"买入", "加仓"})
    async def buy(self, event: AstrMessageEvent, code: str, amount: str, nav: str = ""):
        """记录加仓：/fund buy 代码 金额 [净值]"""
        code = code.strip()
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
        msg = f"已记录买入 {name}（{code}）"
        if self._show_amount(event):
            msg += (
                f"\n金额 {amt:,.2f} ｜ 按净值 {price} 计 {t.shares:,.2f} 份"
                f"\n现持有 {h.shares:,.2f} 份 ｜ 成本 {h.cost:,.2f}"
            )
        if not nav:
            msg += "\n（按最新净值估算份额，实际以基金公司确认为准；确认后可用 /fund add 校准）"
        yield event.plain_result(msg)

    @fund.command("sell", alias={"卖出", "赎回"})
    async def sell(self, event: AstrMessageEvent, code: str, shares: str, nav: str = ""):
        """记录赎回：/fund sell 代码 份额|全部 [净值]"""
        code = code.strip()
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
        msg = f"已记录卖出 {h.name}（{code}）"
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
        uid = self._uid(event)
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
            status, text = await self._build_report(uid, st_tmp, "manual", datetime.now(CN_TZ))
            yield event.plain_result(text if status == "ok" else "还没有持仓，或暂时取不到行情。")
        else:
            state = "已开启" if st.push_on else "未开启"
            yield event.plain_result(
                f"每日推送：{state}\n交易日 {close_t} 收盘估值，{nav_t} 起净值日报。\n"
                "开启：/fund push on（发到当前会话） ｜ 关闭：/fund push off ｜ 立即推送一次：/fund push now"
            )

    @fund.command("alert", alias={"提醒"})
    async def alert(self, event: AstrMessageEvent, kind: str = "", value: str = ""):
        """提醒阈值：/fund alert day 3 ｜ /fund alert dd 20"""
        uid = self._uid(event)
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

    async def _build_report(self, uid: str, st: Settings, mode: str, now: datetime) -> tuple[str, str]:
        """生成推送内容。返回 (状态, 文本)，状态：ok / wait（净值未出齐，稍后再试）/ skip（非交易日或无持仓）。"""
        holdings = self.storage.list(uid)
        if not holdings:
            return "skip", ""
        codes = [h.code for h in holdings]
        try:
            quotes = await self.ds.get_quotes(codes)
        except DataSourceError as e:
            logger.error(f"[fund_board] 推送取行情失败: {e}")
            return "wait", ""
        today = now.strftime("%Y-%m-%d")
        if mode in ("close", "nav") and not has_trading_today(quotes, today):
            return "skip", ""
        retry_until = self._cfg_time("nav_retry_until", "23:30")
        if mode == "nav":
            ready, pending = nav_readiness(quotes, codes, today)
            late = now.strftime("%H:%M") >= retry_until
            if (pending or not ready) and not late:
                return "wait", ""
            if not ready:
                return "skip", ""

        summary = build_summary(holdings, quotes, now)
        show = st.private or bool(self.config.get("group_show_amount", False))
        lines = [render_board(summary, show, now, REPORT_TITLES[mode])]
        day_th, dd_th = self._thresholds(st)

        if mode == "close":
            if day_th > 0 and summary.day_pct is not None and summary.day_pct <= -day_th:
                lines.append(f"⚠️ 按盘中估值，组合今日约下跌 {abs(summary.day_pct):.2f}%，超过 {day_th:g}% 提醒线。")
        else:
            portfolio = None
            if mode == "nav" and summary.day_pct is not None:
                portfolio = self.storage.record_index(uid, today, summary.day_pct)
            elif mode == "manual":
                portfolio = self.storage.latest_index(uid)
            fund_dds = await self._fund_drawdowns(codes) if dd_th > 0 else {}
            alert_lines = alerts.evaluate(
                self.storage, uid, summary, day_th, dd_th, portfolio, fund_dds, dry=(mode == "manual")
            )
            if alert_lines:
                lines.append("")
                lines.extend(alert_lines)
        return "ok", "\n".join(lines)

    async def _send(self, st: Settings, text: str) -> None:
        try:
            ok = await self.context.send_message(st.umo, MessageChain().message(text))
            if not ok:
                logger.warning(f"[fund_board] 推送失败，找不到会话 {st.umo}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[fund_board] 推送出错 {st.umo}: {e}")

    async def _tick(self, now: datetime) -> None:
        if now.weekday() >= 5:  # 周末不推送；节假日由"今天有没有估值/净值"判断
            return
        hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
        close_t, nav_t = self._cfg_time("close_push_time", "15:05"), self._cfg_time("nav_push_time", "21:30")
        subs = self.storage.subscribers()

        if close_t <= hm < nav_t:
            for st in subs:
                if st.last_close == today:
                    continue
                status, text = await self._build_report(st.user_id, st, "close", now)
                if status == "wait":
                    continue
                if status == "ok":
                    await self._send(st, text)
                st.last_close = today
                self.storage.save_settings(st)

        if hm >= nav_t:
            if self._nav_next_try and now < self._nav_next_try:
                return
            waiting = False
            for st in subs:
                if st.last_nav == today:
                    continue
                status, text = await self._build_report(st.user_id, st, "nav", now)
                if status == "wait":
                    waiting = True
                    continue
                if status == "ok":
                    await self._send(st, text)
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
                replace_all = text == "确认覆盖"
                self.storage.upsert_many(uid, [(r.code, r.name, r.shares, r.cost) for r in rows], replace_all)
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
