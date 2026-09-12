"""OISystem 工具模块：时间/JSON/文件/日志/状态码等基础工具。

设计参考 zzoi/utils/helpers.py，扩展为 OISystem 所需的当日日志结构化能力。
"""
import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# 运行时根目录：打包后以 exe 所在目录为根，开发时以项目根为根
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DIALOGS_DIR = os.path.join(DATA_DIR, "dialogs")
DAILY_LOGS_DIR = os.path.join(DATA_DIR, "daily")  # 当日结构化日志（JSON）
DAILY_MD_DIR = os.path.join(LOGS_DIR, "daily")    # 当日人类可读日志（Markdown）
ERR_MD_PATH = os.path.join(BASE_DIR, "Err.md")    # 错误追踪文档


def ensure_dirs():
    """确保所有运行时目录存在。"""
    for d in [DATA_DIR, LOGS_DIR, DIALOGS_DIR, DAILY_LOGS_DIR, DAILY_MD_DIR]:
        os.makedirs(d, exist_ok=True)


def append_err_record(module: str, title: str, detail: str):
    """自动将运行时错误追加到 Err.md。"""
    try:
        now_str = format_time(now_cst())
        # 每行需含 | 分隔
        line = f"| {now_str[:10]} | {module[:20]} | {title[:40]} | {detail[:60]} | ⏳ |\n"
        with open(ERR_MD_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def load_json(filepath: str, default=None):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}
    except OSError as e:
        # round48：权限/磁盘等系统错误也不能让启动流程崩溃，记录后按缺失处理
        try:
            logger.warning(f"读取 JSON 失败: {filepath}: {e}")
        except Exception:
            pass
        return default if default is not None else {}


def save_json(filepath: str, data):
    """原子写 JSON：先写临时文件再 replace，避免进程中断写坏配置。"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except OSError:
        # 原子替换失败（只读介质/权限）时退回直接写入，保证功能仍可用
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


def now_cst() -> datetime:
    return datetime.now(CST)


def format_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return now_cst().strftime("%Y%m%d")


def daily_log_path(date_str: str = None) -> str:
    """返回当日结构化日志路径，如 data/daily/20260720.json"""
    return os.path.join(DAILY_LOGS_DIR, f"{date_str or today_str()}.json")


def setup_logger(name: str = "OISystem") -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        ensure_dirs()
        fh = logging.FileHandler(
            os.path.join(DATA_DIR, "app.log"),
            encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        # Windows GBK 控制台无法输出 emoji/中文特殊符号，强制 UTF-8 避免 UnicodeEncodeError
        try:
            ch.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(ch)
    return log


logger = setup_logger()


def safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in s)


# ============ 当日结构化日志 ============

def _empty_daily_log() -> dict:
    return {
        "date": today_str(),
        "events": [],          # 时间线事件
        "focus_sessions": [],  # 专注模式会话
        "ai_errors_found": [], # AI 指出的错误
        "dialog_summaries": [],# 对话摘要
        "danger_ops": [],      # 危险操作
        "reminders": 0,        # 提醒次数
        "submission_errors": {},  # 提交错误类型 -> 数量
        "site_blocks": [],     # 被掐掉的网站
    }


# round48：多线程（ZZOI 自动检查/手动检查/对话存档）都会调用 log_event，
# 读-改-写当日 JSON 必须串行化，避免并发写坏或丢失事件。
_DAILY_LOG_LOCK = threading.Lock()


def _normalize_daily_log(data: dict) -> dict:
    """把历史遗留/手改/损坏的 JSON 结构自愈为标准结构，防止 UI/同步代码崩溃。"""
    base = _empty_daily_log()
    base["date"] = data.get("date") or base["date"]
    for key in ("events", "focus_sessions", "ai_errors_found",
                "dialog_summaries", "danger_ops", "site_blocks"):
        v = data.get(key)
        if isinstance(v, list):
            # round48：复制元素 dict 后再修改 detail，保证坏 detail 的修复能被
            # normalized != data 检测到并落盘（否则原地改原对象导致比较相等跳过保存）
            base[key] = [dict(x) for x in v if isinstance(x, dict)]
        else:
            base[key] = []
    rem = data.get("reminders", 0)
    try:
        base["reminders"] = max(0, int(rem))
    except (TypeError, ValueError, OverflowError):
        base["reminders"] = 0
    sub = data.get("submission_errors", {})
    if isinstance(sub, dict):
        cleaned = {}
        for k, v in sub.items():
            try:
                cleaned[str(k)] = max(0, int(v))
            except (TypeError, ValueError, OverflowError):
                cleaned[str(k)] = 0
        base["submission_errors"] = cleaned
    else:
        base["submission_errors"] = {}
    # round48：事件/危险操作内层的 detail 也必须 dict，否则渲染 MD 时 .get 崩溃
    for key in ("events", "danger_ops"):
        for entry in base[key]:
            if isinstance(entry, dict) and not isinstance(entry.get("detail"), dict):
                entry["detail"] = {}
    return base


def get_today_log() -> dict:
    path = daily_log_path()
    data = load_json(path, None)
    if not isinstance(data, dict) or not data:
        data = _empty_daily_log()
        save_json(path, data)
        return data
    normalized = _normalize_daily_log(data)
    if normalized != data:
        save_json(path, normalized)  # 自愈写回，避免每次读取都重复兜底
    return normalized


def log_event(event_type: str, detail: dict):
    """追加一条事件到当日日志。

    event_type 取值：focus_start/focus_emergency_exit/focus_normal_exit/
    ai_error_found/dialog_summary/danger_op/site_block/reminder/submission_error 等。
    """
    with _DAILY_LOG_LOCK:
        path = daily_log_path()  # 单次取路径，避免跨午夜写错日期
        # round48：统一经 get_today_log 自愈，历史坏 JSON（events 非 list）不再抛 AttributeError
        data = get_today_log()
        entry = {
            "ts": format_time(now_cst()),
            "type": event_type,
            "detail": detail,
        }
        data["events"].append(entry)
        if event_type == "reminder":
            data["reminders"] = data.get("reminders", 0) + 1
        elif event_type == "danger_op":
            data["danger_ops"].append(entry)
        elif event_type == "site_block":
            data["site_blocks"].append(detail)
        elif event_type == "ai_error_found":
            data["ai_errors_found"].append(detail)
        elif event_type == "dialog_summary":
            data["dialog_summaries"].append(detail)
        elif event_type == "submission_error":
            err_type = detail.get("type", "unknown") if isinstance(detail, dict) else "unknown"
            data["submission_errors"][err_type] = data["submission_errors"].get(err_type, 0) + 1
        save_json(path, data)
        logger.info(f"[daily-log] {event_type}: {detail}")
    # 同步更新人类可读的 Markdown 日志（去抖：最多每5秒写一次）
    _sync_daily_md(today_str())


# MD 写入去抖缓存
_last_md_write: str = ""
_last_md_write_time: float = 0

def _sync_daily_md(date_str: str):
    """带去抖的 MD 日志写入，最多每5秒重写一次。"""
    global _last_md_write, _last_md_write_time
    now = time.time()
    if date_str == _last_md_write and now - _last_md_write_time < 5.0:
        return
    try:
        write_daily_markdown(date_str)
        _last_md_write = date_str
        _last_md_write_time = now
    except Exception:
        pass


def daily_md_path(date_str: str = None) -> str:
    """返回当日人类可读日志路径，如 logs/daily/20260720.md"""
    return os.path.join(DAILY_MD_DIR, f"{date_str or today_str()}.md")


def write_daily_markdown(date_str: str = None):
    """将 JSON 结构化日志渲染为人类可读的 Markdown 文件。"""
    date_str = date_str or today_str()
    data = load_json(daily_log_path(date_str), None)
    if not isinstance(data, dict):
        return
    data = _normalize_daily_log(data)

    lines = []
    lines.append(f"# {date_str[:4]}-{date_str[4:6]}-{date_str[6:]} 学习记录")
    lines.append("")
    lines.append("> 由 OISystem 自动整理。")
    lines.append("")

    events = data.get("events", []) or []
    focus_sessions = [e for e in events if isinstance(e, dict) and e.get("type","").startswith("focus_")]
    dialog_summaries = data.get("dialog_summaries", [])
    ai_errors = data.get("ai_errors_found", [])
    danger_ops = data.get("danger_ops", [])
    site_blocks = data.get("site_blocks", [])
    sub_errors = data.get("submission_errors", {})
    reminders = data.get("reminders", 0)

    # 专注模式
    if focus_sessions:
        lines.append("## 专注模式")
        for s in focus_sessions:
            ts = s.get("ts", "")[11:16]
            etype = s.get("type", "")
            detail = s.get("detail", {})
            if not isinstance(detail, dict):
                detail = {}
            dur = detail.get("duration_minutes", detail.get("remaining_seconds", 0))
            if etype == "focus_start":
                lines.append(f"- {ts} 开始专注（{dur}分钟）")
            elif etype == "focus_emergency_exit":
                lines.append(f"- {ts} ⚠️ 急事退出：{detail.get('reason','')}")
            elif etype == "focus_normal_exit":
                lines.append(f"- {ts} ✅ 正常退出")
            elif etype == "focus_problem_exit_started":
                lines.append(f"- {ts} ⏳ 做题退出：等待完成分配的题目")
            elif etype == "focus_problem_solved_exit":
                pid = detail.get("pid", "")
                title = detail.get("title", "")
                lines.append(f"- {ts} ✅ 做题通过退出（{pid} {title}）")
            elif etype == "focus_problem_exit_cancelled":
                lines.append(f"- {ts} ↩️ 取消做题流程，继续专注")
            elif etype == "focus_normal_complete":
                lines.append(f"- {ts} ✅ 专注自然结束")
            elif etype == "focus_force_released":
                lines.append(f"- {ts} 🔓 强制释放")
            elif etype == "focus_locked_for_zzoi":
                lines.append(f"- {ts} 🔒 ZZOI 锁定")
        lines.append("")

    # AI 对话
    if dialog_summaries:
        lines.append("## AI 对话摘要")
        for d in dialog_summaries:
            summary = d.get("summary", "")[:100]
            msg_count = d.get("message_count", "?")
            lines.append(f"- 对话（{msg_count}条）: {summary}")
        lines.append("")

    # AI 指出的错误
    if ai_errors:
        lines.append("## AI 指出的错误")
        err_counts = {}
        for e in ai_errors:
            err_text = e.get("error_summary", str(e)[:40])
            err_counts[err_text] = err_counts.get(err_text, 0) + 1
        for err, cnt in sorted(err_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {err}（{cnt}次）")
        lines.append("")

    # 提交错误
    if sub_errors:
        lines.append("## 提交错误统计")
        for err_type, cnt in sorted(sub_errors.items(), key=lambda x: -x[1]):
            lines.append(f"- {err_type}: {cnt}次")
        lines.append("")

    # 危险操作
    if danger_ops:
        lines.append("## 危险操作记录")
        for op in danger_ops:
            ts = op.get("ts", "")[11:16]
            detail = op.get("detail", {})
            lines.append(f"- {ts} {detail}")
        lines.append("")

    # 网站拦截
    if site_blocks:
        lines.append("## 网站拦截")
        for b in site_blocks:
            if isinstance(b, dict):
                lines.append(f"- {b.get('site', b.get('url','?'))}")
            else:
                lines.append(f"- {b}")
        lines.append("")

    # 提醒
    if reminders:
        lines.append(f"## 提醒次数: {reminders}")
        lines.append("")

    lines.append("---")
    lines.append(f"*由 OISystem 于 {format_time(now_cst())} 自动生成*")
    lines.append("")

    path = daily_md_path(date_str)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def parse_oj_time(time_str: str) -> datetime:
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(time_str, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt
    except (ValueError, TypeError):
        return now_cst()


STATUS_MAP = {
    "Accepted": "AC",
    "Wrong Answer": "WA",
    "Time Limit Exceeded": "TLE",
    "Memory Limit Exceeded": "MLE",
    "Runtime Error": "RE",
    "Compile Error": "CE",
    "Segmentation Fault": "RE",
    "Output Limit Exceeded": "OLE",
    "Pending": "PD",
    "Pending Rejudge": "PR",
    "No Testdata": "NODATA",
    "Unknown Error": "UKE",
    "Format Error": "FE",
    "Judgement Failed": "JF",
    "System Error": "SE",
    "Cancelled": "CAN",
    "Ignore": "IGN",
    "Hacked": "HACK",
}

CN_STATUS_MAP = {
    "正确": "AC",
    "答案错误": "WA",
    "时间超限": "TLE",
    "内存超限": "MLE",
    "运行错误": "RE",
    "编译错误": "CE",
    "格式错误": "PE",
    "输出超限": "OLE",
    "等待": "PD",
    "评测中": "JUDGING",
}


def normalize_status(status_text: str) -> str:
    s = status_text.strip()
    if s in STATUS_MAP:
        return STATUS_MAP[s]
    for k, v in STATUS_MAP.items():
        if k.lower() in s.lower():
            return v
    for cn_k, v in CN_STATUS_MAP.items():
        if cn_k in s:
            return v
    return s[:10]
