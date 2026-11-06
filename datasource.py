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
# 单只基金的详情脚本（阶段收益、净值走势等）
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"


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

    async def get_period_returns(self, code: str) -> dict[str, float | None]:
        text = await self._fetch_text(PINGZHONG_URL.format(code=code), params={"v": int(time.time())})
        return self.parse_period_returns(text)
