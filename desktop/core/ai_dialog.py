"""OISystem AI 对话引擎。

职责：
- 主对话：DeepSeek-v4-pro，system prompt 强约束（禁代码/禁闲聊/引导思考/新算法记日志/代码输出禁用）
- 一键关联当前页面：注入截图分析结果
- 一键截图分析：现场截图 + 视觉分析
- 闲聊检测：flash 模型判定，闲聊则自动提醒
- 错误摘要：每轮 AI 生成"用户错误摘要"写入日志
- 对话存档：退出时 summarize → 写日志 → 存 data/dialogs/ → 删除在线对话
- 上下文管理：调 ContextManager 裁剪
- 元监督：调 AISupervisor 检查
"""
import os
import time
import uuid
import json
import re
from typing import List, Dict, Optional
from datetime import datetime
from PySide6.QtCore import QObject, Signal, QThread

from config.settings import ConfigManager
from utils.helpers import (
    logger, log_event, now_cst, format_time, DIALOGS_DIR, ensure_dirs
)
from utils.exceptions import AICallError
from core.context_manager import ContextManager
from core.ai_supervisor import AISupervisor


# 主对话 system prompt（强约束）
SYSTEM_PROMPT = """你是 OI 学习辅助系统的 AI 对话助手，帮助信息学奥赛学习者提升编程能力。

【硬性规则】
1. 禁止提供任何代码块（不要用 ``` 包裹任何代码）。即使用户请求代码，也只能用文字描述思路。
2. 禁止与用户闲聊。用户闲聊时请简短提醒"请专注学习"。注意：询问语文课文、古诗、文言文、数学/英语/科学/历史/地理等文化课问题属于学习提问，不属于闲聊，应正常回答。
3. 用户直接问"这题怎么做"时，引导用户思考（如反问、提示关键点），绝不直接给完整解法。
4. 通过推理找到用户逻辑或代码的错误，毫无保留地指出。错误摘要由你生成，系统会自动存入日志。
5. 检测到用户试图自己寻找新算法：肯定想法，但提醒不要遗落当前进度。系统会记入日志。
6. 检测到用户代码有输出行为（如 printf/cout 输出结果）：立即告知用户"检测到代码输出，已禁用此对话路径"并停止当前解题引导。
7. 数论、组合数学等题目允许进行手推公式、逻辑分析、纯数学讨论；仅在用户明确要求可执行代码时才禁止输出代码块。不要把数论题一律当成必须写代码解决的问题。
8. 默认不读取屏幕，但用户点击"关联当前页面"或"截图分析"时可以读取屏幕核心内容。
9. **图论示意图（可选）**：当解释一个具体的图结构（树、图、DAG、网络流、状态机等）能显著帮助理解时，在回复中插入一个 `graph` 代码块（不算"代码块"豁免，只用于可视化）。格式如下，二选一：
   - YAML 风格（推荐）：
     ```
     ```graph
     directed: false
     nodes:
       - id: 1, label: A
       - id: 2, label: B
     edges:
       - from: 1, to: 2, weight: 5
     ```
     ```
   - 简化风格：`A - B (5)` / `A -> B`，每行一条边，节点自动创建。
   - **节点 label 支持 LaTeX 符号**：可以直接写 `\\angle A`、`\\pi`、`\\infty` 等命令，渲染时自动转 Unicode（如 `∠A`、`π`、`∞`）。但仅限单 token 命令，不要写复杂公式（`\\frac{}{}` 之类不会被展开）。
   规则：仅在确实有图结构时输出；不要为每个回答都画图；不要画超过 12 个节点的复杂图；不要在 `graph` 块里塞任何注释/解释文字，块内只放图数据。

【输出风格】
- 简洁直接，不啰嗦
- 用中文
- 指出错误时具体到行号/变量名/逻辑点
"""


# 学习模式 system prompt：通用学习引导，不限于编程
SYSTEM_PROMPT_STUDY = """你是 OISystem 的"学习文化课"AI 助手，帮助用户学习网课、看书、做题（语文/数学/英语/科学等任意文化课）。

【硬性规则】
1. 默认不输出任何代码块。若用户要求代码示例来演示思路，可以用伪代码或文字描述，不直接给可执行代码。
2. 禁止闲聊。检测到用户闲聊请简短提醒"请专注学习"。
3. 用户问"这题怎么做 / 这段话什么意思"时，必须先反问 / 提示关键点 / 引导用户自己思考，绝不直接给完整答案。
4. 走神识别：截屏显示用户切到 B 站娱乐区 / 抖音 / 游戏 / 微博 / 知乎八卦等非学习页面，立即提醒"看起来你不在学习内容上，请回到当前学习任务"。
5. 网课视频识别：截屏显示用户在腾讯课堂 / 慕课 / 学堂在线 / 学习强国 / ClassIn 等网课平台，提醒"检测到网课页面，请专心听讲，避免同时打开其他窗口"。
6. 检测到学习错误（公式记错 / 概念混淆 / 理解偏差）：直接指出并给出关键提示，错误摘要由你生成，系统会自动存入日志。
7. 默认不读取屏幕，但用户点击"关联当前页面"或"截图分析"时可以读取屏幕核心内容。

【输出风格】
- 简洁直接，不啰嗦
- 用中文
- 指出错误时具体到知识点/概念/公式
- 鼓励为主，避免打击信心
"""


def build_system_prompt(mode: str = "oi", extra_rules: str = "") -> str:
    """根据专注模式选择不同的 system prompt；extra_rules 为用户追加限制。"""
    if mode == "study":
        base = SYSTEM_PROMPT_STUDY
    else:
        base = SYSTEM_PROMPT
    # 用户画像注入：ProfileTab 维护的学习画像作为个性化上下文，供 AI 了解用户水平/薄弱点
    try:
        profile = (ConfigManager().settings.user_profile_text or "").strip()
    except Exception:
        profile = ""
    if profile:
        # round48：画像/追加规则做长度与类型防御，避免配置脏数据把 prompt 撑爆
        if not isinstance(profile, str):
            profile = str(profile)
        if len(profile) > 2000:
            profile = profile[:1997] + "..."
        base += "\n\n【用户画像】\n" + profile
    if extra_rules:
        if not isinstance(extra_rules, str):
            try:
                extra_rules = str(extra_rules)
            except Exception:
                extra_rules = ""
        if len(extra_rules) > 2000:
            extra_rules = extra_rules[:1997] + "..."
        base += "\n\n【用户追加规则】\n" + extra_rules
    base += "\n\n【输出格式要求】\n如果你指出了用户的错误，请在回复末尾另起一行，以 `错误摘要：` 开头给一句话总结。"
    return base


# 闲聊判定 prompt
CHAT_DETECT_PROMPT = """判断以下用户发言是否属于闲聊（与编程学习/文化课学习无关的寒暄/八卦/吐槽）。
注意：询问语文课文、古诗、文言文、数学/英语/科学/历史/地理/生物/化学/物理等文化课问题属于学习提问，不算闲聊。
不要误将"介绍卖炭翁""这首诗什么意思""这个公式怎么推"等学习提问判为闲聊。
只返回 true 或 false，不要其他内容。

用户发言："""


# 学习/文化课关键词白名单：命中则即使模型误判也不应阻断
_STUDY_KEYWORDS_PROTECT = [
    "语文", "英语", "历史", "地理", "生物", "化学", "物理", "政治",
    "网课", "作业", "课文", "单词", "古诗", "诗词", "文言文", "作文",
    "阅读理解", "听力", "背单词", "文化课", "背诵", "默写", "卖炭翁",
    "作者", "朝代", "翻译", "赏析", "中心思想", "修辞手法",
    "数学", "科学", "方程式", "函数", "几何", "代数", "化学方程式",
    "这首诗", "这篇", "这首词", "这句诗", "介绍一下", "什么意思",
]


# 领域判定 prompt（用于模式不匹配提醒）
DOMAIN_DETECT_PROMPT = """判断以下用户发言的主要领域。只返回 "oi" 或 "study"，不要其他内容。
- "oi"：信息学奥赛、编程、算法、代码、题解、编译、数据结构、OI/ACM/NOIP/CSP、数论、组合数学等。
- "study"：文化课、语文、英语、科学、网课、历史、地理、生物、化学、物理、作业等。

用户发言："""


# ---------- 检测函数（模块级，可在线程中调用） ----------
def _is_chat(text: str, cfg) -> bool:
    """用 flash 模型判定是否闲聊。失败时用关键词兜底。

    关键保护：若用户消息命中学习/文化课关键词白名单，即使模型误判也
    不视为闲聊，避免"介绍卖炭翁"等学习提问被阻断。
    """
    # 先过白名单：命中学习关键词直接放行
    if any(k in text for k in _STUDY_KEYWORDS_PROTECT):
        return False

    from core.ai_client import chat, resolve_flash_target
    try:
        _fp, _fm = resolve_flash_target()
        raw = chat(
            [{"role": "user", "content": CHAT_DETECT_PROMPT + text}],
            provider=_fp,
            model=_fm,
            temperature=0.0,
            max_tokens=10,
        )
        is_chat = raw.strip().lower().startswith("true")
        # 白名单在函数入口已做第一道保护；此处保留二次校验，防御模型
        # 对"卖炭翁/古诗/文言文"等边界词产生误判。
        if is_chat and any(k in text for k in _STUDY_KEYWORDS_PROTECT):
            return False
        return is_chat
    except AICallError:
        pass
    except Exception:
        pass
    # 兜底关键词（不包括"你好"等正常问候，避免误杀）
    chat_keywords = [
        "在吗", "今天天气", "吃饭", "吃什么", "睡觉", "游戏", "八卦", "无聊",
        "中午", "晚上", "外卖", "奶茶", "电视剧", "综艺", "明星",
    ]
    return any(k in text for k in chat_keywords)


def _has_code_output(text: str) -> bool:
    """检测用户消息是否含代码输出行为。"""
    # 中文"输出是 xxx"，但排除疑问句如"输出是什么/吗/么"
    m = re.search(r"输出[是为：:]\s*(.+?)(?:\n|$)", text)
    if m:
        tail = m.group(1).strip()
        if tail and not re.search(r"^[是什怎吗么].*[吗么？?]?$", tail):
            return True
    # 英文 "output is xxx"
    if re.search(r"output\s+(is|was|=)\s", text, re.IGNORECASE):
        return True
    # 贴了运行结果（纯数字/带单位的结果行）
    if re.search(r"^\s*结果[是为：:]\s*\S+", text, re.MULTILINE):
        return True
    return False


def _is_new_algo_idea(text: str) -> bool:
    """检测用户是否在尝试自己寻找新算法。"""
    keywords = ["新算法", "新思路", "换个方法", "试着自己", "我想到了", "能不能用"]
    return any(k in text for k in keywords)


def _detect_domain_mismatch(text: str, cfg) -> Optional[str]:
    """检测用户发言领域是否与当前 focus_mode 不匹配。

    返回建议的模式字符串（"oi"/"study"）或 None。
    先使用 flash 模型做零样本分类，失败时走关键词兜底。
    """
    current_mode = cfg.settings.focus_mode
    if current_mode not in ("oi", "study"):
        return None

    from core.ai_client import chat, resolve_flash_target
    try:
        _fp, _fm = resolve_flash_target()
        raw = chat(
            [{"role": "user", "content": DOMAIN_DETECT_PROMPT + text}],
            provider=_fp,
            model=_fm,
            temperature=0.0,
            max_tokens=10,
        )
        pred = raw.strip().lower()
        if pred in ("oi", "study") and pred != current_mode:
            return pred
    except AICallError:
        pass
    except Exception:
        pass

    # 关键词兜底
    oi_keywords = [
        "代码", "算法", "题解", "编译", "编程", "C++", "CPP", "NOIP", "CSP",
        "OI", "ACM", "数据结构", "图论", "动态规划", "递推", "递归", "贪心",
        "搜索", "深搜", "广搜", "数论", "组合数学", "质数", "同余", "模运算",
        "堆栈", "队列", "链表", "树状数组", "线段树", "二叉树", "图", "DAG",
    ]
    # 注意："数学" 一词在 OI 中也很常见（数论、组合数学），单独出现时不应判定为文化课。
    study_keywords = [
        "语文", "英语", "历史", "地理", "生物", "化学", "物理", "政治",
        "网课", "作业", "课文", "单词", "古诗", "诗词", "文言文", "作文",
        "阅读理解", "听力", "背单词", "文化课", "背诵", "默写", "卖炭翁",
        "作者", "朝代", "翻译", "赏析", "中心思想", "修辞手法",
        "数学", "科学", "方程式", "函数", "几何", "代数", "化学方程式",
    ]
    has_oi = any(k in text for k in oi_keywords)
    has_study = any(k in text for k in study_keywords)
    if has_oi and not has_study and current_mode != "oi":
        return "oi"
    if has_study and not has_oi and current_mode != "study":
        return "study"
    return None


class _ScreenshotWorker(QObject):
    """截图视觉分析工作线程：异步调用 vision_chat，避免阻塞 UI。

    r38 P1 修复：screenshot_analyze / attach_screen_context 原在主线程同步
    调用 vision_chat，导致 UI 冻结数秒。改为 QThread 异步执行，结果通过信号回传。
    """

    finished = Signal(int, dict)   # (request_id, 结构化分析结果)
    context_ready = Signal(int, dict)  # (request_id, 屏幕上下文 dict)
    failed = Signal(int, str)      # (request_id, reason)
    done = Signal()           # run() 结束时必发，用于清理线程

    def __init__(self, image_bytes: bytes, mode: str = "analyze", request_id: int = 0, parent=None):
        super().__init__(parent)
        self._image_bytes = image_bytes
        self._mode = mode  # "analyze" 或 "context"
        self._request_id = request_id

    def run(self):
        try:
            from core.ai_client import vision_chat, resolve_vision_target
            _vprovider, _vmodel = resolve_vision_target()
            if self._mode == "context":
                # 关联页面：返回简短描述
                raw = vision_chat(
                    "请用一段话描述这个屏幕的核心内容（题目/代码/调试信息等）",
                    self._image_bytes,
                    provider=_vprovider,
                    model=_vmodel or None,
                )
                self.context_ready.emit(self._request_id, {"screen_summary": raw})
            else:
                # 截图分析：返回结构化 JSON
                raw = vision_chat(
                    "请分析这张编程学习屏幕，返回严格 JSON："
                    '{"activity":"活动描述","efficiency":0-100,'
                    '"code_seen":true/false,"progress_delta":"进展",'
                    '"current_problem":"题目ID"}',
                    self._image_bytes,
                    provider=_vprovider,
                    model=_vmodel or None,
                )
                # 复用 ScreenAnalyzer 的 JSON 解析逻辑
                from core.screen_analyzer import _parse_ai_json
                result = _parse_ai_json(raw)
                self.finished.emit(self._request_id, result)
        except AICallError as e:
            self.failed.emit(self._request_id, str(e))
        except Exception as e:
            self.failed.emit(self._request_id, f"异常: {e}")
        finally:
            # round48：emit 自身异常时也要保证线程能退出
            try:
                self.done.emit()
            except RuntimeError:
                pass


def _trim_messages_to_max_rounds(messages: List[Dict], max_rounds) -> List[Dict]:
    """按轮数上限裁剪对话消息（保留最近轮次）。

    审计修复：ai_dialog_max_rounds 此前只在设置页展示，从未被消费。
    一轮 = 一条 user + 一条 assistant。裁剪后保证保留的第一条是 user 消息，
    避免以孤儿 assistant 回复开头（部分模型会拒绝）。
    max_rounds <= 0 / 非法值表示不限制。
    """
    if not isinstance(messages, list):
        return messages
    try:
        max_rounds = int(max_rounds or 0)
    except (TypeError, ValueError):
        return messages
    if max_rounds <= 0:
        return messages
    limit = max_rounds * 2
    if len(messages) <= limit:
        return messages
    drop = len(messages) - limit
    # 对齐到 user 消息边界，避免裁剪后第一条是 assistant
    while drop < len(messages) and messages[drop].get("role") != "user":
        drop += 1
    # r49 P2：若无 user 消息可对齐，回退到直接截取最后 limit 条（避免清空全部）
    if drop >= len(messages):
        return messages[-limit:]
    return messages[drop:]


class _DialogWorker(QObject):
    """对话调用工作线程：先执行检测器，再异步裁剪上下文，最后调用主模型。

    r34 P0 修复：把 `build_blocks / prune_with_flash / to_messages` 从主线程
    移到子线程执行，避免 flash 模型同步调用阻塞 UI 导致"卡死"。
    """

    finished = Signal(str, str, dict)   # (block_id, reply_text, meta)
    failed = Signal(str)
    chat_detected = Signal(str, str)    # (msg, original_text)
    code_output_detected = Signal(str)  # (original_text,)
    new_algo_detected = Signal(str)     # (msg,)
    mode_mismatch_suggested = Signal(str)  # (suggested_mode,)
    done = Signal()                     # run() 结束时必发，用于清理线程

    def __init__(self, blocks: List, extra_rules: str = "",
                 block_id: str = "", mode: str = "oi",
                 last_user_text: str = "", skip_detectors: bool = False,
                 cfg=None):
        super().__init__()
        self._blocks = blocks
        self._extra_rules = extra_rules
        self._block_id = block_id
        self._mode = mode
        self._last_user_text = last_user_text
        self._skip_detectors = skip_detectors
        self._cfg = cfg

    def run(self):
        try:
            if not self._skip_detectors:
                # 闲聊检测：命中则直接返回，不产生主回复
                if _is_chat(self._last_user_text, self._cfg):
                    self.chat_detected.emit(
                        "检测到闲聊，请专注学习。", self._last_user_text
                    )
                    return

                # 代码输出检测：命中则直接返回
                if _has_code_output(self._last_user_text):
                    self.code_output_detected.emit(self._last_user_text)
                    return

                # 新算法想法检测：不阻断主回复
                if _is_new_algo_idea(self._last_user_text):
                    self.new_algo_detected.emit(
                        "想法很好！但提醒你不要遗落当前进度。"
                    )

                # 领域不匹配检测：不阻断主回复
                suggested = _detect_domain_mismatch(self._last_user_text, self._cfg)
                if suggested:
                    self.mode_mismatch_suggested.emit(suggested)

            # r34 P0 修复：上下文裁剪（prune_with_flash 含 flash 模型同步调用）
            # 移到子线程执行，避免阻塞主线程 UI。worker 内新建 ContextManager
            # 实例（只读黑名单+配置），避免跨线程访问 AIDialog 的 ctx_mgr。
            from core.context_manager import ContextManager
            ctx_mgr = ContextManager()
            kept = ctx_mgr.prune_with_flash(self._blocks)
            pruned_messages = ctx_mgr.to_messages(kept)

            # 主对话调用
            from core.ai_client import chat, resolve_dialog_target
            sys_prompt = build_system_prompt(self._mode, self._extra_rules)
            full = [{"role": "system", "content": sys_prompt}] + pruned_messages
            # role 路由：按 deepseek_role/kimi_role/glm_role 解析对话 provider
            _provider, _model = resolve_dialog_target()
            # ai_dialog_base_url 是 deepseek 端点，仅当 provider 为 deepseek 时生效
            _base_url = self._dialog_base_url() if _provider == "deepseek" else None
            reply = chat(
                full,
                provider=_provider,
                model=_model,
                temperature=0.5,
                max_tokens=1500,
                base_url=_base_url,
            )
            meta = self._extract_meta(reply)
            self.finished.emit(self._block_id, reply, meta)
        except AICallError as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"异常: {e}")
        finally:
            # round48：emit 自身异常时也要保证线程能退出
            try:
                self.done.emit()
            except RuntimeError:
                pass

    def _dialog_base_url(self):
        """审计修复：落实设置页的"对话基础 URL"（ai_dialog_base_url）。

        此前该配置项只在设置页展示/保存，对话引擎从未消费，属死配置。
        返回 None 时退回 deepseek provider 的默认 base_url。
        """
        try:
            if self._cfg is None:
                return None
            u = (getattr(self._cfg.settings, "ai_dialog_base_url", "") or "").strip()
            return u or None
        except Exception:
            return None

    def _extract_meta(self, reply: str) -> dict:
        """从回复中提取元信息：是否含代码块/新算法想法/代码输出/错误摘要。"""
        has_code_block = bool(re.search(r"```", reply))
        # 检测用户是否在找新算法（由主对话引擎在用户消息侧判定，这里仅初判）
        new_algo_idea = bool(re.search(r"新算法|新思路|换个方法|试着自己", reply))
        # 错误摘要：让 AI 在回复末尾隐式给出，这里简单提取"错误：xxx"
        err_match = re.search(r"错误[：:]\s*(.+?)(?:\n|$)", reply)
        error_summary = err_match.group(1) if err_match else ""
        return {
            "has_code_block": has_code_block,
            "new_algo_idea": new_algo_idea,
            "error_summary": error_summary,
        }


class _ArchiveWorker(QObject):
    """对话存档/删除摘要工作线程。

    round48：close_dialog 原先在主线程同步调 flash 模型做摘要（最长 60 秒网络等待），
    新建/删除/关闭对话窗口时 UI 会冻结。现改为线程内摘要 + 线程内写档，
    避免主线程网络阻塞。
    """

    finished = Signal(str, str, str)  # (dialog_id, summary, mode)
    failed = Signal(str, str)         # (dialog_id, reason)
    done = Signal()

    def __init__(self, dialog_id: str, messages: List[Dict], mode: str = "archive"):
        super().__init__()
        self._dialog_id = dialog_id
        self._messages = messages
        self._mode = mode  # "archive" / "delete"

    def run(self):
        summary = ""
        try:
            from core.ai_client import chat, resolve_flash_target
            dialog_text = "\n".join(
                f"[{m.get('role','?')}] {m.get('content','')[:200]}"
                for m in self._messages[-20:]
            )
            prompt = f"请用一两句话总结以下对话的核心内容（用户学了什么/遇到什么问题/得到什么引导）：\n\n{dialog_text}"
            _fp, _fm = resolve_flash_target()
            try:
                summary = chat(
                    [{"role": "user", "content": prompt}],
                    provider=_fp,
                    model=_fm,
                    temperature=0.2,
                    max_tokens=200,
                )
            except AICallError as e:
                logger.warning(f"对话摘要生成失败: {e}")
                summary = ""
            if self._mode == "archive" and self._dialog_id:
                ensure_dirs()
                archive = {
                    "dialog_id": self._dialog_id,
                    "closed_at": format_time(now_cst()),
                    "summary": summary,
                    "messages": self._messages,
                }
                path = os.path.join(DIALOGS_DIR, f"{self._dialog_id}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(archive, f, ensure_ascii=False, indent=2)
                logger.info(f"对话 {self._dialog_id} 已存档: {path}")
            self.finished.emit(self._dialog_id, summary, self._mode)
        except Exception as e:
            logger.error(f"对话存档线程异常: {e}")
            self.failed.emit(self._dialog_id, str(e))
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class AIDialog(QObject):
    """AI 对话主引擎。"""

    reply_ready = Signal(str, str, dict)  # (block_id, reply, meta)
    error_occurred = Signal(str)
    chat_detected = Signal(str)       # 闲聊提醒
    new_algo_detected = Signal(str)   # 新算法想法
    code_output_detected = Signal()   # 代码输出
    supervisor_correction = Signal(str)
    mode_mismatch_suggested = Signal(str)  # 建议切换模式（"oi"/"study"）
    # r38 P1 修复：截图分析/关联页面异步化，结果通过信号回传
    # round48 P2：结果携带 request_id，窗口重开后旧 worker 的迟到结果可被精确丢弃
    screenshot_analyzed = Signal(int, dict)    # (request_id, 结构化分析结果)
    screen_context_ready = Signal(int, dict)   # (request_id, 关联页面上下文)
    # round48：把 ContextManager 已有端口转发给 UI，消除"只 emit 无消费者"死端口
    context_pruned = Signal(int)
    context_block_referenced = Signal(str)
    context_block_blacklisted = Signal(str)

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self.ctx_mgr = ContextManager()
        self.supervisor = AISupervisor()
        # 元监督的纠偏：仅注入上下文，不再发独立气泡
        # 信号仍保留（兼容旧调用方），但 dialog_view 已不再显示它
        self.supervisor.correction_needed.connect(self._on_supervisor_correction)
        # round48：上下文裁剪/引用/拉黑信号转发给 UI（见 DialogView 槽函数）
        self.ctx_mgr.context_pruned.connect(self.context_pruned.emit)
        self.ctx_mgr.block_referenced.connect(self.context_block_referenced.emit)
        self.ctx_mgr.block_blacklisted.connect(self.context_block_blacklisted.emit)
        self._pending_corrections: List[str] = []  # 待注入的元监督纠偏（按时间序）
        self._dialog_id: str = ""
        self._messages: List[Dict] = []      # 当前对话消息
        self._blocks = []                     # ContextBlock 列表
        self._referenced_ids: set = set()     # 用户引用的 block_id 集合（跨轮保留）
        self._thread: Optional[QThread] = None
        self._worker: Optional[_DialogWorker] = None
        # r38 P1 修复：截图分析/关联页面异步线程引用
        self._shot_thread: Optional[QThread] = None
        self._shot_worker: Optional[_ScreenshotWorker] = None
        self._shot_mode: str = "analyze"
        self._shot_request_id: int = 0
        self._archive_threads: List[QThread] = []
        self._archive_workers: List[_ArchiveWorker] = []
        self._unlimited_chat_start: Optional[datetime] = None
        self._ignore_worker: bool = False  # 关闭/删除对话后忽略旧 worker 回复
        self._new_dialog()

    def _on_supervisor_correction(self, correction: str):
        """元监督纠偏：仅入队，不发独立气泡；下一轮 send 时注入上下文。"""
        c = (correction or "").strip()
        if not c:
            return
        self._pending_corrections.append(c)
        # 节流：最多保留最近 5 条
        if len(self._pending_corrections) > 5:
            self._pending_corrections = self._pending_corrections[-5:]
        # 仅记录日志，不发 UI 信号（避免被 dialog_view 当气泡显示）
        logger.info(f"元监督纠偏入队：{c[:60]}（共 {len(self._pending_corrections)} 条待注入）")
        # 兼容旧信号（如有外部订阅），但新代码不消费它
        try:
            self.supervisor_correction.emit(c)
        except Exception:
            pass

    def _consume_corrections(self) -> List[str]:
        """取出并清空待注入的纠偏。下一轮 send 时调用。"""
        out = list(self._pending_corrections)
        self._pending_corrections.clear()
        return out

    # ---------- 对话生命周期 ----------
    def _new_dialog(self):
        self._dialog_id = uuid.uuid4().hex[:12]
        self._messages = []
        self._blocks = []
        self._referenced_ids = set()
        self._unlimited_chat_start = None
        self._pending_corrections.clear()  # 关键：新对话不继承老对话的元监督纠偏
        # 重置元监督计数器（P1 修复：调用公开方法而非改私有字段）
        self.supervisor.reset_round_count()
        # r49 P1：断开旧 worker 信号并清除线程引用，使新对话能立即发送
        self._disconnect_old_worker()
        self._thread = None
        self._worker = None
        # r49 P2：清理过期的引用块 ID（避免跨对话累积）
        self._referenced_ids.clear()
        logger.info(f"新对话 {self._dialog_id}")

    def cleanup_archive_threads(self):
        """r49 P2：清理所有正在运行的归档线程（供退出流程调用）。"""
        for t in list(self._archive_threads):
            try:
                if t.isRunning():
                    t.quit()
                    t.wait(1000)
            except Exception:
                pass
        self._archive_threads.clear()
        self._archive_workers.clear()

    def dialog_id(self) -> str:
        return self._dialog_id

    def messages(self) -> List[Dict]:
        return list(self._messages)

    def close_dialog(self):
        """同步关闭当前对话：做摘要 → 写日志 → 存档 → 新建。

        用于退出流程等必须等待存档完成的场景；UI 高频操作请使用
        close_dialog_async() 避免主线程网络阻塞。
        """
        self._ignore_worker = True
        if not self._messages:
            self._new_dialog()
            return
        dialog_id, messages = self._dialog_id, list(self._messages)
        self._new_dialog()
        summary = self._summarize_messages(messages)
        log_event("dialog_summary", {
            "dialog_id": dialog_id,
            "summary": summary,
            "message_count": len(messages),
        })
        try:
            ensure_dirs()
            archive = {
                "dialog_id": dialog_id,
                "closed_at": format_time(now_cst()),
                "summary": summary,
                "messages": messages,
            }
            path = os.path.join(DIALOGS_DIR, f"{dialog_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(archive, f, ensure_ascii=False, indent=2)
            logger.info(f"对话 {dialog_id} 已存档: {path}")
        except Exception as e:
            logger.error(f"对话存档失败: {e}")

    def close_dialog_async(self):
        """异步关闭当前对话：摘要与存档在线程内完成，不冻结 UI。"""
        self._ignore_worker = True
        if not self._messages:
            self._new_dialog()
            return
        dialog_id, messages = self._dialog_id, list(self._messages)
        self._new_dialog()
        self._start_archive_worker(dialog_id, messages, mode="archive")

    def delete_dialog(self):
        """用户手动删除对话：做摘要写日志后丢弃（不存档）。

        同步版本供兼容/退出场景使用；UI 请用 delete_dialog_async()。
        """
        self._ignore_worker = True
        if self._messages:
            summary = self._summarize_messages(self._messages)
            log_event("dialog_summary", {
                "dialog_id": self._dialog_id,
                "summary": summary,
                "message_count": len(self._messages),
                "deleted_by_user": True,
            })
        self._new_dialog()

    def delete_dialog_async(self):
        """异步删除对话：在线程内生成摘要后丢弃，不冻结 UI。"""
        self._ignore_worker = True
        if not self._messages:
            self._new_dialog()
            return
        dialog_id, messages = self._dialog_id, list(self._messages)
        self._new_dialog()
        self._start_archive_worker(dialog_id, messages, mode="delete")

    def _start_archive_worker(self, dialog_id: str, messages: List[Dict], mode: str):
        """启动摘要/存档线程，并持有引用直至线程结束。"""
        thread = QThread()
        worker = _ArchiveWorker(dialog_id, messages, mode)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_archive_finished)
        worker.failed.connect(self._on_archive_failed)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._drop_archive_refs(thread, worker))
        self._archive_threads.append(thread)
        self._archive_workers.append(worker)
        thread.start()

    def _drop_archive_refs(self, thread, worker):
        try:
            if worker in self._archive_workers:
                self._archive_workers.remove(worker)
            if thread in self._archive_threads:
                self._archive_threads.remove(thread)
        except Exception:
            pass

    def _on_archive_finished(self, dialog_id: str, summary: str, mode: str):
        log_event("dialog_summary", {
            "dialog_id": dialog_id,
            "summary": summary,
            "deleted_by_user": mode == "delete",
        })

    def _on_archive_failed(self, dialog_id: str, reason: str):
        logger.warning(f"对话 {dialog_id} 摘要/存档失败: {reason}")
        log_event("dialog_summary", {
            "dialog_id": dialog_id,
            "summary": "",
            "archive_failed": reason,
        })

    def _summarize(self) -> str:
        """同步总结当前对话（兼容旧调用方）。"""
        return self._summarize_messages(self._messages)

    def _summarize_messages(self, messages: List[Dict]) -> str:
        """调 flash 模型生成对话摘要。"""
        from core.ai_client import chat, resolve_flash_target
        try:
            dialog_text = "\n".join(
                f"[{m.get('role','?')}] {m.get('content','')[:200]}"
                for m in messages[-20:]
            )
            prompt = f"请用一两句话总结以下对话的核心内容（用户学了什么/遇到什么问题/得到什么引导）：\n\n{dialog_text}"
            _fp, _fm = resolve_flash_target()
            return chat(
                [{"role": "user", "content": prompt}],
                provider=_fp,
                model=_fm,
                temperature=0.2,
                max_tokens=200,
            )
        except AICallError as e:
            logger.warning(f"对话摘要生成失败: {e}")
            return ""
        except Exception as e:
            logger.warning(f"对话摘要生成异常: {e}")
            return ""

    # ---------- 发送消息 ----------
    def send(self, user_text: str, screen_context: Optional[dict] = None,
             skip_detectors: bool = False):
        """发送用户消息，异步获取 AI 回复。

        screen_context: 一键关联页面或截图分析的结果 dict
        skip_detectors: 上传文件等场景跳过闲聊/代码输出检测
        """
        # r39 P0 修复：user_text 可能为 None（异常路径），避免 None.strip() 崩溃
        if not user_text or not user_text.strip():
            return

        # 异步调用：先检查是否有运行中的请求，避免悬空用户消息
        if self._thread is not None and self._thread.isRunning():
            self.error_occurred.emit("上一次对话仍在进行")
            return

        self._ignore_worker = False

        # 拼接屏幕上下文
        content = user_text
        if isinstance(screen_context, dict) and screen_context:
            # P1 修复：截断 progress_delta 防止 AI 自由发挥塞超长文本
            safe_ctx = dict(screen_context)
            pd = safe_ctx.get("progress_delta", "")
            if isinstance(pd, str) and len(pd) > 500:
                safe_ctx["progress_delta"] = pd[:497] + "..."
            try:
                ctx_str = json.dumps(safe_ctx, ensure_ascii=False)
            except (TypeError, ValueError):
                ctx_str = json.dumps({"screen_summary": str(screen_context)[:500]}, ensure_ascii=False)
            content = f"[屏幕上下文]\n{ctx_str}\n\n[用户消息]\n{user_text}"

        self._messages.append({"role": "user", "content": content})

        # 审计修复：落实 ai_dialog_max_rounds（此前配置只在设置页展示/保存，
        # 对话引擎从未执行轮数限制）。仅裁剪发给 AI 的上下文，不影响 UI 展示。
        trimmed = _trim_messages_to_max_rounds(
            self._messages, getattr(self.cfg.settings, "ai_dialog_max_rounds", 0)
        )

        # 非专注模式：记录无限制对话开始时间
        from ui.focus_view import _get_global_engine
        try:
            if not _get_global_engine().is_active:
                if self._unlimited_chat_start is None:
                    self._unlimited_chat_start = now_cst()
        except Exception:
            pass

        # r34 P0 修复：build_blocks 仍在主线程（纯本地计算，不阻塞 UI）；
        # prune_with_flash（含 flash 模型同步调用）移到 _DialogWorker 子线程。
        self._blocks = self.ctx_mgr.build_blocks(trimmed)
        for b in self._blocks:
            if b.block_id in self._referenced_ids:
                b.manually_referenced = True
        reply_block_id = self.ctx_mgr.new_block_id(f"reply_{time.time()}", len(self._messages))

        # 元监督纠偏：若有未消费的纠偏，**合并到 system_prompt 尾部**而不是单独 system 消息。
        # 原因：部分模型（Claude 严格模式 / OpenAI 旧版）会拒绝"两条 system 消息"或行为异常。
        # 修复：通过 dynamic_extra_rules 追加到 build_system_prompt 调用，worker 内仍只生成一条 system。
        corrections = self._consume_corrections()
        dynamic_extra = getattr(self.cfg.settings, "ai_dialog_extra_rules", None) or ""
        if corrections:
            inject = "[元监督提示] " + "；".join(corrections)
            if dynamic_extra:
                dynamic_extra = dynamic_extra + "\n\n" + inject
            else:
                dynamic_extra = inject
            logger.info(f"本轮注入 {len(corrections)} 条元监督纠偏到 system_prompt 尾部")

        # r49 P1：断开旧 worker 信号，防止旧线程迟到结果污染新对话
        self._disconnect_old_worker()

        self._thread = QThread()
        self._worker = _DialogWorker(
            self._blocks,                   # r34 P0：worker 子线程内执行 prune_with_flash
            dynamic_extra,  # P0 修复：把元监督纠偏合并到 extra_rules 一起传
            block_id=reply_block_id,
            mode=self.cfg.settings.focus_mode,
            last_user_text=user_text,
            skip_detectors=skip_detectors,
            cfg=self.cfg,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_reply)
        self._worker.failed.connect(self._on_failed)
        self._worker.chat_detected.connect(self._on_worker_chat_detected)
        self._worker.code_output_detected.connect(self._on_worker_code_output)
        self._worker.new_algo_detected.connect(self._on_worker_new_algo)
        self._worker.mode_mismatch_suggested.connect(self._on_worker_mode_mismatch)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        # round48 P1：旧线程的 finished 回调不得清掉新线程引用（代次校验）
        self._thread.finished.connect(
            lambda t=self._thread: self._reset_thread_refs(t)
        )
        self._thread.start()

    def _disconnect_old_worker(self):
        """r49 P1：断开旧 worker 的信号连接，防止迟到信号污染新对话。"""
        old = getattr(self, "_worker", None)
        if old is None:
            return
        for signal, slot in (
            (old.finished, self._on_reply),
            (old.failed, self._on_failed),
            (old.chat_detected, self._on_worker_chat_detected),
            (old.code_output_detected, self._on_worker_code_output),
            (old.new_algo_detected, self._on_worker_new_algo),
            (old.mode_mismatch_suggested, self._on_worker_mode_mismatch),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_reply(self, block_id: str, reply: str, meta: dict):
        if self._ignore_worker:
            return
        meta = meta if isinstance(meta, dict) else {}
        self._messages.append({"role": "assistant", "content": reply})
        from core.context_manager import ContextBlock
        self._blocks.append(ContextBlock(
            block_id=block_id,
            role="assistant",
            content=reply,
        ))
        self.reply_ready.emit(block_id, reply, meta)

        if meta.get("error_summary"):
            log_event("ai_error_found", {
                "dialog_id": self._dialog_id,
                "error_summary": meta["error_summary"],
            })

        if self.supervisor.should_check():
            try:
                self.supervisor.check(self._messages[-10:])
            except Exception as e:
                logger.warning(f"元监督检查失败: {e}")

    def _on_failed(self, reason: str):
        if self._ignore_worker:
            return
        logger.error(f"AI 对话失败: {reason}")
        self.error_occurred.emit(reason)

    def _reset_thread_refs(self, finished_thread=None):
        """线程结束后清理引用；仅当结束的是当前线程时才清空（代次校验）。"""
        if finished_thread is not None and self._thread is not finished_thread:
            return
        self._thread = None
        self._worker = None

    def _on_worker_chat_detected(self, msg: str, original_text: str):
        # round48 P1：旧 worker 的检测信号同样不得污染新对话（与 _on_reply/_on_failed 对齐）
        if self._ignore_worker:
            return
        # 闲聊消息不计入后续上下文
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()
        log_event("chat_detected", {"text": original_text[:100]})
        self.chat_detected.emit(msg)

    def _on_worker_code_output(self, original_text: str):
        # round48 P1：同上，旧 worker 不得 pop 新对话消息
        if self._ignore_worker:
            return
        # 代码输出消息不计入后续上下文
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.pop()
        log_event("code_output_detected", {"text": original_text[:100]})
        self.code_output_detected.emit()

    def _on_worker_new_algo(self, msg: str):
        # round48 P1：旧 worker 的提示不得污染新对话 UI
        if self._ignore_worker:
            return
        log_event("new_algo_idea", {"msg": msg})
        self.new_algo_detected.emit(msg)

    def _on_worker_mode_mismatch(self, suggested_mode: str):
        # round48 P1：旧 worker 的模式建议不得污染新对话 UI
        if self._ignore_worker:
            return
        log_event("mode_mismatch_suggested", {
            "current_mode": self.cfg.settings.focus_mode,
            "suggested_mode": suggested_mode,
        })
        self.mode_mismatch_suggested.emit(suggested_mode)

    # ---------- 一键操作 ----------
    def attach_screen_context(self):
        """一键关联当前页面：异步截图分析，结果通过 screen_context_ready 信号回传。

        返回 int request_id 表示本次请求已受理（含同步失败回执）；返回 None 表示上一次仍在进行。
        DialogView 保存 request_id，只接受属于自己的结果，防止旧窗口迟到的结果泄漏。
        """
        if self._shot_thread is not None and self._shot_thread.isRunning():
            logger.debug("上一次截图分析仍在进行，跳过 attach_screen_context")
            return None
        try:
            from core.screen_analyzer import ScreenAnalyzer
            analyzer = ScreenAnalyzer()
            image_bytes = analyzer.capture()
            if not image_bytes:
                self._shot_request_id += 1
                self.screen_context_ready.emit(self._shot_request_id, {})
                return self._shot_request_id
            return self._start_shot_worker(image_bytes, mode="context")
        except Exception as e:
            logger.warning(f"关联页面失败: {e}")
            self._shot_request_id += 1
            self.screen_context_ready.emit(self._shot_request_id, {})
            return self._shot_request_id

    def screenshot_analyze(self):
        """一键截图分析：异步截图 + 视觉分析，结果通过 screenshot_analyzed 信号回传。

        返回 int request_id 表示本次请求已受理（含同步失败回执）；返回 None 表示上一次仍在进行。
        """
        if self._shot_thread is not None and self._shot_thread.isRunning():
            logger.debug("上一次截图分析仍在进行，跳过 screenshot_analyze")
            return None
        try:
            from core.screen_analyzer import ScreenAnalyzer
            analyzer = ScreenAnalyzer()
            image_bytes = analyzer.capture()
            if not image_bytes:
                from core.screen_analyzer import _make_empty_result
                self._shot_request_id += 1
                self.screenshot_analyzed.emit(
                    self._shot_request_id, _make_empty_result("截图失败"))
                return self._shot_request_id
            return self._start_shot_worker(image_bytes, mode="analyze")
        except Exception as e:
            logger.warning(f"截图分析失败: {e}")
            from core.screen_analyzer import _make_empty_result
            self._shot_request_id += 1
            self.screenshot_analyzed.emit(
                self._shot_request_id, _make_empty_result(f"失败: {e}"))
            return self._shot_request_id

    def is_busy(self) -> bool:
        """r39 P1 修复：暴露公共接口，避免外部访问私有 _thread 属性。

        返回 True 表示截图分析线程仍在运行，外部不应恢复按钮状态。
        """
        return self._shot_thread is not None and self._shot_thread.isRunning()

    def _start_shot_worker(self, image_bytes: bytes, mode: str = "analyze") -> int:
        """启动截图分析工作线程，返回本次 request_id。"""
        self._shot_request_id += 1
        request_id = self._shot_request_id
        self._shot_thread = QThread()
        self._shot_worker = _ScreenshotWorker(image_bytes, mode=mode, request_id=request_id)
        self._shot_mode = mode
        self._shot_worker.moveToThread(self._shot_thread)
        self._shot_thread.started.connect(self._shot_worker.run)
        if mode == "context":
            self._shot_worker.context_ready.connect(self._on_screen_context_ready)
        else:
            self._shot_worker.finished.connect(self._on_screenshot_analyzed)
        self._shot_worker.failed.connect(self._on_shot_failed)
        self._shot_worker.done.connect(self._shot_thread.quit)
        self._shot_worker.done.connect(self._shot_worker.deleteLater)
        self._shot_thread.finished.connect(self._shot_thread.deleteLater)
        # round48 P1：旧线程的 finished 回调不得清掉新线程引用（代次校验）
        self._shot_thread.finished.connect(
            lambda t=self._shot_thread: self._reset_shot_thread_refs(t)
        )
        self._shot_thread.start()
        return request_id

    def _on_screenshot_analyzed(self, request_id: int, result: dict):
        # 不检查 _ignore_worker：截图结果是独立信号，DialogView 关闭时
        # 会通过 _unbind_dialog 断开信号，并用 request_id 过滤迟到结果。
        self.screenshot_analyzed.emit(request_id, result)

    def _on_screen_context_ready(self, request_id: int, ctx: dict):
        self.screen_context_ready.emit(request_id, ctx)

    def _on_shot_failed(self, request_id: int, reason: str):
        logger.warning(f"截图分析失败: {reason}")
        # P0 修复：按当前 worker 模式分流失败信号，否则 context 模式下
        # UI 的 attach_btn 永远得不到恢复信号而永久禁用。
        if getattr(self, "_shot_mode", "analyze") == "context":
            # 关联页面失败：发空上下文，让 UI 恢复 attach_btn
            self.screen_context_ready.emit(request_id, {})
        else:
            # 截图分析失败：发空结果，让 UI 恢复 shot_btn
            from core.screen_analyzer import _make_empty_result
            self.screenshot_analyzed.emit(
                request_id, _make_empty_result(f"失败: {reason}"))

    def _reset_shot_thread_refs(self, finished_thread=None):
        """截图线程结束后清理引用，仅当结束的是当前线程时才清空（代次校验）。"""
        if finished_thread is not None and self._shot_thread is not finished_thread:
            return
        self._shot_thread = None
        self._shot_worker = None
        self._shot_mode = "analyze"

    # ---------- 引用/黑名单 ----------
    def reference_block(self, block_id: str) -> bool:
        """用户引用某块，加入 _referenced_ids 跨轮保留。"""
        ok = self.ctx_mgr.reference_block(block_id, self._blocks)
        if ok:
            self._referenced_ids.add(block_id)
            logger.info(f"引用块 {block_id}, 已加入引用集合")
        return ok

    def blacklist_block(self, block_id: str):
        self.ctx_mgr.blacklist_block(block_id)
        # 从引用集合中也移除
        self._referenced_ids.discard(block_id)

    # ---------- 非专注模式 15 分钟上限 ----------
    def check_unlimited_chat_timeout(self) -> Optional[int]:
        """返回非专注模式对话已持续的秒数，None 表示不适用。"""
        from ui.focus_view import _get_global_engine
        try:
            if _get_global_engine().is_active:
                return None
        except Exception:
            return None
        if self._unlimited_chat_start is None:
            return None
        delta = (now_cst() - self._unlimited_chat_start).total_seconds()
        return int(delta)
