"""OISystem 配置系统。

基于 zzoi/config/settings.py 思路扩展为 OISystem 全功能配置：
- 五大类设置：屏幕检测 / AI 对话 / 专注模式 / 网站管控 / API
- 敏感字段存 secrets.json，非敏感存 config.json
- 单例 ConfigManager，支持观察者
"""
import os
import threading
from dataclasses import dataclass, field, asdict, fields, MISSING
from typing import Optional
from utils.helpers import DATA_DIR, load_json, save_json

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")

# round55：课堂音频感知总开关（源码级）。
# 关闭时 ClassroomMonitor 拒绝启动、demo 直接提示、UI 入口一并隐藏；
# 运行期细粒度开关走 AppSettings.classroom_* / interrupt_*。
ENABLE_CLASSROOM_AUDIO = True


def _coerce_bool(value) -> bool:
    """把常见真/假写法归一化为 bool；无法识别时按真值性判断。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "on", "y"):
            return True
        if s in ("false", "0", "no", "off", "n", ""):
            return False
    return bool(value)


# round48：int 字段业务范围，既防止超 C++ int 的 OverflowError，
# 也防止 0/负数/天文数字进入引擎逻辑。
_INT_RANGES = {
    "focus_duration_minutes": (1, 525600),
    "focus_auto_analyze_interval_sec": (30, 86400),
    "focus_stuck_threshold": (1, 100),
    "classroom_sync_interval_min": (5, 720),
    "screen_capture_interval_sec": (30, 86400),
    "screen_max_width": (0, 4096),
    "ai_dialog_max_rounds": (1, 500),
    "ai_dialog_max_context_blocks": (1, 100),
    "ai_supervisor_interval_rounds": (1, 100),
    "ai_unlimited_chat_max_minutes": (1, 1440),
    "site_risk_score_threshold": (0, 2147483647),
    "mail_smtp_port": (1, 65535),
    "external_ai_remind_cooldown_min": (1, 1440),
    # round55：课堂音频感知与打断引擎
    "classroom_asr_window": (1, 60),
    # 上界与 classroom_stream.MAX_TRANSCRIBE_SEC=30 对齐：合法配置 60s 时
    # 超 30s 的段会被截尾、前 30s 内容静默丢失，违背强制切段"内容不丢"承诺（M3）
    "classroom_max_segment_sec": (2, 30),
    "classroom_buffer_sec": (3, 300),
    "classroom_vad_end_silence_ms": (100, 5000),
    "classroom_vad_min_speech_ms": (50, 3000),
    # 上界 -10 而非 0：0dB 会让 VAD 阈值恒为满幅 1.0，一切语音全切不出段，
    # 整个听觉通道静默失效（M2）
    "classroom_vad_abs_floor_db": (-90, -10),
    "interrupt_cooldown_sec": (0, 3600),
    "interrupt_min_teacher_segments": (1, 20),
    "interrupt_decision_interval_sec": (5, 3600),
}


# round55：float 字段业务范围（VAD 噪声倍数 / 打断置信度阈值）
_FLOAT_RANGES = {
    "classroom_vad_noise_ratio": (1.05, 20.0),
    "interrupt_confidence_min": (0.0, 1.0),
}

# round55：打断分级开关合法值（越权值一律回落到最保守的 on_error）
INTERRUPT_LEVELS = ("off", "on_error", "on_unclear")


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
    focus_mode: str = "oi"                              # oi / study（OI 信息学竞赛 / 学习文化课）
    focus_duration_minutes: int = 30
    focus_emergency_exit_allowed: bool = True
    focus_auto_analyze_interval_sec: int = 300      # 自动分析触发间隔
    focus_stuck_threshold: int = 3                   # 连续 N 次无进展触发 AI 分析
    focus_lock_on_no_submission: bool = True         # 当日零提交自动锁专注
    focus_lock_on_rank_tail: bool = True             # 排行榜末尾自动锁专注
    # 做题退出 v2：OI 模式正常退出需做掉一道 ZZOI 真实题目（AC 后放行）
    problem_exit_enabled: bool = True
    # 学习模式下默认放行的网课/学习类域名（与 OI 白名单独立维护）
    study_site_whitelist: list = field(default_factory=lambda: [
        "ke.qq.com", "iclass.qq.com", "mooc.cn", "icourse163.org",
        "xuetangx.com", "study.163.com", "chaoxing.com", "classin.com",
        "zhihuishan.com", "learning.gov.cn", "xuexi.cn", "bilibili.com/cheese",
        "bilibili.com/medialist", "coursera.org", "edx.org", "khanacademy.org",
        "google.com/docs", "google.com/forms", "office.com", "wps.cn",
    ])

    # ========== 课堂感知（round55：网课音频双通道 + VAD + ASR） ==========
    classroom_audio_enabled: bool = True             # 课堂音频采集总开关（运行期）
    classroom_capture_loopback: bool = True          # 采集系统音频（网课老师声音）
    classroom_capture_mic: bool = True               # 采集麦克风（学生提问）
    classroom_buffer_sec: int = 30                   # 每路内存环形缓冲秒数（超限丢最旧）
    classroom_asr_window: int = 12                   # 决策可用的最近转写条数
    classroom_max_segment_sec: int = 15              # 单段语音硬上限（超时强制切分）
    classroom_vad_end_silence_ms: int = 600          # 段尾静音多久收段
    classroom_vad_min_speech_ms: int = 250           # 语音段最短成立时长（防咳嗽误触发）
    classroom_vad_noise_ratio: float = 2.5           # 语音阈值 = 噪声底 × 该倍数
    classroom_vad_abs_floor_db: int = -50            # 噪声底绝对下限（dBFS）
    classroom_persist_transcript: bool = True        # 转写文字按天落盘（原始音频绝不落盘）
    # ========== 成果互通（round57） ==========
    classroom_sync_enabled: bool = False             # 课堂笔记同步开关（涉及上传，默认关，用户显式开启）
    sync_server_url: str = "http://8.138.12.209:8000"  # 学习 Agent 服务器地址
    sync_server_password: str = ""                   # 学习 Agent 访问密码（X-Auth-Token；拉题讲解/同步共用）
    classroom_sync_interval_min: int = 30            # 同步间隔（分钟）

    # ========== 打断决策引擎（round55） ==========
    interrupt_level: str = "on_error"                # off / on_error / on_unclear（默认保守）
    interrupt_cooldown_sec: int = 180                # 两次主动打断最小间隔
    interrupt_confidence_min: float = 0.6            # AI 决策置信度下限，低于则不打断
    interrupt_min_teacher_segments: int = 3          # 至少积累几段老师讲解才允许决策
    interrupt_decision_interval_sec: int = 45        # 决策轮询最小间隔（省 token）
    interrupt_speak: bool = True                     # 打断时是否出声朗读（TTS）

    # ========== 屏幕检测 ==========
    screen_capture_interval_sec: int = 60            # 截图分析频率
    screen_capture_region: str = "fullscreen"        # fullscreen / active_window / custom
    screen_custom_rect: list = field(default_factory=lambda: [0, 0, 1920, 1080])
    screen_engine: str = "glm-4.6v"                  # 视觉决策引擎
    screen_quality: str = "low"                      # low / medium / high
    screen_max_width: int = 1280                     # 发送前压到最大宽度
    # P1 修复：把外部 AI 提醒的冷却时间从代码里默认提到配置项，让用户能在设置中心调整
    external_ai_remind_cooldown_min: int = 5         # 外部 AI 提醒间隔（分钟）

    # ========== AI 对话 ==========
    ai_dialog_model: str = "deepseek-v4-pro"
    ai_dialog_base_url: str = "https://api.deepseek.com/v1"
    ai_dialog_max_rounds: int = 50
    ai_dialog_max_context_blocks: int = 10
    ai_dialog_extra_rules: str = ""                  # 在默认 prompt 基础上追加的限制
    ai_flash_model: str = "deepseek-flash"           # 用于上下文裁剪/闲聊判定
    ai_supervisor_interval_rounds: int = 5           # 每 N 轮元监督一次
    ai_unlimited_chat_max_minutes: int = 15          # 非专注模式 AI 对话上限（仅提醒，不掐断）

    # ========== 网站管控 ==========
    site_whitelist: list = field(default_factory=_default_site_whitelist)
    site_blacklist: list = field(default_factory=_default_site_blacklist)
    site_news_domains: list = field(default_factory=_default_news_domains)
    site_game_domains: list = field(default_factory=_default_game_domains)
    site_risk_score_threshold: int = 60              # 非专注模式下危险评分阈值

    # ========== 系统集成 ==========
    mute_hotkey: str = "ctrl+shift+q"                # 静音模式快捷键
    ask_hotkey: str = "ctrl+alt+a"                   # R23：随时提问（打断网课并讲解）
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
    # r41 修复：note.ms 已被 Cloudflare 防护，默认关闭，避免每次同步都报 403
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

    # ========== API: XIAOMI MiMo (Token Plan 讲题对话主力 + TTS) ==========
    # Token Plan 专用 URL（tp- 前缀密钥）；按量付费 URL 为 https://api.xiaomimimo.com/v1
    xiaomi_enabled: bool = True
    xiaomi_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    xiaomi_model: str = "mimo-v2.5-pro"              # 推理/对话旗舰（V2 系列 2026-06-30 已下线）
    xiaomi_role: str = "dialog"                      # knowledge / vision / dialog
    # MiMo-V2.5-TTS 讲题朗读
    xiaomi_tts_model: str = "mimo-v2.5-tts"
    xiaomi_tts_voice: str = "mimo_default"           # 内置音色：mimo_default/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean
    tts_auto_read: bool = False                      # AI 回复后自动朗读
    # 语音转写（ASR）：课堂音频 → 文字。实测 input_audio 契约验证用的是 mimo-v2.5，
    # 与对话用的 xiaomi_model(mimo-v2.5-pro) 单列，避免未验证的 -pro 名吃掉整条链路。
    xiaomi_asr_model: str = "mimo-v2.5"
    # xiaomi_api_key 存 secrets

    # ========== 敏感字段（运行时合并自 secrets.json） ==========
    mail_password: str = ""
    zzoi_password: str = ""
    zzoi_sid: str = ""
    zzoi_sid_sig: str = ""
    kimi_api_key: str = ""
    glm_api_key: str = ""
    deepseek_api_key: str = ""
    xiaomi_api_key: str = ""

    def __post_init__(self):
        """round48：配置脏数据（None/str/数字/非 list）统一归一化。

        来自 config.json 的任意合法 JSON 都可能与字段类型不符（例如手改、
        旧版本残留、写入中断）。启动/打开设置前在这里完成类型清洗，避免
        `QSpinBox.setValue(None)`、`max(1, "abc")`、`{**list}` 等运行时崩溃。
        """
        bool_fields = {
            "focus_emergency_exit_allowed", "focus_lock_on_no_submission",
            "focus_lock_on_rank_tail", "problem_exit_enabled",
            "jiyu_snapshot_send", "watchdog_enabled", "watchdog_restart_on_crash",
            "sidebar_expanded", "note_ms_enabled",
            "kimi_enabled", "glm_enabled", "deepseek_enabled",
            "xiaomi_enabled", "tts_auto_read",
            # round55：课堂音频感知与打断引擎
            "classroom_audio_enabled", "classroom_capture_loopback",
            "classroom_capture_mic", "classroom_persist_transcript",
            "interrupt_speak",
            # round57：成果互通
            "classroom_sync_enabled",
        }
        int_fields = {
            "focus_duration_minutes", "focus_auto_analyze_interval_sec",
            "focus_stuck_threshold", "screen_capture_interval_sec",
            "screen_max_width", "ai_dialog_max_rounds",
            "ai_dialog_max_context_blocks", "ai_supervisor_interval_rounds",
            "ai_unlimited_chat_max_minutes", "site_risk_score_threshold",
            "mail_smtp_port", "external_ai_remind_cooldown_min",
            # round55
            "classroom_buffer_sec", "classroom_asr_window",
            "classroom_max_segment_sec", "classroom_vad_end_silence_ms",
            "classroom_vad_min_speech_ms", "classroom_vad_abs_floor_db",
            "interrupt_cooldown_sec", "interrupt_min_teacher_segments",
            "interrupt_decision_interval_sec",
            # round57
            "classroom_sync_interval_min",
        }
        float_fields = {
            "classroom_vad_noise_ratio", "interrupt_confidence_min",
        }
        list_fields = {
            "screen_custom_rect", "study_site_whitelist", "site_whitelist",
            "site_blacklist", "site_news_domains", "site_game_domains",
            "selected_svgs", "mail_receivers",
        }
        dict_fields = {"theme_custom_data"}

        for f in fields(self):
            name = f.name
            value = getattr(self, name)
            if name == "focus_mode":
                if value not in ("oi", "study"):
                    setattr(self, name, "oi")
                # 处理完直接进入下一字段，避免后续字符串兜底用旧 value 覆盖归一化结果
                continue
            if name == "interrupt_level":
                # round55：越权/脏值一律回落到最保守档，绝不因配置错误变成激进打断
                lvl = str(value or "").strip().lower()
                setattr(self, name, lvl if lvl in INTERRUPT_LEVELS else "on_error")
                continue
            if name in bool_fields:
                try:
                    setattr(self, name, _coerce_bool(value))
                except Exception:
                    setattr(self, name, True)
            elif name in int_fields:
                try:
                    normalized = int(value)
                except (TypeError, ValueError, OverflowError):
                    normalized = f.default if f.default is not MISSING else 0
                lo, hi = _INT_RANGES.get(name, (-2147483647, 2147483647))
                setattr(self, name, max(lo, min(hi, normalized)))
            elif name in float_fields:
                # round55：float 字段同样归一化（NaN/Inf/字符串/None 均回退默认值再钳制）
                try:
                    normalized = float(value)
                    if normalized != normalized or normalized in (float("inf"), float("-inf")):
                        raise ValueError("non-finite")
                except (TypeError, ValueError, OverflowError):
                    normalized = f.default if f.default is not MISSING else 0.0
                lo, hi = _FLOAT_RANGES.get(name, (-1e9, 1e9))
                setattr(self, name, max(lo, min(hi, normalized)))
            elif name in list_fields:
                if not isinstance(value, list):
                    if f.default_factory is not MISSING:
                        value = f.default_factory()
                    else:
                        value = f.default if f.default is not MISSING else []
                    setattr(self, name, value)
                else:
                    # round48：列表元素同样归一化，防止 ['0','0'] 进入 grabWindow
                    # 或 [1,2] 进入 ", ".join() 造成功能静默失败
                    if name == "screen_custom_rect":
                        try:
                            rect = [int(x) for x in value]
                            if len(rect) != 4:
                                raise ValueError
                            setattr(self, name, rect)
                        except (TypeError, ValueError, OverflowError):
                            setattr(self, name, [0, 0, 1920, 1080])
                    elif name == "mail_receivers":
                        setattr(self, name, [
                            str(x).strip() for x in value
                            if x is not None and str(x).strip()
                        ])
                    else:
                        # site_* / study_* / selected_svgs：只保留字符串元素
                        setattr(self, name, [x for x in value if isinstance(x, str)])
            elif name in dict_fields:
                if not isinstance(value, dict):
                    if f.default_factory is not MISSING:
                        value = f.default_factory()
                    else:
                        value = {}
                    setattr(self, name, value)
            elif f.type is str or str(f.type) == "<class 'str'>":
                # 其余字符串字段：None → 空串，非字符串统一 str()
                try:
                    setattr(self, name, "" if value is None else str(value))
                except Exception:
                    setattr(self, name, "")

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
    "xiaomi_api_key",
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
        # round48 P1：顶层为合法 JSON 非对象（[]/"str"/123）时不能 {**data} 崩溃
        if not isinstance(data, dict):
            try:
                from utils.helpers import logger
                logger.warning(f"config.json 顶层类型异常（{type(data).__name__}），按空配置自愈")
            except Exception:
                pass
            data = {}
        if not isinstance(secrets, dict):
            try:
                from utils.helpers import logger
                logger.warning(f"secrets.json 顶层类型异常（{type(secrets).__name__}），按空配置自愈")
            except Exception:
                pass
            secrets = {}
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
        # round48：观察者异常不能打断其他观察者通知，也不能让上层误以为保存失败
        for obs in list(self._observers):
            try:
                obs.on_config_changed()
            except Exception:
                try:
                    from utils.helpers import logger
                    logger.warning(f"配置观察者回调失败: {obs!r}", exc_info=True)
                except Exception:
                    pass

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self.save()

    def get(self, key, default=None):
        return getattr(self.settings, key, default)

    def add_observer(self, observer):
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(self, observer):
        if observer in self._observers:
            self._observers.remove(observer)