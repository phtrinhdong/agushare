"""
[示例] 自定义形态：缩量调整后放量启动
=======================================
连续 3 根缩量阴线 + 当日放量阳线，且当日量 >= 5日均量 * 1.5

写法说明
--------
1. 函数签名固定: detect(df) → 返回 bool 或 (bool, score, desc)
2. df 列: open / high / low / close / volume
3. df.iloc[-1] 是最新一根 K线
4. 用 @register_pattern 或 @register_indicator 装饰即可自动注册

复制本文件改名 → 改逻辑 → 重启程序，无需改主代码。
"""
from ashare_agent.patterns.registry import register_pattern, register_indicator


@register_pattern(
    name="volume_surge_after_shrink",   # 唯一 key (snake_case，会写入报告)
    desc="缩量调整后放量启动",            # 报告里显示的中文名
    score=3,                            # 命中加分 (建议 2-4)
)
def detect(df) -> bool:
    if len(df) < 10:
        return False

    # 过去 3 天: 阴线 + 量逐日缩小
    last3_bear = all(df["close"].iloc[i] < df["open"].iloc[i] for i in (-4, -3, -2))
    vol_shrinking = (df["volume"].iloc[-4] >
                     df["volume"].iloc[-3] >
                     df["volume"].iloc[-2])

    # 当日: 阳线 + 放量
    today_bull = df["close"].iloc[-1] > df["open"].iloc[-1]
    vol_ma5 = df["volume"].iloc[-6:-1].mean()
    today_surge = df["volume"].iloc[-1] > vol_ma5 * 1.5

    return last3_bear and vol_shrinking and today_bull and today_surge


# ============================================================
# 还可以写自定义技术指标。下面是个 OBV 上穿 20日均线的示例：
# ============================================================
@register_indicator(
    name="obv_breakout",
    desc="OBV 突破20日均线",
    score=2,
)
def detect_obv(df) -> bool:
    if len(df) < 25:
        return False
    direction = (df["close"].diff().fillna(0) >= 0).astype(int) * 2 - 1
    obv = (direction * df["volume"]).cumsum()
    obv_ma = obv.rolling(20).mean()
    return obv.iloc[-2] <= obv_ma.iloc[-2] and obv.iloc[-1] > obv_ma.iloc[-1]
