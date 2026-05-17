"""
CLI 入口
========

# 单次扫描
python -m ashare_agent.main run

# 只扫描自选股
python -m ashare_agent.main run --watchlist

# 启动定时调度 (每个交易日 15:35 扫描)
python -m ashare_agent.main schedule

# 单只股票回测/查看信号
python -m ashare_agent.main inspect 600519
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import reporter, visualizer
from .data_loader import fetch_kline, get_stock_list
from .patterns import detect_candlestick_patterns, detect_indicator_signals
from .scanner import scan_market
from .utils import ensure_dir, load_config, setup_logger, is_trading_day


# ============================================================
# 命令: run
# ============================================================
def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    runtime_cfg = cfg.get("runtime", {})
    log = setup_logger(runtime_cfg.get("log_level", "INFO"),
                       runtime_cfg.get("log_file"))

    if args.skip_trading_check is False and not is_trading_day():
        log.info("今日非交易日,跳过 (使用 --force 可强制扫描)")
        if not args.force:
            return 0

    if args.watchlist:
        cfg["universe"]["scan_all"] = False

    log.info("开始扫描 A股 K线形态 ...")
    rows = scan_market(cfg)

    # ---- 控制台 ----
    reporter.print_console(rows)

    # ---- Excel ----
    out_cfg = cfg["output"]
    reports_dir = out_cfg.get("reports_dir", "output/reports")
    excel_path = reporter.save_excel(rows, reports_dir)

    # ---- K线图 ----
    chart_paths: list[Path] = []
    if out_cfg.get("draw_charts", True) and rows:
        chart_paths = visualizer.draw_charts(
            rows,
            out_cfg.get("charts_dir", "output/charts"),
            max_n=int(out_cfg.get("max_charts", 20)),
        )

    # ---- 邮件 ----
    if cfg.get("email", {}).get("enabled", False):
        reporter.send_email(cfg["email"], rows,
                            attachments=[excel_path])

    return 0


# ============================================================
# 命令: schedule
# ============================================================
def cmd_schedule(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    sched_cfg = cfg.get("schedule", {})
    runtime_cfg = cfg.get("runtime", {})
    log = setup_logger(runtime_cfg.get("log_level", "INFO"),
                       runtime_cfg.get("log_file"))

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.error("请先 pip install APScheduler")
        return 1

    tz = sched_cfg.get("timezone", "Asia/Shanghai")
    hour = int(sched_cfg.get("cron_hour", 15))
    minute = int(sched_cfg.get("cron_minute", 35))

    scheduler = BlockingScheduler(timezone=tz)

    def _job():
        if not is_trading_day(date.today()):
            log.info("非交易日,跳过")
            return
        try:
            cmd_run(argparse.Namespace(
                config=args.config,
                watchlist=False,
                force=False,
                skip_trading_check=False,
            ))
        except Exception as e:
            log.exception("定时任务异常: %s", e)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
        id="daily_scan",
    )
    log.info("定时任务已启动: 每个交易日 %02d:%02d 扫描 (%s)", hour, minute, tz)
    log.info("按 Ctrl+C 退出 ...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("调度器已停止")
    return 0


# ============================================================
# 命令: inspect (单只股票)
# ============================================================
def cmd_inspect(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    log = setup_logger(cfg.get("runtime", {}).get("log_level", "INFO"))

    code = args.code.zfill(6)
    df = fetch_kline(code, bars=int(cfg["data"].get("bars", 120)),
                     adjust=cfg["data"].get("adjust", "qfq"),
                     cache_dir=cfg["data"].get("cache_dir", "cache"),
                     cache_ttl_hours=int(cfg["data"].get("cache_ttl_hours", 6)))
    if df is None or df.empty:
        log.error("拉不到 %s 的数据", code)
        return 1

    candles = detect_candlestick_patterns(df, cfg["patterns"])
    inds = detect_indicator_signals(df, cfg["patterns"])

    last = df.iloc[-1]
    print(f"\n=== {code} 最新K线 ({df.index[-1].strftime('%Y-%m-%d')}) ===")
    print(f"OHLC: {last['open']:.2f} / {last['high']:.2f} / {last['low']:.2f} / {last['close']:.2f}")

    print("\n[K线形态]")
    if candles:
        for h in candles:
            print(f"  ✔ {h['desc']}  (得分 {h['score']})")
    else:
        print("  无")

    print("\n[技术指标]")
    if inds:
        for h in inds:
            print(f"  ✔ {h['desc']}  (得分 {h['score']})")
    else:
        print("  无")

    print(f"\n综合得分: {sum(h['score'] for h in candles) + sum(h['score'] for h in inds)}")
    return 0


# ============================================================
# argparse 主入口
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A股 K线形态扫描 Agent")
    p.add_argument("--config", default=None, help="配置文件路径 (默认 config.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="单次扫描")
    p_run.add_argument("--watchlist", action="store_true", help="只扫描自选股")
    p_run.add_argument("--force", action="store_true", help="非交易日也扫描")
    p_run.add_argument("--skip-trading-check", action="store_true",
                       help="跳过交易日检查")
    p_run.set_defaults(func=cmd_run)

    p_sched = sub.add_parser("schedule", help="启动定时调度")
    p_sched.set_defaults(func=cmd_schedule)

    p_ins = sub.add_parser("inspect", help="检查单只股票当前信号")
    p_ins.add_argument("code", help="股票代码 (例: 600519)")
    p_ins.set_defaults(func=cmd_inspect)

    p_serve = sub.add_parser("serve", help="启动 Web 看板")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_diag = sub.add_parser("diagnose", help="网络连通性 + 数据源诊断")
    p_diag.set_defaults(func=cmd_diagnose)

    p_bt = sub.add_parser("backtest", help="历史回测 (验证形态策略)")
    p_bt.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p_bt.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD (默认: 今天)")
    p_bt.add_argument("--hold-days", type=int, default=5, help="持有日数 (默认 5)")
    p_bt.add_argument("--stop-loss", type=float, default=-5.0,
                      help="止损 %% (默认 -5; 传 0 表示禁用)")
    p_bt.add_argument("--take-profit", type=float, default=10.0,
                      help="止盈 %% (默认 10; 传 0 表示禁用)")
    p_bt.add_argument("--codes", default=None,
                      help="只测这些代码,逗号分隔。例: 600519,000001,300750")
    p_bt.add_argument("--codes-file", default=None,
                      help="代码文件,每行一个 (优先级低于 --codes)")
    p_bt.add_argument("--limit", type=int, default=200,
                      help="universe 上限 (默认 200, 0=不限制)")
    p_bt.add_argument("--workers", type=int, default=None,
                      help="并发数 (默认读 config.yaml)")
    p_bt.set_defaults(func=cmd_backtest)

    p_hist = sub.add_parser("refresh-history", help="批量刷新历史数据库 (深度 K 线缓存)")
    p_hist.add_argument("--bars", type=int, default=800,
                        help="每只股票拉取的 K 线根数 (默认 800 ≈ 3.2 年)")
    p_hist.add_argument("--codes", default=None,
                        help="只刷这些代码 (逗号分隔)")
    p_hist.add_argument("--workers", type=int, default=None)
    p_hist.add_argument("--force", action="store_true",
                        help="强制刷新 (忽略 7 天 TTL,所有都重拉)")
    p_hist.set_defaults(func=cmd_refresh_history)

    return p


def cmd_refresh_history(args: argparse.Namespace) -> int:
    from . import history_cache
    from .data_loader import get_stock_list

    cfg = load_config(args.config)
    runtime_cfg = cfg.get("runtime", {})
    log = setup_logger(runtime_cfg.get("log_level", "INFO"),
                       runtime_cfg.get("log_file"))
    workers = args.workers or int(runtime_cfg.get("max_workers", 3))

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        df = get_stock_list(
            exclude_chinext_star=cfg["universe"].get("exclude_chinext_star", False),
            exclude_st=cfg["universe"].get("exclude_st", True),
            cache_dir=cfg["data"].get("cache_dir", "cache"),
        )
        codes = df["code"].tolist()

    log.info("准备刷新 %d 只股票 → data/history/", len(codes))
    result = history_cache.bulk_refresh(
        codes=codes,
        bars=args.bars,
        adjust=cfg["data"].get("adjust", "qfq"),
        workers=workers,
        skip_fresh=not args.force,
    )
    print(f"\n刷新完成: ok={result['ok']} skipped={result['skipped']} fail={result['fail']}")

    s = history_cache.stats()
    print(f"\n当前数据库状态:")
    print(f"  文件数:    {s['total_files']}")
    print(f"  占用空间:  {s['total_size_mb']} MB")
    print(f"  最新文件:  {s['newest_days']} 天前")
    print(f"  最旧文件:  {s['oldest_days']} 天前")
    print(f"  平均年龄:  {s['avg_age_days']} 天")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from datetime import date
    from . import backtest, backtest_report
    from .data_loader import get_stock_list

    cfg = load_config(args.config)
    runtime_cfg = cfg.get("runtime", {})
    log = setup_logger(runtime_cfg.get("log_level", "INFO"),
                       runtime_cfg.get("log_file"))

    # 解析参数
    end = args.end or date.today().strftime("%Y-%m-%d")
    sl = None if args.stop_loss == 0 else args.stop_loss
    tp = None if args.take_profit == 0 else args.take_profit
    workers = args.workers or int(runtime_cfg.get("max_workers", 3))

    # 确定股票池
    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
        names_map = {}
        try:
            df = get_stock_list(exclude_st=False)
            names_map = dict(zip(df["code"], df["name"]))
        except Exception:
            pass
    elif args.codes_file:
        from pathlib import Path
        text = Path(args.codes_file).read_text(encoding="utf-8")
        codes = [ln.strip().zfill(6) for ln in text.splitlines() if ln.strip()]
        df = get_stock_list(exclude_st=False)
        names_map = dict(zip(df["code"], df["name"]))
    else:
        df = get_stock_list(
            exclude_chinext_star=cfg["universe"].get("exclude_chinext_star", False),
            exclude_st=cfg["universe"].get("exclude_st", True),
        )
        if args.limit and args.limit > 0:
            df = df.head(args.limit)
        codes = df["code"].tolist()
        names_map = dict(zip(df["code"], df["name"]))

    log.info("回测股票池: %d 只 (前 5: %s)", len(codes), codes[:5])

    # 跑回测
    result = backtest.run_backtest(
        codes=codes, names=names_map,
        start=args.start, end=end,
        pattern_cfg=cfg["patterns"],
        hold_days=args.hold_days,
        stop_loss=sl, take_profit=tp,
        adjust=cfg["data"].get("adjust", "qfq"),
        cache_dir=cfg["data"].get("cache_dir", "cache"),
        cache_ttl_hours=24,
        workers=workers,
    )

    # 控制台概览
    o = result.overall()
    print("\n" + "=" * 60)
    print(f"回测概览  {args.start} ~ {end}  (持有 {args.hold_days} 日)")
    print("=" * 60)
    if o["count"] == 0:
        print("  无任何交易触发 (可能区间太短或形态过滤太严)")
    else:
        print(f"  交易笔数:    {o['count']}")
        print(f"  胜率:        {o['win_rate']:.1f}%")
        print(f"  平均单笔:    {o['avg_return']:+.2f}%")
        print(f"  累计净值:    {o['total_return']:+.2f}%")
        print(f"  最大回撤:    {o['max_drawdown']:.2f}%")
        print()
        print("  按形态分组 (Top 10):")
        print(f"  {'形态':24s}{'命中':>6s}{'胜率':>8s}{'平均':>10s}{'盈亏比':>8s}")
        for name, s in list(result.by_pattern().items())[:10]:
            print(f"  {name:24s}{s['count']:>6d}{s['win_rate']:>7.1f}%"
                  f"{s['avg_return']:>+9.2f}%{s['profit_factor']:>8.2f}")

    # HTML 报告
    reports_dir = cfg["output"].get("reports_dir", "output/reports")
    fp = backtest_report.render_html(result, reports_dir)
    print(f"\nHTML 报告: {fp}")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """逐层定位:网络 → 股票列表 → 东方财富单股 → 新浪单股"""
    import socket
    import urllib.request
    from . import data_loader

    print("=" * 60)
    print("agushare · 网络与数据源诊断")
    print("=" * 60)

    # 0) 时间 / 时区
    from datetime import datetime
    print(f"[0] 当前时间: {datetime.now().isoformat()}")

    # 1) DNS
    targets = ["push2.eastmoney.com", "hq.sinajs.cn", "www.baidu.com"]
    print("\n[1] DNS 解析:")
    for host in targets:
        try:
            ip = socket.gethostbyname(host)
            print(f"   ✔ {host} -> {ip}")
        except Exception as e:
            print(f"   ✘ {host} 解析失败: {e}")

    # 2) HTTPS 连通性 (urllib 测试)
    urls = [
        ("eastmoney 行情接口", "https://push2.eastmoney.com/"),
        ("新浪行情",            "https://hq.sinajs.cn/"),
    ]
    print("\n[2] HTTPS 连通:")
    for label, url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                "Referer": "https://www.eastmoney.com/",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                print(f"   ✔ {label}: HTTP {resp.status}")
        except Exception as e:
            print(f"   ✘ {label}: {type(e).__name__}: {e}")

    # 3) akshare 股票列表
    print("\n[3] akshare 股票列表 (stock_info_a_code_name):")
    try:
        df = data_loader.get_stock_list(exclude_st=False)
        print(f"   ✔ 拉到 {len(df)} 只 (前 3 只: {df['code'].head(3).tolist()})")
        sample_code = df["code"].iloc[100]  # 拿第 100 只测试
    except Exception as e:
        print(f"   ✘ 失败: {type(e).__name__}: {e}")
        return 2

    # 4) 单股 — 东方财富
    from datetime import timedelta
    print(f"\n[4] 东方财富 K线测试 (code={sample_code}):")
    try:
        df_raw = data_loader._fetch_em(
            sample_code,
            (datetime.now() - timedelta(days=60)).strftime("%Y%m%d"),
            datetime.now().strftime("%Y%m%d"),
            "qfq",
        )
        if df_raw is None or df_raw.empty:
            print("   ⚠ 返回空 DataFrame")
        else:
            print(f"   ✔ 拉到 {len(df_raw)} 行")
    except Exception as e:
        print(f"   ✘ 失败: {type(e).__name__}: {e!r}")

    # 5) 单股 — 新浪
    print(f"\n[5] 新浪 K线测试 (code={sample_code}):")
    try:
        df_raw = data_loader._fetch_sina(sample_code, "qfq")
        if df_raw is None or df_raw.empty:
            print("   ⚠ 返回空 DataFrame")
        else:
            print(f"   ✔ 拉到 {len(df_raw)} 行 (最近: {df_raw.tail(1).iloc[0].to_dict()})")
    except Exception as e:
        print(f"   ✘ 失败: {type(e).__name__}: {e!r}")

    print("\n=" * 30)
    print("解读:")
    print(" - [2] 任一行 HTTP 200/30x = 网络可达;若 403/401/SSL 错 = 上游主动拒绝你的 IP")
    print(" - [4] 东方财富失败 + [5] 新浪成功 = 仅东方财富被封,代码已自动回退新浪")
    print(" - [4][5] 都失败 = IP 在被两边都拦,建议换机房或加 HTTP 代理")
    print(" - 全部成功 = 接口正常,之前的失败可能是临时限流,重试即可")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("请先 pip install fastapi uvicorn", file=sys.stderr)
        return 1
    uvicorn.run(
        "ashare_agent.server:app",
        host=args.host, port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
