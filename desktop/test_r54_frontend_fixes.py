"""R54 回归测试：全系统巡检修复（做题退出前端 + ZZOI 抓取器）。

覆盖：
T1  focus_view._start_problem_task 接受 since_ts 并传入 worker
    （修复：点「我已AC · 检测」TypeError → 检测通道失效 + 按钮死锁）
T2  FocusView 窗口重开恢复面板：key-only 目标（作业/比赛）不触发重复选题
T3  oj_tracker.fetch_ac_pids_today 单一定义（重复定义清理）
T4  check_solved 展示ID 白名单清洗（非法输入不发请求）
T5  fetch_today_submissions 分页 all-or-nothing（中途页失败 → fetch_ok=False）

运行：python test_r54_frontend_fixes.py
"""
import os
import sys
import inspect
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PySide6.QtWidgets import QApplication
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


# ==================== T1 _start_problem_task since_ts ====================
print("T1 做题检测通道 since_ts 贯通")
import ui.focus_view as fv
sig = inspect.signature(fv.FocusView._start_problem_task)
check("1.1 签名含 since_ts 形参", "since_ts" in sig.parameters)
src = inspect.getsource(fv.FocusView._start_problem_task)
check("1.2 since_ts 透传给 _ProblemWorker",
      "_ProblemWorker(task,pid,since_ts)" in src.replace(" ", ""))
worker_sig = inspect.signature(fv._ProblemWorker.__init__)
check("1.3 worker 构造含 since_ts", "since_ts" in worker_sig.parameters)


# ==================== T2 重开恢复 key-only 目标 ====================
print("T2 窗口重开恢复面板（目标制）")
view = None
try:
    view = fv.FocusView()
except Exception as e:
    print("  (FocusView 构建依赖主题等，跳过 UI 实例化:", e, ")")

if view is not None:
    calls = []

    class _FakeEngine:
        is_active = True
        problem_exit_pending = True
        lock_reason = ""
        assigned_problem = {"kind": "homework", "key": "hw001", "pid": "",
                            "title": "作业《测试》", "url": "https://x/hw001",
                            "count": 3, "source": "homework"}

        def remaining_seconds(self):
            return 100

    view._engine = _FakeEngine()
    # 拦截重新选题入口
    orig_start = view._start_problem_task
    view._start_problem_task = lambda *a, **k: calls.append((a, k))
    try:
        view._sync_state_from_engine()
    except Exception as e:
        check("2.1 同步不抛异常", False, str(e))
    check("2.2 key-only 目标不触发重复选题", len(calls) == 0,
          f"pick 被重复调用 {calls}")
    check("2.3 面板展示引擎权威副本",
          (view._assigned_problem or {}).get("key") == "hw001")
    # 对照：pid-only 目标同样恢复
    view._assigned_problem = None
    _FakeEngine.assigned_problem = {"kind": "problemset", "key": "", "pid": "P2001",
                                    "title": "P2001", "count": 1}
    view._start_problem_task = orig_start
    view._start_problem_task = lambda *a, **k: calls.append((a, k))
    view._sync_state_from_engine()
    check("2.4 pid-only 目标也不重选", len(calls) == 0 and
          (view._assigned_problem or {}).get("pid") == "P2001")


# ==================== T3 重复定义清理 ====================
print("T3 fetch_ac_pids_today 单一定义")
import core.oj_tracker as ojt
import core.oj_tracker
src_oj = inspect.getsource(core.oj_tracker)
check("3.1 源码中仅一处定义",
      src_oj.count("def fetch_ac_pids_today") == 1,
      f"出现 {src_oj.count('def fetch_ac_pids_today')} 次")
check("3.2 类方法可调用", callable(ojt.ZzoiTracker.fetch_ac_pids_today))


# ==================== T4 display_pid 清洗 ====================
print("T4 check_solved 展示ID清洗")


class _FakeResp:
    def __init__(self, text="", code=200):
        self.text = text
        self.status_code = code


class _FakeSess:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout=None, **k):
        self.urls.append(url)
        return _FakeResp("<html><body></body></html>")

    def post(self, *a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("不应触发登录请求")


tracker = ojt.ZzoiTracker()
tracker._logged_in = True          # 免登录直入抓取路径
sess = _FakeSess()
tracker._session = sess

bad_inputs = ["&page=3#x", "P2001 OR 1=1", "../../admin", "P20 01;drop"]
for bad in bad_inputs:
    r = tracker.check_solved(display_pid=bad)
    if ojt.ZzoiTracker._PID_RE.match(ojt._ZzoiTracker__nothing__ if False else "") :
        pass
    hit = any(bad.split()[0] in u or ("&" in bad and "&page" in u) for u in sess.urls)
    check(f"4.x 非法输入[{bad[:14]}]未污染URL", not hit, f"urls={sess.urls[-1:] if sess.urls else []}")

# 合法输入仍正常发请求且 URL 含 pid 参数
r = tracker.check_solved(display_pid="P2001")
check("4.y 合法展示ID发出精确过滤请求",
      sess.urls and "pid=P2001" in sess.urls[-1], f"url={sess.urls[-1:]}")
check("4.z 返回结构完整",
      set(r.keys()) >= {"solved", "fetch_ok", "mode"})


# ==================== T5 分页 all-or-nothing ====================
print("T5 提交抓取分页 all-or-nothing")


def _mk_row(rid, ts, status="100 Accepted"):
    return (f'<tr><td><a href="/d/ZZOI/record/{rid}">r</a></td>'
            f'<td class="col--status"><div>{status}</div></td>'
            f'<td>*</td>'
            f'<td class="col--submit-at"><span data-timestamp="{ts}">t</span></td></tr>')


class _PageSess(_FakeSess):
    """page1 成功、page2 返回 403 的会话（模拟 WAF 中途拦截）。"""

    def __init__(self):
        super().__init__()
        import time
        now_ts = int(time.time())

    def get(self, url, timeout=None, **k):
        self.urls.append(url)
        if "page=1" in url or "page=" not in url:
            row = _mk_row("aabbccdd0011", 1787500000)
            return _FakeResp(f"<html><body><table>{row}</table></body></html>", 200)
        return _FakeResp("", 403)


t5 = ojt.ZzoiTracker()
t5._logged_in = True
t5._session = _PageSess()
subs = t5.fetch_today_submissions()
check("5.1 中途页失败返回空列表", subs == [])
check("5.2 fetch_ok 必须为 False（防零提交误锁）",
      t5._last_fetch_ok is False, f"got {t5._last_fetch_ok}")

# 全部成功路径 fetch_ok=True
class _OkSess(_FakeSess):
    def get(self, url, timeout=None, **k):
        self.urls.append(url)
        if "page=2" in url:
            return _FakeResp("<html><body></body></html>", 200)  # 空页收口
        row = _mk_row("aabbccdd0022", 1787500000)
        return _FakeResp(f"<html><body><table>{row}</table></body></html>", 200)

t6 = ojt.ZzoiTracker()
t6._logged_in = True
t6._session = _OkSess()
t6.fetch_today_submissions()
check("5.3 全部成功 fetch_ok=True", t6._last_fetch_ok is True)


# ==================== 汇总 ====================
print(f"\n===== R54 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
