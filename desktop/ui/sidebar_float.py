"""OISystem 悬浮快捷键式侧边栏（Xbox 风格）。

全黑半透明悬浮面板，通过全局快捷键唤起/隐藏。
可放在屏幕任何位置，默认出现在鼠标附近。
失焦自动隐藏。
"""
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QPushButton, QApplication, QWidget,
)
from PySide6.QtCore import (
    Qt, QPoint, QRect, QRectF, QTimer, QObject, Slot, QMetaObject,
)
from PySide6.QtGui import (
    QPainter, QColor, QFont, QPainterPath,
    QPen, QLinearGradient, QCursor, QRadialGradient,
    QKeySequence, QShortcut,
)

from ui.icons import SIDEBAR_BUTTONS, render_svg, SVG_BELL, SVG_BELL_OFF, get_sidebar_buttons
from ui.themes import ThemeManager
from utils.helpers import logger
from utils.exceptions import FocusLockedError
from config.settings import ConfigManager

PANEL_W = 220
BUTTON_H = 44
BUTTON_GAP = 4
MARGIN_H = 14
MARGIN_V = 16
CORNER_R = 16
OPACITY = 0.94

_FLOAT_SIDEBAR_HOTKEY_LISTENER = None   # pynput 全局热键监听（本模块专用）
_FLOAT_SIDEBAR_BRIDGE = None            # 主线程桥：把悬浮热键回调 marshal 回主线程


class _FloatHotkeyBridge(QObject):
    """把 pynput 监听线程的悬浮热键回调投递回主线程执行。

    pynput 监听线程没有 Qt 事件循环，QTimer.singleShot 从该线程投递的事件
    永远不会被处理（round48 P1）。必须由主线程创建 QObject 桥，再用
    QueuedConnection 把回调 marshal 回桥所在线程。
    """

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self._callback = callback

    @Slot()
    def _do_toggle(self):
        try:
            if self._callback is not None:
                self._callback()
        except Exception as e:
            logger.warning(f"悬浮侧边栏热键回调失败: {e}")


def _dispatch_float_toggle(callback):
    """从任意线程安全触发悬浮侧边栏显示/隐藏切换。"""
    global _FLOAT_SIDEBAR_BRIDGE
    if _FLOAT_SIDEBAR_BRIDGE is None:
        # 桥未创建（创建失败/未在主线程注册）：宁可跨线程直调，也不让热键静默失效
        callback()
        return
    try:
        QMetaObject.invokeMethod(_FLOAT_SIDEBAR_BRIDGE, "_do_toggle", Qt.QueuedConnection)
    except Exception:
        callback()


# ---------- 按钮 ----------
class SidebarButton(QPushButton):
    """悬浮式侧边栏按钮：图标在左，文字在右。"""

    def __init__(self, key, label, svg_str, accent, parent=None):
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self.svg_str = svg_str
        self.accent = accent
        self.setFixedHeight(BUTTON_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)

    def set_edge(self, edge):
        self.update()

    def set_accent(self, color):
        self.accent = color
        self.update()

    def set_svg(self, svg_str):
        self.svg_str = svg_str
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        accent = QColor(self.accent)
        if not accent.isValid():
            accent = QColor("#3b82f6")

        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 36))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 8, rect.height() - 4), 10, 10)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 8, rect.height() - 4), 10, 10)

        color = self.accent if self.isChecked() else ("#f8fafc" if self.underMouse() else "#94a3b8")

        pm = render_svg(self.svg_str, size=18, color=color)
        p.drawPixmap(MARGIN_H, (rect.height() - 18) // 2, pm)

        p.setPen(QColor(color))
        p.setFont(QFont("Microsoft YaHei", 10))
        text_rect = QRectF(MARGIN_H + 28, 0, rect.width() - MARGIN_H - 32, rect.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.label_text)

        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(2, rect.height() // 2 - 10, 3, 20), 1.5, 1.5)

        p.end()


# ---------- 主侧边栏 ----------
class Sidebar(QFrame):

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._buttons = {}
        self._expanded = False      # 默认隐藏
        self._open_views = {}
        self._mute_observer = None
        self._layout = None
        self.edge = "left"          # 兼容接口

        self._accent = ThemeManager().current_theme.get("accent", "#3b82f6")

        self._setup_ui()
        self._set_initial_geometry()

        # 注册全局快捷键
        self._register_hotkey()

        try:
            from core.mute_mode import add_state_observer, is_mute_mode
            self._mute_observer = self._on_mute_mode_changed
            add_state_observer(self._mute_observer)
            mute_btn = self._buttons.get("mute")
            if mute_btn is not None:
                mute_btn.setChecked(is_mute_mode())
        except Exception:
            pass

        self._update_focus_button_svg()
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            eng.focus_started.connect(self._on_focus_state_changed)
            eng.focus_ended.connect(self._on_focus_state_changed)
        except Exception:
            pass

    def _setup_ui(self):
        self.setObjectName("SidebarFloat")

        self._container = QWidget(self)
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(BUTTON_GAP)
        self._layout.setContentsMargins(MARGIN_H, MARGIN_V, MARGIN_H, MARGIN_V)

        for key, label, svg in get_sidebar_buttons():
            btn = SidebarButton(key, label, svg, self._accent, self._container)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._on_button(k))
            self._layout.addWidget(btn)
            self._buttons[key] = btn

        self._layout.addStretch(1)

    def _panel_height(self):
        n = len(SIDEBAR_BUTTONS)
        return MARGIN_V * 2 + n * BUTTON_H + max(0, n - 1) * BUTTON_GAP + 8

    def _set_initial_geometry(self):
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        if not geo.isValid():
            geo = QRect(0, 0, 1920, 1080)
        ph = self._panel_height()
        x = geo.center().x() - PANEL_W // 2
        y = geo.center().y() - ph // 2
        self.setGeometry(x, y, PANEL_W, ph)
        self._container.setGeometry(0, 0, PANEL_W, ph)

    def _register_hotkey(self):
        """注册 pynput 全局热键监听。"""
        global _FLOAT_SIDEBAR_HOTKEY_LISTENER, _FLOAT_SIDEBAR_BRIDGE
        # round48：重复注册前先停旧监听器，防止 Sidebar 重建时线程泄漏 + 双监听器重复响应
        if _FLOAT_SIDEBAR_HOTKEY_LISTENER is not None:
            try:
                _FLOAT_SIDEBAR_HOTKEY_LISTENER.stop()
            except Exception:
                pass
            _FLOAT_SIDEBAR_HOTKEY_LISTENER = None
        # round48 P1：桥必须在本函数（主线程）内创建，QObject 线程归属才是主线程。
        # 每次注册都重建，避免旧桥仍指向已销毁的 Sidebar。
        if _FLOAT_SIDEBAR_BRIDGE is not None:
            try:
                _FLOAT_SIDEBAR_BRIDGE.deleteLater()
            except Exception:
                pass
            _FLOAT_SIDEBAR_BRIDGE = None
        try:
            _FLOAT_SIDEBAR_BRIDGE = _FloatHotkeyBridge(self._toggle_visible)
        except Exception:
            _FLOAT_SIDEBAR_BRIDGE = None
        cfg = ConfigManager().settings
        hotkey_str = (getattr(cfg, "sidebar_float_hotkey", "ctrl+shift+s") or "ctrl+shift+s")
        try:
            from pynput import keyboard as pynput_keyboard
            modifiers = {"ctrl", "alt", "shift", "cmd", "win"}
            parts = [p.strip().lower() for p in hotkey_str.split("+")]
            if len(parts) > 1:
                mod_parts = parts[:-1]
                valid_mods = [p for p in mod_parts if p in modifiers]
                if len(valid_mods) != len(mod_parts):
                    logger.warning(f"悬浮侧边栏快捷键含无效修饰符: {hotkey_str}")
                    return
                mod_str = "+".join(f"<{p}>" for p in valid_mods)
                pynput_str = f"{mod_str}+{parts[-1]}" if mod_str else parts[-1]
            else:
                pynput_str = parts[0]

            def _on_triggered():
                # 监听线程回调：marshal 回主线程，避免跨线程操作 QWidget
                _dispatch_float_toggle(self._toggle_visible)

            _FLOAT_SIDEBAR_HOTKEY_LISTENER = pynput_keyboard.GlobalHotKeys({
                pynput_str: _on_triggered
            })
            _FLOAT_SIDEBAR_HOTKEY_LISTENER.daemon = True
            _FLOAT_SIDEBAR_HOTKEY_LISTENER.start()
            logger.info(f"悬浮侧边栏快捷键已注册: {hotkey_str}")
        except Exception as e:
            logger.warning(f"悬浮侧边栏快捷键注册失败: {e}")

    def _toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self._show_at_cursor()

    def _show_at_cursor(self):
        """在鼠标位置附近显示面板。"""
        pos = QCursor.pos()
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        if not geo.isValid():
            geo = QRect(0, 0, 1920, 1080)
        ph = self._panel_height()
        pw = PANEL_W

        # 以鼠标为中心，但不超出屏幕
        x = pos.x() - pw // 2
        y = pos.y() - ph // 2
        x = max(geo.left(), min(geo.right() - pw, x))
        y = max(geo.top(), min(geo.bottom() - ph, y))

        self.setGeometry(x, y, pw, ph)
        self._container.setGeometry(0, 0, pw, ph)
        self.show()
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._container is not None:
            self._container.setGeometry(0, 0, self.width(), self.height())

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            p.end()
            return

        # 圆角矩形裁剪
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), CORNER_R, CORNER_R)
        p.setClipPath(path)

        # 全黑半透明背景
        p.fillRect(0, 0, w, h, QColor(8, 12, 24, int(255 * OPACITY)))

        # 顶部微渐变高光
        accent = QColor(self._accent)
        tg = QLinearGradient(0, 0, 0, 40)
        tg.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 16))
        tg.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(0, 0, w, 40, tg)

        # 边框
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        p.setClipping(False)
        p.end()

    # ---------- 失焦隐藏 ----------
    def focusOutEvent(self, event):
        """失焦时延迟隐藏，允许点击按钮时不立即隐藏。"""
        if self.isVisible():
            QTimer.singleShot(200, self._maybe_hide)
        super().focusOutEvent(event)

    def _maybe_hide(self):
        # round48：PySide6 无 sip 模块，改用 shiboken6 检查对象是否已销毁
        try:
            from shiboken6 import isValid
            if not isValid(self):
                return
        except ImportError:
            try:
                self.isVisible()
            except RuntimeError:
                return
        if not self.isActiveWindow() and not self._has_open_views():
            self.hide()

    def _has_open_views(self):
        return bool(self._open_views)

    # ---------- 兼容接口 ----------
    def _expand(self):
        self._show_at_cursor()

    def _collapse(self):
        if not self._has_open_views():
            self.hide()

    # ---------- 按钮动作 ----------
    def _on_button(self, key):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.update()
        try:
            from core.mute_mode import is_mute_mode
            if is_mute_mode():
                mute_btn = self._buttons.get("mute")
                if mute_btn is not None:
                    mute_btn.setChecked(True)
        except Exception:
            pass
        logger.info(f"Sidebar button: {key}")
        if key == "exit":
            self._exit_clicked()
        elif key == "mute":
            self._mute_clicked()
        elif key == "focus":
            self._open_focus()
        elif key == "dialog":
            self._open_dialog()
        elif key == "graph":
            self._open_graph()
        elif key == "settings":
            self._open_settings()
        elif key == "log":
            self._open_log()
        elif key == "zzoi":
            self._open_zzoi()
        elif key == "lecture":
            # F9 修复：三种非默认侧边栏样式此前都没有 lecture 分支，而按钮仍由
            # get_sidebar_buttons() 渲染出来 → 点「讲课」静默无响应。补上与 sidebar_v2 一致的分发。
            self._open_lecture()

    def _toggle_view(self, key, view_cls):
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setEnabled(False)
        existing = self._open_views.get(key)
        if existing is not None:
            try:
                existing.close()
                existing.deleteLater()
            except Exception:
                pass
            self._open_views.pop(key, None)
            if btn is not None:
                btn.setChecked(False)
                btn.setEnabled(True)
            return
        try:
            view = view_cls(self)
            def _on_view_closed():
                if self._open_views.get(key) is view:
                    self._open_views.pop(key, None)
                if btn is not None:
                    btn.setChecked(False)
                    btn.setEnabled(True)
            orig_close = view.closeEvent
            def _patched_close(event):
                _on_view_closed()
                if orig_close:
                    orig_close(event)
            view.closeEvent = _patched_close
            view.show()
            self._open_views[key] = view
            if btn is not None:
                btn.setEnabled(True)
        except Exception as e:
            logger.warning(f"{view_cls.__name__} 未就绪: {e}")
            if btn is not None:
                btn.setChecked(False)
                btn.setEnabled(True)

    def _open_focus(self):
        from ui.focus_view import FocusView
        self._toggle_view("focus", FocusView)

    def _open_dialog(self):
        from ui.dialog_view import DialogView
        self._toggle_view("dialog", DialogView)

    def _open_settings(self):
        from ui.settings_view import SettingsView
        self._toggle_view("settings", SettingsView)

    def _open_log(self):
        from ui.log_view import LogView
        self._toggle_view("log", LogView)

    def _open_zzoi(self):
        from ui.zzoi_view import ZzoiView
        self._toggle_view("zzoi", ZzoiView)

    def _open_lecture(self):
        # F9：与 sidebar_v2._open_lecture 保持一致
        from ui.lecture_view import LectureView
        self._toggle_view("lecture", LectureView)

    def _open_graph(self):
        from ui.graph_editor import GraphEditor
        self._toggle_view("graph", GraphEditor)

    def _exit_clicked(self):
        btn = self._buttons.get("exit")
        if btn is not None:
            btn.setEnabled(False)
        try:
            from core.exit_flow import request_exit
            request_exit()
        except FocusLockedError:
            # 专注/ZZOI 锁定拦截：request_exit 已弹提示，不得再 quit 绕过锁定。
            logger.info("退出被专注模式锁定拦截")
        except Exception as e:
            logger.warning(f"Exit flow 未就绪: {e}")
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(None, "退出失败", f"退出流程异常: {e}")
            except Exception:
                pass
        finally:
            if btn is not None:
                btn.setEnabled(True)

    def _mute_clicked(self):
        btn = self._buttons.get("mute")
        if btn is not None:
            btn.setEnabled(False)
        try:
            from core.mute_mode import toggle_mute_mode
            toggle_mute_mode()
        except Exception as e:
            logger.warning(f"静音模式未就绪: {e}")
        finally:
            if btn is not None:
                def _reenable():
                    try:
                        btn.setEnabled(True)
                    except RuntimeError:
                        pass
                QTimer.singleShot(300, _reenable)

    def _on_mute_mode_changed(self, enabled):
        mute_btn = self._buttons.get("mute")
        if mute_btn is not None:
            mute_btn.setChecked(enabled)

    def _on_focus_state_changed(self, *args):
        self._update_focus_button_svg()

    def _update_focus_button_svg(self):
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            btn = self._buttons.get("focus")
            if btn is None:
                return
            btn.set_svg(SVG_BELL_OFF if eng.is_active else SVG_BELL)
        except Exception:
            pass

    def closeEvent(self, event):
        global _FLOAT_SIDEBAR_HOTKEY_LISTENER, _FLOAT_SIDEBAR_BRIDGE
        if _FLOAT_SIDEBAR_HOTKEY_LISTENER is not None:
            try:
                _FLOAT_SIDEBAR_HOTKEY_LISTENER.stop()
            except Exception:
                pass
            _FLOAT_SIDEBAR_HOTKEY_LISTENER = None
        if _FLOAT_SIDEBAR_BRIDGE is not None:
            try:
                _FLOAT_SIDEBAR_BRIDGE.deleteLater()
            except Exception:
                pass
            _FLOAT_SIDEBAR_BRIDGE = None
        if self._mute_observer is not None:
            try:
                from core.mute_mode import remove_state_observer
                remove_state_observer(self._mute_observer)
            except Exception:
                pass
            self._mute_observer = None
        # round48：与 sidebar_v2 对齐，断开全局 FocusEngine 信号，防止销毁后被触发
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            if eng is not None:
                eng.focus_started.disconnect(self._on_focus_state_changed)
                eng.focus_ended.disconnect(self._on_focus_state_changed)
        except (TypeError, RuntimeError):
            pass
        except Exception:
            pass
        super().closeEvent(event)
