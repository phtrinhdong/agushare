"""
K线形态识别
============
每个 detect_xxx 函数接收最近 N 根K线 DataFrame，返回 (hit: bool, score: int, desc: str)

DataFrame 列: open, high, low, close, volume
约定: df.iloc[-1] 是最新K线
"""
from __future__ import annotations

import pandas as pd

# ---------- 通用工具 ----------
def _body(row) -> float:
    return abs(row["close"] - row["open"])


def _range(row) -> float:
    return max(row["high"] - row["low"], 1e-9)


def _upper_shadow(row) -> float:
    return row["high"] - max(row["close"], row["open"])


def _lower_shadow(row) -> float:
    return min(row["close"], row["open"]) - row["low"]


def _is_bull(row) -> bool:
    return row["close"] > row["open"]


def _is_bear(row) -> bool:
    return row["close"] < row["open"]


def _is_downtrend(df: pd.DataFrame, lookback: int = 5) -> bool:
    """简单下降趋势判断：当前收盘 < N日前收盘 且 5日均线下行"""
    if len(df) < lookback + 1:
        return False
    closes = df["close"]
    return closes.iloc[-1] < closes.iloc[-lookback - 1] * 0.98


# ============================================================
# 单根K线 形态
# ============================================================
def detect_hammer(df: pd.DataFrame):
    """锤子线：下影长 >= 2*实体，上影很小，处于下跌趋势末端"""
    if len(df) < 6:
        return False, 0, ""
    row = df.iloc[-1]
    body = _body(row)
    if body < 1e-6:
        return False, 0, ""
    lower = _lower_shadow(row)
    upper = _upper_shadow(row)
    rng = _range(row)
    cond = (lower >= 2 * body) and (upper <= body * 0.5) and (body / rng < 0.4)
    if cond and _is_downtrend(df):
        return True, 3, "锤子线(底部反转)"
    return False, 0, ""


def detect_inverted_hammer(df: pd.DataFrame):
    """倒锤子：上影长，下影短，处于下跌末端"""
    if len(df) < 6:
        return False, 0, ""
    row = df.iloc[-1]
    body = _body(row)
    if body < 1e-6:
        return False, 0, ""
    upper = _upper_shadow(row)
    lower = _lower_shadow(row)
    rng = _range(row)
    cond = (upper >= 2 * body) and (lower <= body * 0.5) and (body / rng < 0.4)
    if cond and _is_downtrend(df):
        return True, 2, "倒锤子线"
    return False, 0, ""


def detect_bullish_doji(df: pd.DataFrame):
    """底部十字星：实体极小，处于下跌末端"""
    if len(df) < 6:
        return False, 0, ""
    row = df.iloc[-1]
    body = _body(row)
    rng = _range(row)
    if rng < 1e-6:
        return False, 0, ""
    if (body / rng < 0.1) and _is_downtrend(df):
        return True, 2, "底部十字星"
    return False, 0, ""


# ============================================================
# 双K线 形态
# ============================================================
def detect_bullish_engulfing(df: pd.DataFrame):
    """看涨吞没：前阴后阳，阳线实体完全吞没前阴线实体"""
    if len(df) < 6:
        return False, 0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    cond = (
        _is_bear(prev) and _is_bull(cur)
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
        and _body(cur) > _body(prev) * 1.0
    )
    if cond and _is_downtrend(df):
        return True, 4, "看涨吞没"
    return False, 0, ""


def detect_piercing_line(df: pd.DataFrame):
    """刺透形态(曙光初现)：前阴后阳，阳线开盘低于前低、收盘穿越前阴实体中点以上"""
    if len(df) < 6:
        return False, 0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    midpoint = (prev["open"] + prev["close"]) / 2
    cond = (
        _is_bear(prev) and _is_bull(cur)
        and cur["open"] < prev["low"]
        and cur["close"] > midpoint
        and cur["close"] < prev["open"]
    )
    if cond and _is_downtrend(df):
        return True, 3, "曙光初现(刺透)"
    return False, 0, ""


# ============================================================
# 三K线 形态
# ============================================================
def detect_morning_star(df: pd.DataFrame):
    """启明星：大阴 + 跳空小实体 + 大阳收复 50% 以上"""
    if len(df) < 6:
        return False, 0, ""
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    midpoint = (a["open"] + a["close"]) / 2
    cond = (
        _is_bear(a) and _body(a) / _range(a) > 0.5
        and _body(b) < _body(a) * 0.5
        and _is_bull(c)
        and c["close"] > midpoint
        and max(b["open"], b["close"]) < a["close"]
    )
    if cond:
        return True, 4, "启明星"
    return False, 0, ""


def detect_three_white_soldiers(df: pd.DataFrame):
    """红三兵：连续三根阳线，逐根创新高，开盘价在前一根实体内"""
    if len(df) < 5:
        return False, 0, ""
    a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
    cond = (
        _is_bull(a) and _is_bull(b) and _is_bull(c)
        and b["close"] > a["close"]
        and c["close"] > b["close"]
        and a["open"] < b["open"] < c["open"]
        and b["open"] < a["close"]
        and c["open"] < b["close"]
    )
    if cond:
        return True, 4, "红三兵"
    return False, 0, ""


# ============================================================
# 多K线 形态
# ============================================================
def detect_rising_three(df: pd.DataFrame):
    """上升三法：1大阳 + 3根小阴(回调在大阳内) + 1根大阳创新高"""
    if len(df) < 7:
        return False, 0, ""
    first = df.iloc[-5]
    inside = df.iloc[-4:-1]
    last = df.iloc[-1]
    cond = (
        _is_bull(first) and _is_bull(last)
        and last["close"] > first["close"]
        and all(row["high"] <= first["high"] and row["low"] >= first["low"]
                for _, row in inside.iterrows())
    )
    if cond:
        return True, 3, "上升三法"
    return False, 0, ""


def detect_gap_up(df: pd.DataFrame):
    """向上跳空缺口：今日开盘 > 昨日最高，且收阳，量能放大"""
    if len(df) < 6:
        return False, 0, ""
    prev, cur = df.iloc[-2], df.iloc[-1]
    vol_ma5 = df["volume"].tail(6).head(5).mean()
    cond = (
        cur["open"] > prev["high"] * 1.005
        and _is_bull(cur)
        and cur["volume"] > vol_ma5 * 1.2
    )
    if cond:
        return True, 3, "向上跳空缺口"
    return False, 0, ""


def detect_bull_arrangement(df: pd.DataFrame):
    """均线多头排列：MA5 > MA10 > MA20 > MA60，且 MA5 上行"""
    if len(df) < 65:
        return False, 0, ""
    close = df["close"]
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    cond = (
        ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1]
        and ma5.iloc[-1] > ma5.iloc[-3]
    )
    if cond:
        return True, 3, "均线多头排列"
    return False, 0, ""


def detect_ma_breakthrough(df: pd.DataFrame):
    """突破均线：昨日收盘在 MA20 下，今日放量收盘上穿 MA20"""
    if len(df) < 25:
        return False, 0, ""
    close = df["close"]
    ma20 = close.rolling(20).mean()
    if pd.isna(ma20.iloc[-1]) or pd.isna(ma20.iloc[-2]):
        return False, 0, ""
    vol_ma5 = df["volume"].tail(6).head(5).mean()
    cond = (
        close.iloc[-2] < ma20.iloc[-2]
        and close.iloc[-1] > ma20.iloc[-1]
        and _is_bull(df.iloc[-1])
        and df["volume"].iloc[-1] > vol_ma5 * 1.1
    )
    if cond:
        return True, 3, "放量突破MA20"
    return False, 0, ""


# ============================================================
# 派发器 (兼容老接口) + 注入注册中心
# ============================================================
DETECTORS = {
    "hammer": detect_hammer,
    "inverted_hammer": detect_inverted_hammer,
    "bullish_doji": detect_bullish_doji,
    "bullish_engulfing": detect_bullish_engulfing,
    "piercing_line": detect_piercing_line,
    "morning_star": detect_morning_star,
    "three_white_soldiers": detect_three_white_soldiers,
    "rising_three": detect_rising_three,
    "gap_up": detect_gap_up,
    "bull_arrangement": detect_bull_arrangement,
    "ma_breakthrough": detect_ma_breakthrough,
}

# 内置形态的中文名 + 默认分值 (用于注册中心)
_BUILTIN_META = {
    "hammer":               ("锤子线(底部反转)", 3),
    "inverted_hammer":      ("倒锤子线",         2),
    "bullish_doji":         ("底部十字星",       2),
    "bullish_engulfing":    ("看涨吞没",         4),
    "piercing_line":        ("曙光初现(刺透)",   3),
    "morning_star":         ("启明星",           4),
    "three_white_soldiers": ("红三兵",           4),
    "rising_three":         ("上升三法",         3),
    "gap_up":               ("向上跳空缺口",     3),
    "bull_arrangement":     ("均线多头排列",     3),
    "ma_breakthrough":      ("放量突破MA20",     3),
}

# 把内置形态注入到 registry
from .registry import register_builtin  # noqa: E402

for _key, _fn in DETECTORS.items():
    _desc, _score = _BUILTIN_META.get(_key, (_key, 2))
    register_builtin("pattern", _key, _desc, _score, _fn)


def detect_candlestick_patterns(df: pd.DataFrame, enabled: dict[str, bool]) -> list[dict]:
    """老接口保留，内部转发到 registry"""
    from .registry import detect_all_patterns
    return detect_all_patterns(df, enabled)
