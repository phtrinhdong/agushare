"""
金三角放量确认
==============
通达信公式:
    金三角放量确认:= MA5>MA10 AND MA10>MA20
                  AND REF(MA5<MA10, 1)
                  AND VOL > REF(MA(VOL,20), 1) * 1.5

含义:
  ① 今日 MA5 > MA10 > MA20    → 短中长均线呈多头排列
  ② 昨日 MA5 < MA10            → 这两根均线"今天"才金叉 (而非很久以前就金叉)
  ③ 今日成交量 > 昨日的 20 日均量 × 1.5  → 量能确认,放量启动

整体: 中长期已在多头格局,短期均线今天跟着金叉过来,并伴随放量 → 趋势启动瞬间
"""
from ashare_agent.patterns.registry import register_pattern


@register_pattern(
    name="golden_triangle_volume",
    desc="金三角放量确认",
    score=4,
)
def detect(df) -> bool:
    if len(df) < 22:           # 需要至少 22 根才能算 20 日均量 + 看昨日值
        return False

    close = df["close"]
    vol = df["volume"]
    ma5  = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    vol_ma20 = vol.rolling(20).mean()

    # 必须能算出昨日的 20日均量 (即至少要有 21 天数据)
    import pandas as pd
    if pd.isna(ma20.iloc[-1]) or pd.isna(vol_ma20.iloc[-2]):
        return False

    # 条件 ①+②: 今天 MA5 > MA10 > MA20 (均线多头排列)
    today_bull = ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1]

    # 条件 ③: 昨日 MA5 < MA10 → 也就是今天 MA5 才上穿 MA10
    cross_today = ma5.iloc[-2] < ma10.iloc[-2]

    # 条件 ④: 今日量 > 昨日的 20 日均量 × 1.5
    volume_surge = vol.iloc[-1] > vol_ma20.iloc[-2] * 1.5

    return today_bull and cross_today and volume_surge
