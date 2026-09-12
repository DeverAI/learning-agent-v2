"""OISystem 设置中心窗口。

- 所有 Tab 只修改内存 pending 数据，不立即写盘。
- 窗口底部提供「保存所有更改」一键持久化。
- 主题 Tab 支持内置四主题、取色器（点击选中目标→调色盘直接应用）、实时预览（AI 完整界面模拟）、JSON 导入导出。
- 设置窗口整窗使用 ThemeManager.get_css() 跟随主题变化。
"""
import json
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTabWidget,
    QFormLayout, QLineEdit, QSpinBox, QComboBox, QTextEdit, QCheckBox,
    QListWidget, QMessageBox, QFileDialog, QGroupBox, QScrollArea,
    QColorDialog, QFrame, QSizePolicy, QGridLayout, QTextBrowser,
    QProgressBar, QMenu,
)
from PySide6.QtCore import Qt, Signal, QSize, QByteArray
from PySide6.QtGui import QColor, QFont, QPainter, QMouseEvent, QAction, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ui.frame_mixin import RoundedFrameMixin
from ui.icons import SVG_USER
from ui.themes import ThemeManager, _BUILTIN, _render_qss
from config.settings import ConfigManager
from utils.helpers import logger, format_time, now_cst


# ---------- 通用颜色按钮（显示+取色+选中目标+右键重置） ----------
class ColorButton(QPushButton):
    """显示当前颜色，点击弹出 QColorDialog。
    选中时高亮边框，作为调色盘的目标字段。
    右键菜单：重置为默认值 / 复制颜色值。
    """

    color_changed = Signal(str)
    target_selected = Signal(object)

    def __init__(self, color: str, key: str = "", parent=None):
        super().__init__(parent)
        self._color = color
        self._key = key
        self._default = color
        self._selected = False
        self.setMinimumSize(40, 22)
        self.setMaximumSize(80, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(self._style())
        self.clicked.connect(self._pick)

    def _style(self) -> str:
        border_color = "#fbbf24" if self._selected else "#64748b"
        border_w = "2px" if self._selected else "1px"
        text_color = self._contrast_text(self._color)
        w = self.width()
        # 窗口缩小时隐藏颜色按钮文字
        show_text = w >= 60
        return f"""
            QPushButton {{
                background: {self._color};
                border: {border_w} solid {border_color};
                border-radius: 4px;
                text-align: left; padding-left: 4px;
                color: {text_color if show_text else 'transparent'}; font-size: 8pt;
            }}
            QPushButton:hover {{ border: 2px solid #ffffff; }}
        """

    @staticmethod
    def _contrast_text(hex_color: str) -> str:
        """根据背景色亮度返回黑或白文字。"""
        try:
            c = QColor(hex_color)
            if not c.isValid():
                return "#ffffff"
            luminance = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())
            return "#000000" if luminance > 160 else "#ffffff"
        except Exception:
            return "#ffffff"

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "选择颜色")
        if c.isValid():
            self._color = c.name()
            self.setStyleSheet(self._style())
            self.color_changed.emit(self._color)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_reset = menu.addAction("重置为默认值")
        act_copy = menu.addAction("复制颜色值")
        chosen = menu.exec_(event.globalPos())
        if chosen == act_reset:
            self._color = self._default
            self.setStyleSheet(self._style())
            self.color_changed.emit(self._color)
        elif chosen == act_copy:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self._color)

    @property
    def color(self) -> str:
        return self._color

    @property
    def key(self) -> str:
        return self._key

    def set_color(self, color: str):
        self._color = color
        self.setStyleSheet(self._style())

    def set_selected(self, selected: bool):
        self._selected = selected
        self.setStyleSheet(self._style())

    def is_selected(self) -> bool:
        return self._selected


# ---------- 调色盘（常用色块） ----------
class PaletteBar(QWidget):
    """一排常用颜色方块，点击后设置当前编辑颜色。"""

    color_picked = Signal(str)

    COLORS = [
        "#000000", "#1e293b", "#0f172a", "#0a1628", "#132444",
        "#ffffff", "#f8fafc", "#f1f5f9", "#faf8f5", "#f5f0e8",
        "#3b82f6", "#2563eb", "#1d4ed8", "#60a5fa", "#93c5fd",
        "#ef4444", "#dc2626", "#b91c1c", "#991b1b", "#f87171",
        "#22c55e", "#16a34a", "#15803d", "#fbbf24", "#d97706",
        "#8b6914", "#6b5010", "#a39680", "#64748b", "#94a3b8",
        "#e2e8f0", "#cbd5e1", "#334155", "#475569", "#1e3a5f",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(0, 0, 0, 0)
        for c in self.COLORS:
            btn = QPushButton()
            btn.setFixedSize(18, 18)
            btn.setStyleSheet(f"QPushButton {{ background: {c}; border: 1px solid #64748b; border-radius: 3px; }} QPushButton:hover {{ border: 1px solid #ffffff; }}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, col=c: self.color_picked.emit(col))
            layout.addWidget(btn)
        layout.addStretch(1)


# ---------- 主题预览面板 ----------
class ThemePreview(QFrame):
    """完整界面模拟，应用当前编辑主题。
    左侧：侧边栏按钮模拟（含选中态高光条）。
    右侧：AI 对话框模拟（用户气泡 + AI 气泡含代码块 + 输入区 + 滚动条）。
    底部：进度条 + 复选框 + 下拉框。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(280)
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.title = QLabel("预览区域 — 实时跟随主题")
        self.title.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        main_layout.addWidget(self.title)

        self.tip = QLabel("这是辅助文本 / 时间戳")
        self.tip.setObjectName("tip")
        main_layout.addWidget(self.tip)

        # 左右分栏
        split = QHBoxLayout()
        split.setSpacing(8)

        # --- 左侧：侧边栏按钮模拟 ---
        sidebar_frame = QFrame()
        sidebar_frame.setObjectName("SidebarPreview")
        sidebar_layout = QVBoxLayout(sidebar_frame)
        sidebar_layout.setSpacing(4)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_label = QLabel("侧边栏")
        sidebar_label.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        sidebar_layout.addWidget(sidebar_label)
        self._sidebar_btns = []
        for i, name in enumerate(["专注", "对话", "设置", "日志", "退出"]):
            btn = QPushButton(name)
            btn.setFixedHeight(28)
            if i == 1:
                btn.setObjectName("SidebarActive")
            btn.clicked.connect(lambda _=False, idx=i: self._select_sidebar(idx))
            self._sidebar_btns.append(btn)
            sidebar_layout.addWidget(btn)
        sidebar_layout.addStretch(1)
        split.addWidget(sidebar_frame, 0)

        # --- 右侧：AI 对话模拟 ---
        chat_frame = QFrame()
        chat_frame.setObjectName("ChatPreview")
        chat_layout = QVBoxLayout(chat_frame)
        chat_layout.setSpacing(6)
        chat_layout.setContentsMargins(4, 4, 4, 4)

        chat_label = QLabel("AI 对话")
        chat_label.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
        chat_layout.addWidget(chat_label)

        # 用户气泡（右对齐）
        user_row = QHBoxLayout()
        user_row.addStretch(1)
        user_bubble = QFrame()
        user_bubble.setObjectName("UserBubble")
        ub_layout = QVBoxLayout(user_bubble)
        ub_layout.setContentsMargins(10, 6, 10, 6)
        ub_lbl = QLabel("这题为什么 TLE？")
        ub_lbl.setStyleSheet("background: transparent;")
        ub_layout.addWidget(ub_lbl)
        user_row.addWidget(user_bubble, 0)
        chat_layout.addLayout(user_row)

        # AI 气泡
        ai_bubble = QFrame()
        ai_bubble.setObjectName("AiBubble")
        ab_layout = QVBoxLayout(ai_bubble)
        ab_layout.setContentsMargins(10, 6, 10, 6)
        ab_layout.setSpacing(4)
        ab_lbl = QLabel("检查循环边界，数组大小是否足够？")
        ab_lbl.setWordWrap(True)
        ab_lbl.setStyleSheet("background: transparent;")
        ab_layout.addWidget(ab_lbl)
        # 代码块
        code_block = QFrame()
        code_block.setObjectName("CodeBlock")
        cb_layout = QVBoxLayout(code_block)
        cb_layout.setContentsMargins(8, 4, 8, 4)
        code_lbl = QLabel("for (int i=0; i<=n; i++)")
        code_lbl.setStyleSheet("background: transparent; font-family: Consolas, monospace; font-size: 9pt;")
        cb_layout.addWidget(code_lbl)
        ab_layout.addWidget(code_block)
        chat_layout.addWidget(ai_bubble)

        chat_layout.addStretch(1)

        # 输入区
        input_row = QHBoxLayout()
        self._preview_input = QLineEdit("输入消息...")
        input_row.addWidget(self._preview_input, 1)
        send_btn = QPushButton("发送")
        send_btn.setObjectName("primary")
        input_row.addWidget(send_btn)
        chat_layout.addLayout(input_row)

        split.addWidget(chat_frame, 1)
        main_layout.addLayout(split, 1)

        # --- 底部：进度条 + 复选框 + 下拉框 ---
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("进度:"), 0)
        self._progress = QProgressBar()
        self._progress.setValue(65)
        bottom.addWidget(self._progress, 1)
        self._checkbox = QCheckBox("自动分析")
        bottom.addWidget(self._checkbox)
        self._combo = QComboBox()
        self._combo.addItems(["GLM-4.6V", "KIMI", "DeepSeek"])
        bottom.addWidget(self._combo)
        main_layout.addLayout(bottom)

    def _select_sidebar(self, idx: int):
        for i, btn in enumerate(self._sidebar_btns):
            if i == idx:
                btn.setObjectName("SidebarActive")
            else:
                btn.setObjectName("")
            btn.style().unpolish(btn)
            btn.style().repolish(btn)

    def apply_theme(self, theme: dict):
        css = _render_qss(theme)
        # 追加预览专用样式
        accent = theme.get('accent', '#3b82f6')
        accent_hover = theme.get('accent_hover', '#2563eb')
        css += f"""
            QFrame#SidebarPreview {{
                background: {theme['surface']};
                border: 1px solid {theme['border']};
                border-radius: 6px;
                max-width: 100px;
            }}
            QFrame#ChatPreview {{
                background: {theme['bg']};
                border: 1px solid {theme['border']};
                border-radius: 6px;
            }}
            QPushButton#SidebarActive {{
                background: {accent_hover};
                color: white;
                border: 1px solid {accent};
                border-radius: 4px;
                text-align: left;
                padding-left: 8px;
            }}
            QFrame#UserBubble {{
                background: {theme['bubble_user_bg']};
                border: none; border-radius: 10px;
            }}
            QFrame#AiBubble {{
                background: {theme['bubble_ai_bg']};
                border: none; border-radius: 10px;
            }}
            QFrame#CodeBlock {{
                background: {theme['code_bg']};
                border: 1px solid {theme['border_light']};
                border-radius: 4px;
            }}
        """
        self.setStyleSheet(css)
        # 让气泡内 label 透明
        for child in self.findChildren(QLabel):
            if child.objectName() != "tip":
                child.setStyleSheet("background: transparent;")


# ---------- 主题设置 Tab ----------
class ThemeTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self.tm = ThemeManager()
        self._theme = self.tm.current_theme
        self._key = self.tm.current_key
        self._color_btns = {}
        self._current_target = None
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        # 内置主题
        builtin_group = QGroupBox("内置主题")
        builtin_layout = QHBoxLayout(builtin_group)
        self._builtin_btns = {}
        for key, name in self.tm.list_builtin().items():
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._load_builtin(k))
            builtin_layout.addWidget(btn)
            self._builtin_btns[key] = btn
        builtin_layout.addStretch(1)
        layout.addWidget(builtin_group)

        # 导入导出 + 重置
        io_row = QHBoxLayout()
        import_btn = QPushButton("导入主题 JSON")
        import_btn.setObjectName("primary")
        import_btn.clicked.connect(self._import_json)
        io_row.addWidget(import_btn)
        export_btn = QPushButton("导出当前主题 JSON")
        export_btn.clicked.connect(self._export_json)
        io_row.addWidget(export_btn)
        reset_btn = QPushButton("重置为内置默认")
        reset_btn.clicked.connect(self._reset_to_default)
        io_row.addWidget(reset_btn)
        io_row.addStretch(1)
        layout.addLayout(io_row)

        # 颜色编辑器 + 预览（整体可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setSpacing(8)
        editor_layout.setContentsMargins(2, 2, 2, 2)

        groups = [
            ("基础", ["bg", "surface", "text", "text_dim"]),
            ("强调", ["accent", "accent_hover"]),
            ("边框", ["border", "border_light"]),
            ("对话气泡", ["bubble_user_bg", "bubble_ai_bg"]),
            ("输入与代码", ["input_bg", "input_border", "input_focus", "code_bg"]),
            ("滚动条", ["scrollbar_handle", "scrollbar_hover"]),
            ("状态色", ["danger", "danger_hover", "success", "warning"]),
        ]

        names = {
            "bg": "背景", "surface": "面板", "text": "主文字", "text_dim": "次要文字",
            "accent": "强调色", "accent_hover": "强调悬停",
            "border": "边框", "border_light": "浅边框",
            "bubble_user_bg": "用户气泡", "bubble_ai_bg": "AI 气泡",
            "input_bg": "输入背景", "input_border": "输入边框", "input_focus": "输入焦点", "code_bg": "代码背景",
            "scrollbar_handle": "滚动条", "scrollbar_hover": "滚动条悬停",
            "danger": "危险", "danger_hover": "危险悬停", "success": "成功", "warning": "警告",
        }

        defaults = self._theme
        for title, keys in groups:
            group = QGroupBox(title)
            grid = QGridLayout(group)
            grid.setSpacing(4)
            grid.setContentsMargins(8, 12, 8, 8)
            for i, key in enumerate(keys):
                label = QLabel(names.get(key, key))
                label.setWordWrap(False)
                label.setMinimumWidth(30)
                # 文字过长时省略号显示
                label.setTextInteractionFlags(Qt.NoTextInteraction)
                label.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Fixed)
                grid.addWidget(label, i, 0)
                default_val = defaults.get(key, "#000000")
                btn = ColorButton(default_val, key=key)
                btn.color_changed.connect(lambda col, k=key: self._on_color_changed(k, col))
                btn.clicked.connect(lambda _=False, b=btn: self._select_target(b))
                grid.addWidget(btn, i, 1)
                self._color_btns[key] = btn
            # 列拉伸比例：标签紧凑，颜色框自适应
            grid.setColumnStretch(0, 0)
            grid.setColumnStretch(1, 1)
            editor_layout.addWidget(group)

        # 调色盘
        palette_group = QGroupBox("调色盘（先点上方颜色块选目标，再点下方色块应用）")
        palette_layout = QVBoxLayout(palette_group)
        self._target_label = QLabel("当前目标: 未选择")
        self._target_label.setStyleSheet("color: #fbbf24; font-size: 9pt;")
        palette_layout.addWidget(self._target_label)
        self._palette = PaletteBar()
        self._palette.color_picked.connect(self._on_palette_color)
        palette_layout.addWidget(self._palette)
        editor_layout.addWidget(palette_group)

        # 实时预览也放入滚动区域，避免页面过高
        preview_group = QGroupBox("实时预览")
        preview_layout = QVBoxLayout(preview_group)
        self.preview = ThemePreview()
        self.preview.setFixedHeight(260)
        preview_layout.addWidget(self.preview)
        editor_layout.addWidget(preview_group)

        editor_layout.addStretch(1)
        scroll.setWidget(editor)
        layout.addWidget(scroll, 1)

    def _select_target(self, btn: ColorButton):
        """选中某个 ColorButton 作为调色盘目标。"""
        self._current_target = btn
        for b in self._color_btns.values():
            b.set_selected(b is btn)
        self._target_label.setText(f"当前目标: {btn.key}")

    def _on_color_changed(self, key: str, color: str):
        self._theme[key] = color
        self._key = "custom"
        self._update_builtin_checks()
        self.preview.apply_theme(self._theme)

    def _on_palette_color(self, color: str):
        if self._current_target is not None:
            key = self._current_target.key
            self._current_target.set_color(color)
            self._theme[key] = color
            self._key = "custom"
            self._update_builtin_checks()
            self.preview.apply_theme(self._theme)

    def _load_builtin(self, key: str):
        self._theme = _BUILTIN[key].copy()
        self._key = key
        self._load_color_buttons()
        self._update_builtin_checks()
        self.preview.apply_theme(self._theme)

    def _reset_to_default(self):
        """重置当前主题为内置默认（black）。"""
        self._load_builtin("black")

    def _update_builtin_checks(self):
        for k, btn in self._builtin_btns.items():
            btn.setChecked(k == self._key)

    def _load_color_buttons(self):
        for key, btn in self._color_btns.items():
            btn.set_color(self._theme.get(key, "#000000"))

    def _load(self):
        self._load_color_buttons()
        self._update_builtin_checks()
        self.preview.apply_theme(self._theme)

    def _import_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入主题", "", "JSON (*.json)")
        if not path:
            return
        try:
            data = self.tm.load_custom(path)
            self._theme = data
            self._key = "custom"
            self._load_color_buttons()
            self._update_builtin_checks()
            self.preview.apply_theme(self._theme)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))

    def _export_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出主题", "theme.json", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._theme, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def collect(self) -> dict:
        return {"theme": self._key, "theme_custom_data": self._theme if self._key == "custom" else {}}


# ---------- 屏幕检测设置 ----------
class ScreenTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        container_layout.addLayout(form)
        container_layout.addStretch(1)

        self.interval = QSpinBox()
        self.interval.setRange(30, 3600)
        self.interval.setSuffix(" 秒")
        form.addRow("截图分析频率", self.interval)

        self.region = QComboBox()
        self.region.addItems(["fullscreen", "active_window", "custom"])
        form.addRow("截图区域", self.region)

        self.rect = QLineEdit()
        self.rect.setPlaceholderText("x,y,w,h（custom 模式）")
        form.addRow("自定义矩形", self.rect)

        self.engine = QLineEdit()
        form.addRow("视觉引擎", self.engine)

        self.quality = QComboBox()
        self.quality.addItems(["low", "medium", "high"])
        form.addRow("发送清晰度", self.quality)

        self.max_width = QSpinBox()
        self.max_width.setRange(0, 4096)
        form.addRow("最大宽度(0=不限)", self.max_width)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    def _load(self):
        s = self.cfg.settings
        self.interval.setValue(s.screen_capture_interval_sec)
        self.region.setCurrentText(s.screen_capture_region)
        self.rect.setText(",".join(str(x) for x in s.screen_custom_rect))
        self.engine.setText(s.screen_engine)
        self.quality.setCurrentText(s.screen_quality)
        self.max_width.setValue(s.screen_max_width)
        self.rect.setStyleSheet("")

    def collect(self) -> dict:
        rect_text = self.rect.text().strip()
        try:
            rect = [int(x.strip()) for x in rect_text.split(",")] if rect_text else [0, 0, 1920, 1080]
            if len(rect) != 4:
                raise ValueError("自定义矩形需要 4 个整数")
        except ValueError:
            self.rect.setStyleSheet("border: 1px solid #dc2626;")
            raise ValueError("自定义矩形格式错误")
        self.rect.setStyleSheet("")
        return dict(
            screen_capture_interval_sec=self.interval.value(),
            screen_capture_region=self.region.currentText(),
            screen_custom_rect=rect,
            screen_engine=self.engine.text().strip(),
            screen_quality=self.quality.currentText(),
            screen_max_width=self.max_width.value(),
        )


# ---------- AI 对话设置 ----------
class AITab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        container_layout.addLayout(form)
        container_layout.addStretch(1)

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("API Base URL", self.base_url)

        self.model = QLineEdit()
        form.addRow("对话模型", self.model)

        self.max_rounds = QSpinBox()
        self.max_rounds.setRange(1, (self.base_url.text() for _ in "._"))

        self.max_ctx = QSpinBox()
        self.max_ctx.setRange(1, 100)
        form.addRow("上下文块上限", self.max_ctx)

        self.flash_model = QLineEdit()
        form.addRow("Flash 模型(裁剪/闲聊判定)", self.flash_model)

        self.supervisor_interval = QSpinBox()
        self.supervisor_interval.setRange(1, 50)
        form.addRow("元监督间隔(轮)", self.supervisor_interval)

        self.unlimited_min = QSpinBox()
        self.unlimited_min.setRange(1, 120)
        form.addRow("非专注对话上限(分钟)", self.unlimited_min)

        self.kill_delay = QSpinBox()
        self.kill_delay.setRange(5, 300)
        form.addRow("提醒后掐断延迟(秒)", self.kill_delay)

        self.extra_rules = QTextEdit()
        self.extra_rules.setPlaceholderText("在默认 prompt 基础上追加的限制...")
        form.addRow("追加规则", self.extra_rules)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    def _load(self):
        s = self.cfg.settings
        self.model.setText(s.ai_dialog_model)
        self.max_rounds.setValue(s.ai_dialog_max_rounds)
        self.max_ctx.setValue(s.ai_dialog_max_context_blocks)
        self.flash_model.setText(s.ai_flash_model)
        self.supervisor_interval.setValue(s.ai_supervisor_interval_rounds)
        self.unlimited_min.setValue(s.ai_unlimited_chat_max_minutes)
        self.kill_delay.setValue(s.ai_unlimited_chat_kill_delay_sec)
        self.extra_rules.setPlainText(s.ai_dialog_extra_rules)

    def collect(self) -> dict:
        return dict(
            ai_dialog_model=self.model.text().strip(),
            ai_dialog_max_rounds=self.max_rounds.value(),
            ai_dialog_max_context_blocks=self.max_ctx.value(),
            ai_flash_model=self.flash_model.text().strip(),
            ai_supervisor_interval_rounds=self.supervisor_interval.value(),
            ai_unlimited_chat_max_minutes=self.unlimited_min.value(),
            ai_unlimited_chat_kill_delay_sec=self.kill_delay.value(),
            ai_dialog_extra_rules=self.extra_rules.toPlainText().strip(),
        )


# ---------- 专注模式设置 ----------
class FocusTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        container_layout.addLayout(form)
        container_layout.addStretch(1)

        self.duration = QSpinBox()
        self.duration.setRange(5, 480)
        self.duration.setSuffix(" 分钟")
        form.addRow("默认时长", self.duration)

        self.emergency_allowed = QCheckBox("允许急事退出")
        form.addRow(self.emergency_allowed)

        self.analyze_interval = QSpinBox()
        self.analyze_interval.setRange(30, 3600)
        self.analyze_interval.setSuffix(" 秒")
        form.addRow("自动分析间隔", self.analyze_interval)

        self.stuck_threshold = QSpinBox()
        self.stuck_threshold.setRange(1, 20)
        form.addRow("卡住触发阈值", self.stuck_threshold)

        self.lock_no_submit = QCheckBox("当日零提交自动锁定")
        form.addRow(self.lock_no_submit)

        self.lock_rank_tail = QCheckBox("排行榜末尾自动锁定")
        form.addRow(self.lock_rank_tail)

        self.mute_hotkey = QLineEdit()
        form.addRow("静音模式快捷键", self.mute_hotkey)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    def _load(self):
        s = self.cfg.settings
        self.duration.setValue(s.focus_duration_minutes)
        self.emergency_allowed.setChecked(s.focus_emergency_exit_allowed)
        self.analyze_interval.setValue(s.focus_auto_analyze_interval_sec)
        self.stuck_threshold.setValue(s.focus_stuck_threshold)
        self.lock_no_submit.setChecked(s.focus_lock_on_no_submission)
        self.lock_rank_tail.setChecked(s.focus_lock_on_rank_tail)
        self.mute_hotkey.setText(s.mute_hotkey)

    def collect(self) -> dict:
        return dict(
            focus_duration_minutes=self.duration.value(),
            focus_emergency_exit_allowed=self.emergency_allowed.isChecked(),
            focus_auto_analyze_interval_sec=self.analyze_interval.value(),
            focus_stuck_threshold=self.stuck_threshold.value(),
            focus_lock_on_no_submission=self.lock_no_submit.isChecked(),
            focus_lock_on_rank_tail=self.lock_rank_tail.isChecked(),
            mute_hotkey=self.mute_hotkey.text().strip(),
        )


# ---------- 网站黑白名单设置 ----------
class SiteTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        wl_group = QGroupBox("白名单（专注模式允许）")
        wl_layout = QVBoxLayout(wl_group)
        self.whitelist = QListWidget()
        wl_layout.addWidget(self.whitelist)
        wl_row = QHBoxLayout()
        self.wl_input = QLineEdit()
        self.wl_input.setPlaceholderText("域名，如 luogu.com")
        wl_row.addWidget(self.wl_input)
        wl_add = QPushButton("添加")
        wl_add.clicked.connect(self._wl_add)
        wl_row.addWidget(wl_add)
        wl_del = QPushButton("删除选中")
        wl_del.clicked.connect(self._wl_del)
        wl_row.addWidget(wl_del)
        wl_layout.addLayout(wl_row)
        layout.addWidget(wl_group)

        bl_group = QGroupBox("黑名单（始终禁止）")
        bl_layout = QVBoxLayout(bl_group)
        self.blacklist = QListWidget()
        bl_layout.addWidget(self.blacklist)
        bl_row = QHBoxLayout()
        self.bl_input = QLineEdit()
        bl_row.addWidget(self.bl_input)
        bl_add = QPushButton("添加")
        bl_add.clicked.connect(self._bl_add)
        bl_row.addWidget(bl_add)
        bl_del = QPushButton("删除选中")
        bl_del.clicked.connect(self._bl_del)
        bl_row.addWidget(bl_del)
        bl_layout.addLayout(bl_row)
        layout.addWidget(bl_group)

        layout.addStretch(1)

    def _load(self):
        s = self.cfg.settings
        self.whitelist.clear()
        self.whitelist.addItems(s.site_whitelist)
        self.blacklist.clear()
        self.blacklist.addItems(s.site_blacklist)

    def _wl_add(self):
        t = self.wl_input.text().strip()
        if t:
            self.whitelist.addItem(t)
            self.wl_input.clear()

    def _wl_del(self):
        for item in self.whitelist.selectedItems():
            self.whitelist.takeItem(self.whitelist.row(item))

    def _bl_add(self):
        t = self.bl_input.text().strip()
        if t:
            self.blacklist.addItem(t)
            self.bl_input.clear()

    def _bl_del(self):
        for item in self.blacklist.selectedItems():
            self.blacklist.takeItem(self.blacklist.row(item))

    def collect(self) -> dict:
        wl = [self.whitelist.item(i).text() for i in range(self.whitelist.count())]
        bl = [self.blacklist.item(i).text() for i in range(self.blacklist.count())]
        return dict(site_whitelist=wl, site_blacklist=bl)


# ---------- API 设置 ----------
class APITab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(8, 8, 8, 8)

        kimi_group = QGroupBox("KIMI（默认知识检索）")
        kf = QFormLayout(kimi_group)
        self.kimi_enabled = QCheckBox("启用")
        kf.addRow(self.kimi_enabled)
        self.kimi_base = QLineEdit()
        kf.addRow("Base URL", self.kimi_base)
        self.kimi_model = QLineEdit()
        kf.addRow("模型", self.kimi_model)
        self.kimi_key = QLineEdit()
        self.kimi_key.setEchoMode(QLineEdit.Password)
        kf.addRow("API Key", self.kimi_key)
        layout.addWidget(kimi_group)

        glm_group = QGroupBox("GLM（默认 4.6V 视觉决策）")
        gf = QFormLayout(glm_group)
        self.glm_enabled = QCheckBox("启用")
        gf.addRow(self.glm_enabled)
        self.glm_base = QLineEdit()
        gf.addRow("Base URL", self.glm_base)
        self.glm_model = QLineEdit()
        gf.addRow("模型", self.glm_model)
        self.glm_key = QLineEdit()
        self.glm_key.setEchoMode(QLineEdit.Password)
        gf.addRow("API Key", self.glm_key)
        layout.addWidget(glm_group)

        ds_group = QGroupBox("DEEPSEEK（默认对话主力）")
        df = QFormLayout(ds_group)
        self.ds_enabled = QCheckBox("启用")
        df.addRow(self.ds_enabled)
        self.ds_base = QLineEdit()
        df.addRow("Base URL", self.ds_base)
        self.ds_model = QLineEdit()
        df.addRow("模型", self.ds_model)
        self.ds_key = QLineEdit()
        self.ds_key.setEchoMode(QLineEdit.Password)
        df.addRow("API Key", self.ds_key)
        layout.addWidget(ds_group)

        mail_group = QGroupBox("邮件（日志发送）")
        mf = QFormLayout(mail_group)
        self.mail_smtp = QLineEdit()
        mf.addRow("SMTP 服务器", self.mail_smtp)
        self.mail_port = QSpinBox()
        self.mail_port.setRange(1, 65535)
        mf.addRow("端口", self.mail_port)
        self.mail_sender = QLineEdit()
        mf.addRow("发件邮箱", self.mail_sender)
        self.mail_receivers = QLineEdit()
        self.mail_receivers.setPlaceholderText("多个用逗号分隔")
        mf.addRow("收件邮箱", self.mail_receivers)
        self.mail_pwd = QLineEdit()
        self.mail_pwd.setEchoMode(QLineEdit.Password)
        mf.addRow("SMTP 密码", self.mail_pwd)
        layout.addWidget(mail_group)

        jy_group = QGroupBox("极域快照")
        jf = QFormLayout(jy_group)
        self.jy_path = QLineEdit()
        jf.addRow("Snapshots 路径", self.jy_path)
        self.jy_send = QCheckBox("启用打包发送")
        jf.addRow(self.jy_send)
        layout.addWidget(jy_group)

        layout.addStretch(1)

    def _load(self):
        s = self.cfg.settings
        self.kimi_enabled.setChecked(s.kimi_enabled)
        self.kimi_base.setText(s.kimi_base_url)
        self.kimi_model.setText(s.kimi_model)
        self.kimi_key.setText(s.kimi_api_key)
        self.glm_enabled.setChecked(s.glm_enabled)
        self.glm_base.setText(s.glm_base_url)
        self.glm_model.setText(s.glm_model)
        self.glm_key.setText(s.glm_api_key)
        self.ds_enabled.setChecked(s.deepseek_enabled)
        self.ds_base.setText(s.deepseek_base_url)
        self.ds_model.setText(s.deepseek_model)
        self.ds_key.setText(s.deepseek_api_key)
        self.mail_smtp.setText(s.mail_smtp_server)
        self.mail_port.setValue(s.mail_smtp_port)
        self.mail_sender.setText(s.mail_sender)
        self.mail_receivers.setText(",".join(s.mail_receivers))
        self.mail_pwd.setText(s.mail_password)
        self.jy_path.setText(s.jiyu_snapshot_path)
        self.jy_send.setChecked(s.jiyu_snapshot_send)

    def collect(self) -> dict:
        receivers = [r.strip() for r in self.mail_receivers.text().split(",") if r.strip()]
        return dict(
            kimi_enabled=self.kimi_enabled.isChecked(),
            kimi_base_url=self.kimi_base.text().strip(),
            kimi_model=self.kimi_model.text().strip(),
            kimi_api_key=self.kimi_key.text().strip(),
            glm_enabled=self.glm_enabled.isChecked(),
            glm_base_url=self.glm_base.text().strip(),
            glm_model=self.glm_model.text().strip(),
            glm_api_key=self.glm_key.text().strip(),
            deepseek_enabled=self.ds_enabled.isChecked(),
            deepseek_base_url=self.ds_base.text().strip(),
            deepseek_model=self.ds_model.text().strip(),
            deepseek_api_key=self.ds_key.text().strip(),
            mail_smtp_server=self.mail_smtp.text().strip(),
            mail_smtp_port=self.mail_port.value(),
            mail_sender=self.mail_sender.text().strip(),
            mail_receivers=receivers,
            mail_password=self.mail_pwd.text().strip(),
            jiyu_snapshot_path=self.jy_path.text().strip(),
            jiyu_snapshot_send=self.jy_send.isChecked(),
        )


# ---------- ZZOI 设置 ----------
class ZzoiTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        container_layout.addLayout(form)
        container_layout.addStretch(1)

        self.uid = QLineEdit()
        form.addRow("用户 UID", self.uid)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("密码", self.password)

        self.sid = QLineEdit()
        form.addRow("SID（可选）", self.sid)

        self.sid_sig = QLineEdit()
        form.addRow("SID_SIG（可选）", self.sid_sig)

        self.base_url = QLineEdit()
        form.addRow("Base URL", self.base_url)

        self.domain_prefix = QLineEdit()
        form.addRow("域名前缀", self.domain_prefix)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(container)

    def _load(self):
        s = self.cfg.settings
        self.uid.setText(s.zzoi_uid)
        self.password.setText(s.zzoi_password)
        self.sid.setText(s.zzoi_sid)
        self.sid_sig.setText(s.zzoi_sid_sig)
        self.base_url.setText(s.zzoi_base_url)
        self.domain_prefix.setText(s.zzoi_domain_prefix)

    def collect(self) -> dict:
        return dict(
            zzoi_uid=self.uid.text().strip(),
            zzoi_password=self.password.text().strip(),
            zzoi_sid=self.sid.text().strip(),
            zzoi_sid_sig=self.sid_sig.text().strip(),
            zzoi_base_url=self.base_url.text().strip() or "https://zzoi.com.cn",
            zzoi_domain_prefix=self.domain_prefix.text().strip() or "d/ZZOI",
        )


# ---------- 用户画像设置 ----------
class ProfileTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = ConfigManager()
        self._setup_ui()
        self._load()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(8, 8, 8, 8)

        avatar_group = QGroupBox("默认头像（暂不可修改）")
        avatar_layout = QVBoxLayout(avatar_group)
        avatar_layout.setAlignment(Qt.AlignCenter)
        avatar_label = QLabel()
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        renderer = QSvgRenderer(QByteArray(SVG_USER.replace("currentColor", "#e2e8f0").encode("utf-8")))
        if renderer.isValid():
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing)
            renderer.render(painter)
            painter.end()
        avatar_label.setPixmap(pm)
        avatar_label.setFixedSize(72, 72)
        avatar_label.setStyleSheet("border: 2px solid #334155; border-radius: 36px; background: #111c30;")
        avatar_label.setAlignment(Qt.AlignCenter)
        avatar_layout.addWidget(avatar_label, 0, Qt.AlignCenter)
        avatar_layout.addWidget(QLabel("SVG_USER"), 0, Qt.AlignCenter)
        layout.addWidget(avatar_group)

        profile_group = QGroupBox("AI 提取的用户画像")
        profile_layout = QVBoxLayout(profile_group)

        info = QLabel(
            "以下内容由 AI 从对话和日志中自动提取，描述用户的学习风格、薄弱点、偏好等。"
        )
        info.setStyleSheet("color: #94a3b8; font-size: 9pt; padding: 4px 0;")
        info.setWordWrap(True)
        profile_layout.addWidget(info)

        self.profile_edit = QTextEdit()
        self.profile_edit.setPlaceholderText(
            "AI 将在此处生成用户画像…\n\n"
            "格式示例：\n"
            "- 用户年级: 高一\n"
            "- 擅长: 动态规划、图论\n"
            "- 薄弱: 数论、组合数学\n"
            "- 学习风格: 需要逐步引导，偏好代码示例\n"
            "- 常见错误: 数组越界、忘记取模\n"
            "- 近期目标: 提高 DP 题 AC 率"
        )
        self.profile_edit.setMinimumHeight(200)
        profile_layout.addWidget(self.profile_edit, 1)

        meta_row = QHBoxLayout()
        self.update_label = QLabel("上次更新: 未更新")
        self.update_label.setStyleSheet("color: #64748b; font-size: 9pt;")
        meta_row.addWidget(self.update_label)
        meta_row.addStretch(1)
        profile_layout.addLayout(meta_row)

        layout.addWidget(profile_group, 1)

        layout.addStretch(1)

    def _load(self):
        s = self.cfg.settings
        self.profile_edit.setPlainText(s.user_profile_text)
        if s.user_profile_updated_at:
            self.update_label.setText(f"上次更新: {s.user_profile_updated_at}")

    def collect(self) -> dict:
        text = self.profile_edit.toPlainText().strip()
        return dict(
            user_profile_text=text,
            user_profile_updated_at=format_time(now_cst()) if text else "",
        )


# ---------- 设置中心主窗口 ----------
class SettingsView(QWidget, RoundedFrameMixin):
    """设置中心主窗口：底部一键保存所有 Tab 的更改。整窗跟随主题。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("设置")
        self.setWindowTitle("OISystem - 设置")
        self.resize(600, 700)
        # 允许自由拖拽调整高度
        self.setMinimumSize(480, 400)
        self.setMaximumSize(900, 1200)
        self._setup_ui()
        self._apply_theme_style()

    def _apply_theme_style(self):
        """应用 ThemeManager 生成的 QSS 到整窗。"""
        css = ThemeManager().get_css()
        self.setStyleSheet(css)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 44, 14, 14)
        layout.setSpacing(10)

        title = QLabel("设置中心")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet("background: transparent;")
        layout.addWidget(title)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._tabs = {}
        self._add_tab(ThemeTab(), "主题风格")
        self._add_tab(ScreenTab(), "屏幕检测")
        self._add_tab(AITab(), "AI 对话")
        self._add_tab(FocusTab(), "专注模式")
        self._add_tab(ProfileTab(), "用户画像")
        self._add_tab(SiteTab(), "网站名单")
        self._add_tab(ZzoiTab(), "ZZOI")
        self._add_tab(APITab(), "API/邮件")
        layout.addWidget(self.tabs, 1)

        # 底部保存/取消/退出
        btn_row = QHBoxLayout()
        self.save_btn = QPushButton("保存所有更改")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save_all)
        btn_row.addWidget(self.save_btn)

        cancel_btn = QPushButton("恢复当前配置")
        cancel_btn.clicked.connect(self._reload_all)
        btn_row.addWidget(cancel_btn)

        btn_row.addStretch(1)

        exit_btn = QPushButton("正常退出 OISystem")
        exit_btn.setObjectName("danger")
        exit_btn.clicked.connect(self._exit_clicked)
        btn_row.addWidget(exit_btn)
        layout.addLayout(btn_row)

    def _add_tab(self, widget, label):
        self.tabs.addTab(widget, label)
        self._tabs[label] = widget

    def _save_all(self):
        """批量保存所有 Tab 设置，防重复点击。"""
        self.save_btn.setEnabled(False)
        try:
            merged = {}
            errors = []
            for label, tab in self._tabs.items():
                try:
                    merged.update(tab.collect())
                except Exception as e:
                    errors.append(f"{label}: {e}")
            if errors:
                QMessageBox.warning(self, "保存失败", "\n".join(errors))
                return
            try:
                ConfigManager().update(**merged)
                theme_key = merged.get("theme", "black")
                theme_data = merged.get("theme_custom_data", {})
                if theme_key == "custom" and theme_data:
                    ThemeManager().set_custom(theme_data)
                else:
                    ThemeManager().select(theme_key)
                # 重新应用主题样式
                self._apply_theme_style()
                QMessageBox.information(self, "已保存", "所有设置已保存并生效")
                self._reload_all()
            except Exception as e:
                logger.warning(f"保存设置失败: {e}")
                QMessageBox.warning(self, "保存失败", str(e))
        finally:
            self.save_btn.setEnabled(True)

    def _reload_all(self):
        for tab in self._tabs.values():
            tab._load()

    def _exit_clicked(self):
        # 立即禁用按钮防连点
        sender = self.sender()
        if sender is not None:
            sender.setEnabled(False)
        try:
            from core.exit_flow import request_exit
            request_exit()
        except Exception:
            pass
