"""
实时行情 (盘中快照)
==================
策略:
  1. 优先用 ak.stock_zh_a_spot_em() 一次性拉全市场 (eastmoney)
  2. 失败回退新浪逐只查 ak.stock_zh_a_spot()  (或 stock_individual_info)
  3. 5 秒内存缓存,避免页面频繁刷新打爆上游
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore

import pandas as pd

log = logging.getLogger("ashare_agent")


# ---------- 缓存 ----------
_SPOT_CACHE: dict[str, dict] = {}    # code -> {price, chg, chg_pct, volume, ...}
_SPOT_CACHE_TS: float = 0
_SPOT_CACHE_TTL = 5.0                # 秒
_LOCK = threading.Lock()


# ---------- 是否交易时段 ----------
def is_trading_hours(now: datetime | None = None) -> bool:
    """简化判断: 周一到周五 9:25-15:05"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    morning = (t.hour == 9 and t.minute >= 25) or (t.hour == 10) or (t.hour == 11 and t.minute < 30)
    afternoon = (t.hour == 13) or (t.hour == 14) or (t.hour == 15 and t.minute <= 5)
    return morning or afternoon


# ---------- 拉全市场快照 ----------
def _fetch_spot_all() -> dict[str, dict]:
    """返回 {code: {...}} 字典"""
    if ak is None:
        raise RuntimeError("akshare 未安装")
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        return {}

    # 列名: 代码/名称/最新价/涨跌幅/涨跌额/成交量/成交额/振幅/最高/最低/今开/昨收 ...
    df = df.rename(columns={
        "代码": "code", "名称": "name",
        "最新价": "price", "涨跌幅": "chg_pct", "涨跌额": "chg",
        "成交量": "volume", "成交额": "amount",
        "最高": "high", "最低": "low", "今开": "open", "昨收": "prev_close",
        "换手率": "turnover", "市盈率-动态": "pe",
    })
    df["code"] = df["code"].astype(str).str.zfill(6)

    cols = ["code", "name", "price", "chg_pct", "chg",
            "volume", "amount", "high", "low", "open", "prev_close"]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].dropna(subset=["code"])

    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        result[row["code"]] = {k: (None if pd.isna(row[k]) else
                                   (float(row[k]) if k != "code" and k != "name" else row[k]))
                               for k in cols}
    return result


# ---------- 公开 API ----------
def get_quotes(codes: list[str]) -> dict[str, dict]:
    """
    返回给定股票代码的实时快照。
    输出: {code: {price, chg_pct, ...}} 缺失代码会被跳过。
    使用 5s 缓存,首次调用约 1-3 秒,后续命中缓存几乎瞬时。
    """
    global _SPOT_CACHE, _SPOT_CACHE_TS

    with _LOCK:
        fresh = (time.time() - _SPOT_CACHE_TS) < _SPOT_CACHE_TTL and _SPOT_CACHE
        if fresh:
            return {c: _SPOT_CACHE[c] for c in codes if c in _SPOT_CACHE}

    # 拉新
    try:
        snapshot = _fetch_spot_all()
        with _LOCK:
            _SPOT_CACHE = snapshot
            _SPOT_CACHE_TS = time.time()
        log.info("实时快照刷新: %d 只", len(snapshot))
        return {c: snapshot[c] for c in codes if c in snapshot}
    except Exception as e:
        log.warning("实时快照拉取失败: %s", e)
        # 失败时返回旧缓存 (better than nothing)
        with _LOCK:
            return {c: _SPOT_CACHE[c] for c in codes if c in _SPOT_CACHE}


def force_refresh():
    """绕过缓存,立即重拉"""
    with _LOCK:
        global _SPOT_CACHE_TS
        _SPOT_CACHE_TS = 0
