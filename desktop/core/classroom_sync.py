"""课堂笔记同步器（round 57）：课堂文字流 + AI 补讲记录 → 学习 Agent 服务器。

设计：Design.md「成果互通：课堂笔记同步到学习 Agent（round 57）」。

- 数据源：ClassroomMonitor.recent()（转写滚动窗）+ ClassroomCoach 补讲日志
- 形态：组装一篇 Markdown 笔记 POST {server}/api/notes（title/content/
  knowledge_tags/subject），手机端「笔记」页直接可见
- 增量水位：data/classroom/sync_state.json 记 last_synced_ts，只传水位之后的
  条目，上传成功才推进（服务端无去重，客户端保证幂等）
- 失败：记 Err 不推进水位，下次定时自然重试；转写 jsonl 本就按天落盘不丢
- 隐私：只上传转写文字与补讲记录，原始音频绝不离开本机；同步开关默认关
"""
import json
import os
import threading
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer

from config.settings import ConfigManager
from core.classroom_stream import CLASSROOM_DIR
from utils.helpers import logger, append_err_record, now_cst

import requests

SYNC_STATE_PATH = os.path.join(CLASSROOM_DIR, "sync_state.json")
# 补讲日志上限（防长课堂内存无界）
TEACH_LOG_MAX = 100
# 单次上传笔记正文上限（服务端 content 上限 500KB，这里远低于它）
NOTE_CONTENT_MAX = 100_000


def _load_state() -> dict:
    try:
        with open(SYNC_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    try:
        os.makedirs(CLASSROOM_DIR, exist_ok=True)
        tmp = SYNC_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, SYNC_STATE_PATH)
    except OSError as e:
        logger.warning(f"同步水位保存失败: {str(e)[:150]}")


class ClassroomSync(QObject):
    """课堂笔记同步器：定时/手动把增量转写与补讲记录上传学习 Agent。"""

    def __init__(self, monitor, coach=None, parent=None):
        super().__init__(parent)
        self._monitor = monitor
        self._coach = coach
        self._teach_log = deque(maxlen=TEACH_LOG_MAX)   # 补讲日志（有界）
        self._timer = QTimer(self)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self.trigger_sync)
        self._syncing = False          # 工作线程在途标志（防重叠上传）
        self._lock = threading.Lock()

    # ---------- 接线 ----------

    def start(self):
        s = ConfigManager().settings
        interval_min = max(5, min(720, int(getattr(s, "classroom_sync_interval_min", 30) or 30)))
        self._timer.start(interval_min * 60 * 1000)
        logger.info(f"课堂笔记同步器已启动（每 {interval_min} 分钟）")

    def shutdown(self):
        try:
            self._timer.stop()
        except RuntimeError:
            pass

    # ---------- 补讲日志（coach 通过 teach_logger 回调调用） ----------

    def add_teach(self, teach_point: str, speech: str, source: str = "ai"):
        """coach 补讲完成后登记（主线程调用）。"""
        self._teach_log.append({
            "ts": now_cst().isoformat(),
            "point": str(teach_point or "")[:200],
            "speech": str(speech or "")[:500],
            "source": str(source or "ai"),
        })

    # ---------- 组装 ----------

    def _collect_entries(self, since_ts: str) -> list:
        """收集水位之后的转写条目（ts 升序，按 ts 去重）。

        recent() 是 400 条滚动窗口：窗口滑动快于同步间隔时，未同步的早期
        条目会被挤出窗口。此时回读按天落盘的 jsonl（当日+前日）补漏，
        与窗口合并——jsonl 落盘是 round 55 的既有机制，兜底不丢。"""
        try:
            entries = list(self._monitor.recent(400) or [])
        except Exception:
            entries = []
        by_ts = {}
        for e in entries:
            ts = str(e.get("ts", ""))
            if since_ts and ts <= since_ts:
                continue
            if ts:
                by_ts[ts] = e
        # 窗口未覆盖水位（最老窗口条目都晚于水位）→ 有条目被挤出，回读 jsonl
        if since_ts and entries:
            try:
                oldest = min(str(e.get("ts", "")) for e in entries if e.get("ts"))
            except ValueError:
                oldest = ""
            if oldest and oldest > since_ts:
                for e in self._read_jsonl_since(since_ts):
                    ts = str(e.get("ts", ""))
                    if ts and ts not in by_ts:
                        by_ts[ts] = e
        return [by_ts[k] for k in sorted(by_ts)]

    def _read_jsonl_since(self, since_ts: str) -> list:
        """回读当日与前日的转写落盘（jsonl 每行一条 entry JSON）。"""
        from datetime import timedelta
        days = [now_cst().strftime("%Y%m%d"),
                (now_cst() - timedelta(days=1)).strftime("%Y%m%d")]
        out = []
        for d in set(days):
            path = os.path.join(CLASSROOM_DIR, f"{d}.jsonl")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(e, dict) and str(e.get("ts", "")) > since_ts:
                            out.append(e)
            except OSError:
                continue    # 文件不存在/不可读：跳过该日
        if not getattr(ConfigManager().settings, "classroom_persist_transcript", True):
            logger.warning("课堂转写落盘已关闭，滚动窗口外的早期条目无法补漏（可能漏传）")
        return out

    def _collect_teaches(self, since_ts: str) -> list:
        """补讲日志在同步器自身（coach 经 teach_logger 回调登记）。"""
        log = list(self._teach_log)
        out = [t for t in log if not since_ts or str(t.get("ts", "")) > since_ts]
        out.sort(key=lambda t: str(t.get("ts", "")))
        return out

    def build_note(self, entries: list, teaches: list) -> dict:
        """组装笔记 payload：标题/正文/标签/学科。"""
        now = now_cst()
        title = f"课堂笔记 {now.month}月{now.day}日 {now.hour:02d}:{now.minute:02d}"
        lines = []
        teach_ts = {t["ts"] for t in teaches}
        merged = []
        for e in entries:
            merged.append(("entry", str(e.get("ts", "")), e))
        for t in teaches:
            merged.append(("teach", str(t.get("ts", "")), t))
        merged.sort(key=lambda x: x[1])
        for kind, _ts, item in merged:
            if kind == "entry":
                speaker = "老师" if item.get("speaker") == "teacher" else "学生"
                dur = item.get("dur", 0)
                lines.append(f"- **[{speaker}]** ({dur:.1f}s) {item.get('text', '')}")
            else:
                src = "学生提问" if item.get("source") == "student" else "AI 主动补讲"
                lines.append(f"> **🤖 AI 补讲（{src}）**：{item.get('point', '')}")
                if item.get("speech"):
                    lines.append(f"> {item['speech']}")
        content = "\n\n".join([
            f"# {title}", "",
            f"- 同步时间：{now.isoformat(timespec='seconds')}",
            f"- 覆盖：转写 {len(entries)} 条 / AI 补讲 {len(teaches)} 次", "",
            *lines])[:NOTE_CONTENT_MAX]
        tags = []
        if any(t.get("source") == "student" for t in teaches):
            tags.append("课堂提问")
        if len(entries) >= 10:
            tags.append("课堂记录")
        return {"title": title, "content": content,
                "knowledge_tags": tags, "subject": "", "grade": ""}

    # ---------- 上传 ----------

    def trigger_sync(self):
        """定时/手动触发：工作线程执行，防重叠。"""
        if self._syncing:
            logger.info("上一轮同步仍在途，跳过本轮")
            return
        s = ConfigManager().settings
        if not getattr(s, "classroom_sync_enabled", False):
            return
        if self._monitor is None or not getattr(self._monitor, "running", False):
            return
        self._syncing = True

        def _job():
            try:
                self._sync_job_inner()
            finally:
                self._syncing = False

        try:
            threading.Thread(target=_job, daemon=True,
                             name="ClassroomSync").start()
        except Exception as e:
            # start 失败必须复位标志，否则同步永久静默死亡（同 coach C1 修法）
            self._syncing = False
            logger.error(f"同步工作线程启动失败: {type(e).__name__}: {str(e)[:150]}")
            try:
                append_err_record("core/classroom_sync.py", "同步线程启动失败",
                                  f"{type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass

    def _sync_job_inner(self):
        try:
            state = _load_state()
            since = str(state.get("last_synced_ts", ""))
            entries = self._collect_entries(since)
            teaches = self._collect_teaches(since)
            if not entries and not teaches:
                return
            note = self.build_note(entries, teaches)
            base = str(getattr(ConfigManager().settings, "sync_server_url", "")
                       or "").strip().rstrip("/")
            if not base:
                logger.warning("sync_server_url 未配置，跳过同步")
                return
            # 修复（2026-09-11 审计确证）：原实现 `requests.post(..., json=note)` **不带任何鉴权头**，
            # 而服务端 password_guard（backend/main.py:283/303）对一切 `/api/` 路径都要求密码：
            # 只要服务器配了密码（生产必然配），这里就恒定 401 → raise_for_status() 抛错 →
            # 水位永不推进 → **课堂笔记从来没有同步成功过**，且只留一条 warning、用户无感知。
            # 对照：拉题侧 lecture_engine._headers() 一直是带的。
            headers = {"Content-Type": "application/json"}
            _pwd = str(getattr(ConfigManager().settings, "sync_server_password", "") or "").strip()
            if _pwd:
                headers["X-Auth-Token"] = _pwd
            else:
                logger.warning(
                    "sync_server_password 未配置：若服务器启用了密码，本次同步将返回 401。"
                    "请在设置中填写与服务器一致的密码。"
                )
            resp = requests.post(f"{base}/api/notes", json=note, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            new_ts = max(
                [str(e.get("ts", "")) for e in entries]
                + [str(t.get("ts", "")) for t in teaches] + [since])
            state["last_synced_ts"] = new_ts
            _save_state(state)
            note_id = ""
            try:
                note_id = str((data or {}).get("id", ""))   # 服务端返回 {"id", "message"}
            except AttributeError:
                pass
            logger.info(f"课堂笔记已同步：转写 {len(entries)} 条 / 补讲 {len(teaches)} 次"
                        f" -> {note_id or 'ok'}")
        except requests.RequestException as e:
            logger.warning(f"课堂笔记同步失败（不推进水位，下次重试）: {str(e)[:200]}")
            try:
                append_err_record("core/classroom_sync.py", "同步网络失败", str(e)[:300])
            except Exception:
                pass
        except Exception as e:
            logger.error(f"课堂同步异常: {type(e).__name__}: {str(e)[:180]}")
            try:
                append_err_record("core/classroom_sync.py", "同步异常",
                                  f"{type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
        finally:
            self._syncing = False
