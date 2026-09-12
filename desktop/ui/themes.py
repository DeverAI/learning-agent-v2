"""OISystem 主题系统。

4个内置预设 + JSON导入/导出 + 自定义主题持久化。
通过 ThemeManager.get_css() 获取 QSS，覆盖全控件类型。
"""
import os
import json
import copy
from typing import Dict

from PySide6.QtGui import QColor

THEMES_DIR = os.path.join(os.path.dirname(__file__), "themes")

# 内置主题默认值，供导出和 fallback
_BUILTIN: Dict[str, dict] = {
    "black": {
        "name": "纯黑",
        "bg": "#000000", "surface": "#1e293b",
        "text": "#e2e8f0", "text_dim": "#64748b",
        "accent": "#3b82f6", "accent_hover": "#2563eb",
        "border": "#334155", "border_light": "#2b3a52",
        "bubble_user_bg": "rgba(37, 99, 235, 38)",
        "bubble_ai_bg": "#1a2740",
        "scrollbar_handle": "#334155", "scrollbar_hover": "#475569",
        "input_bg": "#1e293b", "input_border": "#2b3a52",
        "input_focus": "#3b82f6", "code_bg": "#0f172a",
        "danger": "#991b1b", "danger_hover": "#b91c1c",
        "success": "#22c55e", "warning": "#fbbf24",
    },
    "white": {
        "name": "纯白",
        "bg": "#ffffff", "surface": "#f8fafc",
        "text": "#1e293b", "text_dim": "#94a3b8",
        "accent": "#2563eb", "accent_hover": "#1d4ed8",
        "border": "#e2e8f0", "border_light": "#f1f5f9",
        "bubble_user_bg": "rgba(37, 99, 235, 12)",
        "bubble_ai_bg": "#f1f5f9",
        "scrollbar_handle": "#cbd5e1", "scrollbar_hover": "#94a3b8",
        "input_bg": "#ffffff", "input_border": "#e2e8f0",
        "input_focus": "#2563eb", "code_bg": "#f1f5f9",
        "danger": "#dc2626", "danger_hover": "#b91c1c",
        "success": "#16a34a", "warning": "#d97706",
    },
    "cream": {
        "name": "米色",
        "bg": "#faf8f5", "surface": "#f5f0e8",
        "text": "#3d3226", "text_dim": "#a39680",
        "accent": "#8b6914", "accent_hover": "#6b5010",
        "border": "#ddd5c5", "border_light": "#ede8dd",
        "bubble_user_bg": "rgba(139, 105, 20, 10)",
        "bubble_ai_bg": "#ede8dd",
        "scrollbar_handle": "#d5cec0", "scrollbar_hover": "#b8ad9a",
        "input_bg": "#ffffff", "input_border": "#ddd5c5",
        "input_focus": "#8b6914", "code_bg": "#f0ece5",
        "danger": "#c53030", "danger_hover": "#9b2c2c",
        "success": "#2f855a", "warning": "#b7791f",
    },
    "blue": {
        "name": "蓝调",
        "bg": "#0a1628", "surface": "#132444",
        "text": "#c8dff5", "text_dim": "#5a7da0",
        "accent": "#60a5fa", "accent_hover": "#3b82f6",
        "border": "#1e3a5f", "border_light": "#162d4a",
        "bubble_user_bg": "rgba(59, 130, 246, 30)",
        "bubble_ai_bg": "#142842",
        "scrollbar_handle": "#1e3a5f", "scrollbar_hover": "#2a4a75",
        "input_bg": "#0f2040", "input_border": "#1e3a5f",
        "input_focus": "#60a5fa", "code_bg": "#060e1a",
        "danger": "#991b1b", "danger_hover": "#b91c1c",
        "success": "#22c55e", "warning": "#fbbf24",
    },
}

_REQUIRED_KEYS = {"bg", "surface", "text", "accent"}


def _load_cfg_theme():
    """从 ConfigManager 读取已持久化的主题设置，避免循环导入。"""
    try:
        from config.settings import ConfigManager
        cfg = ConfigManager().settings
        return cfg.theme, cfg.theme_custom_data
    except Exception:
        return "black", {}


class ThemeManager:
    """主题管理单例。构建完整的 QSS 样式表。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._current = "black"
            cls._instance._custom = None
            cls._instance._load_from_cfg()
        return cls._instance

    def _load_from_cfg(self):
        key, custom = _load_cfg_theme()
        if key == "custom" and isinstance(custom, dict) and custom \
                and _REQUIRED_KEYS.issubset(custom.keys()):
            self._current = "custom"
            self._custom = copy.deepcopy(custom)
        elif key in _BUILTIN:
            self._current = key
            self._custom = None
        else:
            self._current = "black"
            self._custom = None

    # ---- 获取 ----
    @property
    def current_theme(self) -> dict:
        # r39 P1 修复：custom 主题原本返回引用，被 ThemeTab 修改时会污染 _custom。
        # 统一返回 deepcopy，保证所有调用方拿到独立副本。
        # round48 P0：custom 主题可能只含 _REQUIRED_KEYS（导入校验只要求 4 个必填键），
        # 直接返回会导致 MessageBubble 等 theme["bubble_ai_bg"] 抛 KeyError；
        # 这里统一经 _fill_theme_defaults 补齐全部标准键后再返回。
        if self._current == "custom" and self._custom is not None:
            return _fill_theme_defaults(copy.deepcopy(self._custom))
        return _fill_theme_defaults(copy.deepcopy(_BUILTIN.get(self._current, _BUILTIN["black"])))

    @property
    def current_key(self) -> str:
        return self._current

    @property
    def is_custom(self) -> bool:
        return self._current == "custom"

    def list_builtin(self) -> Dict[str, str]:
        """返回 {key: name} 的内置主题列表。"""
        return {k: v["name"] for k, v in _BUILTIN.items()}

    # ---- 切换 ----
    def select(self, key: str):
        if key in _BUILTIN:
            self._current = key
            self._custom = None
        elif key == "custom" and self._custom is not None:
            self._current = "custom"

    def set_custom(self, data: dict):
        """设置并切换到自定义主题。"""
        if not isinstance(data, dict):
            raise ValueError("自定义主题必须是 JSON 对象")
        if not _REQUIRED_KEYS.issubset(data.keys()):
            raise ValueError(f"自定义主题缺少必要字段: {_REQUIRED_KEYS - set(data.keys())}")
        self._custom = copy.deepcopy(data)
        self._current = "custom"

    def update_custom(self, data: dict):
        """更新当前自定义主题数据（不切换 key）。"""
        if self._current != "custom":
            raise RuntimeError("仅在自定义主题下可 update_custom")
        self._custom = copy.deepcopy(data)

    def load_custom(self, path: str) -> dict:
        """从 JSON 文件导入自定义主题。返回主题 dict。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            raise ValueError(f"读取主题文件失败: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("主题 JSON 必须是对象")
        if not _REQUIRED_KEYS.issubset(data.keys()):
            raise ValueError(f"缺少必要字段: {_REQUIRED_KEYS - set(data.keys())}")
        # P2 修复：与 set_custom 对齐，使用 deepcopy 防止外部修改污染内部状态
        self._custom = copy.deepcopy(data)
        self._current = "custom"
        return copy.deepcopy(data)

    def export_theme(self, path: str):
        """导出当前主题为 JSON。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.current_theme, f, ensure_ascii=False, indent=2)

    def export_builtin(self, key: str, path: str):
        """导出指定内置主题为 JSON。"""
        data = _BUILTIN.get(key)
        if data:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def save_builtin_to_files(self):
        """将所有内置主题写入 ui/themes/ 目录。"""
        os.makedirs(THEMES_DIR, exist_ok=True)
        for key, data in _BUILTIN.items():
            path = os.path.join(THEMES_DIR, f"{key}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- 生成 QSS ----
    def get_css(self) -> str:
        """生成当前主题的完整 QSS 样式表。"""
        return _render_qss(self.current_theme)


def _fill_theme_defaults(t: dict) -> dict:
    """为主题 dict 填充缺失键的合理默认值（从已有键派生）。"""
    t = dict(t)
    bg = t.get('bg', '#000000')
    surface = t.get('surface', bg)
    text = t.get('text', '#e2e8f0')
    accent = t.get('accent', '#3b82f6')
    border = t.get('border', '#334155')
    t.setdefault('surface', bg)
    t.setdefault('text', '#e2e8f0')
    t.setdefault('text_dim', text)
    t.setdefault('accent', '#3b82f6')
    t.setdefault('accent_hover', accent)
    t.setdefault('border', '#334155')
    t.setdefault('border_light', border)
    t.setdefault('bubble_user_bg', f'rgba(37,99,235,30)')
    t.setdefault('bubble_ai_bg', surface)
    t.setdefault('scrollbar_handle', border)
    t.setdefault('scrollbar_hover', '#475569')
    t.setdefault('input_bg', surface)
    t.setdefault('input_border', border)
    t.setdefault('input_focus', accent)
    t.setdefault('code_bg', bg)
    t.setdefault('danger', '#991b1b')
    t.setdefault('danger_hover', '#b91c1c')
    t.setdefault('success', '#22c55e')
    t.setdefault('warning', '#fbbf24')
    t.setdefault('name', 'unknown')
    return t


def _luminance(hex_color: str) -> float:
    """计算 hex 颜色的相对亮度 (0~1)。"""
    try:
        c = QColor(hex_color)
        if not c.isValid():
            return 0.0
        return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    except Exception:
        return 0.0


def is_light_theme(theme: dict = None) -> bool:
    """判断当前主题是否为亮色主题（基于背景亮度）。"""
    if theme is None:
        theme = ThemeManager().current_theme
    return _luminance(theme.get('bg', '#000000')) > 0.5


def get_icon_color(theme: dict = None) -> str:
    """根据主题背景亮度返回合适的图标颜色（对比度客观决定）。"""
    if theme is None:
        theme = ThemeManager().current_theme
    return '#1e293b' if is_light_theme(theme) else '#e2e8f0'


def get_contrast_text(bg_color: str) -> str:
    """根据背景色亮度返回黑或白文字色。"""
    lum = _luminance(bg_color)
    return '#000000' if lum > 0.5 else '#ffffff'


def _render_qss(t: dict) -> str:
    """将主题 dict 渲染为覆盖全控件的 QSS。"""
    t = _fill_theme_defaults(t)
    text_dim = t.get('text_dim', t.get('text', '#888888'))
    warning = t.get('warning', t.get('accent', '#fbbf24'))
    return f"""
    /* === OISystem 主题：{t.get('name', '')} === */
    QWidget {{ background: {t['bg']}; color: {t['text']}; }}
    QLabel {{ background: transparent; color: {t['text']}; }}
    QLabel#tip {{ color: {text_dim}; background: transparent; }}
    QLabel#section {{ color: {t['accent']}; background: transparent; font-weight: bold; }}

    /* === 按钮 === */
    QPushButton {{
        background: {t['surface']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 8px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ background: {t['accent_hover']}; border-color: {t['accent']}; color: white; }}
    QPushButton:pressed {{ background: {t['surface']}; }}
    QPushButton:disabled {{ background: {t['code_bg']}; color: {text_dim}; border-color: {t['border_light']}; }}
    QPushButton#primary {{ background: {t['accent']}; border-color: {t['accent']}; color: white; }}
    QPushButton#primary:hover {{ background: {t['accent_hover']}; border-color: {t['accent']}; }}
    QPushButton#primary:disabled {{ background: {t['code_bg']}; color: {text_dim}; border-color: {t['border_light']}; }}
    QPushButton#danger {{ background: {t['danger']}; border-color: {t['danger_hover']}; color: white; }}
    QPushButton#danger:hover {{ background: {t['danger_hover']}; border-color: red; }}
    QPushButton#LoadMore {{ color: {text_dim}; background: transparent; border: none; }}
    QPushButton#LoadMore:hover {{ color: {t['accent']}; }}

    /* === 输入框 === */
    QLineEdit, QTextEdit, QTextBrowser {{
        background: {t['input_bg']}; color: {t['text']};
        border: 1px solid {t['input_border']}; border-radius: 10px;
        padding: 10px;
        selection-background-color: {t['accent']};
    }}
    QLineEdit:focus, QTextEdit:focus, QTextBrowser:focus {{ border: 1px solid {t['input_focus']}; }}
    QLineEdit::placeholder {{ color: {text_dim}; }}

    /* === SpinBox === */
    QSpinBox {{
        background: {t['input_bg']}; color: {t['text']};
        border: 1px solid {t['input_border']}; border-radius: 6px; padding: 4px 6px;
        selection-background-color: {t['accent']};
    }}
    QSpinBox:focus {{ border: 1px solid {t['input_focus']}; }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background: {t['surface']}; border: none; width: 16px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {t['accent_hover']}; }}
    QSpinBox::up-arrow, QSpinBox::down-arrow {{ width: 8px; height: 8px; }}

    /* === 输入卡片 === */
    QFrame#InputCard {{
        background: {t['surface']}; border: 1px solid {t['border_light']};
        border-radius: 14px;
    }}

    /* === 滚动区域 === */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['scrollbar_handle']}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['scrollbar_hover']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t['scrollbar_handle']}; border-radius: 3px; min-width: 30px; }}
    QScrollBar::handle:horizontal:hover {{ background: {t['scrollbar_hover']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    /* === 下拉框 === */
    QComboBox {{
        background: {t['surface']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 6px;
        padding: 4px 8px; min-height: 20px;
    }}
    QComboBox:hover {{ border-color: {t['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox::drop-down:hover {{ background: {t['accent_hover']}; border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
    QComboBox QAbstractItemView {{
        background: {t['surface']}; color: {t['text']};
        border: 1px solid {t['border']}; selection-background-color: {t['accent']}; selection-color: white;
        outline: 0px;
    }}

    /* === 进度条 === */
    QProgressBar {{
        border: none; border-radius: 2px;
        background: {t['code_bg']}; text-align: center; height: 3px;
        color: {text_dim};
    }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 1px; }}

    /* === 复选框 === */
    QCheckBox {{ color: {t['text']}; spacing: 6px; background: transparent; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {t['border']}; border-radius: 3px;
        background: {t['input_bg']};
    }}
    QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
    QCheckBox::indicator:checked {{
        background: {t['accent']}; border-color: {t['accent']};
    }}

    /* === GroupBox === */
    QGroupBox {{
        border: 1px solid {t['border']}; border-radius: 8px;
        margin-top: 12px; padding-top: 10px;
        background: {t['surface']}; font-weight: bold;
    }}
    QGroupBox::title {{
        color: {warning}; subcontrol-origin: margin;
        left: 10px; padding: 0 4px; background: {t['bg']};
    }}

    /* === Tab === */
    QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: 8px; top: -1px; background: {t['bg']}; }}
    QTabBar::tab {{
        background: transparent; color: {text_dim}; padding: 8px 16px;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
    }}
    QTabBar::tab:hover {{ color: {t['text']}; }}
    QTabBar::tab:selected {{ background: {t['surface']}; color: {t['accent']}; }}

    /* === 列表 === */
    QListWidget {{
        background: {t['input_bg']}; color: {t['text']};
        border: 1px solid {t['input_border']}; border-radius: 6px; padding: 4px;
        outline: 0px;
    }}
    QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}
    QListWidget::item:hover {{ background: {t['accent_hover']}; }}
    QListWidget::item:selected {{ background: {t['accent']}; color: white; }}

    /* === 菜单/Tooltip === */
    QToolTip {{
        background: {t['surface']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 4px; padding: 4px;
    }}
    QMenu {{
        background: {t['surface']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 6px; padding: 4px;
    }}
    QMenu::item {{ padding: 4px 16px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t['accent']}; color: white; }}
    QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 8px; }}

    /* === 对话气泡帧 === */
    QFrame#UserBubble {{ background: {t['bubble_user_bg']}; border: none; border-radius: 12px; }}
    QFrame#AiBubble {{ background: {t['bubble_ai_bg']}; border: none; border-radius: 12px; }}
    QFrame#CodeBlock {{ background: {t['code_bg']}; border: 1px solid {t['border_light']}; border-radius: 6px; }}
    """


def init_themes():
    """启动时导出内置主题到文件。"""
    os.makedirs(THEMES_DIR, exist_ok=True)
    for key, data in _BUILTIN.items():
        path = os.path.join(THEMES_DIR, f"{key}.json")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
