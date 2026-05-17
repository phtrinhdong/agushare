"""
平氏1号 - 五项启动信号 + 放量确认 (复合策略)
=============================================
作者公式 (通达信原文):
    N:=20; M:=7.5; 近期M:=7; 回撤上限:=6; 市值下限:=15; 放量倍数:=1.5;
    ... (见下方逐项翻译)

策略组成:

  [基础条件 — 全部必须满足]
    1) 趋势OK         : MA20 > MA60 且 CLOSE > MA20
    2) 今日强势       : 涨幅 ≥ 7.5%
    3) 非一字板       : OPEN < HIGH (今日有过波动)
    4) 放量突破       : VOL > 昨日20日均量 × 1.5
    5) 回调OK         : 从20日高点回撤 ≤ 6%
    6) 近期强势       : 20日内至少 1 次 ≥ 7% 涨幅
    7) 流通市值 ≥ 15亿 — 暂未实现 (需额外接口,见文末 TODO)

  [五项加分信号 — 至少 2 项命中才视为入选]
    A) 三线开花               : MA5>MA10>MA20 且 MA5 上行
    B) MACD 零轴上方首次金叉  : 今日金叉 + DIFF>0 + DEA>0
    C) 突破半年线             : CLOSE 上穿 MA120 + 放量 2.5×
    D) 首板模式               : 昨日涨停 → 今日缩量 + 涨停
    E) 金三角放量确认         : 今日 MA5 上穿 MA10 + 放量 1.5×

最终选股:
    base_conditions AND signal_count >= 2

得分规则 (动态):
    基础 8 分 + 每个加分信号 +1 分;最高 13 分 (5 项全中)

数据要求:
    至少 130 根日K线 (MA120 + 余量),建议 config.yaml > data.bars ≥ 150

TODO:
    流通市值条件需要从 akshare spot 数据预先拉取并缓存,目前直接跳过
    (绝大多数 ≥ 7.5% 涨幅且放量的股票流通市值都 > 15亿,影响较小)
"""
from ashare_agent.patterns.registry import register_pattern


@register_pattern(
    name="pingshi_1",
    desc="平氏1号",
    score=8,           # 占位,实际由 detect 动态返回
)
def detect(df):
    if len(df) < 130:
        return False

    import pandas as pd

    close = df["close"]
    high = df["high"]
    vol = df["volume"]

    # === 均线/量能预计算 ===
    ma5   = close.rolling(5).mean()
    ma10  = close.rolling(10).mean()
    ma20  = close.rolling(20).mean()
    ma60  = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    vol_ma5  = vol.rolling(5).mean()
    vol_ma20 = vol.rolling(20).mean()

    if pd.isna(ma120.iloc[-1]) or pd.isna(vol_ma20.iloc[-2]):
        return False

    today = df.iloc[-1]
    yesterday = df.iloc[-2]
    prev_close = float(close.iloc[-2])
    today_chg = (today["close"] - prev_close) / prev_close * 100

    # ============================================================
    # 基础条件 (任一不满足直接 return)
    # ============================================================

    # 1) 趋势OK: MA20>MA60 AND CLOSE>MA20
    if not (ma20.iloc[-1] > ma60.iloc[-1] and today["close"] > ma20.iloc[-1]):
        return False

    # 2) 今日强势: 涨幅 ≥ 7.5%
    if today_chg < 7.5:
        return False

    # 3) 非一字板: OPEN < HIGH
    if today["open"] >= today["high"]:
        return False

    # 4) 放量突破: VOL > 昨日20日均量 × 1.5
    if today["volume"] <= vol_ma20.iloc[-2] * 1.5:
        return False

    # 5) 回调OK: 从 20 日最高点回撤 ≤ 6%
    recent_high_20 = high.tail(20).max()
    if recent_high_20 > 0:
        pullback = (recent_high_20 - today["close"]) / recent_high_20 * 100
        if pullback > 6:
            return False

    # 6) 近期强势: 20 日内至少 1 次 ≥ 7% 涨幅 (涨停或大阳线)
    daily_chg_20 = (close.diff() / close.shift(1) * 100).tail(20)
    if not (daily_chg_20 >= 7).any():
        return False

    # 7) 流通市值 ≥ 15亿  —— TODO: 暂未实现,默认通过

    # ============================================================
    # 五项加分信号 (统计命中数,需 ≥ 2)
    # ============================================================
    signals: list[str] = []

    # A) 三线开花: MA5>MA10>MA20 且 MA5 较昨日上行
    if (ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1]
            and ma5.iloc[-1] > ma5.iloc[-2]):
        signals.append("三线开花")

    # B) MACD 零轴上方首次金叉
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    diff = ema12 - ema26
    dea = diff.ewm(span=9, adjust=False).mean()
    if (diff.iloc[-2] <= dea.iloc[-2]
            and diff.iloc[-1] > dea.iloc[-1]
            and diff.iloc[-1] > 0
            and dea.iloc[-1] > 0):
        signals.append("MACD零轴上方首金")

    # C) 突破半年线: 今 CLOSE 上穿 MA120 + 放量 2.5×
    if (today["close"] > ma120.iloc[-1]
            and close.iloc[-2] <= ma120.iloc[-2]
            and today["volume"] > vol_ma20.iloc[-2] * 2.5):
        signals.append("突破半年线")

    # D) 首板模式: 昨日涨停 → 今日缩量 + 涨停 + 不破前低 + 站上 MA20
    if len(df) >= 3 and not pd.isna(vol_ma5.iloc[-2]):
        yesterday_limit_up = close.iloc[-2] >= close.iloc[-3] * 1.095
        today_shrink_vol   = today["volume"] < vol_ma5.iloc[-2] * 0.6
        higher_low         = today["low"] > yesterday["low"]
        today_limit_up     = today["close"] >= prev_close * 1.095
        above_ma20         = today["close"] > ma20.iloc[-1]
        if (yesterday_limit_up and today_shrink_vol and higher_low
                and today_limit_up and above_ma20):
            signals.append("首板模式")

    # E) 金三角放量确认 (今日 MA5 上穿 MA10)
    if (ma5.iloc[-1] > ma10.iloc[-1] > ma20.iloc[-1]
            and ma5.iloc[-2] < ma10.iloc[-2]
            and today["volume"] > vol_ma20.iloc[-2] * 1.5):
        signals.append("金三角放量确认")

    # ============================================================
    # 命中判定 + 动态打分
    # ============================================================
    if len(signals) < 2:
        return False

    score = 8 + len(signals)        # 基础 8 + 每信号 1, 上限 13
    desc = f"平氏1号 {len(signals)}/5 [{' '.join(signals)}]"
    return True, score, desc
