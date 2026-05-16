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

    return p


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
