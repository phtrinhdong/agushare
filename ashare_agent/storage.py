"""扫描结果持久化 (JSON Lines per day)"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .scanner import SignalRow
from .utils import ensure_dir, project_path


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else project_path(str(p))

log = logging.getLogger("ashare_agent")


def _row_to_json(r: SignalRow) -> dict:
    return {
        "code": r.code,
        "name": r.name,
        "close": round(r.close, 2),
        "chg_pct": round(r.chg_pct, 2),
        "chg_5d":  None if r.chg_5d  is None else round(r.chg_5d, 2),
        "chg_15d": None if r.chg_15d is None else round(r.chg_15d, 2),
        "chg_30d": None if r.chg_30d is None else round(r.chg_30d, 2),
        "score": r.score,
        "candlestick": list(r.candlestick),
        "indicators": list(r.indicators),
    }


def save_snapshot(rows: list[SignalRow], out_dir: str | Path) -> Path:
    """每次扫描产出 signals_YYYYMMDD_HHMM.json + latest.json (覆盖)"""
    out_dir = ensure_dir(out_dir)
    now = datetime.now()
    payload = {
        "scanned_at": now.isoformat(timespec="seconds"),
        "count": len(rows),
        "results": [_row_to_json(r) for r in rows],
    }
    fname = f"signals_{now.strftime('%Y%m%d_%H%M')}.json"
    fp = Path(out_dir) / fname
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = Path(out_dir) / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("信号快照已保存: %s", fp)
    return fp


def load_latest(out_dir: str | Path) -> dict | None:
    fp = _resolve(out_dir) / "latest.json"
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("读取 latest.json 失败: %s", e)
        return None


def list_snapshots(out_dir: str | Path) -> list[dict]:
    out_dir = _resolve(out_dir)
    if not out_dir.exists():
        return []
    items = []
    for fp in sorted(out_dir.glob("signals_*.json"), reverse=True):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            items.append({
                "file": fp.name,
                "scanned_at": data.get("scanned_at"),
                "count": data.get("count", 0),
            })
        except Exception:
            continue
    return items
