"""
特别关注列表
============
持久化到 data/watchlist.json (相对 cache_dir 同级,在 docker 里挂载到宿主机)

数据结构:
{
  "items": [
    {
      "code": "600519",
      "name": "贵州茅台",
      "cost_price": 1650.00,    // 成本价,可空
      "quantity": 100,          // 数量,可空
      "added_at": "2026-05-17T10:30:00",
      "note": ""
    },
    ...
  ]
}
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from .utils import ensure_dir, project_path

log = logging.getLogger("ashare_agent")

_LOCK = threading.Lock()
_WATCHLIST_FILE = "data/watchlist.json"   # 相对项目根


def _file_path() -> Path:
    fp = project_path(_WATCHLIST_FILE)
    ensure_dir(fp.parent)
    return fp


def load_all() -> list[dict]:
    fp = _file_path()
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data.get("items", [])
    except Exception as e:
        log.warning("读取关注列表失败: %s", e)
        return []


def save_all(items: list[dict]) -> None:
    fp = _file_path()
    with _LOCK:
        fp.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def add(code: str, name: str = "", cost_price: float | None = None,
        quantity: float | None = None, note: str = "") -> dict:
    """添加/更新一只股票。若已存在则合并字段。返回最终条目。"""
    code = str(code).zfill(6)
    items = load_all()
    existing = next((i for i in items if i["code"] == code), None)

    if existing:
        if name: existing["name"] = name
        if cost_price is not None: existing["cost_price"] = cost_price
        if quantity is not None: existing["quantity"] = quantity
        if note: existing["note"] = note
        item = existing
    else:
        item = {
            "code": code,
            "name": name or code,
            "cost_price": cost_price,
            "quantity": quantity,
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "note": note,
        }
        items.append(item)

    save_all(items)
    return item


def remove(code: str) -> bool:
    code = str(code).zfill(6)
    items = load_all()
    n_before = len(items)
    items = [i for i in items if i["code"] != code]
    save_all(items)
    return len(items) < n_before


def update(code: str, **fields) -> dict | None:
    code = str(code).zfill(6)
    items = load_all()
    for it in items:
        if it["code"] == code:
            for k, v in fields.items():
                if k in {"name", "cost_price", "quantity", "note"}:
                    it[k] = v
            save_all(items)
            return it
    return None
