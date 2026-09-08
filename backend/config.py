import os
import re
import sys
import json
import tempfile
import threading
from pathlib import Path
from logger import log_error

# 将工作区根目录加入 path，以便导入统一 api_keys 模块；
# 该文件含真实密钥、位于项目外部，允许缺失（缺失时视为无额外 Key）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
try:
    from api_keys import get_api_key
except ImportError:
    def get_api_key(api_name: str, fallback: str = "") -> str:
        return fallback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
QUESTIONS_DIR = os.path.join(STORAGE_DIR, "questions")
PAPERS_DIR = os.path.join(STORAGE_DIR, "papers")
CORRECTIONS_DIR = os.path.join(STORAGE_DIR, "corrections")
# 图库与校准数据目录：测试环境可在 import main 前替换为临时目录，
# 避免端点契约测试写入真实 data/gallery。
GALLERY_DIR = os.path.join(BASE_DIR, "data", "gallery")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(STORAGE_DIR, 'app.db')}"

MAX_LAYOUT_RETRIES = 3

ALLOWED_PAPER_SIZES = {"A4", "A3", "A5", "B5", "Letter", "Legal"}

DEFAULT_CORS_ORIGINS = [
    "http://localhost",
    "http://127.0.0.1",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    # 注意：默认不加入 "null"，避免 file:// 页面跨域带凭证访问；
    # 如需本地文件调试，请在 settings.json 的 cors_allowed_origins 中显式添加 "null"。
]

# 仅允许 scheme://host[:port] 格式，拒绝 *、userinfo、path
CORS_ORIGIN_RE = re.compile(r"^https?://[^\s/@:]+(:\d+)?$")

# 模块开关
ENABLE_STRUCTURE_GRAPH = True  # 结构梳理图（逻辑思维导图）
ENABLE_COMPARISON_MODE = True  # 题目对比模式（结构图与标准证明联动着色）
ENABLE_TAG_UNIFICATION = True  # 夜晚巡检标签统一
ENABLE_NOTE_REFERENCES = True  # 笔记引用与自动建库
ENABLE_WORKSHEET = True        # 学习单
ENABLE_CALCULATOR = True       # 计算器 AGENT 工具
ENABLE_SEARCH = True           # 单题拍照搜题
ENABLE_CORRECT = True          # 单题批改与试卷批改
ENABLE_FOCUS_MODE = True       # 专注模式（AI 分段教学 + 语音交互 + 海马体记忆）
ENABLE_FOCUS_BLACKBOARD = True # 专注模式黑板板书（AI 板书文字/图形 + 快照回看；需 ENABLE_FOCUS_MODE）
ENABLE_OFFLINE_CACHE = True    # 离线增强（GET 只读缓存 + 上传队列，IndexedDB；无需 HTTPS/SW）
ENABLE_XIAOMI_TTS = True       # 小米 MiMo TTS 配音（/api/tts；关闭时讲解回退浏览器合成）

DEFAULT_PROMPT_TEMPLATES = {
    "default_paper": {
        "name": "默认组卷",
        "type": "system",
        "is_default": True,
        "content": (
            "你是一位专业的{grade}{subject}教师。请根据以下要求生成一份试卷：\n\n"
            "【用户要求】\n{user_prompt}\n\n"
            "【题目来源】共{question_count}道：\n{questions}\n\n"
            "【知识点范围】{knowledge_tags}\n\n"
            "【答题空间模式】{answer_space}\n"
            "- sheet: 试卷不预留答题空位，卷末附独立答题卡表格\n"
            "- inline: 每题下方预留足够答题区域（填空至少4个___，简答至少留5行）\n"
            "- both: 题目下方留空+卷末答题卡\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
             "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【组卷要求】\n"
            "1. 题目从易到难排列\n"
            "2. 填空题根据答案长度决定___数量，数字答案至少2个___，文字答案至少4个___\n"
            "3. 解答题/简答题下方保留充足空白（至少5行），让学生有足够书写空间\n"
            "4. 生成排版美观的HTML试卷，包含试卷标题、考试说明、题目区\n"
            "5. 卷末按答题空间模式决定：只生成答题卡/只留空/两者都有\n"
            "6. 适合{paper_size}纸打印\n"
            "7. 数学公式用LaTeX，$$...$$独立公式，$...$行内公式\n"
            "8. 分数标注清晰\n\n"
            "输出完整HTML，试卷和答案用 <!-- ANSWER_SPLIT --> 分隔。答案含评/答/析三段。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "question_count", "questions", "knowledge_tags", "paper_size"]
    },
    "mock_exam": {
        "name": "模拟卷",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是资深{grade}{subject}命题专家。请生成标准模拟试卷：\n\n"
            "【命题要求】{user_prompt}\n\n"
            "【可用题目】{questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【规范】\n"
            "1. 参照{grade}考试大纲\n"
            "2. 难度分布：基础30%、中档50%、难题20%\n"
            "3. 题型按标准考试排列\n"
            "4. 总分{total_score}分，标注每题分值\n"
            "5. 考试时长{exam_duration}分钟\n"
            "6. 标准密封线——用CSS writing-mode:vertical-rl竖排文字'密封线内不要答题'，左边缘位置\n"
            "7. 试卷头含姓名、学号、班级、得分栏（书写框，不需要2B铅笔填涂选项）\n"
            "8. 卷末附独立答题卡（书写答题框，每题一个框）\n"
            "9. 适合{paper_size}纸打印\n"
            "10. 数学公式LaTeX\n\n"
            "输出HTML，试卷和答案用 <!-- ANSWER_SPLIT --> 分隔。试卷部分含密封线、信息栏、题目、答题卡。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "questions", "total_score", "exam_duration", "paper_size"]
    },
    "regular_paper": {
        "name": "平时卷",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是{grade}{subject}教师。请生成一份平时练习卷：\n\n"
            "【用户要求】{user_prompt}\n\n"
            "【可用题目】{questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【布局规范 - 无密封线】\n"
            "1. 不要密封线，也不要任何竖排文字\n"
            "2. 只在第一页正上方居中位置放试卷标题（h1）\n"
            "3. 标题下方一行放：日期：____  姓名：____  学号：____  班级：____\n"
            "4. 然后直接开始题目，题目下方预留答题空位（填空至少____，简答至少5行）\n"
            "5. 不生成答题卡\t6. 卷尾不加额外页面\n"
            "7. 适合{paper_size}纸打印\n"
            "8. 数学公式LaTeX\n\n"
            "输出完整HTML，试卷和答案用 <!-- ANSWER_SPLIT --> 分隔。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "questions", "paper_size"]
    },
    "collection_paper": {
        "name": "集合卷",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是{grade}{subject}教学专家。请生成一份集合/综合训练卷：\n\n"
            "【用户要求】{user_prompt}\n\n"
            "【可用题目】{questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【布局规范 - 无密封线简洁版】\n"
            "1. 不要密封线，不要信息栏\n"
            "2. 只在第一页正上方居中: 日期：____\n"
            "3. 然后直接开始题目，题目紧凑排列（题目间留小空行即可）\n"
            "4. 不生成答题卡\t5. 适合{paper_size}纸打印\n"
            "6. 数学公式LaTeX\n\n"
            "输出完整HTML，试卷和答案用 <!-- ANSWER_SPLIT --> 分隔。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "questions", "paper_size"]
    },
    "final_exam": {
        "name": "压轴卷",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是{grade}{subject}竞赛辅导专家。生成高难度压轴试卷：\n\n"
            "【方向】{user_prompt}\n\n"
            "【候选题目】{questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【要求】\n"
            "1. 侧重综合运用和创新思维\n"
            "2. 难度：中档20%、难题50%、压轴30%\n"
            "3. 至少2道综合压轴大题\n"
            "4. 每题标难度星级\n"
            "5. 答案含完整解题步骤\n"
            "6. 知识点覆盖{knowledge_tags}\n"
            "7. 适合{paper_size}纸打印\n\n"
            "输出HTML，试卷答案用 <!-- ANSWER_SPLIT --> 分隔。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "questions", "knowledge_tags", "paper_size"]
    },
    "topic_exam": {
        "name": "专题卷",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是{subject}专题教学专家。围绕专题生成针对性训练卷：\n\n"
            "【专题】{user_prompt}\n\n"
            "【相关题目】{questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积），只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "大题标题格式: 一、选择题  二、填空题 ……\n"
            "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
            "【要求】\n"
            "1. 紧密围绕知识点：{knowledge_tags}\n"
            "2. 由浅入深，覆盖全面\n"
            "3. 含概念理解、基础应用、综合提高三层次\n"
            "4. 开头加知识框架图\n"
            "5. 每题标注考察知识点\n"
            "6. 答案提供易错点提示\n"
            "7. 适合{paper_size}纸打印\n\n"
            "输出HTML，试卷答案用 <!-- ANSWER_SPLIT --> 分隔。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "questions", "knowledge_tags", "paper_size"]
    },
    "worksheet": {
        "name": "学习单",
        "type": "preset",
        "is_default": True,
        "content": (
            "你是{grade}{subject}教学专家。请根据以下主题和资料生成一份学习单。\n\n"
            "【主题/学习目标】{user_prompt}\n\n"
            "【相关概念与笔记】\n{concept_notes}\n\n"
            "【例题】\n{example_questions}\n\n"
            "【练习题】\n{practice_questions}\n\n"
            "【样式要求】\n"
            "禁止任何灰色/纯色色块填充区域，只使用白色背景、深灰色细文字、1px灰色细边框\n"
            "学习单结构分三部分：\n"
            "1. 概念讲解：占较大篇幅，用清晰的语言讲解核心概念、公式、定理，配以必要示例\n"
            "2. 典型例题：2-4道，每题包含完整解析和关键步骤说明\n"
            "3. 巩固练习：3-6道，题目下方预留答题空白（至少3-5行）\n"
            "数学公式用LaTeX，$$...$$独立公式，$...$行内公式\n"
            "适合{paper_size}纸打印，布局宽松、强调阅读体验\n\n"
            "输出完整HTML，学习单内容用 <!-- WORKSHEET_SPLIT --> 分隔答案/解析（解析放在后半段，前半段为学习单主体）。"
        ),
        "blocks": ["grade", "subject", "user_prompt", "concept_notes", "example_questions", "practice_questions", "paper_size"]
    }
}

_default_settings = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-v4-pro",
    "kimi_api_key": "",
    "kimi_base_url": "https://api.moonshot.cn/v1",
    "kimi_model": "kimi-k2.6",
    "zhipuai_api_key": "",  # 智谱AI GLM，主力视觉/OCR模型（glm-4v-flash免费）
    "zhipuai_base_url": "https://open.bigmodel.cn/api/paas/v4",
    "zhipuai_model": "glm-4v-flash",  # GLM 模型名（可改为 glm-4v / glm-4-flash 等）
    "zhipuai_max_tokens": 4096,
    "deepseek_max_tokens": 16384,
    "kimi_max_tokens": 4096,
    "custom_openai_key": "",
    "custom_openai_url": "",
    "custom_openai_model": "",
    "custom_openai_scopes": [],
    "custom_apis": [],
    # 小米 MiMo（token plan CN 网关）：视觉备用链路 + TTS 配音
    "xiaomi_token_plan_api_key": "",
    "xiaomi_token_plan_base_url": "https://token-plan-cn.xiaomimimo.com/v1",
    "xiaomi_vision_model": "mimo-v2.5",
    "xiaomi_tts_model": "mimo-v2.5-tts",
    "xiaomi_tts_voice": "mimo_default",
    "focus_voice_engine": "auto",  # auto: 有小米 Key 优先服务器配音, browser: 仅浏览器合成
    "ai_timeout": 900,
    "quote_topic": "",
    "dark_mode_auto": False,
    "dark_mode_start": "18:00",
    "dark_mode_end": "06:00",
    "night_patrol_enabled": True,
    "night_patrol_start": "01:00",
    "night_patrol_end": "05:00",
    "accent_color": "",  # 空=默认钢蓝; 可选: steel/amber/jade/mint/rose/custom
    "accent_custom_light": "",  # 自定义HEX(浅色主题accent)
    "accent_custom_dark": "",   # 自定义HEX(深色主题accent)
    "auto_bank_threshold": 5,    # 标签自动建库阈值
    "search_mode": "text",       # 搜题模式: text/ai/hybrid
    "auto_add_new_to_bank": False,  # 批改时识别到新题是否自动加入题库
    "cors_allowed_origins": [],  # 允许跨域的来源列表；空或无效时使用 DEFAULT_CORS_ORIGINS
    "api_password": "",  # API 访问密码，空时使用默认值；可通过环境变量 LEARNING_AGENT_PASSWORD 覆盖
}

_settings_lock = threading.RLock()


def _atomic_write_json(path: str, data: object) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".tmp-", suffix=".json", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _atomic_write_text(path: str, content: str) -> None:
    """在目标目录内 fsync 后原子替换文本文件。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False
        ) as temp_file:
            temp_path = temp_file.name
            temp_file.write(str(content))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def load_settings() -> dict:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            out = dict(_default_settings)
            out.update(data)
            # 统一 api.txt 中的 key 仅作空值兜底：项目 settings.json 显式保存的
            # Key 优先生效（2026-08-25 用户决策），共享文件不再覆盖本项目配置。
            _api_mappings = {
                "deepseek_api_key": "deepseek",
                "kimi_api_key": "kimi",
            }
            for setting_key, api_name in _api_mappings.items():
                current_value = out.get(setting_key, "")
                if isinstance(current_value, str) and current_value.strip():
                    continue
                api_val = get_api_key(api_name, fallback="")
                if api_val:
                    out[setting_key] = api_val
            return out
    except (json.JSONDecodeError, IOError, OSError) as e:
        log_error("config", f"settings.json corrupted, resetting: {e}")
    return dict(_default_settings)


def load_cors_origins() -> list[str]:
    """加载 CORS 允许来源。settings.json 中的 cors_allowed_origins 必须是字符串列表。

    - 仅保留符合 CORS_ORIGIN_RE 的 origin，拒绝 *、userinfo、path 等危险项。
    - 若配置项缺失、无效或过滤后为空，回退到 DEFAULT_CORS_ORIGINS。
      如需显式禁止所有跨域来源，请在列表中保留一个占位字符串或从设置页移除该键。
    """
    try:
        origins = load_settings().get("cors_allowed_origins")
        if isinstance(origins, list):
            clean = [str(o).strip() for o in origins if isinstance(o, str) and CORS_ORIGIN_RE.match(str(o).strip())]
            if clean:
                return clean
    except Exception as e:
        log_error("config", f"load_cors_origins failed: {e}")
    return list(DEFAULT_CORS_ORIGINS)


CORS_ALLOWED_ORIGINS = load_cors_origins()


def save_settings(data: dict):
    merged = dict(_default_settings)
    merged.update(data)
    try:
        with _settings_lock:
            _atomic_write_json(SETTINGS_FILE, merged)
    except (IOError, OSError) as e:
        log_error("config", f"save_settings failed: {e}")
        raise


def get_setting(key: str, default=""):
    return load_settings().get(key, default)


PAPER_CONFIGS_DIR = os.path.join(STORAGE_DIR, "paper_configs")
_PAPER_CONFIG_ID_RE = re.compile(r"^[a-f0-9]{12}$")

for d in [STORAGE_DIR, QUESTIONS_DIR, PAPERS_DIR, CORRECTIONS_DIR, PAPER_CONFIGS_DIR]:
    os.makedirs(d, exist_ok=True)


# ── 以下三个函数已废弃（deprecated）─────────────────────────────────
# 组卷参数目前统一由 services/config_service.py 的 DB 版实现维护；
# 这些文件版实现已无任何调用方，仅保留兼容，请勿新增引用。
def save_paper_config(data: dict) -> str:
    """[已废弃] 组卷参数请使用 services.config_service.save_paper_config（DB 版）。"""
    import hashlib, json, time
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    cid = hashlib.md5((raw + str(time.time())).encode()).hexdigest()[:12]
    path = os.path.join(PAPER_CONFIGS_DIR, f"{cid}.json")
    try:
        _atomic_write_json(path, data)
    except (IOError, OSError) as e:
        log_error("config", f"save_paper_config failed: {e}")
        raise
    return cid


def load_paper_config(config_id: str) -> dict:
    """[已废弃] 组卷参数请使用 services.config_service.load_paper_config（DB 版）。"""
    if not isinstance(config_id, str) or not _PAPER_CONFIG_ID_RE.fullmatch(config_id):
        return {}
    path = os.path.join(PAPER_CONFIGS_DIR, f"{config_id}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            log_error("config", f"load_paper_config failed for {config_id}: {e}")
    return {}


def list_paper_configs(limit: int = 20) -> list[dict]:
    """[已废弃] 组卷参数请使用 services.config_service.list_paper_configs（DB 版）。"""
    files = sorted(
        [f for f in os.listdir(PAPER_CONFIGS_DIR) if f.endswith(".json")],
        key=lambda f: os.path.getmtime(os.path.join(PAPER_CONFIGS_DIR, f)),
        reverse=True
    )[:limit]
    result = []
    for fname in files:
        path = os.path.join(PAPER_CONFIGS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({"id": fname[:-5], "data": data,
                           "title": data.get("title", "") or f"{data.get('grade','')}{data.get('subject','')}试卷",
                           "time": os.path.getmtime(path)})
        except (json.JSONDecodeError, IOError, OSError, TypeError, ValueError) as e:
            log_error("config", f"list_paper_configs skipped {fname}: {e}")
    return result
