"""
FastAPI Web 后端
================
启动: uvicorn ashare_agent.server:app --reload --port 8000
或:   python -m ashare_agent.main serve

接口:
    GET  /                  → 前端单页 (index.html)
    GET  /api/signals       → 最新扫描结果 JSON
    GET  /api/patterns      → 所有可用形态/指标
    POST /api/scan          → 触发后台扫描
    GET  /api/scan/status   → 扫描进度
    GET  /api/chart/{code}  → 该股票的K线图 PNG (若存在)
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from . import data_loader, realtime, reporter, storage, visualizer, watchlist as wl
from .patterns import (
    detect_candlestick_patterns,
    detect_indicator_signals,
    list_all,
)
from .scanner import SignalRow, scan_market
from .utils import ensure_dir, load_config, project_path, setup_logger

log = logging.getLogger("ashare_agent")


# ============================================================
# 全局扫描状态 (线程安全)
# ============================================================
class ScanState:
    """扫描状态 + 实时进度 + 实时命中列表 (供 Web 边扫边显示)"""

    # 推到前端的最大行数 (按得分排序后取前 N)
    MAX_LIVE_ROWS = 200

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.message = "idle"
        self.error: str | None = None
        self.progress: dict = {"total": 0, "scanned": 0, "ok": 0, "fail": 0, "hit": 0}
        # 命中行 (dict 形式, 与 storage._row_to_json 同 schema)
        self._results: list[dict] = []

    def reset(self):
        with self.lock:
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.message = "扫描中..."
            self.error = None
            self.progress = {"total": 0, "scanned": 0, "ok": 0, "fail": 0, "hit": 0}
            self._results = []

    def push_result(self, row_dict: dict):
        with self.lock:
            self._results.append(row_dict)

    def update_progress(self, prog: dict, message: str | None = None):
        with self.lock:
            self.progress = prog
            if message is not None:
                self.message = message

    def finish(self, message: str, error: str | None = None):
        with self.lock:
            self.running = False
            self.finished_at = time.time()
            self.message = message
            self.error = error

    def to_dict(self) -> dict:
        with self.lock:
            elapsed = None
            if self.started_at:
                end = self.finished_at or time.time()
                elapsed = round(end - self.started_at, 1)
            # 按得分降序,截断到 MAX_LIVE_ROWS
            top = sorted(self._results, key=lambda r: -r.get("score", 0))[: self.MAX_LIVE_ROWS]
            return {
                "running": self.running,
                "message": self.message,
                "error": self.error,
                "elapsed_sec": elapsed,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "progress": self.progress,
                "result_count": len(self._results),
                "results": top,
            }


STATE = ScanState()


# ============================================================
# 回测状态 (独立于扫描)
# ============================================================
class BacktestState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.message = "idle"
        self.error: str | None = None
        self.params: dict = {}
        self.report_file: str | None = None     # 仅文件名,如 backtest_xxxx.html
        self.summary: dict = {}                 # 关键指标速览

    def reset(self, params: dict):
        with self.lock:
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.message = "拉取数据 + 回测中..."
            self.error = None
            self.params = params
            self.report_file = None
            self.summary = {}

    def update_message(self, msg: str):
        with self.lock:
            self.message = msg

    def finish(self, summary: dict, report_file: str | None,
               error: str | None = None):
        with self.lock:
            self.running = False
            self.finished_at = time.time()
            self.summary = summary
            self.report_file = report_file
            self.error = error
            self.message = "完成" if error is None else "失败"

    def to_dict(self) -> dict:
        with self.lock:
            elapsed = None
            if self.started_at:
                end = self.finished_at or time.time()
                elapsed = round(end - self.started_at, 1)
            return {
                "running": self.running,
                "message": self.message,
                "error": self.error,
                "params": self.params,
                "summary": self.summary,
                "report_file": self.report_file,
                "elapsed_sec": elapsed,
            }


BT_STATE = BacktestState()


# ============================================================
# 后台扫描
# ============================================================
def _run_scan_background(cfg: dict):
    STATE.reset()

    def on_result(row):
        # 用 storage 里的英文 key 序列化,前端表格直接消费
        STATE.push_result(storage._row_to_json(row))

    def on_progress(prog: dict):
        msg = f"扫描中 {prog['scanned']}/{prog['total']}  (命中 {prog['hit']}, 失败 {prog['fail']})"
        STATE.update_progress(prog, message=msg)

    try:
        rows = scan_market(cfg, on_result=on_result, on_progress=on_progress)

        STATE.update_progress(STATE.progress, message="保存快照与图表...")

        reports_dir = cfg["output"].get("reports_dir", "output/reports")
        storage.save_snapshot(rows, reports_dir)
        reporter.save_excel(rows, reports_dir)

        if cfg["output"].get("draw_charts", True) and rows:
            visualizer.draw_charts(
                rows,
                cfg["output"].get("charts_dir", "output/charts"),
                max_n=int(cfg["output"].get("max_charts", 20)),
            )

        STATE.finish(f"完成,命中 {len(rows)} 只")
    except Exception as e:
        log.exception("扫描失败")
        STATE.finish("扫描失败", error=str(e))


# ============================================================
# 可选 HTTP Basic Auth (公网部署强烈建议开启)
# ============================================================
_AUTH_USER = os.environ.get("AGU_AUTH_USER", "")
_AUTH_PASS = os.environ.get("AGU_AUTH_PASS", "")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)
_security = HTTPBasic(auto_error=False)

# 不走认证的路径白名单 (健康检查等)
_AUTH_WHITELIST = {"/healthz"}


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_security),
):
    if not _AUTH_ENABLED:
        return  # 未配置就完全不强制
    if request.url.path in _AUTH_WHITELIST:
        return  # 健康检查等放行
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, _AUTH_USER)
    ok_pass = secrets.compare_digest(credentials.password, _AUTH_PASS)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Basic"},
        )


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(title="A股形态扫描 Agent", version="0.2.0",
              dependencies=[Depends(require_auth)])

_CFG: dict[str, Any] = {}
_WEB_DIR = Path(__file__).parent / "web"


@app.on_event("startup")
def _startup():
    global _CFG
    _CFG = load_config()
    setup_logger(_CFG.get("runtime", {}).get("log_level", "INFO"),
                 _CFG.get("runtime", {}).get("log_file"))
    log.info("Web 服务启动,已加载 %d 个形态 + %d 个指标 (auth=%s)",
             len(list_all()["patterns"]), len(list_all()["indicators"]),
             "ON" if _AUTH_ENABLED else "OFF")


# 健康检查 — 不走 auth (Docker / Caddy / 负载均衡用)
@app.get("/healthz", dependencies=[])
def healthz():
    return {"ok": True, "running": STATE.running}


@app.get("/")
def index():
    fp = _WEB_DIR / "index.html"
    if not fp.exists():
        return JSONResponse({"error": "index.html missing"}, status_code=500)
    return FileResponse(fp)


@app.get("/api/signals")
def get_signals():
    reports_dir = _CFG["output"].get("reports_dir", "output/reports")
    data = storage.load_latest(reports_dir)
    if data is None:
        return {"scanned_at": None, "count": 0, "results": []}
    return data


@app.get("/api/patterns")
def get_patterns():
    return list_all()


@app.post("/api/scan")
def trigger_scan(watchlist_only: bool = False):
    with STATE.lock:
        if STATE.running:
            raise HTTPException(409, "已有扫描在运行中")

    cfg = {**_CFG}
    if watchlist_only:
        # 浅拷贝改 universe
        cfg = dict(cfg)
        cfg["universe"] = {**cfg["universe"], "scan_all": False}

    t = threading.Thread(target=_run_scan_background, args=(cfg,), daemon=True)
    t.start()
    return {"ok": True, "message": "已启动后台扫描"}


@app.get("/api/scan/status")
def scan_status():
    return STATE.to_dict()


@app.get("/api/snapshots")
def list_snapshots():
    reports_dir = _CFG["output"].get("reports_dir", "output/reports")
    return {"items": storage.list_snapshots(reports_dir)}


# ============================================================
# 回测
# ============================================================
def _run_backtest_background(params: dict):
    """在后台线程跑回测,完成后写 BT_STATE"""
    from datetime import date
    from . import backtest, backtest_report
    from .data_loader import get_stock_list

    BT_STATE.reset(params)
    try:
        # 解析参数
        start = params.get("start") or "2024-01-01"
        end = params.get("end") or date.today().strftime("%Y-%m-%d")
        hold_days = int(params.get("hold_days", 5))
        sl = params.get("stop_loss")
        tp = params.get("take_profit")
        sl = None if (sl is None or float(sl) == 0) else float(sl)
        tp = None if (tp is None or float(tp) == 0) else float(tp)

        limit = int(params.get("limit", 200))
        codes_str = (params.get("codes") or "").strip()

        BT_STATE.update_message("加载股票池...")
        if codes_str:
            codes = [c.strip().zfill(6) for c in codes_str.split(",") if c.strip()]
            try:
                df = get_stock_list(exclude_st=False)
                names_map = dict(zip(df["code"], df["name"]))
            except Exception:
                names_map = {}
        else:
            df = get_stock_list(
                exclude_chinext_star=_CFG["universe"].get("exclude_chinext_star", False),
                exclude_st=_CFG["universe"].get("exclude_st", True),
            )
            if limit > 0:
                df = df.head(limit)
            codes = df["code"].tolist()
            names_map = dict(zip(df["code"], df["name"]))

        BT_STATE.update_message(f"回测 {len(codes)} 只股票 · {start} ~ {end}")

        result = backtest.run_backtest(
            codes=codes, names=names_map,
            start=start, end=end,
            pattern_cfg=_CFG["patterns"],
            hold_days=hold_days,
            stop_loss=sl, take_profit=tp,
            adjust=_CFG["data"].get("adjust", "qfq"),
            cache_dir=_CFG["data"].get("cache_dir", "cache"),
            cache_ttl_hours=24,
            workers=int(_CFG.get("runtime", {}).get("max_workers", 3)),
        )

        BT_STATE.update_message("生成 HTML 报告...")
        reports_dir = _CFG["output"].get("reports_dir", "output/reports")
        fp = backtest_report.render_html(result, reports_dir)

        o = result.overall()
        summary = {
            "count": o.get("count", 0),
            "win_rate": round(o.get("win_rate", 0), 1),
            "avg_return": round(o.get("avg_return", 0), 2),
            "total_return": round(o.get("total_return", 0), 2),
            "max_drawdown": round(o.get("max_drawdown", 0), 2),
            "universe_size": len(codes),
            "top_patterns": [
                {"name": k, "count": v["count"],
                 "win_rate": round(v["win_rate"], 1),
                 "avg_return": round(v["avg_return"], 2)}
                for k, v in list(result.by_pattern().items())[:5]
            ],
        }
        BT_STATE.finish(summary=summary, report_file=fp.name)
    except Exception as e:
        log.exception("回测失败")
        BT_STATE.finish(summary={}, report_file=None, error=str(e))


@app.post("/api/backtest")
def trigger_backtest(payload: dict):
    """触发回测 (后台线程)"""
    with BT_STATE.lock:
        if BT_STATE.running:
            raise HTTPException(409, "已有回测正在运行")
    if STATE.to_dict()["running"]:
        raise HTTPException(409, "扫描正在运行,请等待扫描完成再回测")

    t = threading.Thread(target=_run_backtest_background, args=(payload,),
                         daemon=True)
    t.start()
    return {"ok": True, "message": "已启动后台回测"}


@app.get("/api/backtest/status")
def backtest_status():
    return BT_STATE.to_dict()


@app.get("/api/backtest/report/{filename}")
def backtest_report_file(filename: str):
    """返回 HTML 报告 (仅允许 backtest_*.html, 防路径穿越)"""
    if not filename.startswith("backtest_") or not filename.endswith(".html"):
        raise HTTPException(400, "非法文件名")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    reports_dir = Path(_CFG["output"].get("reports_dir", "output/reports"))
    if not reports_dir.is_absolute():
        reports_dir = project_path(str(reports_dir))
    fp = reports_dir / filename
    if not fp.exists():
        raise HTTPException(404, "报告不存在")
    return FileResponse(fp, media_type="text/html; charset=utf-8")


# ============================================================
# 特别关注 (Watchlist)
# ============================================================
def _enrich_watchlist(items: list[dict]) -> list[dict]:
    """给每条关注项补上实时行情 + 持仓盈亏"""
    if not items:
        return []
    codes = [it["code"] for it in items]
    quotes = realtime.get_quotes(codes)
    out = []
    for it in items:
        q = quotes.get(it["code"], {})
        price = q.get("price")
        cost = it.get("cost_price")
        qty = it.get("quantity")

        # 持仓盈亏
        pnl_pct = None
        pnl_amt = None
        if cost and price:
            pnl_pct = (price - cost) / cost * 100
            if qty:
                pnl_amt = (price - cost) * qty

        out.append({
            **it,
            "price":      price,
            "chg_pct":    q.get("chg_pct"),
            "chg":        q.get("chg"),
            "volume":     q.get("volume"),
            "high":       q.get("high"),
            "low":        q.get("low"),
            "open":       q.get("open"),
            "prev_close": q.get("prev_close"),
            "pnl_pct":    None if pnl_pct is None else round(pnl_pct, 2),
            "pnl_amount": None if pnl_amt is None else round(pnl_amt, 2),
            "market_value": None if (price is None or qty is None) else round(price * qty, 2),
        })
    return out


@app.get("/api/watchlist")
def list_watchlist():
    items = _enrich_watchlist(wl.load_all())
    return {
        "items": items,
        "count": len(items),
        "trading_hours": realtime.is_trading_hours(),
    }


@app.post("/api/watchlist")
def add_watchlist(payload: dict):
    code = str(payload.get("code", "")).strip().zfill(6)
    if not code or not code.isdigit():
        raise HTTPException(400, "code 必填且为数字")

    name = payload.get("name", "") or _get_name(code)
    cost_price = payload.get("cost_price")
    quantity = payload.get("quantity")
    note = payload.get("note", "")

    # 类型转换 (允许空字符串/null)
    try:
        cost_price = float(cost_price) if cost_price not in (None, "", 0) else None
    except (ValueError, TypeError):
        cost_price = None
    try:
        quantity = float(quantity) if quantity not in (None, "", 0) else None
    except (ValueError, TypeError):
        quantity = None

    item = wl.add(code, name=name, cost_price=cost_price,
                  quantity=quantity, note=note)
    return {"ok": True, "item": item}


@app.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    ok = wl.remove(code)
    if not ok:
        raise HTTPException(404, "未找到该代码")
    return {"ok": True}


@app.patch("/api/watchlist/{code}")
def update_watchlist(code: str, payload: dict):
    fields = {}
    for k in ("name", "cost_price", "quantity", "note"):
        if k in payload:
            v = payload[k]
            if k in ("cost_price", "quantity"):
                try:
                    v = float(v) if v not in (None, "", 0) else None
                except (ValueError, TypeError):
                    v = None
            fields[k] = v
    item = wl.update(code, **fields)
    if item is None:
        raise HTTPException(404, "未找到该代码")
    return {"ok": True, "item": item}


@app.get("/api/realtime")
def get_realtime(codes: str):
    """codes: 逗号分隔的代码列表"""
    code_list = [c.strip().zfill(6) for c in codes.split(",") if c.strip()]
    return {"quotes": realtime.get_quotes(code_list),
            "trading_hours": realtime.is_trading_hours()}


@app.get("/api/backtest/list")
def backtest_list():
    """列出历史回测报告"""
    reports_dir = Path(_CFG["output"].get("reports_dir", "output/reports"))
    if not reports_dir.is_absolute():
        reports_dir = project_path(str(reports_dir))
    if not reports_dir.exists():
        return {"items": []}
    items = []
    for fp in sorted(reports_dir.glob("backtest_*.html"), reverse=True):
        items.append({
            "file": fp.name,
            "size_kb": round(fp.stat().st_size / 1024, 1),
            "mtime": time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(fp.stat().st_mtime)),
        })
    return {"items": items[:30]}


# ============================================================
# 股票名称缓存 (用于按需绘图时拿到中文名)
# ============================================================
_NAME_CACHE: dict[str, str] = {}
_NAME_CACHE_LOADED_AT: float = 0
_NAME_CACHE_TTL_SEC = 24 * 3600  # 24h


def _get_name(code: str) -> str:
    global _NAME_CACHE_LOADED_AT
    if (time.time() - _NAME_CACHE_LOADED_AT) > _NAME_CACHE_TTL_SEC:
        try:
            df = data_loader.get_stock_list(exclude_st=False)
            _NAME_CACHE.clear()
            _NAME_CACHE.update(dict(zip(df["code"], df["name"])))
            _NAME_CACHE_LOADED_AT = time.time()
        except Exception as e:
            log.warning("加载股票名称失败: %s", e)
    return _NAME_CACHE.get(code, code)


def _resolve_charts_dir() -> Path:
    charts_dir = Path(_CFG["output"].get("charts_dir", "output/charts"))
    if not charts_dir.is_absolute():
        charts_dir = project_path(str(charts_dir))
    return charts_dir


@app.get("/api/chart/{code}")
def get_chart(code: str, fresh: bool = False):
    """
    返回该股票的 K线 PNG。
    - 默认先返回今天的缓存 PNG (如果存在)
    - 没有就现场拉 K 线 → 识别信号 → 绘图 → 写盘 → 返回
    - fresh=true 强制重新绘图
    """
    code = code.zfill(6)
    charts_dir = _resolve_charts_dir()
    today = time.strftime("%Y%m%d")
    cached = charts_dir / f"{code}_{today}.png"

    if cached.exists() and not fresh:
        return FileResponse(cached, media_type="image/png")

    # ---------- 按需生成 ----------
    bars = int(_CFG["data"].get("bars", 120))
    df = data_loader.fetch_kline(
        code,
        bars=bars,
        adjust=_CFG["data"].get("adjust", "qfq"),
        cache_dir=_CFG["data"].get("cache_dir", "cache"),
        cache_ttl_hours=int(_CFG["data"].get("cache_ttl_hours", 6)),
    )
    if df is None or df.empty:
        raise HTTPException(404, f"未拿到 {code} 的K线数据 (数据源可能临时不可用)")

    # 重新检测形态/指标,用于在图上标注
    pat_cfg = _CFG["patterns"]
    candles = detect_candlestick_patterns(df, pat_cfg)
    inds = detect_indicator_signals(df, pat_cfg)

    last = df.iloc[-1]
    prev_close = df["close"].iloc[-2] if len(df) > 1 else last["close"]
    chg = (last["close"] - prev_close) / prev_close * 100 if prev_close else 0.0
    score = sum(h["score"] for h in candles) + sum(h["score"] for h in inds)

    row = SignalRow(
        code=code,
        name=_get_name(code),
        close=float(last["close"]),
        chg_pct=float(chg),
        score=score,
        candlestick=[h["desc"] for h in candles],
        indicators=[h["desc"] for h in inds],
        df=df,
    )

    try:
        fp = visualizer.draw_signal_chart(row, charts_dir)
    except Exception as e:
        log.exception("绘图失败 %s", code)
        raise HTTPException(500, f"绘图失败: {e}")

    if fp is None or not fp.exists():
        raise HTTPException(500, "绘图失败 (mplfinance 未安装或返回空)")

    return FileResponse(fp, media_type="image/png")


# 静态文件 (备用，比如未来加 css/js 单独文件)
if _WEB_DIR.exists():
    ensure_dir(_WEB_DIR)
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")
