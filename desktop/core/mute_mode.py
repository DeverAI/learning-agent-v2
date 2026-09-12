"""OISystem 静音模式。

CTRL+SHIFT+Q 后所有弹窗隐藏，日志记录正常，关闭页面功能正常。
快捷键可在设置中修改。
"""
from utils.helpers import logger, log_event, format_time, now_cst

try:
    from PySide6.QtCore import QObject, Slot, QMetaObject, Qt
    _HAS_QT = True
except Exception:
    _HAS_QT = False


_MUTE_MODE = False
_HOTKEY_LISTENER = None
_TOASTS = []          # 当前活跃的弹窗引用
_HIDDEN_TOASTS = []   # 本次静音模式开启期间被隐藏的弹窗
_STATE_OBSERVERS = []  # 状态变化回调（sidebar 按钮高亮等）
_bridge = None         # 主线程桥：把静音切换 marshal 回主线程


class _MuteToggleBridge(QObject):
    """把静音切换 marshal 回主线程执行，避免在 pynput 监听线程直接操作 QWidget。"""

    @Slot()
    def _do_toggle(self):
        toggle_mute_mode()


def _dispatch_toggle_to_main_thread():
    """从任意线程安全触发静音切换。

    pynput 全局热键在监听线程回调，直接调用 toggle_mute_mode 会在非主线程操作
    QWidget（toast.hide/show、sidebar 按钮 setChecked），存在崩溃风险。此处用
    QueuedConnection 把切换投递回 bridge 所在线程（主线程）执行。

    注意：bridge 只允许在 start_global_hotkey（主线程）中创建；若 bridge 尚未
    创建（start_global_hotkey 未调用或创建失败），若在监听线程里临时创建 QObject，
    QueuedConnection 会投递到无事件循环的监听线程导致切换静默失效，因此此时
    直接切换兜底（宁可冒跨线程风险也不能让热键静默失效）。
    """
    global _bridge
    if not _HAS_QT or _bridge is None:
        toggle_mute_mode()
        return
    try:
        QMetaObject.invokeMethod(_bridge, "_do_toggle", Qt.QueuedConnection)
    except Exception:
        toggle_mute_mode()


def is_mute_mode() -> bool:
    return _MUTE_MODE


def add_state_observer(callback):
    """注册静音模式状态变化回调，参数为 enabled: bool。"""
    _STATE_OBSERVERS.append(callback)


def remove_state_observer(callback):
    """注销静音模式状态变化回调。"""
    try:
        _STATE_OBSERVERS.remove(callback)
    except ValueError:
        pass


def _notify_state_changed():
    # r39 P1 修复：回调 cb 可能调用 remove_state_observer(cb) 自注销，
    # 修改正在迭代的列表会抛 RuntimeError。改为快照迭代。
    for cb in list(_STATE_OBSERVERS):
        try:
            cb(_MUTE_MODE)
        except Exception:
            pass


def toggle_mute_mode():
    global _MUTE_MODE
    _MUTE_MODE = not _MUTE_MODE
    logger.warning(f"静音模式: {'开启' if _MUTE_MODE else '关闭'}")
    log_event("mute_mode_toggle", {
        "enabled": _MUTE_MODE,
        "ts": format_time(now_cst()),
    })
    if _MUTE_MODE:
        _HIDDEN_TOASTS.clear()
        for t in _TOASTS:
            try:
                if t.isVisible():
                    t.hide()
                    _HIDDEN_TOASTS.append(t)
            except Exception:
                pass
    else:
        for t in _HIDDEN_TOASTS:
            try:
                t.show()
            except Exception:
                pass
        _HIDDEN_TOASTS.clear()
    _notify_state_changed()
    return _MUTE_MODE


def register_toast(toast):
    """注册弹窗，静音模式开启时立即隐藏。"""
    _TOASTS.append(toast)
    # 清理已关闭/销毁的弹窗引用，避免 _TOASTS 随会话时长无界增长
    _TOASTS[:] = [t for t in _TOASTS if _toast_alive(t)]
    if _MUTE_MODE:
        try:
            toast.hide()
            _HIDDEN_TOASTS.append(toast)
        except Exception:
            pass


def _toast_alive(t) -> bool:
    """判断 toast 的 C++ 对象是否仍存活（关闭后返回 False，供清理）。"""
    try:
        from shiboken6 import isValid
        return isValid(t)
    except ImportError:
        try:
            t.isVisible()
            return True
        except RuntimeError:
            return False


def start_global_hotkey(hotkey_str: str = "ctrl+shift+q"):
    """启动全局热键监听。"""
    global _HOTKEY_LISTENER, _bridge
    # 全量检修修复：重复调用时先停止旧监听器，避免 pynput 监听线程泄漏
    if _HOTKEY_LISTENER is not None:
        try:
            _HOTKEY_LISTENER.stop()
        except Exception:
            pass
        _HOTKEY_LISTENER = None
    # 在主线程提前创建 bridge，确保 QObject 线程归属是主线程（本函数由 main.py 主线程调用）
    if _HAS_QT and _bridge is None:
        try:
            _bridge = _MuteToggleBridge()
        except Exception:
            _bridge = None
    try:
        from pynput import keyboard as pynput_keyboard

        # pynput GlobalHotKeys 要求格式 "<ctrl>+<shift>+q"
        modifiers = {"ctrl", "alt", "shift", "cmd", "win"}
        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        if len(parts) > 1:
            mod_str = "+".join(f"<{p}>" for p in parts[:-1] if p in modifiers)
            pynput_str = f"{mod_str}+{parts[-1]}" if mod_str else parts[-1]
        else:
            pynput_str = parts[0]

        def _on_triggered():
            # 监听线程回调：把 Qt 操作 marshal 回主线程，避免跨线程操作 QWidget
            _dispatch_toggle_to_main_thread()

        _HOTKEY_LISTENER = pynput_keyboard.GlobalHotKeys({
            pynput_str: _on_triggered
        })
        _HOTKEY_LISTENER.daemon = True
        _HOTKEY_LISTENER.start()
        logger.info(f"静音模式全局热键已注册: {hotkey_str}")
    except ImportError:
        logger.warning("pynput 未安装，静音模式热键不可用")
    except Exception as e:
        logger.warning(f"静音模式热键注册失败: {e}")


def stop_global_hotkey():
    global _HOTKEY_LISTENER
    if _HOTKEY_LISTENER is not None:
        try:
            _HOTKEY_LISTENER.stop()
        except Exception:
            pass
        _HOTKEY_LISTENER = None
