"""数据获取：akshare 拉取 A股股票列表 & 日K线，本地 parquet 缓存"""
from __future__ import annotations

import logging
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
def get_stock_list(
    exclude_chinext_star: bool = False,
    exclude_st: bool = True,
    min_listed_days: int = 120,
) -> pd.DataFrame:
    """返回 [code, name] 两列的 DataFrame。"""
    if ak is None:
        raise RuntimeError("akshare 未安装，请先 pip install akshare")

    # ak.stock_info_a_code_name() 返回所有 A股 [code, name]
    df = ak.stock_info_a_code_name()
    df = df.rename(columns={"code": "code", "name": "name"})
    df["code"] = df["code"].astype(str).str.zfill(6)

    if exclude_st:
        df = df[~df["name"].str.contains("ST|退", case=False, na=False)]

    if exclude_chinext_star:
        df = df[~df["code"].str.startswith(("30", "68", "8", "4"))]
    else:
        # 至少排除北交所/新三板 (4/8 开头),数据接口经常缺
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


def fetch_kline(
    code: str,
    bars: int = 120,
    adjust: str = "qfq",
    cache_dir: str | Path = "cache",
    cache_ttl_hours: int = 6,
    retries: int = 2,
) -> pd.DataFrame | None:
    """
    拉取单只股票的日K线，返回 DataFrame:
        index: date (Timestamp)
        cols : open, high, low, close, volume, amount
    若失败返回 None。
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
            pass  # 缓存坏了就重新拉

    end_date = datetime.now().strftime("%Y%m%d")
    # 多拉一点保证 bars 充足
    start_date = (datetime.now() - timedelta(days=bars * 2 + 60)).strftime("%Y%m%d")

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust or "",
            )
            if df is None or df.empty:
                return None

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
            df = df[cols + extra].astype({c: "float64" for c in cols})

            try:
                df.to_parquet(cache_path)
            except Exception as e:  # parquet 写失败不要影响主流程
                log.debug("缓存写入失败 %s: %s", code, e)

            return df.tail(bars)
        except Exception as e:  # noqa
            last_err = e
            time.sleep(0.5 * (attempt + 1))

    log.warning("拉取 %s K线失败: %s", code, last_err)
    return None


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
