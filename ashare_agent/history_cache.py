"""
历史数据库 (深度 K 线缓存)
==========================
独立于扫描用的短期 cache,专门给"K型回测观察"用。

设计:
  - 存放位置: data/history/{code}_{adjust}.parquet
  - 默认 800 根 K 线 (~3.2 年)
  - 默认 7 天 TTL (一周自动失效,可手动 refresh)
  - 与 data/cache/ 分开:
      cache/   → 每天/每次扫描频繁刷新,150 根
      history/ → 偶尔批量更新一次,几百根

读取策略:
  pattern_observer 调用 load() 拿 df:
    - 文件存在且新鲜  → 直接读
    - 文件存在但过期  → 也读 (用旧数据),并标记可刷新
    - 不存在          → 返回 None, 调用方降级到 fetch_kline
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd
from tqdm import tqdm

from . import data_loader
from .utils import ensure_dir, project_path

log = logging.getLogger("ashare_agent")

# ---------- 配置 (这些值可由 config.yaml 覆盖) ----------
HISTORY_SUBDIR = "history"               # 相对 data 目录的子目录
DEFAULT_BARS = 800                       # ≈3.2 年
DEFAULT_TTL_DAYS = 7                     # 一周内的不再重拉


# ---------- 路径 ----------
def _resolve_history_dir() -> Path:
    # data/history (相对 project root)
    return ensure_dir(f"data/{HISTORY_SUBDIR}")


def _file_path(code: str, adjust: str = "qfq") -> Path:
    return _resolve_history_dir() / f"{code}_{adjust or 'raw'}.parquet"


# ---------- 元信息 ----------
def age_days(code: str, adjust: str = "qfq") -> float | None:
    fp = _file_path(code, adjust)
    if not fp.exists():
        return None
    mtime = datetime.fromtimestamp(fp.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 86400


def is_fresh(code: str, adjust: str = "qfq", ttl_days: int = DEFAULT_TTL_DAYS) -> bool:
    age = age_days(code, adjust)
    return age is not None and age < ttl_days


# ---------- 读 / 写 ----------
def load(code: str, adjust: str = "qfq",
         min_bars: int = 0) -> pd.DataFrame | None:
    """读历史缓存。文件不存在返回 None。"""
    fp = _file_path(code, adjust)
    if not fp.exists():
        return None
    try:
        df = pd.read_parquet(fp)
        if min_bars and len(df) < min_bars:
            return None
        return df
    except Exception as e:
        log.warning("读 history %s 失败: %s", code, e)
        return None


def save(code: str, df: pd.DataFrame, adjust: str = "qfq") -> None:
    fp = _file_path(code, adjust)
    try:
        df.to_parquet(fp)
    except Exception as e:
        log.warning("写 history %s 失败: %s", code, e)


# ---------- 单股刷新 ----------
def refresh_one(code: str, bars: int = DEFAULT_BARS,
                adjust: str = "qfq") -> bool:
    """从 akshare 拉取 bars 根日K,写入历史缓存。返回是否成功。"""
    # 调用 fetch_kline 时让它强制刷新 (传 ttl=0)
    # 但 fetch_kline 内部会写它自己的短期 cache, 这里我们额外写一份到 history
    df = data_loader.fetch_kline(
        code, bars=bars, adjust=adjust,
        cache_dir="cache",   # 共用一份短期 parquet, 不浪费
        cache_ttl_hours=0,   # 强制重新拉
    )
    if df is None or df.empty:
        return False
    save(code, df, adjust=adjust)
    return True


# ---------- 批量刷新 ----------
def bulk_refresh(
    codes: list[str],
    bars: int = DEFAULT_BARS,
    adjust: str = "qfq",
    workers: int = 3,
    on_progress: Callable[[dict], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
    skip_fresh: bool = True,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict:
    """
    批量更新历史数据库。
    skip_fresh=True 时,已经新鲜的代码不重拉。
    """
    log.info("history 批量刷新: %d 只, bars=%d, skip_fresh=%s",
             len(codes), bars, skip_fresh)

    total = len(codes)
    ok = 0
    fail = 0
    skipped = 0

    def _job(code: str):
        if skip_fresh and is_fresh(code, adjust, ttl_days):
            return code, "skipped"
        success = refresh_one(code, bars=bars, adjust=adjust)
        return code, "ok" if success else "fail"

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_job, c): c for c in codes}
        for fut in tqdm(as_completed(futures), total=total, desc="history刷新"):
            if stop_flag is not None and stop_flag():
                for f in futures:
                    if not f.done():
                        f.cancel()
                log.info("history 刷新收到停止信号")
                break
            try:
                code, status = fut.result()
            except Exception as e:
                log.debug("history 刷新 %s 失败: %s", futures[fut], e)
                fail += 1
                continue
            if status == "ok":
                ok += 1
            elif status == "skipped":
                skipped += 1
            else:
                fail += 1

            if on_progress is not None:
                try:
                    on_progress({
                        "total": total,
                        "scanned": ok + fail + skipped,
                        "ok": ok, "fail": fail, "skipped": skipped,
                    })
                except Exception:
                    pass

    log.info("history 批量完成: ok=%d skipped=%d fail=%d", ok, skipped, fail)
    return {"total": total, "ok": ok, "fail": fail, "skipped": skipped}


# ---------- 统计 ----------
def stats() -> dict:
    """汇总历史数据库的覆盖情况"""
    dir_ = _resolve_history_dir()
    if not dir_.exists():
        return {"total_files": 0, "total_size_mb": 0, "oldest_days": None,
                "newest_days": None, "avg_age_days": None}
    files = list(dir_.glob("*.parquet"))
    if not files:
        return {"total_files": 0, "total_size_mb": 0, "oldest_days": None,
                "newest_days": None, "avg_age_days": None}
    now = time.time()
    ages = [(now - f.stat().st_mtime) / 86400 for f in files]
    return {
        "total_files": len(files),
        "total_size_mb": round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1),
        "oldest_days": round(max(ages), 1),
        "newest_days": round(min(ages), 1),
        "avg_age_days": round(sum(ages) / len(ages), 1),
    }
