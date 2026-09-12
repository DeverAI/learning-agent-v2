"""OISystem 配置系统。

基于 zzoi/config/settings.py 思路扩展为 OISystem 全功能配置：
- 五大类设置：屏幕检测 / AI 对话 / 专注模式 / 网站管控 / API
- 敏感字段存 secrets.json，非敏感存 config.json
- 单例 ConfigManager，支持观察者
"""
import os
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
from utils.helpers import DATA_DIR, load_json, save_json

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")


def _default_site_whitelist() -> list:
    # 专注模式默认白名单（OJ/IDE/文档类）
    return [
        "zzoi.com.cn", "luogu.com", "codeforces.com", "atcoder.jp",
        "vijos.org", "leetcode.cn", "nowcoder.com",
        "visualstudio.com", "vscode.dev", "github.com",
    ]


def _default_site_blacklist() -> list:
    return [
        "bilibili.com", "douyin.com", "youtube.com", "weibo.com",
        "zhihu.com", "tieba.baidu.com", "reddit.com", "twitter.com",
    ]


def _default_news_domains() -> list:
    return ["news.", "资讯", "sspai.com", "ithome.com", "36kr.com"]


def _default_game_domains() -> list:
    return ["game", "games", "4399.com", "7k7k.com", "steam.com", "epic.com"]


@dataclass
class AppSettings:
    # ========== 专注模式 ==========
    focus_duration_minutes: int = 30
    focus_emergency_exit_allowed: bool = True
    focus_auto_analyze_interval_sec: int = 300      # 自动分析触发间隔
    focus_stuck_threshold: int = 3                   # 连续 N 次无进展触发 AI 分析
    focus_lock_on_no_submission: bool = True         # 当日零提交自动锁专注
    focus_lock_on_rank_tail: bool = True             # 排行榜末尾自动锁专注

    # ========== 屏幕检测 ==========
    screen_capture_interval_sec: int = 60            # 截图分析频率
    screen_capture_region: str = "fullscreen"        # fullscreen / active_window / custom
    screen_custom_rect: list = field(default_factory=lambda: [0, 0, 1920, 1080])
    screen_engine: str = "glm-4.6v"                  # 视觉决策引擎
    screen_quality: str = "low"                      # low / medium / high
    screen_max_width: int = 1280                     # 发送前压到最大宽度

    # ========== AI 对话 ==========
    ai_dialog_model: str = "deepseek-v4-pro"
    ai_dialog_base_url: str = "https://api.deepseek.com/v1"
    ai_dialog_max_rounds: int = 50
    ai_dialog_max_context_blocks: int = 10

    # ========== 网站管控 ==========
    site_whitelist: list = field(default_factory=_default_site_whitelist)
    site_blacklist: list = field(default_factory=_default_site_blacklist)
    site_news_domains: list = field(default_factory=_default_news_domains)
    site_game_domains: list = field(default_factory=_default_game_domains)
    site_risk_score_threshold: int = 60              # 非专注模式下危险评分阈值

    # ========== 系统集成 ==========
    mute_hotkey: str = "ctrl+shift+q"                # 静音模式快捷键
    jiyu_snapshot_path: str = r"C:\Users\Public\Documents\极域课堂管理系统软件V6.0 2016 豪华版\Snapshots"
    jiyu_snapshot_send: bool = True                  # 是否打包发送极域快照
    watchdog_enabled: bool = True
    watchdog_restart_on_crash: bool = True

    # ========== 侧边栏状态持久化 ==========
    sidebar_style: str = "trapezoid"                 # trapezoid/rect/full/float
    sidebar_edge: str = "right"                      # left/right/top/bottom
    sidebar_expanded: bool = False
    sidebar_float_hotkey: str = "ctrl+shift+s"       # 悬浮式快捷键

    # ========== 主题 ==========
    theme: str = "black"
    theme_custom_data: dict = field(default_factory=dict)

    # ========== SVG 选择器 ==========
    selected_svgs: list = field(default_factory=list)

    # ========== 用户画像 ==========
    user_profile_text: str = ""                      # AI 提取的用户画像描述
    user_profile_updated_at: str = ""                # 上次更新时间

    # ========== 邮件 ==========
    mail_smtp_server: str = "smtp.163.com"
    mail_smtp_port: int = 465
    mail_sender: str = ""
    mail_receivers: list = field(default_factory=list)  # 多收件人
    # mail_password 存 secrets

    # ========== note.ms 同步 ==========
    # r41 修复：note.ms 已被 Cloudflare 防护，默认关闭
    note_ms_enabled: bool = False
    note_ms_base_url: str = "https://note.ms"
    note_ms_suffix: str = "OISYSTEM"                 # 拼成 note.ms/YYYYMMDDOISYSTEM

    # ========== ZZOI ==========
    zzoi_base_url: str = "https://zzoi.com.cn"
    zzoi_domain_prefix: str = "d/ZZOI"
    zzoi_uid: str = ""
    # zzoi_password / sid / sid_sig 存 secrets

    # ========== API: KIMI (默认最便宜视觉模型，知识检索) ==========
    kimi_enabled: bool = True
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "moonshot-v1-8k-vision-preview"
    kimi_role: str = "knowledge"                     # knowledge / vision / dialog
    # kimi_api_key 存 secrets

    # ========== API: GLM (默认 4.6V，视觉决策) ==========
    glm_enabled: bool = True
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4.6v"
    glm_role: str = "vision"                         # vision / dialog
    # glm_api_key 存 secrets

    # ========== API: DEEPSEEK (默认 v4-pro，对话主力) ==========
    deepseek_enabled: bool = True
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_role: str = "dialog"                    # dialog / vision
    # deepseek_api_key 存 secrets

    # ========== 敏感字段（运行时合并自 secrets.json） ==========
    mail_password: str = ""
    zzoi_password: str = ""
    zzoi_sid: str = ""
    zzoi_sid_sig: str = ""
    kimi_api_key: str = ""
    glm_api_key: str = ""
    deepseek_api_key: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        valid_keys = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


# 敏感字段清单：保存时拆到 secrets.json
SENSITIVE_KEYS = {
    "mail_password",
    "zzoi_uid", "zzoi_password", "zzoi_sid", "zzoi_sid_sig",
    "kimi_api_key", "glm_api_key", "deepseek_api_key",
}


class ConfigManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.settings = AppSettings()
        self._observers = []
        self._load()

    def _load(self):
        data = load_json(CONFIG_FILE, {})
        secrets = load_json(SECRETS_FILE, {})
        merged = {**data, **secrets}
        self.settings = AppSettings.from_dict(merged)

    def save(self):
        public_data = {}
        secrets_data = {}
        for k, v in self.settings.to_dict().items():
            if k in SENSITIVE_KEYS:
                secrets_data[k] = v
            else:
                public_data[k] = v
        save_json(CONFIG_FILE, public_data)
        save_json(SECRETS_FILE, secrets_data)
        self._notify()

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self.save()

    def is_api_configured(self, provider: str) -> bool:
        """provider: kimi/glm/deepseek"""
        key = f"{provider}_api_key"
        return bool(getattr(self.settings, key, ""))

    def add_observer(self, callback):
        self._observers.append(callback)

    def _notify(self):
        for cb in self._observers:
            try:
                cb(self.settings)
            except Exception:
                pass
