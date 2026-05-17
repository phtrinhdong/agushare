"""
后台 K 线预拉
=============
设计目标:
  全市场 ~5000 只股票,按低速率轮询拉取日K线,写入本地 parquet 缓存。
  - 缓存新鲜 (< TTL) 的代码自动跳过,不浪费网络
  - 控制速率避免被 akshare 上游限流
  - 暂停/继续可由信号控制
  - 与"特别关注"实时行情错峰

调用方:
  server.py 启动时如 prefetch.enabled = true 则起 daemon 线程。

运行节奏:
  默认 ~30 次/分钟 (每 2 秒 1 只),全市场 5000 只一轮约 2.8 小时。
  TTL 6h 内必被覆盖。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import data_loader, watchlist as wl

log = logging.getLogger("ashare_agent")


@dataclass
class PrefetchState:
    enabled: bool = False
    running: bool = False
    paused: bool = False
    scope: str = "all"               # all / watchlist / signals
    rate_per_min: int = 30
    cache_ttl_hours: int = 6
    pause_during_trading: bool = False

    # 统计
    cycle: int = 0
    pos: int = 0                     # 当前轮次的位置
    total_codes: int = 0
    skipped_fresh: int = 0           # 本轮因缓存新鲜跳过
    fetched: int = 0                 # 本轮实际拉取
    failed: int = 0                  # 本轮失败
    cycle_started_at: float = 0
    last_action_at: float = 0
    last_message: str = "idle"

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "enabled": self.enabled,
                "running": self.running,
                "paused": self.paused,
                "scope": self.scope,
                "rate_per_min": self.rate_per_min,
                "cycle": self.cycle,
                "progress": {
                    "pos": self.pos,
                    "total": self.total_codes,
                    "skipped_fresh": self.skipped_fresh,
                    "fetched": self.fetched,
                    "failed": self.failed,
                },
                "cycle_elapsed_sec": round(now - self.cycle_started_at, 0) if self.cycle_started_at else 0,
                "last_message": self.last_message,
                "last_action_iso": (datetime.fromtimestamp(self.last_action_at).isoformat(timespec="seconds")
                                    if self.last_action_at else None),
            }


STATE = PrefetchState()
_STOP_EVENT = threading.Event()


# ---------- 工具 ----------
def _is_trading_hours() -> bool:
    """简化判断, 沿用 realtime 的"""
    try:
        from . import realtime
        return realtime.is_trading_hours()
    except Exception:
        return False


def _resolve_codes(cfg: dict) -> list[str]:
    """根据 scope 解析要预拉的代码"""
    scope = STATE.scope
    if scope == "watchlist":
        items = wl.load_all()
        return [it["code"] for it in items]
    elif scope == "signals":
        # 从最近一次扫描的命中股票
        try:
            from . import storage
            data = storage.load_latest(cfg["output"].get("reports_dir", "output/reports"))
            if data:
                return [r["code"] for r in data.get("results", [])]
        except Exception:
            pass
        return []
    else:  # all
        try:
            df = data_loader.get_stock_list(
                exclude_chinext_star=cfg["universe"].get("exclude_chinext_star", False),
                exclude_st=cfg["universe"].get("exclude_st", True),
                cache_dir=cfg["data"].get("cache_dir", "cache"),
            )
            return df["code"].tolist()
        except Exception as e:
            log.warning("prefetcher 拿不到股票列表: %s", e)
            return []


# ---------- 主循环 ----------
def _loop(cfg: dict):
    bars = int(cfg["data"].get("bars", 120))
    adjust = cfg["data"].get("adjust", "qfq")
    cache_dir = cfg["data"].get("cache_dir", "cache")
    ttl = STATE.cache_ttl_hours

    log.info("[prefetcher] 启动 scope=%s rate=%d/min ttl=%dh",
             STATE.scope, STATE.rate_per_min, ttl)

    with STATE._lock:
        STATE.running = True

    while not _STOP_EVENT.is_set():
        # 暂停检查
        if STATE.paused or (STATE.pause_during_trading and _is_trading_hours()):
            STATE.last_message = "已暂停" if STATE.paused else "交易时段暂停"
            _STOP_EVENT.wait(30)
            continue

        codes = _resolve_codes(cfg)
        if not codes:
            STATE.last_message = "无候选代码"
            _STOP_EVENT.wait(60)
            continue

        # 新一轮
        with STATE._lock:
            STATE.cycle += 1
            STATE.pos = 0
            STATE.total_codes = len(codes)
            STATE.skipped_fresh = 0
            STATE.fetched = 0
            STATE.failed = 0
            STATE.cycle_started_at = time.time()
            STATE.last_message = f"第 {STATE.cycle} 轮启动: {len(codes)} 只"

        # 计算每只之间的间隔
        interval = 60.0 / max(1, STATE.rate_per_min)

        # 打乱顺序,避免每次都从 000001 开始
        random.shuffle(codes)

        for code in codes:
            if _STOP_EVENT.is_set():
                break
            if STATE.paused or (STATE.pause_during_trading and _is_trading_hours()):
                STATE.last_message = "已暂停"
                _STOP_EVENT.wait(15)
                continue

            # 检查缓存是否已经新鲜,新鲜就跳过 (零网络)
            cache_fp = Path(cache_dir)
            if not cache_fp.is_absolute():
                from .utils import project_path
                cache_fp = project_path(str(cache_fp))
            parquet = cache_fp / f"{code}_{adjust or 'raw'}.parquet"
            if data_loader._is_cache_fresh(parquet, ttl):
                with STATE._lock:
                    STATE.skipped_fresh += 1
                    STATE.pos += 1
                continue

            # 拉一只 (走与扫描相同的 fetch_kline,自带 EM->sina 回退)
            t0 = time.time()
            try:
                df = data_loader.fetch_kline(
                    code, bars=bars, adjust=adjust,
                    cache_dir=cache_dir, cache_ttl_hours=ttl,
                )
                with STATE._lock:
                    if df is not None:
                        STATE.fetched += 1
                    else:
                        STATE.failed += 1
                    STATE.pos += 1
                    STATE.last_action_at = time.time()
                    STATE.last_message = (
                        f"第 {STATE.cycle} 轮 · {STATE.pos}/{STATE.total_codes} · "
                        f"已拉 {STATE.fetched} · 跳过 {STATE.skipped_fresh} · 失败 {STATE.failed}"
                    )
            except Exception as e:
                with STATE._lock:
                    STATE.failed += 1
                    STATE.pos += 1
                log.debug("prefetcher 拉 %s 失败: %s", code, e)

            # 速率控制 - 减去本次拉数据耗时
            elapsed = time.time() - t0
            sleep_for = max(0, interval - elapsed)
            if sleep_for > 0:
                _STOP_EVENT.wait(sleep_for)

        # 一轮结束,稍歇
        with STATE._lock:
            STATE.last_message = (
                f"第 {STATE.cycle} 轮完成: 拉 {STATE.fetched} 只, "
                f"跳过 {STATE.skipped_fresh}, 失败 {STATE.failed}"
            )
        log.info("[prefetcher] %s", STATE.last_message)
        _STOP_EVENT.wait(60)   # 1 分钟后开始下一轮 (会重新走 _is_cache_fresh 大部分跳过)

    with STATE._lock:
        STATE.running = False
    log.info("[prefetcher] 已停止")


# ---------- 控制接口 ----------
def start(cfg: dict) -> threading.Thread | None:
    pf_cfg = cfg.get("prefetch") or {}
    if not pf_cfg.get("enabled", False):
        log.info("[prefetcher] 配置未启用,不启动")
        return None

    with STATE._lock:
        if STATE.running:
            log.warning("[prefetcher] 已在运行")
            return None
        STATE.enabled = True
        STATE.scope = pf_cfg.get("scope", "all")
        STATE.rate_per_min = int(pf_cfg.get("rate_per_min", 30))
        STATE.cache_ttl_hours = int(pf_cfg.get("cache_ttl_hours",
                                                cfg["data"].get("cache_ttl_hours", 6)))
        STATE.pause_during_trading = bool(pf_cfg.get("pause_during_trading", False))

    _STOP_EVENT.clear()
    t = threading.Thread(target=_loop, args=(cfg,), daemon=True, name="prefetcher")
    t.start()
    return t


def stop():
    _STOP_EVENT.set()


def pause():
    with STATE._lock:
        STATE.paused = True


def resume():
    with STATE._lock:
        STATE.paused = False
