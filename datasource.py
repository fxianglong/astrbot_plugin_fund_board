"""基金数据源：估值/净值、基金列表、阶段收益。

只依赖 aiohttp（AstrBot 自带）。所有外部请求都带超时、重试和缓存，
接口变动时只需要修改本文件。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 天天基金批量估值接口：一次返回名称、盘中估值、最新净值及其日期、净值涨跌幅
VALUATION_URL = "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
VALUATION_FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE,NAVCHGRT"
# 全量基金代码表：[[代码, 拼音缩写, 名称, 类型, 拼音全称], ...]
FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
# 单只基金的详情脚本（阶段收益、净值走势、现任经理、资产配置等）
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
# 天天基金移动端：最新一期季报前十大重仓股（含所属行业）
HOLDINGS_URL = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"


def _num(v) -> float | None:
    """把接口返回的值安全地转成 float；'--'、空串、None 都返回 None。"""
    if v is None:
        return None
    try:
        f = float(str(v).replace("%", "").strip())
    except ValueError:
        return None
    return f


@dataclass
class Quote:
    code: str
    name: str
    nav: float | None  # 最新官方单位净值
    nav_date: str | None  # 净值日期 YYYY-MM-DD
    nav_pct: float | None  # 该净值日的涨跌幅 %
    est: float | None  # 盘中估值
    est_pct: float | None  # 估算涨跌幅 %
    est_time: str | None  # 估值时间 YYYY-MM-DD HH:MM


@dataclass
class StockPos:
    code: str
    name: str
    weight: float  # 占基金净值比例 %
    industry: str


@dataclass
class FundHoldings:
    report_date: str | None  # 季报截止日
    stocks: list[StockPos]


@dataclass
class FundProfile:
    """pingzhongdata 里体检用到的信息。"""

    stock_ratio: float | None  # 最新一期股票占净比 %
    managers: list[tuple[str, int]]  # [(姓名, 任职天数)]
    return_1y: float | None  # 近一年收益 %


def extract_js_var(text: str, name: str):
    """从 `var name = <JSON>;` 中取出 JSON 值，取不到返回 None。"""
    m = re.search(rf"var\s+{re.escape(name)}\s*=\s*", text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, m.end())
    except json.JSONDecodeError:
        return None
    return value


def parse_work_days(s: str) -> int:
    """'9年又50天' / '1年' / '200天' -> 天数。"""
    years = re.search(r"(\d+)\s*年", s or "")
    days = re.search(r"(\d+)\s*天", s or "")
    return (int(years.group(1)) * 365 if years else 0) + (int(days.group(1)) if days else 0)


@dataclass
class FundMeta:
    code: str
    name: str
    ftype: str


class DataSourceError(Exception):
    pass


class FundDataSource:
    def __init__(self, cache_dir: Path, timeout: int = 10):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._quote_cache: dict[str, tuple[float, Quote]] = {}
        self._quote_ttl = 60  # 秒；同一基金一分钟内不重复请求
        self._fund_list: list[FundMeta] | None = None
        self._fund_map: dict[str, FundMeta] = {}
        self._list_lock = asyncio.Lock()
        self._pz_cache: dict[str, tuple[str, str]] = {}  # code -> (日期, 脚本内容)
        self._holdings_cache: dict[str, tuple[str, "FundHoldings"]] = {}
        self._pz_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ HTTP
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": UA, "Referer": "https://fund.eastmoney.com/"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_text(self, url: str, params: dict | None = None, retries: int = 2) -> str:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                session = await self._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status != 200:
                        raise DataSourceError(f"HTTP {resp.status}")
                    return await resp.text(encoding="utf-8", errors="ignore")
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        raise DataSourceError(f"请求失败 {url}: {last_err}")

    # ------------------------------------------------------------ 估值/净值
    @staticmethod
    def parse_valuation(payload: dict) -> dict[str, Quote]:
        if not payload or not payload.get("success"):
            raise DataSourceError("估值接口返回失败")
        out: dict[str, Quote] = {}
        for item in payload.get("data") or []:
            code = str(item.get("FCODE") or "").strip()
            if not code:
                continue
            gztime = item.get("GZTIME")
            gztime = str(gztime)[:16] if gztime not in (None, "", "--") else None
            pdate = item.get("PDATE")
            pdate = str(pdate)[:10] if pdate not in (None, "", "--") else None
            out[code] = Quote(
                code=code,
                name=str(item.get("SHORTNAME") or ""),
                nav=_num(item.get("NAV")),
                nav_date=pdate,
                nav_pct=_num(item.get("NAVCHGRT")),
                est=_num(item.get("GSZ")),
                est_pct=_num(item.get("GSZZL")),
                est_time=gztime,
            )
        return out

    async def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        """批量获取估值和最新净值，带 60 秒缓存。拿不到的代码不会出现在结果里。"""
        now = time.time()
        result: dict[str, Quote] = {}
        need: list[str] = []
        for c in dict.fromkeys(codes):
            hit = self._quote_cache.get(c)
            if hit and now - hit[0] < self._quote_ttl:
                result[c] = hit[1]
            else:
                need.append(c)
        for i in range(0, len(need), 50):
            chunk = need[i : i + 50]
            text = await self._fetch_text(
                VALUATION_URL, params={"FCODES": ",".join(chunk), "FIELDS": VALUATION_FIELDS}
            )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as e:
                raise DataSourceError("估值接口返回的不是 JSON") from e
            for code, q in self.parse_valuation(payload).items():
                self._quote_cache[code] = (now, q)
                result[code] = q
        return result

    # ------------------------------------------------------------ 基金列表
    @staticmethod
    def parse_fund_list(text: str) -> list[FundMeta]:
        m = re.search(r"=\s*(\[.*\])\s*;?\s*$", text.strip(), re.S)
        if not m:
            raise DataSourceError("基金列表格式无法识别")
        rows = json.loads(m.group(1))
        return [FundMeta(code=r[0], name=r[2], ftype=r[3]) for r in rows if len(r) >= 4]

    async def get_fund_list(self) -> list[FundMeta]:
        """全量基金列表，本地缓存 24 小时；下载失败时退回旧缓存。"""
        async with self._list_lock:
            if self._fund_list is not None:
                return self._fund_list
            cache = self.cache_dir / "fund_list.json"
            rows = None
            if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
                rows = json.loads(cache.read_text("utf-8"))
            else:
                try:
                    text = await self._fetch_text(FUND_LIST_URL)
                    metas = self.parse_fund_list(text)
                    rows = [[m.code, m.name, m.ftype] for m in metas]
                    cache.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
                except DataSourceError:
                    if cache.exists():
                        rows = json.loads(cache.read_text("utf-8"))
                    else:
                        raise
            self._fund_list = [FundMeta(code=r[0], name=r[1], ftype=r[2]) for r in rows]
            self._fund_map = {m.code: m for m in self._fund_list}
            return self._fund_list

    async def get_meta(self, code: str) -> FundMeta | None:
        await self.get_fund_list()
        return self._fund_map.get(code)

    # ------------------------------------------------------------ 阶段收益
    @staticmethod
    def parse_period_returns(text: str) -> dict[str, float | None]:
        keys = {"syl_1y": "近1月", "syl_3y": "近3月", "syl_6y": "近6月", "syl_1n": "近1年"}
        out: dict[str, float | None] = {}
        for k, label in keys.items():
            m = re.search(rf'var\s+{k}\s*=\s*"([^"]*)"', text)
            out[label] = _num(m.group(1)) if m else None
        return out

    async def _get_pingzhong(self, code: str) -> str:
        """单只基金详情脚本，按天缓存（阶段收益和走势一天内不会变）。"""
        day = time.strftime("%Y%m%d")
        lock = self._pz_locks.setdefault(code, asyncio.Lock())
        async with lock:  # 同一基金并发请求时只下载一次
            hit = self._pz_cache.get(code)
            if hit and hit[0] == day:
                return hit[1]
            text = await self._fetch_text(PINGZHONG_URL.format(code=code), params={"v": int(time.time())})
            self._pz_cache[code] = (day, text)
            return text

    async def get_period_returns(self, code: str) -> dict[str, float | None]:
        return self.parse_period_returns(await self._get_pingzhong(code))

    # ------------------------------------------------------------ 净值走势
    @staticmethod
    def parse_ac_trend(text: str) -> list[tuple[int, float]]:
        """累计净值走势 [(毫秒时间戳, 累计净值)]。用累计净值算回撤，避免分红造成的假下跌。"""
        m = re.search(r"var\s+Data_ACWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S)
        if not m:
            return []
        try:
            rows = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []
        out = []
        for r in rows:
            if isinstance(r, list) and len(r) >= 2 and r[1] is not None:
                v = _num(r[1])
                if v is not None:
                    out.append((int(r[0]), v))
        return out

    async def get_ac_trend(self, code: str) -> list[tuple[int, float]]:
        return self.parse_ac_trend(await self._get_pingzhong(code))

    @staticmethod
    def parse_unit_trend(text: str) -> list[tuple[int, float]]:
        """单位净值走势 [(毫秒时间戳, 单位净值)]，定投按扣款日的单位净值记账。"""
        rows = extract_js_var(text, "Data_netWorthTrend") or []
        out = []
        for r in rows:
            if isinstance(r, dict) and r.get("x") is not None:
                v = _num(r.get("y"))
                if v is not None:
                    out.append((int(r["x"]), v))
        return out

    async def get_unit_trend(self, code: str) -> list[tuple[int, float]]:
        return self.parse_unit_trend(await self._get_pingzhong(code))

    # ------------------------------------------------------------ 体检资料
    @staticmethod
    def parse_profile(text: str) -> FundProfile:
        stock_ratio = None
        alloc = extract_js_var(text, "Data_assetAllocation")
        if isinstance(alloc, dict):
            for s in alloc.get("series") or []:
                if "股票" in str(s.get("name", "")) and s.get("data"):
                    stock_ratio = _num(s["data"][-1])
        managers = []
        for m in extract_js_var(text, "Data_currentFundManager") or []:
            if isinstance(m, dict) and m.get("name"):
                managers.append((str(m["name"]), parse_work_days(str(m.get("workTime", "")))))
        m1y = re.search(r'var\s+syl_1n\s*=\s*"([^"]*)"', text)
        return FundProfile(stock_ratio, managers, _num(m1y.group(1)) if m1y else None)

    async def get_profile(self, code: str) -> FundProfile:
        return self.parse_profile(await self._get_pingzhong(code))

    @staticmethod
    def parse_holdings(payload: dict) -> FundHoldings:
        if not payload or not payload.get("Success"):
            raise DataSourceError(f"重仓股接口返回失败: {payload.get('ErrMsg') if payload else ''}")
        stocks = []
        for s in (payload.get("Datas") or {}).get("fundStocks") or []:
            w = _num(s.get("JZBL"))
            name = str(s.get("GPJC") or "").strip()
            if w is None or not name:
                continue
            stocks.append(
                StockPos(
                    code=str(s.get("GPDM") or "").strip(),
                    name=name,
                    weight=w,
                    industry=str(s.get("INDEXNAME") or "").strip() or "其他",
                )
            )
        rd = payload.get("Expansion")
        return FundHoldings(str(rd)[:10] if rd else None, stocks[:10])

    async def get_holdings(self, code: str) -> FundHoldings:
        """最新一期季报前十大重仓股，按天缓存。"""
        day = time.strftime("%Y%m%d")
        hit = self._holdings_cache.get(code)
        if hit and hit[0] == day:
            return hit[1]
        text = await self._fetch_text(
            HOLDINGS_URL,
            params={"FCODE": code, "deviceid": "Wap", "plat": "WAP", "product": "EFund", "version": "2.0.0"},
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise DataSourceError("重仓股接口返回的不是 JSON") from e
        h = self.parse_holdings(payload)
        self._holdings_cache[code] = (day, h)
        return h
