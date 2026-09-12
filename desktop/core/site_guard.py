"""OISystem 网站/窗口管控模块。

职责：
- 根据配置白名单/黑名单识别问题窗口/页面
- 对命中黑名单的窗口发送关闭信号（WM_CLOSE）
- 与 screen_analyzer 集成：AI 识别到娱乐/游戏/八卦页面时同样触发
- 记录日志，供后续审计

设计：
- 单例模式，与 screen_analyzer 同生命周期
- 仅在 Windows 平台执行窗口关闭；其他平台只做检测与日志
- 3 秒冷却，避免同一窗口被反复关闭
- 白名单优先，避免误关学习/IDE/OJ 窗口
"""
import time
from typing import Optional

from config.settings import ConfigManager
from utils.helpers import logger, log_event


# AI activity 描述中的危险关键词（作为域名黑名单的补充）
_RISK_ACTIVITY_KEYWORDS = [
    "bilibili", "哔哩哔哩", "抖音", "douyin", "youtube", "微博", "weibo",
    "知乎", "zhihu", "贴吧", "tieba", "reddit", "twitter", "x.com",
    "游戏", "game", "娱乐", "八卦", "综艺", "电视剧", "电影",
    "小说", "漫画", "manga", "聊天", "qq", "微信", "wechat",
]


def _as_list(value) -> list:
    """配置列表安全化：非 list 返回空 list；list 返回仅含字符串元素的新列表（拷贝）。

    拷贝防止调用方（如 _is_whitelisted 的 extend）原地污染全局配置列表；
    过滤非字符串元素防止脏配置（如数字）在 item.lower() 处抛异常导致管控静默失效。
    """
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]


class SiteGuard:
    """窗口/网站管控器（单例）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.cfg = ConfigManager()
        self._last_enforce_at = 0.0
        self._cooldown_sec = 3.0  # 同一窗口不重复关闭

    def enforce(self, screen_result: Optional[dict] = None):
        """执行一次窗口检查与关闭。

        优先检查当前活动窗口；同时参考屏幕分析结果中的 activity。
        命中黑名单且未在白名单内时，发送 WM_CLOSE 关闭窗口。
        """
        screen_result = screen_result if isinstance(screen_result, dict) else {}
        now = time.time()
        if now - self._last_enforce_at < self._cooldown_sec:
            return
        self._last_enforce_at = now

        try:
            title, exe, hwnd = self._get_active_window_info()
        except Exception as e:
            logger.debug(f"获取活动窗口信息失败: {e}")
            return

        # 组合待检测文本：窗口标题 + 进程名 + AI 活动描述
        activity = screen_result.get("activity", "")
        if not isinstance(activity, str):
            activity = "" if activity is None else str(activity)
        combined = " ".join([
            title or "", exe or "", activity
        ]).lower()

        # 先过白名单
        if self._is_whitelisted(combined, title, exe):
            return

        # 黑名单命中
        matched = self._match_blacklist(combined, title, exe)
        if not matched:
            return

        # 非专注模式下按风险评分阈值（site_risk_score_threshold）决定是否关闭；
        # 专注模式下命中即关闭（严格管控，毫不留情）。
        if not self._is_focus_active():
            try:
                threshold = int(getattr(self.cfg.settings, "site_risk_score_threshold", 60))
            except (TypeError, ValueError):
                threshold = 60
            # 0/负数 → 命中即关；>100 → 非专注模式不拦截（评分上限 100，阈值 >100 恒不命中）
            threshold = max(0, threshold)
            score = self._risk_score(combined)
            if score < threshold:
                logger.debug(
                    f"非专注模式风险评分 {score} < 阈值 {threshold}，跳过关闭 ({matched})"
                )
                return

        self._close_window(hwnd, title, exe, matched, screen_result)

    def _is_focus_active(self) -> bool:
        """检查全局 FocusEngine 是否激活。专注模式下无条件关闭问题窗口。"""
        try:
            from ui.focus_view import _get_global_engine
            return _get_global_engine().is_active
        except Exception:
            return False

    def _risk_score(self, combined: str) -> int:
        """按命中规则计算风险评分（0-100）。

        权重：游戏域 70 > 显式黑名单 60 > 资讯八卦域 40 > 活动关键词 30。
        多命中累加，上限 100。用于非专注模式的关闭阈值判定。
        """
        score = 0
        for item in _as_list(self.cfg.settings.site_game_domains):
            if item and item.lower() in combined:
                score += 70
        for item in _as_list(self.cfg.settings.site_blacklist):
            if item and item.lower() in combined:
                score += 60
        for item in _as_list(self.cfg.settings.site_news_domains):
            if item and item.lower() in combined:
                score += 40
        for kw in _RISK_ACTIVITY_KEYWORDS:
            if kw and kw.lower() in combined:
                score += 30
        return min(100, score)

    def _get_active_window_info(self) -> tuple:
        """返回 (title, exe, hwnd)。"""
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "", "", 0
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ""
        try:
            import psutil
            exe = psutil.Process(pid).name()
        except Exception:
            pass
        return title, exe, hwnd

    def _is_whitelisted(self, combined: str, title: str, exe: str) -> bool:
        """判断当前窗口是否在白名单内。"""
        mode = self.cfg.settings.focus_mode
        if mode == "study":
            whitelist = (
                _as_list(self.cfg.settings.site_whitelist)
                + _as_list(self.cfg.settings.study_site_whitelist)
            )
        else:
            whitelist = _as_list(self.cfg.settings.site_whitelist)

        # 永远放行 OISystem 自身及相关进程
        whitelist.extend([
            "OISystem", "oisystem", "python",
            "pycharm", "pycharm64", "pycharm32",
            "vscode", "visual studio code", "code.exe",
            "cursor", "cursor.exe",
            "clion", "idea", "webstorm",
        ])

        # 放行系统进程与辅助工具
        system_exes = {
            "explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
            "taskmgr.exe", "textinputhost.exe", "widgetservice.exe",
        }
        exe_lower = (exe or "").lower()
        if exe_lower in system_exes:
            return True

        # 检查组合文本、标题、进程名任一命中白名单
        haystack = f"{combined} {title or ''} {exe_lower}".lower()
        for item in whitelist:
            if item and item.lower() in haystack:
                return True
        return False

    def _match_blacklist(self, combined: str, title: str, exe: str) -> Optional[str]:
        """返回命中的黑名单项；未命中返回 None。"""
        blacklists = {
            "site_blacklist": _as_list(self.cfg.settings.site_blacklist),
            "site_game_domains": _as_list(self.cfg.settings.site_game_domains),
            "site_news_domains": _as_list(self.cfg.settings.site_news_domains),
        }
        for name, items in blacklists.items():
            for item in items:
                if item and item.lower() in combined:
                    return f"{name}:{item}"
        for kw in _RISK_ACTIVITY_KEYWORDS:
            if kw and kw.lower() in combined:
                return f"activity:{kw}"
        return None

    def _close_window(self, hwnd, title: str, exe: str, matched: str,
                      screen_result: dict):
        """发送 WM_CLOSE 关闭窗口，并记录日志。"""
        reason = f"命中管控规则 {matched}"
        try:
            import win32gui
            import win32con
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            logger.warning(f"已关闭问题窗口: {title} ({exe}) {reason}")
            log_event("site_guard_enforced", {
                "title": title,
                "exe": exe,
                "matched": matched,
                "activity": screen_result.get("activity", ""),
            })
        except Exception as e:
            logger.warning(f"关闭窗口失败: {title} ({exe}) {e}")
