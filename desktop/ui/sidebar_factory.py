"""OISystem 侧边栏工厂 — 根据配置创建对应样式的侧边栏。

支持的样式：
- trapezoid: 梯形吸附式（sidebar_v2.py）
- rect:      矩形吸附式（sidebar_rect.py）
- full:      全屏固定式（sidebar_full.py）
- float:     悬浮快捷键式（sidebar_float.py）

用法：
    from ui.sidebar_factory import create_sidebar
    sidebar = create_sidebar()       # 从配置读取样式
    sidebar = create_sidebar("full") # 指定样式
"""
import importlib

from config.settings import ConfigManager
from utils.helpers import logger


_STYLES = {
    "trapezoid": "ui.sidebar_v2",
    "rect": "ui.sidebar_rect",
    "full": "ui.sidebar_full",
    "float": "ui.sidebar_float",
}


def create_sidebar(style=None):
    """创建侧边栏实例。

    Args:
        style: 样式 ID（trapezoid/rect/full/float）。
               若为 None，从 ConfigManager 读取 sidebar_style。

    Returns:
        Sidebar 实例（统一接口）。
    """
    if style is None:
        cfg = ConfigManager().settings
        style = getattr(cfg, "sidebar_style", "trapezoid")

    if style not in _STYLES:
        logger.warning(f"未知侧边栏样式 '{style}'，回退到 trapezoid")
        style = "trapezoid"

    module_path = _STYLES[style]
    try:
        mod = importlib.import_module(module_path)
        sidebar = mod.Sidebar()
        logger.info(f"侧边栏样式已加载: {style} ({module_path})")
        return sidebar
    except Exception as e:
        logger.error(f"加载侧边栏样式 '{style}' 失败: {e}")
        # 回退到梯形（若本身就是梯形则直接抛出）
        if style != "trapezoid":
            try:
                mod = importlib.import_module("ui.sidebar_v2")
                sidebar = mod.Sidebar()
                logger.warning("已回退到 trapezoid 样式")
                return sidebar
            except Exception as e2:
                logger.error(f"回退到 trapezoid 也失败: {e2}")
        raise
