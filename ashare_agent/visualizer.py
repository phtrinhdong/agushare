"""
K线图绘制 (mplfinance)
=====================
为命中信号的股票生成 K线 + 均线 + 成交量图，并在右下角标注命中的形态/指标。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .scanner import SignalRow
from .utils import ensure_dir

log = logging.getLogger("ashare_agent")

try:
    import matplotlib
    matplotlib.use("Agg")  # 无 GUI 环境
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    _HAS_MPF = True
except Exception:
    _HAS_MPF = False
    log.warning("mplfinance 未安装,跳过K线图绘制")


# 中文字体（尽量优雅地降级）
def _setup_chinese_font():
    try:
        from matplotlib import rcParams
        rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB",
                                       "Microsoft YaHei", "SimHei", "Arial Unicode MS"]
        rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


def _make_style():
    mc = mpf.make_marketcolors(
        up="red", down="green",
        edge="inherit", wick="inherit", volume="inherit",
    )
    return mpf.make_mpf_style(marketcolors=mc, gridstyle="--", y_on_right=False,
                              rc={"font.sans-serif": ["PingFang SC", "Microsoft YaHei",
                                                      "SimHei", "Arial Unicode MS"],
                                  "axes.unicode_minus": False})


def draw_signal_chart(row: SignalRow, out_dir: str | Path,
                      show_bars: int = 90) -> Path | None:
    if not _HAS_MPF or row.df is None or row.df.empty:
        return None

    _setup_chinese_font()
    df = row.df.tail(show_bars).copy()
    # mplfinance 需要 OHLCV 大写
    df.columns = [c.capitalize() if c.lower() in ("open", "high", "low", "close", "volume") else c
                  for c in df.columns]

    style = _make_style()

    # 均线
    apds = []
    # 在最后一根K线下方加买入箭头
    arrow_y = df["Low"].iloc[-1] * 0.985
    marker_series = pd.Series([float("nan")] * len(df), index=df.index)
    marker_series.iloc[-1] = arrow_y
    apds.append(mpf.make_addplot(marker_series, type="scatter",
                                 markersize=120, marker="^", color="red"))

    title = f"{row.code} {row.name}  得分:{row.score}"

    out_dir = ensure_dir(out_dir)
    fp = Path(out_dir) / f"{row.code}_{datetime.now().strftime('%Y%m%d')}.png"

    try:
        fig, axes = mpf.plot(
            df,
            type="candle",
            mav=(5, 10, 20),
            volume=True,
            style=style,
            addplot=apds,
            title=title,
            ylabel="价格",
            ylabel_lower="成交量",
            figsize=(11, 6),
            returnfig=True,
            tight_layout=True,
        )
        signal_text = "形态: " + (",".join(row.candlestick) or "—") + \
                      "\n指标: " + (",".join(row.indicators) or "—")
        axes[0].text(
            0.02, 0.02, signal_text,
            transform=axes[0].transAxes,
            fontsize=10,
            verticalalignment="bottom",
            bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.8),
        )
        fig.savefig(fp, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return fp
    except Exception as e:  # noqa
        log.warning("绘图失败 %s: %s", row.code, e)
        return None


def draw_charts(rows: list[SignalRow], out_dir: str | Path, max_n: int = 20) -> list[Path]:
    paths: list[Path] = []
    for row in rows[:max_n]:
        fp = draw_signal_chart(row, out_dir)
        if fp:
            paths.append(fp)
    log.info("生成K线图 %d 张 → %s", len(paths), out_dir)
    return paths
