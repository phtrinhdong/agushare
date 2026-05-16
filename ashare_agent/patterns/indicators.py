"""
技术指标买入信号
================
所有函数: (df) -> (hit, score, desc)
df 列: open, high, low, close, volume
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- 指标计算 ----------
def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


# ============================================================
# 信号检测
# ============================================================
def detect_macd_golden(df: pd.DataFrame):
    """MACD金叉：DIF 上穿 DEA，且 DEA 在 0 轴附近或之下(中低位金叉)"""
    if len(df) < 35:
        return False, 0, ""
    dif, dea, hist = macd(df["close"])
    if pd.isna(dif.iloc[-2]) or pd.isna(dea.iloc[-2]):
        return False, 0, ""
    cross = dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
    low_zone = dea.iloc[-1] < df["close"].iloc[-1] * 0.02  # 0轴附近
    if cross and (low_zone or dea.iloc[-1] < 0):
        return True, 3, "MACD金叉(低位)"
    if cross:
        return True, 2, "MACD金叉"
    return False, 0, ""


def detect_kdj_golden(df: pd.DataFrame):
    """KDJ金叉：K 上穿 D，且 D < 30 (超卖区金叉)"""
    if len(df) < 12:
        return False, 0, ""
    k, d, j = kdj(df)
    if pd.isna(k.iloc[-2]):
        return False, 0, ""
    cross = k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1]
    if cross and d.iloc[-1] < 30:
        return True, 3, "KDJ低位金叉"
    if cross and d.iloc[-1] < 50:
        return True, 2, "KDJ金叉"
    return False, 0, ""


def detect_rsi_oversold(df: pd.DataFrame):
    """RSI超卖反弹：昨日 RSI(6) < 20，今日上穿 20"""
    if len(df) < 10:
        return False, 0, ""
    r = rsi(df["close"], n=6)
    if pd.isna(r.iloc[-2]):
        return False, 0, ""
    if r.iloc[-2] < 20 and r.iloc[-1] > 20:
        return True, 2, "RSI超卖反弹"
    return False, 0, ""


def detect_boll_lower(df: pd.DataFrame):
    """布林带下轨支撑：昨日触及下轨，今日收阳反弹"""
    if len(df) < 25:
        return False, 0, ""
    upper, mid, lower = bollinger(df["close"])
    if pd.isna(lower.iloc[-2]):
        return False, 0, ""
    touched = df["low"].iloc[-2] <= lower.iloc[-2] * 1.005
    rebound = df["close"].iloc[-1] > df["open"].iloc[-1] and df["close"].iloc[-1] > df["close"].iloc[-2]
    if touched and rebound:
        return True, 2, "布林带下轨反弹"
    return False, 0, ""


# ============================================================
# 派发 + 注入注册中心
# ============================================================
INDICATORS = {
    "macd_golden": detect_macd_golden,
    "kdj_golden": detect_kdj_golden,
    "rsi_oversold": detect_rsi_oversold,
    "boll_lower": detect_boll_lower,
}

_BUILTIN_META = {
    "macd_golden":  ("MACD金叉",          3),
    "kdj_golden":   ("KDJ金叉",           3),
    "rsi_oversold": ("RSI超卖反弹",       2),
    "boll_lower":   ("布林带下轨反弹",    2),
}

from .registry import register_builtin  # noqa: E402

for _key, _fn in INDICATORS.items():
    _desc, _score = _BUILTIN_META.get(_key, (_key, 2))
    register_builtin("indicator", _key, _desc, _score, _fn)


def detect_indicator_signals(df: pd.DataFrame, enabled: dict[str, bool]) -> list[dict]:
    """老接口保留，内部转发到 registry"""
    from .registry import detect_all_indicators
    return detect_all_indicators(df, enabled)
