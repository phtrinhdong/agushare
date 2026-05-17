"""
形态历史信号回溯 (K型回测观察)
==============================
给定一个形态,把它在历史区间 [end - lookback, end] 内**所有**触发记录找出来,
为每条记录计算触发日后 N 个交易日的累计涨跌幅。

与 backtest.py 的区别:
  backtest.py: 模拟交易,有止盈止损/持有期,聚合胜率/收益率统计
  pattern_observer.py: 不假设交易策略,只记录"形态触发"事件 + 多个时间窗的真实表现,
                       让用户从"客观事件后股价怎么走"角度评估形态

输出 schema (每条命中记录):
  {
    "code", "name",
    "signal_date":  "2024-03-15",
    "entry_price":  1700.00,         # 命中日收盘价
    "score":        11,
    "returns": {
      5:   3.45,    # 5 个交易日后涨跌幅 %
      10:  6.20,
      20:  12.40,
      40:  18.30,
      60:  -2.10,
      120: 35.60
    }
  }
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import pandas as pd
from tqdm import tqdm

from . import data_loader
from .patterns.registry import PATTERN_REGISTRY, INDICATOR_REGISTRY

log = logging.getLogger("ashare_agent")

DEFAULT_HORIZONS = [5, 10, 20, 40, 60, 120]
MIN_HISTORY_FOR_DETECT = 130   # 平氏1号需要 130 根 (MA120)


def _resolve_pattern(pattern_name: str):
    if pattern_name in PATTERN_REGISTRY:
        return PATTERN_REGISTRY[pattern_name]
    if pattern_name in INDICATOR_REGISTRY:
        return INDICATOR_REGISTRY[pattern_name]
    return None


def _observe_one_stock(
    code: str,
    name: str,
    df: pd.DataFrame,
    pattern,
    lookback_days: int,
    horizons: list[int],
    min_history: int = MIN_HISTORY_FOR_DETECT,
) -> list[dict]:
    """对一只股票做完整回溯,返回所有命中事件"""
    out: list[dict] = []
    if df is None or len(df) < min_history + 1:
        return out

    n = len(df)
    max_horizon = max(horizons)

    # 候选信号日的范围:
    # - 必须有至少 min_history 根历史 (i >= min_history - 1)
    # - 但允许命中日之后部分 horizon 还没数据 (用 None 占位)
    earliest = max(min_history - 1, n - 1 - lookback_days)
    latest = n - 1

    for i in range(earliest, latest + 1):
        # 给 detect 函数喂"截至 i 那天的视角",窗口 150 根足够
        sub_df = df.iloc[max(0, i - 149): i + 1]
        if len(sub_df) < min_history:
            continue

        ok, score, desc = pattern.evaluate(sub_df)
        if not ok:
            continue

        signal_date = df.index[i]
        entry_price = float(df["close"].iloc[i])
        if entry_price <= 0:
            continue

        # 计算各 horizon 的未来收益
        returns: dict[int, float | None] = {}
        for h in horizons:
            if i + h < n:
                future_price = float(df["close"].iloc[i + h])
                returns[h] = (future_price - entry_price) / entry_price * 100
            else:
                returns[h] = None    # 数据不够

        out.append({
            "code": code,
            "name": name,
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "entry_price": round(entry_price, 3),
            "score": int(score),
            "returns": {str(h): (None if v is None else round(v, 2))
                        for h, v in returns.items()},
        })
    return out


def observe_pattern(
    pattern_name: str,
    codes: list[str],
    names: dict[str, str],
    lookback_days: int = 250,
    horizons: list[int] = None,
    adjust: str = "qfq",
    cache_dir: str = "cache",
    cache_ttl_hours: int = 24,
    workers: int = 3,
    on_progress: Callable[[dict], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> dict:
    """
    对给定形态做历史回溯。
    """
    pattern = _resolve_pattern(pattern_name)
    if pattern is None:
        raise ValueError(f"未注册的形态: {pattern_name}")

    horizons = sorted(horizons or DEFAULT_HORIZONS)
    max_horizon = max(horizons)

    # 每只股票拉的 K 线数 = 回望期 + 最远期 + 历史余量
    bars_needed = lookback_days + max_horizon + MIN_HISTORY_FOR_DETECT + 20
    log.info("观察 %s · 股票池 %d · 回望 %d 天 · 拉 %d 根/股",
             pattern_name, len(codes), lookback_days, bars_needed)

    all_events: list[dict] = []
    failed_codes: list[str] = []

    def _job(code: str):
        df = data_loader.fetch_kline(
            code, bars=bars_needed, adjust=adjust,
            cache_dir=cache_dir, cache_ttl_hours=cache_ttl_hours,
        )
        if df is None or df.empty:
            return code, None, "no_data"
        events = _observe_one_stock(code, names.get(code, code), df,
                                    pattern, lookback_days, horizons)
        return code, events, "ok"

    total = len(codes)
    scanned = 0
    ok_cnt = 0

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_job, c): c for c in codes}
        pbar = tqdm(as_completed(futures), total=total, desc="观察")
        for fut in pbar:
            if stop_flag is not None and stop_flag():
                for f in futures:
                    if not f.done():
                        f.cancel()
                log.info("观察收到停止信号")
                break
            try:
                code, events, status = fut.result()
            except Exception as e:
                log.debug("观察异常 %s: %s", futures[fut], e)
                failed_codes.append(futures[fut])
                scanned += 1
                continue

            if status == "no_data":
                failed_codes.append(code)
            else:
                ok_cnt += 1
                if events:
                    all_events.extend(events)
            scanned += 1

            if on_progress is not None:
                try:
                    on_progress({
                        "total": total,
                        "scanned": scanned,
                        "ok": ok_cnt,
                        "fail": len(failed_codes),
                        "events": len(all_events),
                    })
                except Exception:
                    pass

    log.info("观察完成: 处理 %d 只, 命中事件 %d 条",
             scanned, len(all_events))

    return {
        "pattern": pattern_name,
        "pattern_desc": pattern.desc,
        "lookback_days": lookback_days,
        "horizons": horizons,
        "events": all_events,
        "stats": _aggregate(all_events, horizons),
        "failed_codes": failed_codes,
    }


def _aggregate(events: list[dict], horizons: list[int]) -> dict:
    """每个 horizon 算: 命中数,平均收益,胜率,中位收益"""
    if not events:
        return {"count": 0}

    out = {"count": len(events), "horizons": {}}
    for h in horizons:
        vals = [e["returns"].get(str(h)) for e in events]
        vals = [v for v in vals if v is not None]
        if not vals:
            out["horizons"][h] = {"count": 0}
            continue
        wins = sum(1 for v in vals if v > 0)
        out["horizons"][h] = {
            "count": len(vals),
            "win_rate": round(wins / len(vals) * 100, 1),
            "avg_return": round(sum(vals) / len(vals), 2),
            "median": round(sorted(vals)[len(vals) // 2], 2),
            "max_win": round(max(vals), 2),
            "max_loss": round(min(vals), 2),
        }
    return out
