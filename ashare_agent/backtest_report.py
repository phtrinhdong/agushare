"""
回测结果 HTML 报告生成器
========================
- 概览卡片
- 按形态分组的统计表
- 净值曲线 PNG (base64 嵌入,无需外网依赖)
- 单笔交易明细表 (最多前 200)
"""
from __future__ import annotations

import base64
import io
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .backtest import BacktestResult
from .utils import ensure_dir

log = logging.getLogger("ashare_agent")


# ---------- 净值曲线 PNG ----------
def _equity_png_base64(equity: pd.Series) -> str | None:
    if equity.empty:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # 中文字体 (复用 visualizer 的检测)
        try:
            from .visualizer import _setup_chinese_font
            _setup_chinese_font()
        except Exception:
            pass

        fig, ax = plt.subplots(figsize=(10, 4))
        equity.plot(ax=ax, linewidth=1.5, color="#4f8cff")
        ax.fill_between(equity.index, 1, equity.values, alpha=0.1, color="#4f8cff")
        ax.axhline(1, color="#888", linestyle="--", linewidth=0.8)
        ax.set_title(f"组合净值曲线 (起始 1.000, 终值 {equity.iloc[-1]:.3f})",
                     fontsize=12)
        ax.set_ylabel("净值")
        ax.grid(alpha=0.3)
        fig.autofmt_xdate()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        log.warning("生成净值曲线 PNG 失败: %s", e)
        return None


# ---------- 主入口 ----------
def render_html(result: BacktestResult, out_dir: str | Path) -> Path:
    out_dir = ensure_dir(out_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = Path(out_dir) / f"backtest_{stamp}.html"

    overall = result.overall()
    by_pattern = result.by_pattern()
    equity = result.equity_curve()
    eq_png = _equity_png_base64(equity)

    html = _render_template(result.config, overall, by_pattern,
                            result.trades, eq_png)
    fp.write_text(html, encoding="utf-8")
    log.info("回测报告: %s", fp)
    return fp


# ============================================================
# 模板 (内联 CSS,单文件可发送/上传/打开)
# ============================================================
def _render_template(cfg: dict, overall: dict, by_pattern: dict,
                     trades: list, eq_png: str | None) -> str:
    # ---- 头部 + 概览 ----
    head = _head_html()
    title = _title_html(cfg)
    overview = _overview_html(overall)
    equity_html = _equity_html(eq_png)
    pattern_table = _pattern_table_html(by_pattern)
    trades_table = _trades_table_html(trades, limit=200)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>{head}</head>
<body>
  <div class="page">
    {title}
    {overview}
    {equity_html}
    {pattern_table}
    {trades_table}
    <footer>由 agushare 回测引擎生成 · {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</footer>
  </div>
</body>
</html>"""


def _head_html() -> str:
    return """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>回测报告</title>
<style>
  :root {
    --bg: #0f1115; --panel: #161a22; --panel-2: #1d2230;
    --border: #2a2f3a; --text: #e6e6e6; --muted: #8c93a0;
    --accent: #4f8cff; --up: #ef4f4f; --down: #2cb67d; --warn: #f5a623;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,sans-serif;
    font-size: 14px; line-height: 1.5; }
  .page { max-width: 1200px; margin: 0 auto; padding: 24px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 28px 0 12px; color: var(--accent);
       border-bottom: 1px solid var(--border); padding-bottom: 6px; }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 24px; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px; margin: 14px 0 24px; }
  .card { background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 14px; }
  .card .label { font-size: 11px; color: var(--muted); letter-spacing: .04em;
    text-transform: uppercase; }
  .card .value { font-size: 20px; font-weight: 600; margin-top: 4px;
    font-variant-numeric: tabular-nums; }
  .value.up { color: var(--up); } .value.down { color: var(--down); }

  table { width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    margin-bottom: 24px; }
  th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border);
    font-variant-numeric: tabular-nums; white-space: nowrap; }
  th { background: var(--panel-2); color: var(--muted); font-weight: 500;
    font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  td.num { text-align: right; }
  td.up { color: var(--up); } td.down { color: var(--down); }
  tbody tr:hover { background: rgba(79, 140, 255, 0.06); }

  .equity { background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px; margin: 14px 0 24px; }
  .equity img { width: 100%; height: auto; display: block; border-radius: 4px; }

  footer { color: var(--muted); font-size: 12px; text-align: center;
    margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--border); }

  .badge { display: inline-block; padding: 2px 7px; border-radius: 10px;
    font-size: 11px; background: rgba(79, 140, 255, 0.15);
    color: var(--accent); border: 1px solid rgba(79, 140, 255, 0.35); }
  .badge.warn { background: rgba(245, 166, 35, 0.12); color: var(--warn);
    border-color: rgba(245, 166, 35, 0.35); }
</style>"""


def _title_html(cfg: dict) -> str:
    sl = f"{cfg.get('stop_loss')}%" if cfg.get('stop_loss') is not None else "无"
    tp = f"{cfg.get('take_profit')}%" if cfg.get('take_profit') is not None else "无"
    return f"""<h1>K 线形态回测报告</h1>
<div class="meta">
  <b>区间:</b> {cfg.get('start')} ~ {cfg.get('end')} ·
  <b>持有期:</b> {cfg.get('hold_days')} 日 ·
  <b>止损:</b> {sl} ·
  <b>止盈:</b> {tp} ·
  <b>复权:</b> {cfg.get('adjust', 'qfq')} ·
  <b>股票池:</b> {cfg.get('universe_size', '—')} 只
</div>"""


def _fmt_pct(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "—"


def _pct_class(v) -> str:
    if not isinstance(v, (int, float)):
        return ""
    return "up" if v > 0 else ("down" if v < 0 else "")


def _overview_html(o: dict) -> str:
    if not o or o.get("count", 0) == 0:
        return '<div class="meta">本次回测无任何交易触发,可能区间太短或形态过滤太严。</div>'
    return f"""<h2>整体概览</h2>
<div class="grid">
  <div class="card"><div class="label">交易笔数</div><div class="value">{o['count']}</div></div>
  <div class="card"><div class="label">胜率</div><div class="value">{o['win_rate']:.1f}%</div></div>
  <div class="card"><div class="label">平均单笔</div>
    <div class="value {_pct_class(o['avg_return'])}">{_fmt_pct(o['avg_return'])}</div></div>
  <div class="card"><div class="label">中位单笔</div>
    <div class="value {_pct_class(o.get('median_return',0))}">{_fmt_pct(o.get('median_return',0))}</div></div>
  <div class="card"><div class="label">累计净值</div>
    <div class="value {_pct_class(o['total_return'])}">{_fmt_pct(o['total_return'])}</div></div>
  <div class="card"><div class="label">最大回撤</div>
    <div class="value down">{_fmt_pct(o['max_drawdown'])}</div></div>
</div>"""


def _equity_html(b64: str | None) -> str:
    if b64 is None:
        return ""
    return f"""<div class="equity">
  <img src="data:image/png;base64,{b64}" alt="净值曲线"/>
</div>"""


def _pattern_table_html(by_pattern: dict) -> str:
    if not by_pattern:
        return ""
    rows = []
    for name, s in by_pattern.items():
        rows.append(f"""<tr>
  <td>{name}</td>
  <td class="num">{s['count']}</td>
  <td class="num">{s['win_rate']:.1f}%</td>
  <td class="num {_pct_class(s['avg_return'])}">{_fmt_pct(s['avg_return'])}</td>
  <td class="num {_pct_class(s['median_return'])}">{_fmt_pct(s['median_return'])}</td>
  <td class="num up">{_fmt_pct(s['max_win'])}</td>
  <td class="num down">{_fmt_pct(s['max_loss'])}</td>
  <td class="num">{s['profit_factor']:.2f}</td>
  <td class="num">{s['avg_hold_days']:.1f}</td>
  <td class="num">{s['stop_loss_pct']:.0f}% / {s['take_profit_pct']:.0f}%</td>
</tr>""")
    return f"""<h2>按形态/指标分组</h2>
<table>
  <thead><tr>
    <th>形态</th><th>命中数</th><th>胜率</th>
    <th>平均收益</th><th>中位收益</th>
    <th>最大盈</th><th>最大亏</th>
    <th>盈亏比</th><th>持有日</th>
    <th>止损/止盈触发</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<div class="meta">盈亏比 = 总盈利 / 总亏损绝对值。&gt; 1.5 为有正期望,&gt; 2 较优秀。</div>"""


def _trades_table_html(trades: list, limit: int = 200) -> str:
    if not trades:
        return ""
    # 按 entry_date 倒序,最近的在前
    sorted_trades = sorted(trades, key=lambda t: t.entry_date, reverse=True)[:limit]
    rows = []
    for t in sorted_trades:
        reason_badge = {
            "stop_loss": '<span class="badge warn">止损</span>',
            "take_profit": '<span class="badge">止盈</span>',
            "hold_end": '<span class="badge" style="opacity:.6">到期</span>',
        }.get(t.exit_reason, t.exit_reason)
        rows.append(f"""<tr>
  <td>{t.entry_date.strftime('%Y-%m-%d')}</td>
  <td>{t.code}</td>
  <td>{t.name}</td>
  <td>{t.pattern}</td>
  <td class="num">{t.entry_price:.2f}</td>
  <td class="num">{t.exit_price:.2f}</td>
  <td class="num">{t.hold_days}</td>
  <td class="num {_pct_class(t.pnl_pct)}">{_fmt_pct(t.pnl_pct)}</td>
  <td>{reason_badge}</td>
</tr>""")
    note = ""
    if len(trades) > limit:
        note = f'<div class="meta">仅显示最近 {limit} 笔 (共 {len(trades)} 笔)。</div>'
    return f"""<h2>近期交易明细</h2>
<table>
  <thead><tr>
    <th>入场日</th><th>代码</th><th>名称</th><th>形态</th>
    <th>入场价</th><th>出场价</th><th>持有日</th><th>收益</th><th>出场方式</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
{note}"""
