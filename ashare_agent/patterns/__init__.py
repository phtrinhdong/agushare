"""形态识别 & 技术指标

启动时自动：
1. 注册内置形态/指标
2. 扫描 patterns/custom/ 加载用户自定义形态
"""
from .candlestick import detect_candlestick_patterns  # noqa
from .indicators import detect_indicator_signals      # noqa
from .registry import (
    register_pattern,
    register_indicator,
    load_custom_dir,
    list_all,
    PATTERN_REGISTRY,
    INDICATOR_REGISTRY,
)

# 加载自定义目录 (静默)
_loaded = load_custom_dir()

__all__ = [
    "detect_candlestick_patterns",
    "detect_indicator_signals",
    "register_pattern",
    "register_indicator",
    "load_custom_dir",
    "list_all",
    "PATTERN_REGISTRY",
    "INDICATOR_REGISTRY",
]
