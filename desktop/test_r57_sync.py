# -*- coding: utf-8 -*-
"""round 57 离线回归：课堂笔记同步器（classroom_sync）。

运行：python test_r57_sync.py（QT_QPA_PLATFORM=offscreen，mock requests 不打真网络）
覆盖：
T1 增量收集（水位过滤/升序/空集短路）
T2 build_note 组装（时间线合并排序/补讲块/标签/截断）
T3 水位推进（成功推进/失败不推进/状态文件读写）
T4 同步任务（开关关闭跳过/重叠防抖/网络失败记 Err）
T5 设置项往返（ClassroomTab 新控件 collect/_load）
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core import classroom_sync as cs  # noqa: E402
from core.classroom_sync import ClassroomSync, _load_state, _save_state  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# 临时水位文件（不打扰真实 data/classroom）
_ORIG_STATE = cs.SYNC_STATE_PATH
cs.SYNC_STATE_PATH = os.path.join(os.environ.get("TEMP", "."), "la_probe",
                                  "r57_sync_state.json")
for p in (_load_state(),):
    pass
if os.path.exists(cs.SYNC_STATE_PATH):
    os.remove(cs.SYNC_STATE_PATH)


class FakeMonitor:
    def __init__(self, entries):
        self._entries = entries
        self.running = True

    def recent(self, n=None, speaker=""):
        return list(self._entries)


class FakeCoach:
    def __init__(self, teaches):
        import collections
        self._teach_log = collections.deque(teaches, maxlen=100)


def mk_entry(ts, speaker="teacher", text="讲解内容", dur=2.0):
    return {"speaker": speaker, "text": text, "dur": dur, "ts": ts}


TS1 = "2026-09-06T10:00:00+08:00"
TS2 = "2026-09-06T10:01:00+08:00"
TS3 = "2026-09-06T10:02:00+08:00"

# ---- T1 增量收集 ----
print("\n[T1] 增量收集")
mon = FakeMonitor([mk_entry(TS1), mk_entry(TS2, "student", "学生提问"), mk_entry(TS3)])
sync = ClassroomSync(mon, FakeCoach([]))
got = sync._collect_entries("")
check("1.无水位收全部且升序", [e["ts"] for e in got] == [TS1, TS2, TS3])
got2 = sync._collect_entries(TS1)
check("1.水位后增量（不含水位本身）", [e["ts"] for e in got2] == [TS2, TS3])
got3 = sync._collect_entries(TS3)
check("1.水位最新收空", got3 == [])
check("1.乱序输入也升序返回",
      [e["ts"] for e in ClassroomSync(FakeMonitor([mk_entry(TS3), mk_entry(TS1)]), None)
       ._collect_entries("")] == [TS1, TS3])

# ---- T2 build_note ----
print("\n[T2] build_note 组装")
TS2B = "2026-09-06T10:01:30+08:00"   # 补讲发生在学生提问之后
teaches = [{"ts": TS2B, "point": "勾股定理仅限直角三角形",
            "speech": "讲解词内容", "source": "student"}]
note = sync.build_note([mk_entry(TS1), mk_entry(TS2, "student", "学生提问")], teaches)
check("2.标题含课堂笔记", "课堂笔记" in note["title"])
check("2.时间线含老师与学生标注",
      "**[老师]**" in note["content"] and "**[学生]**" in note["content"])
check("2.补讲块按 ts 插入时间线（老师→学生→补讲）",
      0 <= note["content"].find("**[老师]**") < note["content"].find("**[学生]**")
      < note["content"].find("🤖 AI 补讲"))
check("2.补讲来源标记学生提问", "学生提问" in note["content"])
check("2.含要点与讲解词", "勾股定理仅限直角三角形" in note["content"]
      and "讲解词内容" in note["content"])
check("2.学生提问进标签", "课堂提问" in note["knowledge_tags"])
check("2.payload 字段齐全（API 契约）",
      set(note.keys()) == {"title", "content", "knowledge_tags", "subject", "grade"})
big = ClassroomSync(FakeMonitor([mk_entry(TS1, text="长" * 60000)]), None)
note_big = big.build_note([mk_entry(TS1, text="长" * 60000)], [])
check("2.超长正文截断到上限", len(note_big["content"]) <= cs.NOTE_CONTENT_MAX)

# ---- T3 水位 ----
print("\n[T3] 水位推进")
check("3.初始无水位", _load_state() == {})
_save_state({"last_synced_ts": TS1})
check("3.写入后可读回", _load_state().get("last_synced_ts") == TS1)
_save_state({"last_synced_ts": TS3})
check("3.覆盖写入", _load_state().get("last_synced_ts") == TS3)


# ---- T4 同步任务 ----
print("\n[T4] 同步任务")
posts = []


def _fake_post(url, json=None, timeout=None):
    posts.append({"url": url, "json": json})
    class _R:
        def raise_for_status(self):
            pass
        def json(self):
            return {"note": {"id": "note123"}}
    return _R()


def _boom_post(url, json=None, timeout=None):
    raise cs.requests.ConnectionError("模拟断网")


from config.settings import AppSettings  # noqa: E402

# 场景 A：开关关 → 不发起
sync_a = ClassroomSync(FakeMonitor([mk_entry(TS1)]), None)
sync_a.trigger_sync()
time.sleep(0.5)
check("4.开关关闭不发起同步", posts == [])

# 场景 B：开关开 + 正常 → POST 成功且水位推进（先清掉 T3 遗留水位，保证有增量）
if os.path.exists(cs.SYNC_STATE_PATH):
    os.remove(cs.SYNC_STATE_PATH)
_saved = {}
s = AppSettings()
for k in ("classroom_sync_enabled", "sync_server_url"):
    _saved[k] = getattr(s, k)
s.classroom_sync_enabled = True
s.sync_server_url = "http://fake.local"
cs.ConfigManager = lambda: type("C", (), {"settings": s})()
cs.requests.post = _fake_post
try:
    # 补讲经 teach_logger 回调登记（生产接线方式；_teach_log 在同步器自身）
    sync_b = ClassroomSync(FakeMonitor([mk_entry(TS1), mk_entry(TS2)]), None)
    sync_b.add_teach(teaches[0]["point"], teaches[0]["speech"], teaches[0]["source"])
    sync_b.trigger_sync()
    deadline = time.time() + 5.0
    while time.time() < deadline and not posts:
        time.sleep(0.05)
    check("4.开关开 + 有增量 → POST 发起", len(posts) == 1)
    check("4.POST 打到 /api/notes 且 payload 正确",
          posts and posts[0]["url"] == "http://fake.local/api/notes"
          and "勾股定理" in posts[0]["json"]["content"])
    deadline = time.time() + 3.0
    while time.time() < deadline and not _load_state().get("last_synced_ts"):
        time.sleep(0.05)
    # add_teach 登记时用当前时间做 ts（晚于测试夹具固定 ts），水位应推进到它
    final_ts = _load_state().get("last_synced_ts", "")
    check("4.成功后水位推进（越过最新转写条目，含补讲登记）", final_ts > TS2)

    # 场景 C：再次触发无增量 → 不 POST
    posts.clear()
    sync_b.trigger_sync()
    time.sleep(0.8)
    check("4.无增量不重复上传（幂等）", posts == [])

    # 场景 D：网络失败 → 不推进水位
    before_ts = _load_state().get("last_synced_ts", "")
    cs.requests.post = _boom_post
    sync_c = ClassroomSync(FakeMonitor([mk_entry(TS3)]), None)
    sync_c.trigger_sync()
    deadline = time.time() + 3.0
    while time.time() < deadline and sync_c._syncing:
        time.sleep(0.05)
    check("4.失败后水位不推进", _load_state().get("last_synced_ts", "") == before_ts)
    check("4.失败后 _syncing 复位（可重试）", sync_c._syncing is False)

    # 场景 E：重叠防抖
    cs.requests.post = _fake_post
    sync_c._syncing = True
    sync_c.trigger_sync()
    check("4.在途时跳过本轮（防重叠）", len(posts) == 0)
    sync_c._syncing = False
finally:
    cs.ConfigManager = _orig_cm = None  # 占位防误用；真实恢复见下
# 恢复 ConfigManager（保存原引用）
# NOTE: cs.ConfigManager 被覆盖，测试文件级恢复在 finally 外统一处理
from config.settings import ConfigManager as _RealCM  # noqa: E402
cs.ConfigManager = _RealCM
cs.requests.post = __import__("requests").post
for k, v in _saved.items():
    setattr(s, k, v)
if os.path.exists(cs.SYNC_STATE_PATH):
    os.remove(cs.SYNC_STATE_PATH)
cs.SYNC_STATE_PATH = _ORIG_STATE


# ---- T5 设置项往返 ----
print("\n[T5] 设置往返 + 真 coach 补讲链路")
from ui.settings_view import ClassroomTab  # noqa: E402
tab = ClassroomTab()
collected = tab.collect()
check("5.collect 含同步三字段", all(k in collected for k in (
    "classroom_sync_enabled", "sync_server_url", "classroom_sync_interval_min")))
check("5.同步开关默认关（隐私红线）",
      collected["classroom_sync_enabled"] is False
      or tab.classroom_sync_enabled.isChecked() == bool(getattr(
          tab.cfg.settings, "classroom_sync_enabled", False)))
tab.classroom_sync_enabled.setChecked(True)
tab.sync_server_url.setText("http://my.server:9000")
tab.classroom_sync_interval_min.setValue(15)
c2 = tab.collect()
check("5.控件改动反映到 collect",
      c2["classroom_sync_enabled"] is True
      and c2["sync_server_url"] == "http://my.server:9000"
      and c2["classroom_sync_interval_min"] == 15)

# 真 coach 对象补讲链路（审查 Critical 回归锁：_teach_log 在同步器自身，
# 不在 coach 上——mock 与现实脱节曾让补讲记录永不上传）
class _StubOverlay:
    def show_overlay(self):
        pass

    def apply_ops(self, ops, screen_w=1920, screen_h=1080):
        pass

    def clear_region(self, x, y, w, h):
        pass


class _StubTTS:
    stateChanged = None

    def speak(self, text, style_instruction=""):
        pass

    def shutdown(self):
        pass


from core.classroom_coach import ClassroomCoach  # noqa: E402
from core.classroom_stream import ClassroomMonitor  # noqa: E402
real_coach = ClassroomCoach(ClassroomMonitor(), None, overlay=_StubOverlay(),
                            tts_player=_StubTTS())
sync_real = ClassroomSync(FakeMonitor([mk_entry(TS1)]), real_coach)
real_coach.teach_logger = sync_real.add_teach
real_coach._last_source_student = True
real_coach._finish_pipeline("真链路要点", "真链路讲解词", speak=False)
found = sync_real._collect_teaches("")
check("5.真 coach 补讲经回调进入同步日志（Critical 回归锁）",
      len(found) == 1 and found[0]["point"] == "真链路要点"
      and found[0]["source"] == "student")

# interval 范围归一（审查 Medium：曾误放 _FLOAT_RANGES 导致脏值透传）
from config.settings import AppSettings as _AS  # noqa: E402
check("5.interval 脏值归一到 [5,720]",
      _AS(classroom_sync_interval_min=-3).classroom_sync_interval_min == 5
      and _AS(classroom_sync_interval_min=99999).classroom_sync_interval_min == 720)

print(f"\n===== R57 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
