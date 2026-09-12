"""R52 回归测试：做题退出 v2（真实站点适配版）+ 全量检修。

覆盖：
T1  AppSettings.problem_exit_enabled 归一化 + 设置中心端口
T2  oj_tracker 实测解析：pid 归一化 / 提交记录行(data-timestamp+col--status) /
    翻页失败不误锁 / 比赛题目(UiContextNew.tdoc.pids)
T3  目标池降级链（进行中作业 → 进行中比赛 → 全部作业 → 题库）与 pick/check
T4  FocusEngine 做题退出状态机（pending/assign/confirm/cancel/自然结束优先/锁定拦截/解除通路）
T5  FocusView 题目面板流程（显示/分配/检测/AC 放行/取消/重开恢复/迟到丢弃）
T6  日志渲染

运行：python test_r52_overhaul.py
"""
import os
import sys
import inspect
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ==================== T1 配置字段与端口 ====================
print("T1 配置字段与设置端口")
from config.settings import AppSettings
from ui.settings_view import FocusTab

s = AppSettings.from_dict({"problem_exit_enabled": "false"})
check("1.1 'false' 转 bool False", s.problem_exit_enabled is False)
s2 = AppSettings.from_dict({})
check("1.2 默认开启", s2.problem_exit_enabled is True)
tab_src = inspect.getsource(FocusTab)
check("1.3 设置页有做题退出复选框", "problem_exit" in tab_src and "problem_exit_enabled" in tab_src)
ft = FocusTab()
collected = ft.collect()
check("1.4 collect 返回 problem_exit_enabled", "problem_exit_enabled" in collected)
check("1.5 复选框与配置联动", ft.problem_exit.isChecked() == AppSettings().problem_exit_enabled)

# ==================== T2 实测解析 ====================
print("T2 oj_tracker 真实站点解析")
import core.oj_tracker as ojt
from core.oj_tracker import ZzoiTracker

zt = ZzoiTracker()
check("2.1 pid 归一化合法值", zt._normalize_pid(" P1180 ") == "P1180")
check("2.2 pid 归一化拒绝标题文本", zt._normalize_pid("两数之和") == "")
check("2.3 pid 归一化拒绝空串", zt._normalize_pid("") == "" and zt._normalize_pid(None) == "")

BASE = {"base_url": "https://zz.example.com", "domain_prefix": "d/ZZOI",
        "uid": "daiweishu", "password": "", "sid": "", "sid_sig": ""}


class _FakeResp:
    def __init__(self, text, code=200):
        self.text = text
        self.status_code = code


EMPTY_PAGE = "<html><body></body></html>"


class _PagedSess:
    """按 URL 查询串里的 page 参数返回预置页面；未预置返回空页。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, **k):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        p = int(q.get("page", ["1"])[0])
        self.calls.append(p)
        return self.pages.get(p, _FakeResp(EMPTY_PAGE))


def _mk_record_row(rid, score, verdict, ts, pid_href=""):
    link = f'<a href="/d/ZZOI/record/{rid}" >{score} {verdict}</a>'
    prob = (f'<a href="/d/ZZOI/p/{pid_href}">x</a>' if pid_href else "*")
    return (
        f'<tr><td class="col--status"><div>{link}</div></td>'
        f'<td class="col--problem">{prob}</td>'
        f'<td class="col--submit-by">me</td>'
        f'<td class="col--time">10ms</td><td class="col--memory">7M</td>'
        f'<td class="col--lang">C++</td>'
        f'<td class="col--submit-at"><span data-timestamp="{ts}">t</span></td></tr>'
    )


from utils.helpers import now_cst

# 今日时间戳取"今天 00:05"，避免凌晨运行时 now-1h 跨午夜漂移到昨天
_midnight = now_cst().replace(hour=0, minute=5, second=0, microsecond=0)
_today_ts = int(_midnight.timestamp())
_yest_ts = _today_ts - 86400


def _mk_tracker(sess=None):
    t = ZzoiTracker()
    t._logged_in = True
    t._settings = lambda: dict(BASE)
    if sess is not None:
        t._session = sess
    return t


# 记录行解析：今日行保留、昨日行过滤、学生号 pid 为空
sess = _PagedSess({1: _FakeResp(
    "<table>" + _mk_record_row("aaa1", "100", "Accepted", _today_ts)
    + _mk_record_row("bbb2", "60", "Wrong Answer", _today_ts)
    + _mk_record_row("ccc3", "100", "Accepted", _yest_ts) + "</table>")})
t2 = _mk_tracker(sess)
subs = t2.fetch_today_submissions()
check("2.4 当日行保留且跨日过滤", len(subs) == 2, f"实际 {len(subs)}")
check("2.5 学生号 pid 为空串", all(s0["pid"] == "" for s0 in subs))
check("2.6 状态归一 AC/WA", sorted(x["status"] for x in subs) == ["AC", "WA"])
check("2.7 epoch 时间戳解析", all(x["ts"] > 0 for x in subs))

# 分页部分失败 → 整体 fetch_ok=False（防零提交误锁）
sess_bad = _PagedSess({1: _FakeResp("<table>"
                                    + _mk_record_row("ab12", "100", "Accepted", _today_ts)
                                    + "</table>"),
                       2: _FakeResp("forbidden", 403)})
t3 = _mk_tracker(sess_bad)
subs_bad = t3.fetch_today_submissions()
check("2.8 翻页失败整体返回空", subs_bad == [])
check("2.9 翻页失败 fetch_ok=False", t3._last_fetch_ok is False)
check("2.10 确实尝试了第2页", 2 in sess_bad.calls, f"实际 {sess_bad.calls}")

# 比赛题目：UiContextNew.tdoc.pids（实测通道）
uictx = ("var UiContext = '{\"x\":1}';\n"
         "var UiContextNew = '{\"tdoc\":{\"_id\":\"c1\",\"title\":\"T\","
         "\"pids\":[503,504,505]}}';var more=1;")
sess_c = _PagedSess({1: _FakeResp(f"<html><script>{uictx}</script></html>")})
t4 = _mk_tracker(sess_c)
probs = t4.fetch_contest_problems("c1")
check("2.11 tdoc.pids 解析出题目",
      [p["pid"] for p in probs] == ["503", "504", "505"], f"实际 {probs}")

# 链接兜底通道（通用 Hydro）
link_html = ('<a href="/d/ZZOI/p/P1001">A. 两数之和</a>'
             '<a href="/showProblem/2002">B题</a>')
sess_l = _PagedSess({1: _FakeResp(f"<html><body>{link_html}</body></html>")})
t5 = _mk_tracker(sess_l)
probs_l = t5.fetch_contest_problems("cx")
check("2.12 题目链接兜底通道",
      {p["pid"] for p in probs_l} == {"P1001", "2002"}, f"实际 {probs_l}")

# 异常安全
class _BoomSess:
    def get(self, *a, **k):
        raise RuntimeError("network down")


t6 = _mk_tracker(_BoomSess())
check("2.13 抓取异常返回空列表不崩溃", t6.fetch_contest_problems("CX") == [])

# ==================== T3 目标池与判定 ====================
print("T3 目标池降级与完成判定")


def _mk_pool_tracker(homework=None, contests=None, contest_probs=None,
                     problemset=None):
    t = ZzoiTracker()
    t._logged_in = True
    t._settings = lambda: dict(BASE)
    t.fetch_homework_list = lambda: homework or []
    t.fetch_contest_list = lambda: contests or []
    t.fetch_contest_problems = lambda cid: (contest_probs or {}).get(cid, [])
    t._fetch_problemset_page = lambda: problemset or []
    return t


now_iso_z = now_cst().strftime("%Y-%m-%dT%H:%M:%S.000Z")
future_iso_z = "2999-01-01T00:00:00.000Z"

# 进行中作业优先
t7 = _mk_pool_tracker(
    homework=[{"id": "HW1", "title": "图论专题1", "count": 4,
               "begin_at": now_iso_z, "end_at": future_iso_z}],
    problemset=[{"pid": "P3001", "title": "题库题"}])
pool = t7.fetch_problem_pool()
tg = pool["targets"][0] if pool["targets"] else {}
check("3.1 进行中作业为目标源", pool["source"] == "homework"
      and tg.get("kind") == "homework" and tg.get("key") == "HW1"
      and tg.get("count") == 4, f"实际 {pool}")

# 无进行中作业 → 进行中比赛
t8 = _mk_pool_tracker(
    contests=[{"id": "live", "title": "进行中比赛",
               "end_time": future_iso_z.replace("Z", "")}],
    contest_probs={"live": [{"pid": "LIVE1", "title": "比赛题"}]},
    problemset=[{"pid": "P3001", "title": "题库题"}])
pool8 = t8.fetch_problem_pool()
tg8 = pool8["targets"][0] if pool8["targets"] else {}
check("3.2 比赛作为目标（含题数）", pool8["source"] == "contest"
      and tg8.get("kind") == "contest" and tg8.get("count") == 1
      and "进行中比赛" in tg8.get("title", ""), f"实际 {pool8}")

# 都没有 → 题库兜底为单题目标
t9 = _mk_pool_tracker(problemset=[{"pid": "P3001", "title": "题库题"}])
pool9 = t9.fetch_problem_pool()
tg9 = pool9["targets"][0] if pool9["targets"] else {}
check("3.3 题库兜底为单题目标", tg9.get("kind") == "problem"
      and tg9.get("pid") == "P3001")

picked = t9.pick_problem()
check("3.4 pick 返回可展示目标", isinstance(picked, dict)
      and picked.get("kind") == "problem" and picked.get("url"))

# ---- check_solved 单元（mock 行解析）----
t10 = _mk_tracker()


def _patch_rows(tracker, rows):
    tracker._parse_record_rows = lambda html: rows
    class _R:
        status_code = 200
        text = "x"
    tracker._get_page = lambda path, timeout=15: _R()


_patch_rows(t10, [{"rid": "r1", "pid": "", "status": "AC", "score": "100",
                   "ts": _today_ts, "time_str": ""}])
r_ok = t10.check_solved(display_pid="P1180", since_ts=_today_ts - 10)
check("3.5 pid 通道命中 AC", r_ok.get("solved") is True and r_ok.get("mode") == "pid")
r_old = t10.check_solved(display_pid="P1180", since_ts=_today_ts + 10)
check("3.6 pid 通道早于分配时刻不算", r_old.get("solved") is False)
r_na = t10.check_solved(since_ts=_today_ts - 10)
check("3.7 new_ac 通道命中", r_na.get("solved") is True and r_na.get("mode") == "new_ac")
r_future = t10.check_solved(since_ts=_today_ts + 10)
check("3.8 new_ac 未来时刻不命中", r_future.get("solved") is False)

# ==================== T4 引擎状态机 ====================
print("T4 FocusEngine 做题退出状态机")
from core.focus_engine import FocusEngine
from config.settings import ConfigManager
cfg = ConfigManager()
saved_mode = cfg.settings.focus_mode
saved_pe = cfg.settings.problem_exit_enabled
saved_uid = cfg.settings.zzoi_uid
try:
    cfg.settings.focus_mode = "study"
    eng = FocusEngine()
    fired = {"ended": 0, "started": 0, "assigned": [], "status": [], "solved": []}
    eng.focus_ended.connect(lambda: fired.__setitem__("ended", fired["ended"] + 1))
    eng.focus_problem_exit_started.connect(lambda: fired.__setitem__("started", fired["started"] + 1))
    eng.focus_problem_assigned.connect(lambda d: fired["assigned"].append(d))
    eng.focus_problem_status.connect(lambda x: fired["status"].append(x))
    eng.focus_problem_solved.connect(lambda d: fired["solved"].append(d))

    cfg.settings.focus_mode = "oi"
    cfg.settings.zzoi_uid = "12345"
    cfg.settings.problem_exit_enabled = False
    eng.start(duration_minutes=30)
    eng.request_normal_exit()
    check("4.2 开关关闭直接结束", not eng.is_active and fired["started"] == 0)

    cfg.settings.problem_exit_enabled = True
    eng.start(duration_minutes=30)
    eng.request_normal_exit()
    check("4.3 进入 pending 态", eng.is_active and eng.problem_exit_pending
          and fired["started"] == 1)
    check("4.4 pending 下重复请求被忽略",
          (eng.request_normal_exit() or True) and fired["started"] == 1)

    eng.assign_problem(None)
    check("4.5 空池发出状态信号", len(fired["status"]) == 1)
    # 目标制分配（无单 pid 的作业目标）
    eng.assign_problem({"kind": "homework", "key": "HW9", "title": "作业《X》",
                        "count": 4, "url": "https://x/hw"})
    check("4.6 目标制分配成功", bool(fired["assigned"])
          and fired["assigned"][-1]["key"] == "HW9"
          and eng.assigned_problem["title"] == "作业《X》")
    eng.cancel_problem_exit()
    check("4.7 取消后回到专注且未结束", eng.is_active
          and not eng.problem_exit_pending and fired["ended"] == 1)

    eng.request_normal_exit()
    eng.confirm_problem_solved({"key": "HW9", "title": "作业《X》"})
    check("4.8 完成后放行结束", not eng.is_active
          and not eng.problem_exit_pending and len(fired["solved"]) == 1)

    eng.start(duration_minutes=30)
    eng.request_normal_exit()
    eng._normal_complete()
    check("4.9 自然结束清空 pending 并结束",
          not eng.is_active and not eng.problem_exit_pending)

    eng.lock_for_zzoi("zzoi_no_submit")
    eng.request_normal_exit()
    check("4.10 锁定下正常退出被拦", eng.is_active)
    eng.force_release_if_locked()
    check("4.11 有提交后锁定被解除", not eng.is_active)
    eng.start(duration_minutes=30)
    eng.force_release_if_locked()
    check("4.12 普通专注不被误释放", eng.is_active)
    eng.force_release()

    eng3 = FocusEngine()
    eng3.start(duration_minutes=30)
    eng3.request_normal_exit()
    eng3.assign_problem({"kind": "contest", "key": "C1", "title": "比赛《旧赛》",
                         "count": 8, "url": "",
                         "source_note": "该比赛已结束，若无法提交请换一题"})
    check("4.13 权威副本保留 source_note",
          eng3.assigned_problem.get("source_note") != "")
    eng3.cancel_problem_exit()
    eng3.force_release()
finally:
    cfg.settings.focus_mode = saved_mode
    cfg.settings.problem_exit_enabled = saved_pe
    cfg.settings.zzoi_uid = saved_uid

try:
    cfg.settings.zzoi_uid = ""
    eng2 = FocusEngine()
    eng2.start(duration_minutes=30)
    eng2.request_normal_exit()
    check("4.14 ZZOI 未配置直接结束", not eng2.is_active)
finally:
    pass

# ==================== T5 FocusView 面板流程 ====================
print("T5 FocusView 题目面板")
import ui.focus_view as fv_mod
from ui.focus_view import FocusView, set_global_engine, _ProblemWorker
old_engine = fv_mod._GLOBAL_ENGINE
orig_tracker_cls = ojt.ZzoiTracker
view = None
try:
    engv = FocusEngine()
    set_global_engine(engv)
    cfg.settings.focus_mode = "oi"
    cfg.settings.problem_exit_enabled = True
    cfg.settings.zzoi_uid = "12345"
    view = FocusView()

    _TARGET = {"kind": "homework", "key": "6a83d865", "title": "作业《图论专题1》",
               "count": 4, "url": "https://zz.example.com/d/ZZOI/homework/x",
               "source": "homework", "contest_title": "",
               "source_note": ""}

    class _FakeTracker:
        def __init__(self):
            self._last_fetch_ok = True
            self._session = None
            self._logged_in = False
            self._solved = False
        def pick_problem(self, exclude_solved=True):
            return dict(_TARGET)
        def check_solved(self, display_pid="", since_ts=0):
            return {"solved": bool(self._solved and display_pid),
                    "fetch_ok": True,
                    "mode": "pid" if display_pid else "new_ac"}

    ojt.ZzoiTracker = _FakeTracker  # 全段替换：任何 worker 都不触网

    w = _ProblemWorker("pick")
    got = {}
    w.finished.connect(lambda task, d: got.update(task=task, data=d))
    w.run()
    check("5.1 pick worker 返回真实目标", got.get("data", {}).get("key") == "6a83d865")

    view.show()
    app.processEvents()
    engv.start(duration_minutes=30)
    engv.request_normal_exit()
    app.processEvents()
    check("5.2 pending 后面板可见", view.problem_panel.isVisible())
    check("5.3 正常退出按钮禁用", not view.normal_exit_btn.isEnabled())

    # 分配回调更新面板（真实线程路径也会走到这里）
    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        if view._problem_thread is None and "图论专题1" in view.problem_title_label.text():
            break
        time.sleep(0.02)
    app.processEvents()
    check("5.4 面板展示目标标题", "图论专题1" in view.problem_title_label.text(),
          f"实际 {view.problem_title_label.text()}")
    check("5.5 打开按钮可用", view.problem_open_btn.isEnabled())
    check("5.6 轮询定时器已启动", view._problem_poll_timer.isActive())
    check("5.7 引擎保存权威目标副本",
          isinstance(engv.assigned_problem, dict)
          and engv.assigned_problem["key"] == "6a83d865")

    # check worker（未填展示ID → new_ac 通道，False）
    wc = _ProblemWorker("check", pid="", since_ts=int(time.time()))
    gotc = {}
    wc.finished.connect(lambda task, d: gotc.update(data=d))
    wc.run()
    check("5.8 check worker 未填ID走增量通道",
          gotc["data"]["mode"] == "new_ac" and gotc["data"]["solved"] is False)

    view._on_problem_worker_finished("check", {"solved": False,
                                               "fetch_ok": True, "mode": "new_ac"})
    check("5.9 未命中提示继续加油", "继续加油" in view.problem_status_label.text())

    # AC 放行 → 会话结束 → UI 复位
    ended_box = []
    orig_info = QMessageBox.information
    QMessageBox.information = lambda *a, **k: ended_box.append(True)
    try:
        view._on_problem_worker_finished("check", {"solved": True,
                                                   "fetch_ok": True,
                                                   "mode": "pid"})
        app.processEvents()
    finally:
        QMessageBox.information = orig_info
    check("5.10 AC 后会话结束", not engv.is_active)
    check("5.11 弹出完成提示", bool(ended_box))
    check("5.12 面板复位隐藏", not view.problem_panel.isVisible())
    check("5.13 轮询定时器停止", not view._problem_poll_timer.isActive())
    check("5.14 正常退出按钮恢复可用", view.normal_exit_btn.isEnabled())

    # 重开窗口恢复 pending 面板（引擎权威副本含目标）
    eng2v = FocusEngine()
    set_global_engine(eng2v)
    eng2v.start(duration_minutes=30)
    eng2v.request_normal_exit()
    view2 = FocusView()
    view2.show()
    app.processEvents()
    check("5.15 重开窗口恢复做题面板", view2.problem_panel.isVisible()
          and view2.normal_exit_btn.text() == "做题中…")
    view2._cancel_problem_exit()
    app.processEvents()
    check("5.16 取消后回专注态",
          eng2v.is_active and not eng2v.problem_exit_pending)
    check("5.17 取消后面板隐藏", not view2.problem_panel.isVisible())
    eng2v.force_release()
    view2.close()

    # 取消后迟到的任务结果不复活轮询
    check("5.23 取消后面板隐藏且引擎非 pending",
          not view.problem_panel.isVisible() and not engv.problem_exit_pending)
    view._on_problem_worker_sig("pick", dict(_TARGET))
    check("5.24 迟到 pick 被丢弃", not view._problem_poll_timer.isActive()
          and view._assigned_problem is None)
    # 忙碌串行保护
    class _FakeRunningThread:
        def isRunning(self):
            return True
    saved_thread = view._problem_thread
    view._problem_thread = _FakeRunningThread()
    try:
        view._manual_check_problem()
        check("5.25 忙碌时不误改按钮态", view.problem_check_btn.isEnabled())
    finally:
        view._problem_thread = saved_thread
finally:
    set_global_engine(old_engine)

# closeEvent 清理：轮询定时器停止 + 迟到信号不复活面板
view3 = None
try:
    enge = FocusEngine()
    set_global_engine(enge)
    view3 = FocusView()
    view3.show()
    enge.start(duration_minutes=30)
    enge.request_normal_exit()
    app.processEvents()
    view3.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    check("5.18 关闭后迟到信号不复活定时器", not view3._problem_poll_timer.isActive())
    check("5.22 关闭后迟到信号不复活面板", not view3.problem_panel.isVisible())
finally:
    set_global_engine(old_engine)
    ojt.ZzoiTracker = orig_tracker_cls

# ==================== T6 日志渲染 ====================
print("T6 日志渲染")
import utils.helpers as uh
md_src = inspect.getsource(uh.write_daily_markdown)
check("6.1 渲染做题通过退出行", "focus_problem_solved_exit" in md_src and "做题通过退出" in md_src)
check("6.2 渲染取消做题行", "focus_problem_exit_cancelled" in md_src)
check("6.3 渲染做题退出开始行", "focus_problem_exit_started" in md_src)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
