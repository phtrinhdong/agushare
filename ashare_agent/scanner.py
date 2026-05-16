"""
扫描器：并发扫描股票池 → 聚合形态/指标信号 → 评分排序
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from tqdm import tqdm

from . import data_loader
from .patterns import detect_candlestick_patterns, detect_indicator_signals

log = logging.getLogger("ashare_agent")


@dataclass
class SignalRow:
    code: str
    name: str
    close: float
    chg_pct: float
    score: int
    candlestick: list[str] = field(default_factory=list)
    indicators: list[str] = field(default_factory=list)
    df: pd.DataFrame | None = None  # 保留用于画图

    def to_dict(self, include_df: bool = False) -> dict[str, Any]:
        d = {
            "代码": self.code,
            "名称": self.name,
            "收盘价": round(self.close, 2),
            "涨跌幅%": round(self.chg_pct, 2),
            "综合得分": self.score,
            "K线形态": ",".join(self.candlestick),
            "技术指标": ",".join(self.indicators),
        }
        if include_df:
            d["df"] = self.df
        return d


# ============================================================
# 单只股票扫描
# ============================================================
def scan_one(
    code: str,
    name: str,
    df: pd.DataFrame,
    pattern_cfg: dict,
    indicator_cfg: dict,
) -> SignalRow | None:
    if df is None or len(df) < 30:
        return None

    candles = detect_candlestick_patterns(df, pattern_cfg)
    inds = detect_indicator_signals(df, indicator_cfg)

    if not candles and not inds:
        return None

    score = sum(h["score"] for h in candles) + sum(h["score"] for h in inds)

    last = df.iloc[-1]
    prev_close = df["close"].iloc[-2] if len(df) > 1 else last["close"]
    chg = (last["close"] - prev_close) / prev_close * 100 if prev_close else 0.0

    return SignalRow(
        code=code, name=name,
        close=float(last["close"]),
        chg_pct=float(chg),
        score=score,
        candlestick=[h["desc"] for h in candles],
        indicators=[h["desc"] for h in inds],
        df=df,
    )


# ============================================================
# 批量扫描
# ============================================================
def scan_market(cfg: dict) -> list[SignalRow]:
    universe_cfg = cfg["universe"]
    data_cfg = cfg["data"]
    pattern_cfg = cfg["patterns"]
    indicator_cfg = cfg["patterns"]  # 同 yaml 节
    filt = cfg["filter"]
    runtime_cfg = cfg.get("runtime", {})

    # ---- 选股池 ----
    if universe_cfg.get("scan_all", True):
        stock_df = data_loader.get_stock_list(
            exclude_chinext_star=universe_cfg.get("exclude_chinext_star", False),
            exclude_st=universe_cfg.get("exclude_st", True),
            min_listed_days=universe_cfg.get("min_listed_days", 120),
        )
        codes = stock_df["code"].tolist()
        names = dict(zip(stock_df["code"], stock_df["name"]))
    else:
        codes = [str(c).zfill(6) for c in universe_cfg.get("watchlist", [])]
        # 仅这些股票，名称用 akshare 查一下
        try:
            stock_df = data_loader.get_stock_list(exclude_st=False)
            names = dict(zip(stock_df["code"], stock_df["name"]))
        except Exception:
            names = {}
    log.info("准备扫描 %d 只股票", len(codes))

    bars = int(data_cfg.get("bars", 120))
    adjust = data_cfg.get("adjust", "qfq")
    cache_dir = data_cfg.get("cache_dir", "cache")
    ttl = int(data_cfg.get("cache_ttl_hours", 6))
    workers = int(runtime_cfg.get("max_workers", 8))

    # ---- 并发拉数据 + 扫描 ----
    def _job(code: str) -> SignalRow | None:
        df = data_loader.fetch_kline(code, bars=bars, adjust=adjust,
                                     cache_dir=cache_dir, cache_ttl_hours=ttl)
        return scan_one(code, names.get(code, code), df, pattern_cfg, indicator_cfg)

    results: list[SignalRow] = []
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_job, c): c for c in codes}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="扫描"):
            try:
                r = fut.result()
            except Exception as e:  # noqa
                log.debug("扫描异常 %s: %s", futures[fut], e)
                continue
            if r:
                results.append(r)

    # ---- 过滤 ----
    min_score = int(filt.get("min_score", 0))
    results = [r for r in results if r.score >= min_score]
    if filt.get("require_candlestick", False):
        results = [r for r in results if r.candlestick]
    if filt.get("require_indicator", False):
        results = [r for r in results if r.indicators]

    results.sort(key=lambda x: (-x.score, x.code))
    top_n = int(filt.get("top_n", 50))
    if top_n > 0:
        results = results[:top_n]

    log.info("命中信号股票: %d 只", len(results))
    return results
