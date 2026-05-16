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
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import reporter, storage, visualizer
from .patterns import list_all
from .scanner import scan_market
from .utils import ensure_dir, load_config, setup_logger

log = logging.getLogger("ashare_agent")


# ============================================================
# 全局扫描状态 (线程安全)
# ============================================================
class ScanState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.message = "idle"
        self.error: str | None = None
        self.result_count = 0

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
                "result_count": self.result_count,
                "elapsed_sec": elapsed,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


STATE = ScanState()


# ============================================================
# 后台扫描
# ============================================================
def _run_scan_background(cfg: dict):
    with STATE.lock:
        STATE.running = True
        STATE.started_at = time.time()
        STATE.finished_at = None
        STATE.message = "数据拉取 + 形态识别中..."
        STATE.error = None
        STATE.result_count = 0

    try:
        rows = scan_market(cfg)

        with STATE.lock:
            STATE.message = "保存快照与图表..."
            STATE.result_count = len(rows)

        reports_dir = cfg["output"].get("reports_dir", "output/reports")
        storage.save_snapshot(rows, reports_dir)
        reporter.save_excel(rows, reports_dir)

        if cfg["output"].get("draw_charts", True) and rows:
            visualizer.draw_charts(
                rows,
                cfg["output"].get("charts_dir", "output/charts"),
                max_n=int(cfg["output"].get("max_charts", 20)),
            )

        with STATE.lock:
            STATE.message = f"完成,命中 {len(rows)} 只"
    except Exception as e:
        log.exception("扫描失败")
        with STATE.lock:
            STATE.error = str(e)
            STATE.message = "扫描失败"
    finally:
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = time.time()


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(title="A股形态扫描 Agent", version="0.2.0")

_CFG: dict[str, Any] = {}
_WEB_DIR = Path(__file__).parent / "web"


@app.on_event("startup")
def _startup():
    global _CFG
    _CFG = load_config()
    setup_logger(_CFG.get("runtime", {}).get("log_level", "INFO"),
                 _CFG.get("runtime", {}).get("log_file"))
    log.info("Web 服务启动,已加载 %d 个形态 + %d 个指标",
             len(list_all()["patterns"]), len(list_all()["indicators"]))


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


@app.get("/api/chart/{code}")
def get_chart(code: str):
    charts_dir = Path(_CFG["output"].get("charts_dir", "output/charts"))
    # 找最新的同代码 PNG
    if not charts_dir.is_absolute():
        from .utils import project_path
        charts_dir = project_path(str(charts_dir))
    candidates = sorted(charts_dir.glob(f"{code}_*.png"), reverse=True)
    if not candidates:
        raise HTTPException(404, f"未找到 {code} 的K线图")
    return FileResponse(candidates[0], media_type="image/png")


# 静态文件 (备用，比如未来加 css/js 单独文件)
if _WEB_DIR.exists():
    ensure_dir(_WEB_DIR)
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")
