"""
扫描器：并发扫描股票池 → 聚合形态/指标信号 → 评分排序
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

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
    chg_pct: float            # 当日涨跌幅
    chg_5d: float | None = None    # 5 个交易日  (≈1 周)
    chg_15d: float | None = None   # 15 个交易日 (≈3 周)
    chg_30d: float | None = None   # 30 个交易日 (≈1.5 月)
    score: int = 0
    candlestick: list[str] = field(default_factory=list)
    indicators: list[str] = field(default_factory=list)
    df: pd.DataFrame | None = None  # 保留用于画图

    def to_dict(self, include_df: bool = False) -> dict[str, Any]:
        d = {
            "代码": self.code,
            "名称": self.name,
            "收盘价": round(self.close, 2),
            "涨跌幅%": round(self.chg_pct, 2),
            "5日%": _round(self.chg_5d),
            "15日%": _round(self.chg_15d),
            "30日%": _round(self.chg_30d),
            "综合得分": self.score,
            "K线形态": ",".join(self.candlestick),
            "技术指标": ",".join(self.indicators),
        }
        if include_df:
            d["df"] = self.df
        return d


def _round(v):
    return None if v is None else round(v, 2)


def _load_watchlist_codes() -> list[str]:
    """读 UI 管理的特别关注列表的代码,文件不存在或空则返回 []"""
    try:
        from . import watchlist as wl
        items = wl.load_all()
        return [it["code"] for it in items if it.get("code")]
    except Exception:
        return []


def _short_term_gains(df: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    """返回 (5日, 15日, 30日) 涨跌幅 %,数据不足返回 None"""
    closes = df["close"]
    cur = closes.iloc[-1]
    def chg(n: int):
        if len(closes) < n + 1: return None
        past = closes.iloc[-1 - n]
        return (cur - past) / past * 100 if past > 0 else None
    return chg(5), chg(15), chg(30)


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
    chg5, chg15, chg30 = _short_term_gains(df)

    return SignalRow(
        code=code, name=name,
        close=float(last["close"]),
        chg_pct=float(chg),
        chg_5d=chg5, chg_15d=chg15, chg_30d=chg30,
        score=score,
        candlestick=[h["desc"] for h in candles],
        indicators=[h["desc"] for h in inds],
        df=df,
    )


# ============================================================
# 批量扫描
# ============================================================
def scan_market(
    cfg: dict,
    on_result: Callable[[SignalRow], None] | None = None,
    on_progress: Callable[[dict], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> list[SignalRow]:
    """
    扫描全市场 / 自选股。
    on_result(row)    每命中一只股票回调一次 (实时推到 Web)
    on_progress(stat) 每只处理完回调一次
    stop_flag()       周期性检查;若返回 True 则中途停止,已完成的结果照常保留
    """
    universe_cfg = cfg["universe"]
    data_cfg = cfg["data"]
    pattern_cfg = cfg["patterns"]
    indicator_cfg = cfg["patterns"]  # 同 yaml 节
    filt = cfg["filter"]
    runtime_cfg = cfg.get("runtime", {})

    # ---- 选股池 ----
    cache_dir_cfg = data_cfg.get("cache_dir", "cache")
    if universe_cfg.get("scan_all", True):
        try:
            stock_df = data_loader.get_stock_list(
                exclude_chinext_star=universe_cfg.get("exclude_chinext_star", False),
                exclude_st=universe_cfg.get("exclude_st", True),
                min_listed_days=universe_cfg.get("min_listed_days", 120),
                cache_dir=cache_dir_cfg,
            )
        except Exception as e:
            log.error("无法获取股票列表,扫描中止: %s", e)
            return []
        codes = stock_df["code"].tolist()
        names = dict(zip(stock_df["code"], stock_df["name"]))
    else:
        # 优先用 UI 管理的"特别关注"列表 (data/watchlist.json)
        # 为空时降级到 config.yaml 的 universe.watchlist (向后兼容)
        codes = _load_watchlist_codes()
        if not codes:
            codes = [str(c).zfill(6) for c in universe_cfg.get("watchlist", [])]
        try:
            stock_df = data_loader.get_stock_list(
                exclude_st=False, cache_dir=cache_dir_cfg,
            )
            names = dict(zip(stock_df["code"], stock_df["name"]))
        except Exception:
            names = {}
    log.info("准备扫描 %d 只股票", len(codes))

    # 每次扫描重置统计 (尤其 EM 熔断状态)
    data_loader.reset_stats()

    bars = int(data_cfg.get("bars", 120))
    adjust = data_cfg.get("adjust", "qfq")
    cache_dir = data_cfg.get("cache_dir", "cache")
    ttl = int(data_cfg.get("cache_ttl_hours", 6))
    workers = int(runtime_cfg.get("max_workers", 3))

    # ---- 并发拉数据 + 扫描 ----
    failed_codes: list[str] = []

    def _job(code: str):
        df = data_loader.fetch_kline(code, bars=bars, adjust=adjust,
                                     cache_dir=cache_dir, cache_ttl_hours=ttl)
        if df is None:
            return ("fail", code, None)
        return ("ok", code,
                scan_one(code, names.get(code, code), df, pattern_cfg, indicator_cfg))

    results: list[SignalRow] = []
    ok_cnt = 0
    total = len(codes)
    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_job, c): c for c in codes}
        pbar = tqdm(as_completed(futures), total=total, desc="扫描")
        for fut in pbar:
            # 检查停止信号
            if stop_flag is not None and stop_flag():
                stopped = True
                # 取消还没开始的任务,正在跑的让它们自然结束
                for f in futures:
                    if not f.done():
                        f.cancel()
                log.info("收到停止信号,已取消剩余 future")
                break
            try:
                status, code, r = fut.result()
            except Exception as e:  # noqa
                log.debug("扫描异常 %s: %s", futures[fut], e)
                failed_codes.append(futures[fut])
            else:
                if status == "fail":
                    failed_codes.append(code)
                else:
                    ok_cnt += 1
                    if r:
                        results.append(r)
                        if on_result is not None:
                            try:
                                on_result(r)
                            except Exception:  # noqa
                                pass

            # 每只都推送进度 (Web 端用,代价很低)
            if on_progress is not None:
                try:
                    on_progress({
                        "total": total,
                        "scanned": ok_cnt + len(failed_codes),
                        "ok": ok_cnt,
                        "fail": len(failed_codes),
                        "hit": len(results),
                    })
                except Exception:  # noqa
                    pass

            if (ok_cnt + len(failed_codes)) % 200 == 0:
                pbar.set_postfix(ok=ok_cnt, fail=len(failed_codes),
                                 hit=len(results))

    log.info("扫描%s: 成功 %d / 失败 %d / 命中信号 %d",
             "已停止" if stopped else "完成",
             ok_cnt, len(failed_codes), len(results))

    # 把失败列表落盘,便于补扫
    if failed_codes:
        try:
            from .utils import ensure_dir, project_path
            out_dir = ensure_dir(cfg["output"].get("reports_dir", "output/reports"))
            fp = out_dir / "failed_codes.txt"
            fp.write_text("\n".join(failed_codes), encoding="utf-8")
            log.info("失败股票代码已写入: %s (%d 个)", fp, len(failed_codes))
        except Exception as e:  # noqa
            log.debug("写 failed_codes.txt 失败: %s", e)

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
