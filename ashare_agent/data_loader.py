"""数据获取：akshare 拉取 A股股票列表 & 日K线，本地 parquet 缓存"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import akshare as ak
except ImportError:  # 允许在没装包的环境中 import 模块本身
    ak = None  # type: ignore

from .utils import ensure_dir, project_path

log = logging.getLogger("ashare_agent")


# ============================================================
# 股票池
# ============================================================
_STOCK_LIST_CACHE_FILE = "stock_list.parquet"
_STOCK_LIST_CACHE_TTL_HOURS = 24
_STOCK_LIST_RETRY = 3


def _fetch_stock_list_raw() -> pd.DataFrame:
    """直接调 akshare,带重试。"""
    last_err = None
    for attempt in range(_STOCK_LIST_RETRY):
        try:
            time.sleep(random.uniform(0.1, 0.5))
            df = ak.stock_info_a_code_name()
            if df is None or df.empty:
                raise RuntimeError("akshare 返回空")
            return df
        except Exception as e:  # noqa
            last_err = e
            log.warning("get_stock_list 第 %d 次失败: %s", attempt + 1, e)
            time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    raise RuntimeError(f"get_stock_list 重试 {_STOCK_LIST_RETRY} 次仍失败: {last_err}")


def get_stock_list(
    exclude_chinext_star: bool = False,
    exclude_st: bool = True,
    min_listed_days: int = 120,
    cache_dir: str | Path = "cache",
) -> pd.DataFrame:
    """返回 [code, name] 两列的 DataFrame。
    带 24h 缓存; akshare 失败时降级到旧缓存; 实在没缓存才抛异常。"""
    if ak is None:
        raise RuntimeError("akshare 未安装，请先 pip install akshare")

    cache_path_dir = ensure_dir(cache_dir)
    cache_fp = cache_path_dir / _STOCK_LIST_CACHE_FILE

    # ---- 1. 优先用新鲜缓存 ----
    if _is_cache_fresh(cache_fp, _STOCK_LIST_CACHE_TTL_HOURS):
        try:
            df = pd.read_parquet(cache_fp)
            log.debug("股票列表用缓存 (%d 只)", len(df))
            return _filter_stock_list(df, exclude_chinext_star, exclude_st)
        except Exception as e:
            log.warning("读股票列表缓存失败,重新拉: %s", e)

    # ---- 2. 拉新 (带重试) ----
    try:
        df = _fetch_stock_list_raw()
        df = df.rename(columns={"code": "code", "name": "name"})
        df["code"] = df["code"].astype(str).str.zfill(6)
        df = df[["code", "name"]]
        try:
            df.to_parquet(cache_fp)
        except Exception as e:
            log.debug("写股票列表缓存失败: %s", e)
        log.info("股票列表已刷新: %d 只", len(df))
        return _filter_stock_list(df, exclude_chinext_star, exclude_st)
    except Exception as e:
        log.error("akshare 拉取股票列表失败: %s", e)

    # ---- 3. 降级: 用旧缓存 (即使过期) ----
    if cache_fp.exists():
        try:
            df = pd.read_parquet(cache_fp)
            log.warning("回退到旧缓存的股票列表 (%d 只),建议稍后重试", len(df))
            return _filter_stock_list(df, exclude_chinext_star, exclude_st)
        except Exception:
            pass

    raise RuntimeError("股票列表既拉不到也没缓存,扫描无法进行")


def _filter_stock_list(df: pd.DataFrame,
                       exclude_chinext_star: bool,
                       exclude_st: bool) -> pd.DataFrame:
    if exclude_st:
        df = df[~df["name"].str.contains("ST|退", case=False, na=False)]
    if exclude_chinext_star:
        df = df[~df["code"].str.startswith(("30", "68", "8", "4"))]
    else:
        df = df[~df["code"].str.startswith(("8", "4", "9"))]
    df = df.reset_index(drop=True)
    log.info("候选股票数: %d", len(df))
    return df


# ============================================================
# K线数据 (单只)
# ============================================================
def _cache_file(code: str, adjust: str, cache_dir: Path) -> Path:
    return cache_dir / f"{code}_{adjust or 'raw'}.parquet"


def _is_cache_fresh(path: Path, ttl_hours: int) -> bool:
    if ttl_hours <= 0 or not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(hours=ttl_hours)


# 模块级统计 (供日志/诊断使用,线程安全要求不严)
_STATS = {
    "ok_em": 0, "ok_sina": 0, "fail": 0,
    "em_consec_fail": 0,   # EM 连续失败计数
    "em_disabled": False,  # EM 熔断标志
    "logged_first": False,
}

# EM 连续失败这么多次就跳过 (避免每只股票都浪费 3.5 秒重试)
_EM_FAILURE_THRESHOLD = 5


def _normalize_em(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    """东方财富中文列名 → 英文统一格式"""
    df = df.rename(columns={
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low",
        "成交量": "volume", "成交额": "amount",
        "换手率": "turnover",
    })
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    cols = ["open", "high", "low", "close", "volume"]
    extra = [c for c in ["amount", "turnover"] if c in df.columns]
    return df[cols + extra].astype({c: "float64" for c in cols}).tail(bars)


def _normalize_sina(df: pd.DataFrame, bars: int) -> pd.DataFrame:
    """新浪接口返回的 DataFrame → 英文统一格式"""
    # ak.stock_zh_a_daily 返回列: date,open,high,low,close,volume,...
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    df = df.sort_index()
    cols = ["open", "high", "low", "close", "volume"]
    available = [c for c in cols if c in df.columns]
    return df[available].astype({c: "float64" for c in available}).tail(bars)


def _fetch_em(code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """东方财富源"""
    return ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date,
        adjust=adjust or "",
    )


def _fetch_sina(code: str, adjust: str,
                start_date: str | None = None,
                end_date: str | None = None) -> pd.DataFrame:
    """新浪源 — 用于东方财富不可达时回退。
    sina 代码格式: sh600000 / sz000001 / bj430047
    start_date/end_date 格式: YYYYMMDD,可省略(取全部历史)
    """
    if code.startswith("6"):
        sym = "sh" + code
    elif code.startswith(("0", "3")):
        sym = "sz" + code
    elif code.startswith(("4", "8")):
        sym = "bj" + code
    else:
        sym = "sh" + code
    kw = {"symbol": sym, "adjust": adjust or ""}
    if start_date:
        kw["start_date"] = start_date
    if end_date:
        kw["end_date"] = end_date
    return ak.stock_zh_a_daily(**kw)


def fetch_kline(
    code: str,
    bars: int = 120,
    adjust: str = "qfq",
    cache_dir: str | Path = "cache",
    cache_ttl_hours: int = 6,
    retries: int = 2,
    jitter_range: tuple[float, float] = (0.05, 0.20),
) -> pd.DataFrame | None:
    """
    拉单只股票日K线,失败返回 None。
    流程: 先东方财富 (带 retry),失败回退新浪 (1 次)。
    """
    if ak is None:
        raise RuntimeError("akshare 未安装")

    cache_path_dir = ensure_dir(cache_dir)
    cache_path = _cache_file(code, adjust, cache_path_dir)

    if _is_cache_fresh(cache_path, cache_ttl_hours):
        try:
            df = pd.read_parquet(cache_path)
            return df.tail(bars)
        except Exception:  # noqa
            pass

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=bars * 2 + 60)).strftime("%Y%m%d")

    em_err: Exception | None = None
    sina_err: Exception | None = None

    # ---------- 1. 东方财富 (主, 已熔断则跳过) ----------
    if not _STATS["em_disabled"]:
        for attempt in range(retries + 1):
            try:
                time.sleep(random.uniform(*jitter_range))
                df_raw = _fetch_em(code, start_date, end_date, adjust)
                if df_raw is None or df_raw.empty:
                    # 空数据不算上游故障,直接返回
                    _STATS["em_consec_fail"] = 0
                    return None
                df = _normalize_em(df_raw, bars)
                _save_cache(df, cache_path)
                _STATS["ok_em"] += 1
                _STATS["em_consec_fail"] = 0
                _log_first_success(code, "eastmoney", df)
                return df
            except Exception as e:  # noqa
                em_err = e
                time.sleep((0.5 * (2 ** attempt)) + random.uniform(0, 0.3))

        # 全部 retry 失败 → 计入熔断计数
        _STATS["em_consec_fail"] += 1
        if (_STATS["em_consec_fail"] >= _EM_FAILURE_THRESHOLD
                and not _STATS["em_disabled"]):
            _STATS["em_disabled"] = True
            log.warning("⚡ 东方财富连续失败 %d 次,本次扫描已熔断,后续仅用新浪",
                        _EM_FAILURE_THRESHOLD)

    # ---------- 2. 新浪 (备) ----------
    try:
        time.sleep(random.uniform(0.1, 0.3))
        df_raw = _fetch_sina(code, adjust,
                             start_date=start_date, end_date=end_date)
        if df_raw is not None and not df_raw.empty:
            df = _normalize_sina(df_raw, bars)
            _save_cache(df, cache_path)
            _STATS["ok_sina"] += 1
            _log_first_success(code, "sina", df)
            return df
    except Exception as e:
        sina_err = e

    _STATS["fail"] += 1
    _log_first_failure(code, em_err, sina_err)
    return None


def _save_cache(df: pd.DataFrame, cache_path: Path):
    try:
        df.to_parquet(cache_path)
    except Exception as e:
        log.debug("缓存写入失败 %s: %s", cache_path.name, e)


def _log_first_success(code: str, src: str, df: pd.DataFrame):
    """前几次成功打 INFO,确认源是通的"""
    n = _STATS["ok_em"] + _STATS["ok_sina"]
    if n <= 3:
        log.info("✔ 拉取 %s 成功 [%s] %d 根K线 (累计 ok %d, fail %d)",
                 code, src, len(df), n, _STATS["fail"])
    elif n in (10, 50, 100, 500) or n % 500 == 0:
        log.info("累计: ok=%d (em=%d, sina=%d) fail=%d",
                 n, _STATS["ok_em"], _STATS["ok_sina"], _STATS["fail"])


def _log_first_failure(code: str, em_err, sina_err):
    """前 3 次失败打详细信息,后续只 WARNING"""
    if _STATS["fail"] <= 3:
        log.warning("✘ 拉取 %s 失败  EM=%s  SINA=%s",
                    code, repr(em_err), repr(sina_err))
    else:
        log.warning("拉取 %s 失败: %s", code, repr(em_err))


def reset_stats():
    _STATS["ok_em"] = 0
    _STATS["ok_sina"] = 0
    _STATS["fail"] = 0
    _STATS["em_consec_fail"] = 0
    _STATS["em_disabled"] = False
    _STATS["logged_first"] = False


def get_stats() -> dict:
    return dict(_STATS)


# ============================================================
# 批量
# ============================================================
def iter_klines(
    codes: Iterable[str],
    bars: int = 120,
    adjust: str = "qfq",
    cache_dir: str | Path = "cache",
    cache_ttl_hours: int = 6,
):
    """生成器：逐只产出 (code, DataFrame|None)。"""
    for code in codes:
        df = fetch_kline(code, bars=bars, adjust=adjust,
                         cache_dir=cache_dir, cache_ttl_hours=cache_ttl_hours)
        yield code, df
