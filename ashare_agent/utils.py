"""通用工具：配置加载、日志、目录、交易日判断"""
from __future__ import annotations

import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Any

import yaml

# ---------- 路径 ----------
PROJECT_ROOT = Path(__file__).resolve().parent


def project_path(*parts: str) -> Path:
    p = PROJECT_ROOT.joinpath(*parts)
    return p


def ensure_dir(path: str | Path) -> Path:
    p = Path(path) if Path(path).is_absolute() else project_path(str(path))
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------- 配置 ----------
def load_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else project_path("config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------- 日志 ----------
def setup_logger(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    logger = logging.getLogger("ashare_agent")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        log_path = project_path(log_file) if not Path(log_file).is_absolute() else Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ---------- 交易日 ----------
def is_trading_day(d: date | None = None) -> bool:
    """简单实现：仅排除周末。真实环境可接入交易日历。"""
    d = d or date.today()
    return d.weekday() < 5


def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")
