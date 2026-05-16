"""
回测引擎
========

核心思路 (避免 lookahead bias):
    对每个交易日 D，只用 D 及之前的数据做形态识别；
    命中则 "在 D+1 开盘价买入"，按持有期 hold_days 内的逻辑卖出:
        - 触发止损 (stop_loss%, 用当日收盘价判断) → 立即卖
        - 触发止盈 (take_profit%)              → 立即卖
        - 持满 N 日                            → 收盘卖

为什么用 D+1 开盘价?
    D 收盘后才能确认形态。最早能买入的是下一交易日开盘。
    用收盘价"买入"会偷看未来 → 是回测最常见的偷看 bug。

为什么用日级收盘价判断止盈止损?
    严格意义上应该用日内最高/最低价判断,但需要更细的分钟级数据。
    本 MVP 用收盘价 → 偏保守 (实际触发可能更早,这里假设触发条件下一日补到)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import pandas as pd
from tqdm import tqdm

from . import data_loader
from .patterns import detect_candlestick_patterns, detect_indicator_signals

log = logging.getLogger("ashare_agent")


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Trade:
    code: str
    name: str
    pattern: str            # 形态/指标的中文描述
    pattern_key: str        # 形态/指标的 snake_case key
    pattern_score: int      # 该形态的得分
    signal_score: int       # 触发当日的综合得分
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    hold_days: int
    pnl_pct: float          # (exit - entry) / entry * 100
    exit_reason: str        # "hold_end" | "stop_loss" | "take_profit"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "pattern": self.pattern,
            "pattern_key": self.pattern_key,
            "pattern_score": self.pattern_score,
            "signal_score": self.signal_score,
            "entry_date": self.entry_date.strftime("%Y-%m-%d"),
            "exit_date": self.exit_date.strftime("%Y-%m-%d"),
            "entry_price": round(self.entry_price, 3),
            "exit_price": round(self.exit_price, 3),
            "hold_days": self.hold_days,
            "pnl_pct": round(self.pnl_pct, 2),
            "exit_reason": self.exit_reason,
        }


@dataclass
class BacktestResult:
    config: dict                 # 回测参数快照
    trades: list[Trade] = field(default_factory=list)
    failed_codes: list[str] = field(default_factory=list)
    skipped_codes: list[str] = field(default_factory=list)

    # 派生统计 (lazy)
    _by_pattern: dict[str, dict] | None = None
    _overall: dict | None = None
    _equity: pd.Series | None = None

    # ---------- 派生指标 ----------
    def by_pattern(self) -> dict[str, dict]:
        if self._by_pattern is None:
            self._by_pattern = _aggregate_by_pattern(self.trades)
        return self._by_pattern

    def overall(self) -> dict:
        if self._overall is None:
            self._overall = _aggregate_overall(self.trades)
        return self._overall

    def equity_curve(self) -> pd.Series:
        """等权资金曲线 (同日多信号则等分仓位)"""
        if self._equity is None:
            self._equity = _build_equity_curve(self.trades)
        return self._equity


# ============================================================
# 核心: 单股回测
# ============================================================
def backtest_one_stock(
    code: str,
    name: str,
    df: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    pattern_cfg: dict,
    hold_days: int,
    stop_loss: float | None,
    take_profit: float | None,
    min_history_bars: int = 30,
    lookback_window: int = 120,
) -> list[Trade]:
    """
    在 df 上重放 [start, end] 区间,返回所有命中产生的 Trade。
    df: 完整的 OHLCV 历史 (至少要早于 start 几个月,以便形态识别有足够历史)
    """
    trades: list[Trade] = []
    if df is None or len(df) < min_history_bars + hold_days + 1:
        return trades

    dates = df.index
    for i, d in enumerate(dates):
        if d < start or d > end:
            continue
        # 当前及之前数据
        sub = df.iloc[max(0, i + 1 - lookback_window): i + 1]
        if len(sub) < min_history_bars:
            continue

        # 形态识别 (只用 D 及之前的数据)
        candles = detect_candlestick_patterns(sub, pattern_cfg)
        inds = detect_indicator_signals(sub, pattern_cfg)
        hits = candles + inds
        if not hits:
            continue
        signal_score = sum(h["score"] for h in hits)

        # 没有下一交易日数据 → 无法买入
        if i + 1 >= len(df):
            continue
        entry_date = dates[i + 1]
        entry_price = float(df.iloc[i + 1]["open"])
        if entry_price <= 0:
            continue

        # 寻找出场: 在 [i+1, i+1+hold_days-1] 范围内逐日检查
        exit_price = None
        exit_date = None
        exit_reason = "hold_end"
        actual_hold = 0

        max_j = min(hold_days, len(df) - 1 - i)
        if max_j < 1:
            continue

        for j in range(1, max_j + 1):
            day_idx = i + j  # 持有第 j 天
            row = df.iloc[day_idx]
            ret_pct = (row["close"] - entry_price) / entry_price * 100

            if stop_loss is not None and ret_pct <= stop_loss:
                exit_price = entry_price * (1 + stop_loss / 100)
                exit_date = dates[day_idx]
                actual_hold = j
                exit_reason = "stop_loss"
                break
            if take_profit is not None and ret_pct >= take_profit:
                exit_price = entry_price * (1 + take_profit / 100)
                exit_date = dates[day_idx]
                actual_hold = j
                exit_reason = "take_profit"
                break

            if j == max_j:
                exit_price = float(row["close"])
                exit_date = dates[day_idx]
                actual_hold = j
                exit_reason = "hold_end"

        if exit_price is None:
            continue

        pnl_pct = (exit_price - entry_price) / entry_price * 100

        # 同一日多个形态命中 → 拆成多笔 Trade,便于按形态归因统计
        # (整体净值曲线会去重,见 _build_equity_curve)
        for h in hits:
            trades.append(Trade(
                code=code, name=name,
                pattern=h["desc"], pattern_key=h["name"],
                pattern_score=h["score"], signal_score=signal_score,
                entry_date=entry_date, exit_date=exit_date,
                entry_price=entry_price, exit_price=exit_price,
                hold_days=actual_hold,
                pnl_pct=pnl_pct, exit_reason=exit_reason,
            ))

    return trades


# ============================================================
# 并发回测多只股票
# ============================================================
def run_backtest(
    codes: Iterable[str],
    names: dict[str, str],
    start: str,
    end: str,
    pattern_cfg: dict,
    hold_days: int = 5,
    stop_loss: float | None = -5.0,
    take_profit: float | None = 10.0,
    bars_per_call: int = 1000,
    adjust: str = "qfq",
    cache_dir: str = "cache",
    cache_ttl_hours: int = 24,
    workers: int = 3,
) -> BacktestResult:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    result = BacktestResult(config={
        "start": start, "end": end,
        "hold_days": hold_days,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "adjust": adjust,
        "universe_size": len(list(codes)) if not isinstance(codes, list) else len(codes),
    })

    codes = list(codes)
    log.info("回测 %d 只股票 · %s ~ %s · 持有 %d 日 · 止损 %s · 止盈 %s",
             len(codes), start, end, hold_days, stop_loss, take_profit)

    def _job(code: str):
        df = data_loader.fetch_kline(
            code, bars=bars_per_call, adjust=adjust,
            cache_dir=cache_dir, cache_ttl_hours=cache_ttl_hours,
        )
        if df is None or df.empty:
            return code, None, "no_data"
        trades = backtest_one_stock(
            code, names.get(code, code), df, start_ts, end_ts,
            pattern_cfg, hold_days, stop_loss, take_profit,
        )
        return code, trades, "ok"

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_job, c): c for c in codes}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="回测"):
            try:
                code, trades, status = fut.result()
            except Exception as e:
                log.debug("回测异常 %s: %s", futures[fut], e)
                result.failed_codes.append(futures[fut])
                continue
            if status == "no_data":
                result.failed_codes.append(code)
            elif trades is None or len(trades) == 0:
                result.skipped_codes.append(code)
            else:
                result.trades.extend(trades)

    log.info("回测完成: 交易 %d 笔, 失败 %d, 无信号 %d",
             len(result.trades), len(result.failed_codes), len(result.skipped_codes))
    return result


# ============================================================
# 聚合统计
# ============================================================
def _aggregate_by_pattern(trades: list[Trade]) -> dict[str, dict]:
    """按形态/指标分组算胜率、平均收益等"""
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(t.pattern, []).append(t)

    out: dict[str, dict] = {}
    for name, ts in groups.items():
        wins = [t for t in ts if t.pnl_pct > 0]
        losses = [t for t in ts if t.pnl_pct <= 0]
        pnls = [t.pnl_pct for t in ts]

        gain = sum(t.pnl_pct for t in wins)
        loss = abs(sum(t.pnl_pct for t in losses)) or 1e-9
        profit_factor = gain / loss

        out[name] = {
            "pattern": name,
            "count": len(ts),
            "win_rate": len(wins) / len(ts) * 100,
            "avg_return": sum(pnls) / len(pnls),
            "median_return": sorted(pnls)[len(pnls) // 2],
            "max_win": max(pnls),
            "max_loss": min(pnls),
            "profit_factor": profit_factor,
            "avg_hold_days": sum(t.hold_days for t in ts) / len(ts),
            "stop_loss_pct": sum(1 for t in ts if t.exit_reason == "stop_loss") / len(ts) * 100,
            "take_profit_pct": sum(1 for t in ts if t.exit_reason == "take_profit") / len(ts) * 100,
        }
    # 按命中次数降序
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["count"]))


def _aggregate_overall(trades: list[Trade]) -> dict:
    if not trades:
        return {"count": 0, "win_rate": 0, "avg_return": 0,
                "total_return": 0, "max_drawdown": 0}

    # 去重: 同一 (code, entry_date) 多个形态 → 算 1 笔 (避免重复持仓)
    seen: set = set()
    unique: list[Trade] = []
    for t in sorted(trades, key=lambda x: -x.signal_score):
        key = (t.code, t.entry_date)
        if key not in seen:
            seen.add(key)
            unique.append(t)

    pnls = [t.pnl_pct for t in unique]
    wins = sum(1 for p in pnls if p > 0)
    eq = _build_equity_curve(unique)
    max_dd = _max_drawdown(eq) if not eq.empty else 0

    return {
        "count": len(unique),
        "trades_total": len(trades),         # 含重复形态
        "win_rate": wins / len(unique) * 100,
        "avg_return": sum(pnls) / len(pnls),
        "median_return": sorted(pnls)[len(pnls) // 2],
        "total_return": (eq.iloc[-1] - 1) * 100 if not eq.empty else 0,
        "max_drawdown": max_dd,
    }


def _build_equity_curve(trades: list[Trade]) -> pd.Series:
    """每日等权: 当日所有 trade 平均收益,作为当日组合收益。
    返回累计净值 (起始 1.0)"""
    if not trades:
        return pd.Series(dtype=float)
    # 去重 (code, entry_date),只保留得分最高的形态
    seen = {}
    for t in sorted(trades, key=lambda x: -x.signal_score):
        seen.setdefault((t.code, t.entry_date), t)
    uniq = list(seen.values())

    # 按 entry_date 分组取平均
    by_day: dict[pd.Timestamp, list[float]] = {}
    for t in uniq:
        by_day.setdefault(t.entry_date, []).append(t.pnl_pct)
    days = sorted(by_day.keys())
    daily_ret = pd.Series(
        {d: sum(by_day[d]) / len(by_day[d]) / 100 for d in days},
        name="daily_return",
    )
    equity = (1 + daily_ret).cumprod()
    equity.iloc[0] = (1 + daily_ret.iloc[0])
    return equity


def _max_drawdown(equity: pd.Series) -> float:
    """最大回撤 %"""
    if equity.empty:
        return 0
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return float(dd.min() * 100)
