"""
插件注册中心
============
统一管理所有 K线形态 (PATTERN_REGISTRY) 和技术指标 (INDICATOR_REGISTRY)。

用户自定义方式 (两种均可)：

【方式 A — 装饰器，推荐】
    在 patterns/custom/ 下新建 .py，写函数加装饰器即可：

    from ashare_agent.patterns.registry import register_pattern

    @register_pattern(name="volume_breakout", score=3, desc="放量突破")
    def detect(df):
        # 返回 True/False
        return df['volume'].iloc[-1] > df['volume'].iloc[-20:].mean() * 2

【方式 B — 直接调用 register()】
    from ashare_agent.patterns.registry import register
    register("kind=pattern", name="...", score=..., desc="...", func=lambda df: ...)
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger("ashare_agent")


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Pattern:
    name: str                # 唯一 key (snake_case)
    desc: str                # 中文/英文描述，会出现在报告里
    score: int               # 命中分值
    func: Callable           # (df) -> bool 或 (df) -> (bool, score, desc)
    kind: str = "pattern"    # "pattern" 或 "indicator"
    source: str = "builtin"  # builtin / custom
    enabled_default: bool = True

    def evaluate(self, df) -> tuple[bool, int, str]:
        """统一调用接口：兼容两种返回形式"""
        try:
            result = self.func(df)
        except Exception as e:  # noqa
            log.debug("Pattern %s 异常: %s", self.name, e)
            return False, 0, ""

        if isinstance(result, tuple):
            # (bool, score, desc) — 旧风格，仍支持
            if len(result) == 3:
                ok, score, desc = result
                return bool(ok), int(score) if ok else 0, (desc if ok else "")
            if len(result) == 2:
                ok, _ = result
                return (bool(ok), self.score if ok else 0, self.desc if ok else "")
            return False, 0, ""

        # 纯 bool — 新风格
        ok = bool(result)
        return ok, self.score if ok else 0, self.desc if ok else ""


# ============================================================
# 注册表
# ============================================================
PATTERN_REGISTRY: dict[str, Pattern] = {}
INDICATOR_REGISTRY: dict[str, Pattern] = {}


def _registry_of(kind: str) -> dict[str, Pattern]:
    return INDICATOR_REGISTRY if kind == "indicator" else PATTERN_REGISTRY


def register(
    name: str,
    *,
    desc: str,
    score: int = 2,
    kind: str = "pattern",
    func: Callable | None = None,
    source: str = "custom",
    enabled_default: bool = True,
    overwrite: bool = False,
) -> Callable | None:
    """直接注册一个 pattern/indicator。若不传 func 则返回装饰器。"""
    def _do_register(fn: Callable) -> Callable:
        registry = _registry_of(kind)
        if name in registry and not overwrite:
            log.warning("形态 '%s' 已存在,跳过 (来源 %s)", name, source)
            return fn
        registry[name] = Pattern(
            name=name, desc=desc, score=score, func=fn,
            kind=kind, source=source, enabled_default=enabled_default,
        )
        log.debug("注册 %s: %s (%s, score=%d)", kind, name, source, score)
        return fn

    if func is not None:
        _do_register(func)
        return None
    return _do_register


def register_pattern(name: str, *, desc: str, score: int = 2, **kwargs):
    """K线形态装饰器"""
    return register(name, desc=desc, score=score, kind="pattern", **kwargs)


def register_indicator(name: str, *, desc: str, score: int = 2, **kwargs):
    """技术指标装饰器"""
    return register(name, desc=desc, score=score, kind="indicator", **kwargs)


# ============================================================
# 内置形态注入 (在 candlestick.py / indicators.py 中调用)
# ============================================================
def register_builtin(kind: str, name: str, desc: str, score: int, func: Callable):
    register(name, desc=desc, score=score, kind=kind, func=func,
             source="builtin", overwrite=True)


# ============================================================
# 加载 custom 目录
# ============================================================
def load_custom_dir(custom_dir: str | Path | None = None) -> int:
    """
    动态加载 custom 目录里的所有 .py 模块。
    返回加载到的文件数（不是形态数）。
    """
    if custom_dir is None:
        custom_dir = Path(__file__).parent / "custom"
    custom_dir = Path(custom_dir)
    if not custom_dir.exists():
        return 0

    loaded = 0
    for fp in sorted(custom_dir.glob("*.py")):
        if fp.name.startswith("_"):
            continue
        mod_name = f"ashare_agent.patterns.custom.{fp.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, fp)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            loaded += 1
            log.info("加载自定义模块: %s", fp.name)
        except Exception as e:  # noqa
            log.error("加载自定义模块 %s 失败: %s", fp.name, e)
    return loaded


# ============================================================
# 评估接口 (给 scanner 用)
# ============================================================
def detect_all_patterns(df, enabled: dict[str, bool]) -> list[dict]:
    return _detect(df, PATTERN_REGISTRY, enabled)


def detect_all_indicators(df, enabled: dict[str, bool]) -> list[dict]:
    return _detect(df, INDICATOR_REGISTRY, enabled)


def _detect(df, registry: dict[str, Pattern], enabled: dict[str, bool]) -> list[dict]:
    hits: list[dict] = []
    for name, pat in registry.items():
        # 默认 True; 配置里显式 false 才关闭
        if not enabled.get(name, pat.enabled_default):
            continue
        ok, score, desc = pat.evaluate(df)
        if ok:
            hits.append({"name": name, "score": score, "desc": desc,
                         "source": pat.source, "kind": pat.kind})
    return hits


def list_all() -> dict[str, list[dict]]:
    """供 /api/patterns 接口/调试用"""
    def _dump(reg: dict[str, Pattern]) -> list[dict]:
        return [{"name": p.name, "desc": p.desc, "score": p.score,
                 "source": p.source, "kind": p.kind} for p in reg.values()]
    return {"patterns": _dump(PATTERN_REGISTRY),
            "indicators": _dump(INDICATOR_REGISTRY)}
