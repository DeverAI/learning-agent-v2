"""OISystem AI 对话窗口。

- Markdown 渲染 + 源码/预览切换
- 虚拟滚动：只渲染最近 N 条消息，顶部"加载更多"
- 进度加载指示
- 主题跟随 ThemeManager（主题设置在设置中心统一管理）
"""
import os
import time
from typing import Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QLineEdit, QScrollArea, QFrame, QFileDialog, QMessageBox,
    QProgressBar, QTextBrowser, QSizePolicy, QStackedWidget,
    QPlainTextEdit, QApplication,
)
from PySide6.QtCore import Qt, QTimer, QSize, QDateTime, QEvent
from PySide6.QtGui import QFont, QPixmap, QCursor

from ui.frame_mixin import RoundedFrameMixin
from ui.buttons_svg import svg_pixmap
from ui.icons import (
    SVG_CARD_ADD, SVG_TAG,
    SVG_CLOUD_UPLOAD, SVG_BULB, SVG_EYE
)
from ui.md_renderer import render as render_md, BASE_CSS, _escape_html
from ui.themes import ThemeManager, get_icon_color
from utils.helpers import logger, log_event
from config.settings import ConfigManager

RENDER_CHUNK = 15
MAX_RENDERED = 30
SCROLL_THRESHOLD = 40


def _make_icon_button(svg_text: str, tooltip: str, parent=None, danger: bool = False) -> QPushButton:
    btn = QPushButton("", parent)
    icon_color = get_icon_color()
    btn.setIcon(QPixmap(svg_pixmap(svg_text, color=icon_color)))
    btn.setIconSize(QSize(18, 18))
    btn.setToolTip(tooltip)
    btn.setFixedSize(34, 34)
    btn.setCursor(QCursor(Qt.PointingHandCursor))
    btn.setStyleSheet("""
        QPushButton {
            background: rgba(128,128,128,0.08);
            border: 1px solid rgba(128,128,128,0.18);
            border-radius: 8px; padding: 4px;
        }
        QPushButton:hover { background: rgba(128,128,128,0.18); border-color: rgba(128,128,128,0.35); }
        QPushButton:pressed { background: rgba(128,128,128,0.06); }
    """)
    if danger:
        btn.setObjectName("danger")
    return btn


def _make_avatar(role: str, theme: dict) -> QLabel:
    """圆形角色头像：用户=accent 色，AI=warning 色。"""
    avatar = QLabel()
    avatar.setFixedSize(24, 24)
    avatar.setAlignment(Qt.AlignCenter)
    avatar.setFont(QFont("Microsoft YaHei", 8, QFont.Bold))
    if role == "user":
        bg = theme.get('accent', '#3b82f6')
        fg = "#ffffff"
        text = "我"
    else:
        bg = theme.get('warning', '#fbbf24')
        fg = "#1e293b"
        text = "AI"
    avatar.setText(text)
    avatar.setStyleSheet(
        f"QLabel {{ color: {fg}; background: {bg}; border-radius: 12px; }}"
    )
    return avatar


def _make_text_button(text: str, theme: dict, tooltip: str = "") -> QPushButton:
    """无边框小文字按钮（复制/源码切换等）。"""
    btn = QPushButton(text)
    btn.setFont(QFont("Microsoft YaHei", 7))
    btn.setCursor(QCursor(Qt.PointingHandCursor))
    btn.setStyleSheet(f"""
        QPushButton {{ background: transparent; color: {theme['text_dim']};
            border: none; padding: 2px 6px; }}
        QPushButton:hover {{ color: {theme['accent']}; }}
        QPushButton:checked {{ color: {theme['accent']}; }}
    """)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class MessageBubble(QFrame):
    """单条消息气泡。支持 Markdown 渲染 + 源码/预览切换。"""

    def __init__(self, role: str, text: str, block_id: str = "",
                 on_reference=None, on_blacklist=None, parent=None, timestamp=None,
                 on_speak=None):
        super().__init__(parent)
        self.setObjectName("MessageBubble")
        self.role = role
        self.text = text
        self.block_id = block_id
        self._on_reference = on_reference
        self._on_blacklist = on_blacklist
        self._on_speak = on_speak
        self._show_rendered = True
        self._has_mermaid = False
        self._has_graph = False
        self._timestamp = timestamp or QDateTime.currentDateTime()
        self._setup_ui()

    def _setup_ui(self):
        t = ThemeManager().current_theme
        if self.role == "user":
            self.setStyleSheet(f"""
                QFrame#MessageBubble {{ background: {t['bubble_user_bg']};
                    border: 1px solid {t['accent']}44; border-radius: 10px; }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame#MessageBubble {{ background: {t['bubble_ai_bg']};
                    border: 1px solid {t['border_light']}; border-radius: 10px; }}
            """)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(5)

        role_row = QHBoxLayout()
        role_row.setSpacing(6)
        self._avatar = _make_avatar(self.role, t)
        role_row.addWidget(self._avatar)
        self._name_label = QLabel("我" if self.role == "user" else "助手")
        self._name_label.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        self._name_label.setStyleSheet(
            f"color: {t['accent']}; background: transparent;" if self.role == "user"
            else f"color: {t.get('warning', '#fbbf24')}; background: transparent;"
        )
        role_row.addWidget(self._name_label)
        role_row.addStretch(1)

        # 时间戳
        self._time_label = QLabel(self._timestamp.toString("HH:mm"))
        self._time_label.setFont(QFont("Microsoft YaHei", 7))
        self._time_label.setStyleSheet(f"color: {t['text_dim']}; background: transparent;")
        role_row.addWidget(self._time_label)

        if self.role == "assistant":
            self._copy_btn = _make_text_button("复制", t, "复制纯文本")
            self._copy_btn.clicked.connect(self._copy_text)
            role_row.addWidget(self._copy_btn)

            # r53：讲题朗读（小米 MiMo-V2.5-TTS）
            if self._on_speak is not None:
                self._speak_btn = _make_text_button("朗读", t, "用小米 TTS 朗读本条回复")
                self._speak_btn.setCheckable(True)
                self._speak_btn.clicked.connect(self._toggle_speak)
                role_row.addWidget(self._speak_btn)

            self._toggle_btn = _make_text_button("源码", t, "切换源码/预览")
            self._toggle_btn.setCheckable(True)
            self._toggle_btn.clicked.connect(self._toggle_view)
            role_row.addWidget(self._toggle_btn)

        self._layout.addLayout(role_row)

        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self._source_view = QTextEdit()
        self._source_view.setReadOnly(True)
        self._source_view.setPlainText(self.text)
        self._source_view.setFont(QFont("Cascadia Code", 9))
        self._source_view.setStyleSheet(f"""
            QTextEdit {{ background: {t['code_bg']}; color: {t['text']};
                border: none; padding: 4px; }}
        """)
        self._source_view.setFixedHeight(self._calc_source_height())
        self._stack.addWidget(self._source_view)

        self._render_view = QTextBrowser()
        self._render_view.setOpenExternalLinks(True)
        self._render_view.setStyleSheet(f"""
            QTextBrowser {{ background: transparent; color: {t['text']};
                border: none; padding: 0; }}
        """)
        # r34 P0 修复：长消息需要可滚动，避免内容被截断
        self._render_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._render_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._render_html = ""
        self._render_view.setHtml("")
        self._stack.addWidget(self._render_view)
        self._stack.setCurrentIndex(1)
        self._layout.addWidget(self._stack)

        # 初始化时即完成首次渲染，避免气泡首次显示为空白
        self._ensure_rendered()

        if self.role == "assistant" and self.block_id and self._on_reference:
            ref_row = QHBoxLayout()
            self._ref_btn = QPushButton(f"#{self.block_id}")
            self._ref_btn.setFlat(True)
            self._ref_btn.setStyleSheet(f"""
                QPushButton {{ color: {t['text_dim']}; font-size: 8pt; padding: 2px 0;
                    background: transparent; border: none; text-align: left; }}
                QPushButton:hover {{ color: {t['accent']}; }}
            """)
            self._ref_btn.clicked.connect(lambda: self._on_reference(self.block_id))
            ref_row.addWidget(self._ref_btn)
            self._blacklist_btn = None
            if self._on_blacklist:
                self._blacklist_btn = _make_text_button("拉黑", t, "永久禁止主动调用此上下文块")
                self._blacklist_btn.clicked.connect(lambda: self._on_blacklist(self.block_id))
                ref_row.addWidget(self._blacklist_btn)
            ref_row.addStretch(1)
            self._layout.addLayout(ref_row)

    def _calc_source_height(self) -> int:
        lines = self.text.count("\n") + 1
        return min(400, max(40, lines * 18 + 12))

    def _ensure_rendered(self):
        """确保渲染视图已生成 HTML；未渲染时执行一次。"""
        if not self._render_html:
            try:
                self._render_html, self._has_mermaid, self._has_graph = render_md(
                    self.text, theme=ThemeManager().current_theme
                )
            except Exception as e:
                logger.warning(f"Markdown 渲染失败，降级为纯文本: {e}")
                self._render_html = _escape_html(self.text)
                self._has_mermaid = False
                self._has_graph = False
        t = ThemeManager().current_theme
        html = f"""<html><head>{BASE_CSS}
        <style>
          body {{ color: {t['text']}; background: transparent; }}
          a {{ color: {t['accent']}; }}
          code {{ background: {t['code_bg']}88; }}
          pre {{ background: {t['code_bg']}; }}
          blockquote {{ border-left-color: {t['accent']}66; }}
          th {{ background: {t['code_bg']}88; }}
          td, th {{ border-color: {t['border']}; }}
          h1 {{ border-bottom-color: {t['border']}; }}
          .mermaid-source {{ background: {t['accent']}18; border-left-color: {t['accent']}; }}
          .math {{ background: {t['code_bg']}88; color: {t['text']}; }}
          .math-block {{ background: {t['code_bg']}88; color: {t['text']}; }}
          .graph-container {{ background: rgba(128,128,128,0.06); }}
        </style></head><body>{self._render_html}</body></html>"""
        self._render_view.setHtml(html)

    def _toggle_view(self):
        self._show_rendered = not self._show_rendered
        self._toggle_btn.setText("预览" if not self._show_rendered else "源码")
        if self._show_rendered:
            self._ensure_rendered()
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(0)

    def _copy_text(self):
        """复制消息纯文本到剪贴板，并短暂提示"已复制"。"""
        try:
            QApplication.clipboard().setText(self.text)
            if hasattr(self, "_copy_btn"):
                self._copy_btn.setText("已复制")
                def _restore():
                    try:
                        self._copy_btn.setText("复制")
                    except RuntimeError:
                        pass
                QTimer.singleShot(1200, _restore)
        except Exception as e:
            logger.debug(f"复制失败: {e}")

    def _toggle_speak(self):
        """r53：朗读/停止本条 AI 回复。按钮状态由 TTSPlayer 状态回调驱动。"""
        if getattr(self, "_speak_btn", None) is None or self._on_speak is None:
            return
        try:
            if self._speak_btn.isChecked():
                self._speak_btn.setText("停止")
                self._on_speak("speak", self.text, self._bubble_speak_state)
            else:
                self._speak_btn.setText("朗读")
                self._on_speak("stop", "", None)
        except RuntimeError as e:
            logger.debug(f"朗读按钮回调失败: {e}")

    def _bubble_speak_state(self, state: str):
        """TTSPlayer.stateChanged 回调：播放结束/出错时恢复按钮文案。"""
        try:
            if getattr(self, "_speak_btn", None) is None:
                return
            if state in ("idle", "error"):
                self._speak_btn.setChecked(False)
                self._speak_btn.setText("朗读")
            elif state == "synthesizing":
                self._speak_btn.setChecked(True)
                self._speak_btn.setText("合成中")
            elif state == "playing":
                self._speak_btn.setChecked(True)
                self._speak_btn.setText("停止")
        except RuntimeError:
            pass

    def update_theme(self):
        t = ThemeManager().current_theme
        if self.role == "user":
            self.setStyleSheet(f"""
                QFrame#MessageBubble {{ background: {t['bubble_user_bg']};
                    border: 1px solid {t['accent']}44; border-radius: 10px; }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame#MessageBubble {{ background: {t['bubble_ai_bg']};
                    border: 1px solid {t['border_light']}; border-radius: 10px; }}
            """)
        # r38 P1 修复：主题切换时所有气泡都需刷新，不仅是 graph 气泡。
        # - graph 气泡：SVG 颜色硬编码在 _render_html 中，必须清空重解析
        # - 非 graph 气泡：_render_html（body）不含主题色，但 _ensure_rendered
        #   外层的 <style> 包含主题色，调用 _ensure_rendered 即可刷新包装层
        if self._has_graph:
            self._render_html = ""
        self._ensure_rendered()
        # 刷新源码视图背景色（主题切换后 code_bg 变化）
        self._source_view.setStyleSheet(f"""
            QTextEdit {{ background: {t['code_bg']}; color: {t['text']};
                border: none; padding: 4px; }}
        """)
        # 刷新渲染视图的 widget chrome（背景/边框）
        self._render_view.setStyleSheet(f"""
            QTextBrowser {{ background: transparent; color: {t['text']};
                border: none; padding: 0; }}
        """)
        # r42: 刷新角色行控件（头像/角色名/时间戳/按钮）样式
        try:
            if self.role == "user":
                bg = t.get('accent', '#3b82f6'); fg = "#ffffff"
            else:
                bg = t.get('warning', '#fbbf24'); fg = "#1e293b"
            self._avatar.setStyleSheet(
                f"QLabel {{ color: {fg}; background: {bg}; border-radius: 12px; }}"
            )
            self._name_label.setStyleSheet(
                f"color: {t['accent']}; background: transparent;" if self.role == "user"
                else f"color: {t.get('warning', '#fbbf24')}; background: transparent;"
            )
            self._time_label.setStyleSheet(f"color: {t['text_dim']}; background: transparent;")
            if self.role == "assistant":
                btn_css = (f"QPushButton {{ background: transparent; color: {t['text_dim']};"
                           f" border: none; padding: 2px 6px; }}"
                           f" QPushButton:hover {{ color: {t['accent']}; }}"
                           f" QPushButton:checked {{ color: {t['accent']}; }}")
                self._copy_btn.setStyleSheet(btn_css)
                self._toggle_btn.setStyleSheet(btn_css)
                # r53：朗读按钮样式（可能不存在于旧气泡）
                if getattr(self, "_speak_btn", None) is not None:
                    self._speak_btn.setStyleSheet(btn_css)
                # 引用/拉黑按钮（仅含引用行的 assistant 气泡存在）
                if getattr(self, "_ref_btn", None) is not None:
                    ref_css = (f"QPushButton {{ background: transparent; color: {t['text_dim']};"
                               f" border: none; padding: 2px 0; }}"
                               f" QPushButton:hover {{ color: {t['accent']}; }}")
                    self._ref_btn.setStyleSheet(ref_css)
                if getattr(self, "_blacklist_btn", None) is not None:
                    self._blacklist_btn.setStyleSheet(btn_css)
        except RuntimeError:
            pass

    def sizeHint(self):
        h = 60
        if self._show_rendered and self._render_html:
            h = max(60, min(500, self.text.count("\n") * 22 + 60))
        elif not self._show_rendered:
            h = self._calc_source_height() + 40
        return QSize(480, h)


class DialogView(QWidget, RoundedFrameMixin):
    """AI 对话窗口。主题跟随 ThemeManager，主题设置在设置中心管理。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("AI 对话")
        self.setWindowTitle("OISystem - AI 对话")
        self.resize(560, 660)
        self._dialog = None
        self._all_messages = []
        self._rendered_count = 0
        self._render_start = 0
        self._loading_more = False
        self._user_scrolled_up = False  # round48：用户是否真正离开底部（与 _render_start 解耦）
        self._screenshot_busy = False
        self._screenshot_sent = False  # 截图分析是否触发了 send()（决定 send_btn 由谁恢复）
        self._shot_request_id = None   # round48：截图/关联页面请求代次，过滤旧窗口迟到结果
        self._attach_request_id = None
        self._pending_screen_context = None  # 关联当前页面的屏幕上下文，下一条消息时注入
        self._setup_ui()
        self._bind_dialog()
        self._apply_theme()
        self._timeout_timer = QTimer(self)
        self._timeout_timer.timeout.connect(self._check_chat_timeout)
        self._timeout_timer.start(5000)
        self._timeout_reminded = False
        self._fake_progress_timer = QTimer(self)
        self._fake_progress_timer.setSingleShot(True)
        self._fake_progress_timer.timeout.connect(self._fake_progress_tick)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_dialog)
        # r53：讲题朗读播放器（小米 MiMo-V2.5-TTS）
        from core.tts_player import TTSPlayer
        self._tts = TTSPlayer(self)
        self._active_speak_state_cb = None

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # 顶部工具栏（功能按钮，主题设置已移至设置中心）
        toolbar = QHBoxLayout()
        self.title = QLabel("AI 对话")
        self.title.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        toolbar.addWidget(self.title)
        toolbar.addStretch(1)

        new_btn = _make_icon_button(SVG_CARD_ADD, "新建对话")
        new_btn.clicked.connect(self._new_dialog)
        toolbar.addWidget(new_btn)

        self.del_btn = _make_icon_button(SVG_TAG, "删除对话", danger=True)
        self.del_btn.clicked.connect(self._delete_dialog)
        toolbar.addWidget(self.del_btn)

        self.shot_btn = _make_icon_button(SVG_BULB, "截图分析")
        self.shot_btn.clicked.connect(self._screenshot_analyze)
        toolbar.addWidget(self.shot_btn)

        self.attach_btn = _make_icon_button(SVG_EYE, "关联当前页面")
        self.attach_btn.clicked.connect(self._attach_screen_context)
        toolbar.addWidget(self.attach_btn)
        layout.addLayout(toolbar)

        # 模式不匹配提示横幅（非阻塞）
        self._banner = QFrame()
        self._banner.setObjectName("ModeMismatchBanner")
        self._banner.hide()
        banner_layout = QHBoxLayout(self._banner)
        banner_layout.setContentsMargins(10, 6, 10, 6)
        banner_layout.setSpacing(8)
        self._banner_label = QLabel("")
        self._banner_label.setWordWrap(True)
        banner_layout.addWidget(self._banner_label, 1)
        self._banner_switch = QPushButton("切换")
        self._banner_switch.clicked.connect(self._on_banner_switch)
        banner_layout.addWidget(self._banner_switch)
        self._banner_ignore = QPushButton("忽略")
        self._banner_ignore.clicked.connect(self._on_banner_ignore)
        banner_layout.addWidget(self._banner_ignore)
        layout.addWidget(self._banner)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        layout.addWidget(self.progress)

        # AI 思考状态提示
        self._thinking_label = QLabel("")
        self._thinking_label.setObjectName("tip")
        self._thinking_label.setFont(QFont("Microsoft YaHei", 8))
        self._thinking_label.setAlignment(Qt.AlignCenter)
        self._thinking_label.hide()
        layout.addWidget(self._thinking_label)

        # 消息列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self.messages_widget = QWidget()
        self.messages_layout = QVBoxLayout(self.messages_widget)
        self.messages_layout.setContentsMargins(4, 4, 4, 4)
        self.messages_layout.setSpacing(8)

        self.load_more_btn = QPushButton("▲ 加载更早的消息")
        self.load_more_btn.setFlat(True)
        self.load_more_btn.setObjectName("LoadMore")
        self.load_more_btn.clicked.connect(self._load_more)
        self.load_more_btn.hide()
        self.messages_layout.addWidget(self.load_more_btn)

        # 空状态欢迎页
        self._welcome = self._build_welcome()
        self.messages_layout.addWidget(self._welcome)

        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.messages_widget)
        layout.addWidget(self.scroll, 1)

        # 提示标签
        try:
            _mode = ConfigManager().settings.focus_mode
        except Exception:
            _mode = "oi"
        if _mode == "study":
            self.tip_label = QLabel("学习助手：引导思考、识别走神、不直接给答案。请专注当前学习内容。")
        else:
            self.tip_label = QLabel("AI 不会提供代码，会引导你思考。禁用闲聊。")
        self.tip_label.setObjectName("tip")
        self.tip_label.setFont(QFont("Microsoft YaHei", 9))
        self.tip_label.setWordWrap(True)
        layout.addWidget(self.tip_label)

        # 输入区
        input_card = QFrame()
        input_card.setObjectName("InputCard")
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(10, 8, 10, 8)
        input_layout.setSpacing(6)

        # 工具行：画图、上传、提示、字数
        tool_row = QHBoxLayout()
        tool_row.setSpacing(6)
        self.graph_btn = QPushButton("画图")
        self.graph_btn.setToolTip("打开图论编辑器，画完后点击插入对话")
        self.graph_btn.clicked.connect(self._open_graph_editor)
        tool_row.addWidget(self.graph_btn)

        upload_btn = _make_icon_button(SVG_CLOUD_UPLOAD, "上传文件")
        upload_btn.clicked.connect(self._upload_file)
        tool_row.addWidget(upload_btn)

        tool_row.addStretch(1)

        _t = ThemeManager().current_theme
        hint_label = QLabel("Enter 发送 · Shift+Enter 换行 · Esc 清空")
        hint_label.setFont(QFont("Microsoft YaHei", 7))
        hint_label.setStyleSheet(f"color: {_t['text_dim']}; background: transparent;")
        tool_row.addWidget(hint_label)

        self._char_count = QLabel("0")
        self._char_count.setFont(QFont("Microsoft YaHei", 7))
        self._char_count.setStyleSheet(f"color: {_t['text_dim']}; background: transparent;")
        tool_row.addWidget(self._char_count)
        input_layout.addLayout(tool_row)

        # 多行输入框（自适应高度）
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入消息，按 Enter 发送，Shift+Enter 换行...")
        self.input.setFont(QFont("Microsoft YaHei", 9))
        self.input.setFixedHeight(60)
        self.input.textChanged.connect(self._on_input_changed)
        self.input.installEventFilter(self)
        input_layout.addWidget(self.input)

        # 发送按钮行
        send_row = QHBoxLayout()
        send_row.addStretch(1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._send)
        send_row.addWidget(self.send_btn)
        input_layout.addLayout(send_row)
        layout.addWidget(input_card)

    def _apply_theme(self):
        css = ThemeManager().get_css()
        self.setStyleSheet(css)
        # r39 P0 修复：itemAt 可能返回 None，widget 可能是已 deleteLater 的对象
        for i in range(self.messages_layout.count()):
            item = self.messages_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            try:
                if isinstance(w, MessageBubble):
                    w.update_theme()
            except RuntimeError:
                # C++ 对象已销毁
                continue

    # ---------- 欢迎页 / 输入处理 ----------
    def _build_welcome(self) -> QFrame:
        """空状态欢迎页：引导语 + 快捷问题按钮。"""
        t = ThemeManager().current_theme
        welcome = QFrame()
        welcome.setObjectName("Welcome")
        w_layout = QVBoxLayout(welcome)
        w_layout.setContentsMargins(20, 30, 20, 20)
        w_layout.setSpacing(10)
        w_layout.setAlignment(Qt.AlignCenter)

        icon_label = QLabel("💡")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setFont(QFont("Microsoft YaHei", 30))
        w_layout.addWidget(icon_label)

        title = QLabel("开始一段新的学习对话")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet(f"color: {t['accent']}; background: transparent;")
        w_layout.addWidget(title)

        subtitle = QLabel("AI 会引导你思考，不会直接给代码。试试以下问题：")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {t['text_dim']}; background: transparent;")
        w_layout.addWidget(subtitle)

        for q in self._quick_questions():
            btn = QPushButton(q)
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.setStyleSheet(f"""
                QPushButton {{ background: {t['surface']}; color: {t['text']};
                    border: 1px solid {t['border_light']}; border-radius: 8px;
                    padding: 8px 12px; text-align: left; }}
                QPushButton:hover {{ background: {t.get('accent_hover', t['accent'])};
                    border-color: {t['accent']}; color: white; }}
            """)
            btn.clicked.connect(lambda checked, text=q: self._ask_quick_question(text))
            w_layout.addWidget(btn)
        return welcome

    def _quick_questions(self) -> list:
        """根据当前模式返回快捷问题。"""
        try:
            mode = ConfigManager().settings.focus_mode
        except Exception:
            mode = "oi"
        if mode == "study":
            return [
                "这道题的解题思路是什么？",
                "帮我分析一下这首诗的中心思想",
                "这个公式怎么推导？",
                "我哪里理解错了？",
            ]
        return [
            "这题怎么做？给我一些思路提示",
            "我的代码哪里有逻辑错误？",
            "这个算法的时间复杂度是多少？",
            "帮我画一个图论示意图",
        ]

    def _ask_quick_question(self, text: str):
        """点击快捷问题：填入输入框并发送。"""
        try:
            self.input.setPlainText(text)
            self._on_input_changed()
            self._send()
        except RuntimeError:
            pass

    def _update_welcome_visibility(self):
        """有消息时隐藏欢迎页，无消息时显示。"""
        try:
            self._welcome.setVisible(len(self._all_messages) == 0)
        except RuntimeError:
            pass

    def _on_input_changed(self):
        """输入变化：更新字数计数 + 自适应高度（1~6 行）。"""
        try:
            text = self.input.toPlainText()
            self._char_count.setText(str(len(text)))
            lines = max(1, text.count("\n") + 1)
            line_h = self.input.fontMetrics().height()
            new_h = min(160, max(44, lines * (line_h + 4) + 12))
            self.input.setFixedHeight(new_h)
        except RuntimeError:
            pass

    def eventFilter(self, obj, event):
        """拦截输入框 Enter 键：Enter/Ctrl+Enter 发送，Shift+Enter 换行。"""
        if obj is self.input and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False  # 交给 QPlainTextEdit 处理换行
                self._send()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        """Esc 清空输入框。"""
        if event.key() == Qt.Key_Escape:
            try:
                self.input.clear()
                self._on_input_changed()
            except RuntimeError:
                pass
            return
        super().keyPressEvent(event)

    # ---------- 消息虚拟滚动 ----------
    def _add_message(self, role: str, text: str, block_id: str = ""):
        """增量追加新消息，避免每次重建所有气泡（O(n²) → O(1)）。

        关键状态：
        - _render_start: 当前可见窗口在 _all_messages 中的起始下标
        - _rendered_count: 当前可见窗口大小（≤ RENDER_CHUNK）
        - 可见窗口 = _all_messages[_render_start : _render_start + _rendered_count]

        行为约定：
        - 若用户停在底部（_render_start == 0）：追加新消息 → 渲染新气泡，必要时丢弃最旧
        - 若用户已向上滚动（_render_start > 0）：仅追加数据，不渲染新气泡（避免破坏用户阅读位置）
        """
        self._all_messages.append({"role": role, "text": text, "block_id": block_id})
        self._update_welcome_visibility()
        total = len(self._all_messages)

        # 0) 首次渲染：建初始窗口
        if self._rendered_count == 0:
            self._render_recent()
            return

        # 1) 用户已向上滚动：仅追加数据，不渲染新气泡（避免破坏用户阅读位置）
        if self._user_scrolled_up:
            # 标记有"底部新消息"——在标题处显示一个回到底部按钮（轻量）
            new_at_bottom = total - (self._render_start + self._rendered_count)
            if new_at_bottom > 0:
                self.load_more_btn.setText(
                    f"▼ 跳到底部（新消息 {new_at_bottom} 条）"
                )
                self.load_more_btn.show()
            # 内存硬上限：若总数据 > 2*MAX_RENDERED，强制压缩
            if total > 2 * MAX_RENDERED:
                self._force_compact_data()
            return

        # 2) 用户停在底部：增量渲染
        if self._rendered_count < RENDER_CHUNK:
            self._add_bubble(role, text, block_id)
            self._rendered_count += 1
        else:
            # 窗口已满：滑动窗口（去掉最旧，追加最新）
            self._remove_oldest_bubble()
            self._add_bubble(role, text, block_id)
            # r38 P1 修复：滑动窗口后 _render_start 必须 +1，表示有更早的消息
            # 未渲染。原实现不更新 _render_start，导致 _all_messages 中存在
            # RENDER_CHUNK~MAX_RENDERED 条不可见但无法通过"加载更早"访问的消息。
            self._render_start += 1
            # _rendered_count 不变

        # 3) 内存 trim：总数据 > MAX_RENDERED 时丢弃最早的消息
        if total > MAX_RENDERED:
            trim_n = total - MAX_RENDERED
            self._all_messages = self._all_messages[trim_n:]
            # 调整 _render_start 以反映已丢弃的更早消息
            self._render_start = max(0, self._render_start - trim_n)
            self._rendered_count = min(self._rendered_count, RENDER_CHUNK)

        # 4) 更新 load_more 按钮
        if self._render_start > 0:
            self.load_more_btn.setText(
                f"▲ 加载更早的消息（{self._render_start} 条未显示）"
            )
            self.load_more_btn.show()
        else:
            self.load_more_btn.hide()

        # 5) 滚到底
        # r39 P0 修复：singleShot 包装为安全闭包，窗口销毁后回调不崩溃
        def _safe_scroll_bottom():
            try:
                self._scroll_to_bottom()
            except RuntimeError:
                pass
        QTimer.singleShot(50, _safe_scroll_bottom)

    def _force_compact_data(self):
        """强制压缩：用户已加载过更早 + 总数据爆量时，丢弃最早的消息并重置 _render_start。

        行为：trim 到 RENDER_CHUNK（不是 MAX_RENDERED），然后强制 _render_start=0
        并按 RENDER_CHUNK 条重新渲染，保持"已重置"语义。
        """
        if len(self._all_messages) <= RENDER_CHUNK:
            return
        trim_n = len(self._all_messages) - RENDER_CHUNK
        self._all_messages = self._all_messages[trim_n:]
        # 显式重置到"仅显示最后 RENDER_CHUNK 条"的稳定状态
        self._render_start = 0
        self._rendered_count = 0
        self._user_scrolled_up = False  # round48：压缩后回到稳定底部态
        self._clear_rendered()
        for msg in self._all_messages:
            self._add_bubble(msg["role"], msg["text"], msg.get("block_id", ""))
        self._rendered_count = len(self._all_messages)
        self._ensure_stretch()  # r34 P0 修复：确保 stretch 恢复
        self.load_more_btn.hide()

    def _remove_oldest_bubble(self):
        """移除最早渲染的气泡（保持窗口大小）。"""
        # r39 P1 修复：itemAt 可能返回 None，widget 可能是已销毁对象
        for i in range(self.messages_layout.count()):
            item = self.messages_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            try:
                if isinstance(w, MessageBubble):
                    self.messages_layout.removeWidget(w)
                    w.deleteLater()
                    return
            except RuntimeError:
                continue

    def _remove_oldest_bubble_from_bottom(self):
        """从布局末尾移除最新渲染的气泡（加载更早消息时维持窗口大小）。"""
        for i in range(self.messages_layout.count() - 1, -1, -1):
            item = self.messages_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            try:
                if isinstance(w, MessageBubble):
                    self.messages_layout.removeWidget(w)
                    w.deleteLater()
                    return
            except RuntimeError:
                continue

    def _render_recent(self):
        total = len(self._all_messages)
        if total == 0:
            return
        self._user_scrolled_up = False  # 重渲染后处于底部
        self._clear_rendered()
        start = max(0, total - RENDER_CHUNK)
        for msg in self._all_messages[start:]:
            self._add_bubble(msg["role"], msg["text"], msg.get("block_id", ""))
        self._rendered_count = total - start
        if start > 0:
            self.load_more_btn.setText(f"▲ 加载更早的消息（{start} 条未显示）")
            self.load_more_btn.show()
        else:
            self.load_more_btn.hide()
        self._render_start = start
        self._ensure_stretch()  # r34 P0 修复：确保 stretch 恢复
        # r39 P0 修复：singleShot 包装为安全闭包
        def _safe_scroll_bottom():
            try:
                self._scroll_to_bottom()
            except RuntimeError:
                pass
        QTimer.singleShot(50, _safe_scroll_bottom)

    def _load_more(self):
        if self._loading_more:
            return
        # P0 修复：当前按钮文字是"跳到底部"时，直接跳转而不是加载更早
        if "跳到底部" in self.load_more_btn.text():
            self._jump_to_bottom()
            return
        start = self._render_start
        if start <= 0:
            return
        self._loading_more = True
        self.progress.show()
        self.progress.setValue(30)
        try:
            chunk = min(RENDER_CHUNK, start)
            new_start = start - chunk
            existing = []
            i = 0
            while i < self.messages_layout.count():
                item = self.messages_layout.itemAt(i)
                w = item.widget()
                if isinstance(w, MessageBubble):
                    existing.append(w)
                    self.messages_layout.removeWidget(w)
                    continue
                if i == self.messages_layout.count() - 1:
                    self.messages_layout.removeItem(item)
                    continue
                i += 1
            for msg in self._all_messages[new_start:start]:
                bubble = MessageBubble(msg["role"], msg["text"], msg.get("block_id", ""),
                                       on_reference=self._reference_block,
                                       on_blacklist=self._blacklist_block,
                                       on_speak=self._handle_speak)
                self.messages_layout.addWidget(bubble)
            for b in existing:
                self.messages_layout.addWidget(b)
            self._ensure_stretch()  # P0 修复：保证 stretch 不重复
            self._render_start = new_start
            # P0 修复：_rendered_count 应反映实际可见气泡数，且不超过 RENDER_CHUNK
            # 加载更早 chunk 条后，需要从底部丢弃多余气泡以保持窗口大小
            total_bubbles = chunk + len(existing)
            while total_bubbles > RENDER_CHUNK:
                self._remove_oldest_bubble_from_bottom()
                total_bubbles -= 1
            self._rendered_count = total_bubbles
            if new_start > 0:
                self.load_more_btn.setText(f"▲ 加载更早的消息（{new_start} 条未显示）")
            else:
                self.load_more_btn.hide()
            self.progress.setValue(100)
            # r34 修复：把状态复位和进度条隐藏推迟到下一帧事件循环，避免
            # 当前滚动事件处理过程中 valueChanged 又触发 _load_more 造成重入/卡死。
            # r39 P0 修复：singleShot 包装为安全闭包
            def _safe_finish():
                try:
                    self._finish_load_more()
                except RuntimeError:
                    pass
            QTimer.singleShot(0, _safe_finish)
        except Exception as e:
            # r34 P0 修复：异常路径必须复位 _loading_more，否则后续无法加载/发送。
            # round48：不再向事件循环裸抛异常，避免 Qt 插槽异常冲击全局 excepthook。
            logger.warning(f"加载更早消息失败: {e}")
            self._finish_load_more()

    def _finish_load_more(self):
        """_load_more 的收尾：隐藏进度条并允许下一次加载。"""
        try:
            self.progress.hide()
        except RuntimeError:
            pass
        self._loading_more = False

    def _jump_to_bottom(self):
        """用户向上滚动期间有新消息时，一键回到底部并重置窗口。"""
        self._user_scrolled_up = False
        self._render_start = 0
        self._render_recent()
        self._scroll_to_bottom()

    def _clear_rendered(self, keep_stretch=True):
        """清空已渲染的气泡。

        P0 修复：先移除所有非 widget 的 layout item（包括 stretch），再移除气泡。
        避免多次 reset 时 stretch 累积。
        """
        # 1) 先清理 stretch / spacer 等"非 widget"的 item
        i = 0
        while i < self.messages_layout.count():
            item = self.messages_layout.itemAt(i)
            if item is None:
                i += 1
                continue
            w = item.widget()
            if w is None:
                # 非 widget 项（QSpacerItem 等）
                self.messages_layout.removeItem(item)
                continue
            i += 1

        # 2) 再清理气泡
        # r39 P1 修复：itemAt 可能返回 None，widget 可能是已销毁对象
        i = 0
        while i < self.messages_layout.count():
            item = self.messages_layout.itemAt(i)
            if item is None:
                i += 1
                continue
            w = item.widget()
            if w is None:
                i += 1
                continue
            try:
                if isinstance(w, MessageBubble):
                    self.messages_layout.removeWidget(w)
                    w.deleteLater()
                    continue
            except RuntimeError:
                # C++ 对象已销毁，从布局移除
                try:
                    self.messages_layout.removeItem(item)
                except Exception:
                    pass
                continue
            i += 1

    def _ensure_stretch(self):
        """保证 layout 末尾只有一个 stretch。"""
        # 找最后一个非 widget item
        for i in range(self.messages_layout.count() - 1, -1, -1):
            item = self.messages_layout.itemAt(i)
            if item is None:
                continue
            if item.widget() is None:
                # 已经是 stretch/spacer
                return
            break
        # 否则补一个 stretch
        self.messages_layout.addStretch(1)

    def _add_bubble(self, role: str, text: str, block_id: str = ""):
        bubble = MessageBubble(role, text, block_id,
                               on_reference=self._reference_block,
                               on_blacklist=self._blacklist_block,
                               on_speak=self._handle_speak)
        self.messages_layout.insertWidget(
            self.messages_layout.count() - 1, bubble
        )

    def _handle_speak(self, action: str, text: str, state_cb=None):
        """r53：气泡朗读按钮入口。action: "speak" / "stop"。"""
        try:
            if action == "stop":
                self._tts.stop()
                if state_cb is not None:
                    state_cb("idle")
                return
            # 同一时间只朗读一条：先停掉上一个气泡的按钮状态
            self._tts.stop()
            # state_cb 由 MessageBubble 传入，用于恢复该气泡按钮文案
            if state_cb is not None:
                try:
                    self._tts.stateChanged.disconnect(self._active_speak_state_cb)
                except (TypeError, RuntimeError):
                    pass
                self._active_speak_state_cb = state_cb
                self._tts.stateChanged.connect(state_cb)
            self._tts.speak(text)
        except Exception as e:
            logger.warning(f"朗读处理失败: {e}")

    def _scroll_to_bottom(self):
        self._user_scrolled_up = False
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scroll(self, value: int):
        bar = self.scroll.verticalScrollBar()
        # round48：跟踪"用户是否真的离开底部"，与 _render_start 解耦。
        # _render_start 只表示"更早未渲染的消息数"；滑动窗口时它也 >0，
        # 不能再被当成"用户上滚"标志，否则 17~60 条新消息全部不可见。
        max_value = bar.maximum()
        self._user_scrolled_up = bool(
            max_value > 0 and value < max_value - SCROLL_THRESHOLD
        )
        # r34 修复：增加边界检查，避免无气泡、无更早消息或已加载中时反复触发
        if (
            value <= SCROLL_THRESHOLD
            and self._render_start > 0
            and self._rendered_count > 0
            and not self._loading_more
            and self.messages_layout.count() > 0
        ):
            self._load_more()

    # ---------- 按钮回调 ----------
    def _send(self):
        if not self._dialog:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        # round48：发送前若用户停留在上方，先回到稳定底部再追加消息
        if self._user_scrolled_up or self._render_start > 0:
            self._jump_to_bottom()
        self._add_message("user", text)
        self.input.clear()
        self._on_input_changed()
        self.send_btn.setEnabled(False)
        self.send_btn.setText("生成中")
        self._thinking_label.setText("AI 正在思考...")
        self._thinking_label.show()
        self.progress.show()
        self.progress.setValue(0)
        self._fake_progress()
        # 若有"关联当前页面"的待注入上下文，随本条消息一并发送
        pending_ctx = self._pending_screen_context
        self._pending_screen_context = None
        try:
            self._dialog.send(text, screen_context=pending_ctx)
        except Exception as e:
            # round48 P1：send 的同步阶段（配置脏数据等）抛异常时不能依赖异步信号恢复，
            # 否则发送按钮永久禁用
            logger.warning(f"发送消息失败: {e}")
            self._fake_progress_timer.stop()
            self.progress.hide()
            self._thinking_label.hide()
            self.send_btn.setEnabled(True)
            self.send_btn.setText("发送")
            self._add_message("assistant", f"[发送失败] {e}")

    def _fake_progress(self):
        self._progress_val = 0
        self._fake_progress_timer.stop()
        self._fake_progress_timer.start(100)

    def _fake_progress_tick(self):
        """假进度条步进；窗口关闭后忽略 RuntimeError。"""
        try:
            if not self.progress.isVisible():
                return
            self._progress_val = min(85, self._progress_val + 3)
            self.progress.setValue(self._progress_val)
            if self._progress_val < 85:
                self._fake_progress_timer.start(120)
        except RuntimeError:
            # 窗口已关闭，控件被销毁
            pass

    def _on_reply(self, block_id: str, reply: str, meta: dict):
        self.progress.setValue(100)
        # r39 P0 修复：singleShot 不可取消，窗口销毁后回调会崩溃，包装为安全闭包
        def _safe_hide():
            try:
                self.progress.hide()
            except RuntimeError:
                pass
        QTimer.singleShot(200, _safe_hide)
        self._thinking_label.hide()
        self._add_message("assistant", reply, block_id=block_id)
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        # r39 P1 修复：meta 可能为 None（异常路径）
        meta = meta or {}
        if meta.get("has_code_block"):
            self.tip_label.setText("AI 似乎输出了代码块（违反规则）")
        # r53：自动朗读开关开启时朗读 AI 回复
        try:
            from config.settings import ConfigManager as _CM
            if _CM().settings.tts_auto_read:
                self._tts.speak(reply)
        except Exception as e:
            logger.debug(f"自动朗读失败: {e}")

    def _on_error(self, reason: str):
        self.progress.hide()
        self._thinking_label.hide()
        self._add_message("assistant", f"[错误] {reason}")
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")

    def _on_chat_detected(self, msg: str):
        self.tip_label.setText(msg)
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.progress.hide()
        self._thinking_label.hide()
        log_event("reminder", {"type": "chat", "msg": msg})

    def _on_new_algo(self, msg: str):
        self.tip_label.setText(msg)
        self._add_message("assistant", msg)

    def _on_code_output(self):
        self.tip_label.setText("检测到代码输出，已禁用")
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self.progress.hide()
        self._thinking_label.hide()
        self._add_message("assistant", "检测到代码输出，已禁用。请专注解题思路。")

    def _on_supervisor(self, correction: str):
        """元监督纠偏：仅写入 tip_label，**不**发新气泡。

        实际内容已由 ai_dialog 注入到下次 AI 调用的上下文（system 消息）。
        此方法仅作为"可见提示"：让用户知道监督系统在后台工作。
        """
        if not correction:
            return
        # 节流：每 30 秒最多更新一次 tip_label，避免刷屏
        now = time.time()
        if now - getattr(self, "_last_supervisor_tip_at", 0) < 30:
            return
        self._last_supervisor_tip_at = now
        # 短摘要在 tip_label 上显示
        short = correction[:30] + ("…" if len(correction) > 30 else "")
        self.tip_label.setText(f"[元监督] {short}（已注入上下文）")

    def _on_mode_mismatch_suggested(self, suggested_mode: str):
        """显示顶部非阻塞横幅，提示用户当前模式可能不匹配。"""
        try:
            current_mode = ConfigManager().settings.focus_mode
        except Exception:
            current_mode = "oi"
        current_label = "OI" if current_mode == "oi" else "学习文化课"
        target_label = "学习文化课" if suggested_mode == "study" else "OI"
        self._banner_label.setText(
            f"当前为 {current_label} 模式，检测到您可能在问 {target_label} 相关问题，是否切换到 {target_label} 模式？"
        )
        self._banner.setProperty("suggested_mode", suggested_mode)
        self._banner.show()

    def _on_banner_switch(self):
        suggested = self._banner.property("suggested_mode")
        if not suggested:
            self._banner.hide()
            return
        try:
            from ui.focus_view import switch_focus_mode
            if switch_focus_mode(suggested):
                self._banner.hide()
                mode_text = "学习文化课" if suggested == "study" else "OI"
                self.tip_label.setText(f"已切换到 {mode_text} 模式。")
                # 刷新输入区提示文案
                self._refresh_tip_label()
            else:
                self.tip_label.setText("模式切换失败：可能专注模式运行中，请先退出。")
        except Exception as e:
            logger.warning(f"横幅切换模式失败: {e}")
            self.tip_label.setText("模式切换失败")

    def _on_banner_ignore(self):
        self._banner.hide()

    def _refresh_tip_label(self):
        try:
            mode = ConfigManager().settings.focus_mode
        except Exception:
            mode = "oi"
        if mode == "study":
            self.tip_label.setText("学习助手：引导思考、识别走神、不直接给答案。请专注当前学习内容。")
        else:
            self.tip_label.setText("AI 不会提供代码，会引导你思考。禁用闲聊。")

    def _open_graph_editor(self):
        """打开图论编辑器，接收插入对话信号（单例复用，避免多窗口泄漏）。"""
        try:
            existing = getattr(self, "_graph_editor", None)
            if existing is not None:
                alive = True
                try:
                    from shiboken6 import isValid
                    alive = isValid(existing)
                except ImportError:
                    try:
                        existing.isVisible()
                    except RuntimeError:
                        alive = False
                if alive:
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    return
            from ui.graph_editor import GraphEditor
            editor = GraphEditor(self)
            editor.graph_text_ready.connect(self._insert_graph_text)
            self._graph_editor = editor  # 保持引用，防 GC 且可复用
            editor.show()
        except Exception as e:
            logger.warning(f"打开图论编辑器失败: {e}")
            QMessageBox.warning(self, "失败", f"无法打开图论编辑器: {e}")

    def _insert_graph_text(self, text: str):
        """将图文本插入输入框。"""
        if not text:
            return
        current = self.input.toPlainText()
        wrapper = f"[图论示意图]\n```graph\n{text}\n```"
        if current:
            self.input.setPlainText(current + "\n" + wrapper)
        else:
            self.input.setPlainText(wrapper)
        self._on_input_changed()

    def _new_dialog(self):
        if self._dialog:
            # round48：异步摘要/存档，避免 flash 网络调用冻结新建按钮
            self._dialog.close_dialog_async()
        self._all_messages.clear()
        self._pending_screen_context = None  # 新对话不继承旧对话的"关联页面"上下文
        self._reset_render_state()
        self.tip_label.setText("新对话已开始。")
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")
        self._thinking_label.hide()
        self.progress.hide()

    def _delete_dialog(self):
        self.del_btn.setEnabled(False)
        try:
            reply = QMessageBox.question(
                self, "删除对话", "确定删除当前对话吗？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes and self._dialog:
                # round48：删除摘要改异步，不冻结 UI
                self._dialog.delete_dialog_async()
                self._all_messages.clear()
                self._reset_render_state()
                self.tip_label.setText("对话已删除。")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")
                self._thinking_label.hide()
                self.progress.hide()
        finally:
            self.del_btn.setEnabled(True)

    def _reset_render_state(self):
        """新建/删除对话时统一重置渲染状态（避免 S1 bug：_rendered_count 残留导致消息被吞）。

        P0 修复：改用 _ensure_stretch() 避免 stretch 累积。
        r38 P1 修复：同步重置 _timeout_reminded 并停止 _kill_timer，避免旧的超时
        掐断计时器误杀新对话（原 bug：新建对话后 _kill_timer 仍在运行，_timeout_reminded
        仍为 True，导致后续超时检测永久失效且旧计时器会关闭新对话）。
        """
        self._render_start = 0
        self._rendered_count = 0
        self._last_supervisor_tip_at = 0
        self._timeout_reminded = False
        self._user_scrolled_up = False  # round48：新对话从底部开始
        # 新建/删除对话时清空"关联页面"待注入上下文，避免旧上下文污染新对话
        self._pending_screen_context = None
        # 停止可能正在运行的 kill 计时器，避免误杀新对话
        try:
            self._kill_timer.stop()
        except RuntimeError:
            # 窗口已关闭，控件被销毁
            pass
        self._clear_rendered()
        self.load_more_btn.hide()
        self._ensure_stretch()
        self._update_welcome_visibility()

    def _screenshot_analyze(self):
        # r38 P1 修复：原实现同步调用 screenshot_analyze 阻塞 UI，且不追加用户气泡、
        # 不显示进度、finally 立即恢复 shot_btn（send 异步未完成时用户可重复点击）。
        # 改为：禁用按钮 + 显示进度 + 追加用户气泡 + 异步等待 screenshot_analyzed 信号。
        if self._screenshot_busy:
            return
        if not self._dialog:
            return
        self._screenshot_busy = True
        if self.shot_btn is not None:
            self.shot_btn.setEnabled(False)
        # 同时禁用 send_btn，避免截图分析自动 send 期间用户手动发送冲突
        self.send_btn.setEnabled(False)
        self.progress.show()
        self.progress.setValue(10)
        self.tip_label.setText("正在截图分析...")
        try:
            request_id = self._dialog.screenshot_analyze()
            if request_id is None:
                # 线程占用：无信号，自行恢复（不追加气泡）
                self.tip_label.setText("截图分析失败（上次分析仍在进行）")
                self._finish_screenshot_analyze()
                return
            # 追加用户气泡，让用户看到"截图分析"这一动作的可见反馈
            if self._user_scrolled_up or self._render_start > 0:
                self._jump_to_bottom()
            self._add_message("user", "[截图分析] 正在分析当前屏幕...")
            # 记录本次代次；同步失败/异步结果都带同一 request_id
            self._shot_request_id = request_id
            # 非线程占用（截图失败/异常）：screenshot_analyze 已同步发回空结果，
            # _on_screenshot_analyzed 会完成恢复，这里无需处理
        except Exception as e:
            self.tip_label.setText(f"截图分析失败: {e}")
            logger.warning(f"截图分析失败: {e}")
            self._finish_screenshot_analyze()
            self._add_message("assistant", f"[截图分析失败: {e}]")

    def _on_screenshot_analyzed(self, request_id: int, result: dict):
        """r38 P1 修复：异步接收截图分析结果，送入 AI 对话并恢复 UI 状态。"""
        # round48：请求代次过滤——窗口关闭重开后，旧 worker 的迟到结果直接丢弃，
        # 不得自动作为新窗口的用户消息发送出去
        if self._shot_request_id != request_id:
            return
        self._shot_request_id = None
        self._screenshot_sent = False  # 本次结果是否触发了 send()
        try:
            if not isinstance(result, dict):
                self.tip_label.setText("截图分析返回格式异常")
                self._add_message(
                    "assistant", f"[截图分析返回异常: {result}]"
                )
                return
            activity = result.get("activity", "?")
            efficiency = result.get("efficiency", "?")
            self.tip_label.setText(
                f"截图分析：活动={activity}, 效率={efficiency}"
            )
            # 自动将分析结果作为上下文送入 AI 对话
            if activity and not (
                activity in ("截图失败", "API 失败", "解析失败", "")
                or activity.startswith("失败")
            ):
                # r39 P1 修复：异步信号回调需判 _dialog 是否仍存在（窗口可能已关闭）
                if self._dialog is None:
                    return
                # 截图分析结果取代之前"关联页面"的待注入上下文
                self._pending_screen_context = None
                # 统一由 send() 追加用户消息，避免 UI 与内部历史重复
                self._screenshot_sent = True
                self._dialog.send(
                    "这是我当前的屏幕情况，请根据分析结果给出下一步学习建议。",
                    screen_context=result,
                )
            else:
                self._add_message(
                    "assistant",
                    f"[截图分析未获得有效结果: {activity}]"
                )
        except Exception as e:
            logger.warning(f"处理截图分析结果失败: {e}")
            # send 抛出异常时不能依赖 AI 回复恢复 UI，此处自行恢复
            self._screenshot_sent = False
        finally:
            self._finish_screenshot_analyze()

    def _finish_screenshot_analyze(self):
        """恢复截图分析后的 UI 状态。"""
        self._screenshot_busy = False
        if self.shot_btn is not None:
            self.shot_btn.setEnabled(True)
        # P1 修复：用"是否触发了 send()"标志判断，而非 is_busy()（截图线程）。
        # 原实现里 _shot_thread 尚未 quit 时 is_busy() 恒 True，无效结果路径
        # 永不恢复 send_btn/进度条/思考提示，按钮永久禁用。
        # 若截图分析触发了 send()，send_btn 由 _on_reply/_on_error 等槽函数恢复。
        if not self._screenshot_sent:
            self.send_btn.setEnabled(True)
            self.send_btn.setText("发送")
            self.progress.hide()
            self._thinking_label.hide()
        self._screenshot_sent = False

    def _attach_screen_context(self):
        """一键关联当前页面：异步截图并生成屏幕摘要，下一条消息时作为上下文注入。"""
        if not self._dialog:
            return
        if self.attach_btn is not None:
            self.attach_btn.setEnabled(False)
        self.tip_label.setText("正在读取当前页面...")
        self.progress.show()
        self.progress.setValue(10)
        try:
            request_id = self._dialog.attach_screen_context()
            if request_id is None:
                # 线程占用：attach 不发信号，需自行恢复并提示
                self.tip_label.setText("关联页面失败（上次分析仍在进行）")
                self._finish_attach()
            else:
                self._attach_request_id = request_id
            # 非线程占用（截图失败/异常）：attach 已同步发回空结果，
            # _on_screen_context_ready 会设置提示并完成恢复，这里不再覆写
        except Exception as e:
            logger.warning(f"关联页面失败: {e}")
            self.tip_label.setText(f"关联页面失败: {e}")
            self._finish_attach()

    def _on_screen_context_ready(self, request_id: int, ctx: dict):
        """异步接收屏幕摘要，存入待注入上下文，下一条消息时附带。"""
        # round48：请求代次过滤，旧窗口迟到结果不得写入新窗口
        if self._attach_request_id != request_id:
            return
        self._attach_request_id = None
        try:
            if not isinstance(ctx, dict) or not ctx.get("screen_summary"):
                self.tip_label.setText("关联页面失败（未获得有效内容）")
                return
            summary = str(ctx.get("screen_summary", ""))
            self._pending_screen_context = ctx
            short = summary[:40] + ("…" if len(summary) > 40 else "")
            self.tip_label.setText(f"已关联当前页面：{short}（下一条消息将附带）")
        except Exception as e:
            logger.warning(f"处理屏幕上下文失败: {e}")
        finally:
            self._finish_attach()

    def _finish_attach(self):
        """恢复关联页面操作后的 UI 状态。"""
        if self.attach_btn is not None:
            try:
                self.attach_btn.setEnabled(True)
            except RuntimeError:
                pass
        try:
            self.progress.hide()
        except RuntimeError:
            pass

    def _upload_file(self):
        if not self._dialog:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "", "文本文件 (*.txt *.md *.cpp *.c *.py *.java);;所有文件 (*)"
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if len(content) > 2000:
                    content = content[:2000] + "\n... [文件过长已截断]"
                if self._user_scrolled_up or self._render_start > 0:
                    self._jump_to_bottom()
                self._add_message("user", f"[上传: {os.path.basename(path)}]\n{content}")
                self.send_btn.setEnabled(False)
                self.send_btn.setText("生成中")
                self._thinking_label.setText("AI 正在分析文件...")
                self._thinking_label.show()
                self.progress.show()
                self.progress.setValue(0)
                self._fake_progress()
                # 上传是独立动作，清掉"关联页面"待注入上下文，避免污染后续消息
                self._pending_screen_context = None
                self._dialog.send(
                    f"我上传了文件 {os.path.basename(path)}：\n\n{content}",
                    skip_detectors=True,
                )
            except Exception as e:
                QMessageBox.warning(self, "失败", f"读取文件失败: {e}")
                self.send_btn.setEnabled(True)
                self.send_btn.setText("发送")
                self.progress.hide()
                self._thinking_label.hide()

    def _reference_block(self, block_id: str):
        if self._dialog:
            ok = self._dialog.reference_block(block_id)
            self.tip_label.setText(
                f"已引用 {block_id}" if ok else f"引用失败：{block_id} 不存在"
            )

    def _on_context_pruned(self, count: int):
        """上下文裁剪反馈：告诉用户低价值历史块未发送。"""
        try:
            self.tip_label.setText(f"上下文已裁剪 {count} 块（低价值内容未发送，重要内容可手动引用保留）")
        except RuntimeError:
            pass

    def _on_context_block_referenced(self, block_id: str):
        try:
            self.tip_label.setText(f"已引用上下文块 {block_id}（将强制保留）")
        except RuntimeError:
            pass

    def _on_context_block_blacklisted(self, block_id: str):
        try:
            self.tip_label.setText(f"已拉黑上下文块 {block_id}（不再主动调用）")
        except RuntimeError:
            pass

    def _blacklist_block(self, block_id: str):
        """永久拉黑某上下文块，禁止 AI 主动调用。"""
        if not self._dialog:
            return
        try:
            self._dialog.blacklist_block(block_id)
            self.tip_label.setText(f"已拉黑上下文块 {block_id}（不再主动调用）")
        except Exception as e:
            logger.warning(f"拉黑上下文块失败: {e}")
            self.tip_label.setText(f"拉黑失败: {e}")

    def _bind_dialog(self):
        try:
            self._dialog = _get_global_dialog()
            self._dialog.reply_ready.connect(self._on_reply)
            self._dialog.error_occurred.connect(self._on_error)
            self._dialog.chat_detected.connect(self._on_chat_detected)
            self._dialog.new_algo_detected.connect(self._on_new_algo)
            self._dialog.code_output_detected.connect(self._on_code_output)
            self._dialog.supervisor_correction.connect(self._on_supervisor)
            self._dialog.mode_mismatch_suggested.connect(self._on_mode_mismatch_suggested)
            # r38 P1 修复：连接异步截图分析结果信号
            self._dialog.screenshot_analyzed.connect(self._on_screenshot_analyzed)
            # 关联当前页面：连接异步屏幕摘要信号
            self._dialog.screen_context_ready.connect(self._on_screen_context_ready)
            # round48：上下文裁剪/引用/拉黑反馈端口
            self._dialog.context_pruned.connect(self._on_context_pruned)
            self._dialog.context_block_referenced.connect(self._on_context_block_referenced)
            self._dialog.context_block_blacklisted.connect(self._on_context_block_blacklisted)
        except Exception as e:
            logger.warning(f"绑定 AIDialog 失败: {e}")

    def _check_chat_timeout(self):
        # r40 P0 修复：用户明确要求对话"没有时间限制"，取消自动掐断。
        # 保留本方法仅用于记录日志/提示，不再触发 _kill_dialog。
        if not self._dialog:
            return
        try:
            secs = self._dialog.check_unlimited_chat_timeout()
            if secs is None:
                return
            cfg = ConfigManager().settings
            # round48：脏配置（None/字符串）归一化，避免 * 60 抛 TypeError
            try:
                max_minutes = int(cfg.ai_unlimited_chat_max_minutes)
            except (TypeError, ValueError):
                max_minutes = 15
            max_sec = max(1, max_minutes) * 60
            if secs >= max_sec and not self._timeout_reminded:
                self.tip_label.setText(
                    f"非专注模式对话已持续 {secs // 60} 分钟，可继续交流"
                )
                self._timeout_reminded = True
                log_event("reminder", {"type": "chat_timeout_no_kill", "secs": secs})
        except Exception as e:
            logger.debug(f"对话超时检查失败: {e}")

    def _kill_dialog(self):
        # r40 P0 修复：对话不再因时长上限被自动掐断。
        # _kill_timer 仍保留并在 closeEvent/_reset_render_state 中停止，
        # 但不会再启动，避免误杀长对话。
        self._timeout_reminded = False

    def _unbind_dialog(self):
        """窗口关闭时断开 AIDialog 信号，防止已销毁对象被全局单例信号触发。"""
        if not self._dialog:
            return
        for signal, slot in (
            (self._dialog.reply_ready, self._on_reply),
            (self._dialog.error_occurred, self._on_error),
            (self._dialog.chat_detected, self._on_chat_detected),
            (self._dialog.new_algo_detected, self._on_new_algo),
            (self._dialog.code_output_detected, self._on_code_output),
            (self._dialog.supervisor_correction, self._on_supervisor),
            (self._dialog.mode_mismatch_suggested, self._on_mode_mismatch_suggested),
            (self._dialog.screenshot_analyzed, self._on_screenshot_analyzed),
            (self._dialog.screen_context_ready, self._on_screen_context_ready),
            (self._dialog.context_pruned, self._on_context_pruned),
            (self._dialog.context_block_referenced, self._on_context_block_referenced),
            (self._dialog.context_block_blacklisted, self._on_context_block_blacklisted),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                # 已断开或对象已销毁，均可忽略
                pass
        self._dialog = None

    def closeEvent(self, event):
        # P0 修复：必须停止周期性 timer，否则窗口销毁后仍访问已释放成员
        self._timeout_timer.stop()
        self._fake_progress_timer.stop()
        self._kill_timer.stop()
        # r53：停止并回收朗读线程，避免窗口销毁后回调访问已释放控件
        try:
            self._tts.shutdown()
        except Exception as e:
            logger.debug(f"TTS 关闭失败: {e}")
        # round48：作废本窗口所有截图/关联页面请求代次，迟到的全局结果会被丢弃
        self._shot_request_id = None
        self._attach_request_id = None
        self._screenshot_busy = False
        self._pending_screen_context = None
        # 先存档再断开信号，避免 close_dialog 期间 UI 无法响应
        if self._dialog:
            try:
                # round48：关窗存档改异步，主线程不被 flash 网络调用冻结
                self._dialog.close_dialog_async()
            except Exception as e:
                logger.warning(f"关窗存档失败: {e}")
        self._unbind_dialog()
        super().closeEvent(event)


# ---------- 全局单例 ----------
_GLOBAL_DIALOG = None

def set_global_dialog(dialog):
    global _GLOBAL_DIALOG
    _GLOBAL_DIALOG = dialog

def _get_global_dialog():
    global _GLOBAL_DIALOG
    if _GLOBAL_DIALOG is None:
        from core.ai_dialog import AIDialog
        _GLOBAL_DIALOG = AIDialog()
    return _GLOBAL_DIALOG
