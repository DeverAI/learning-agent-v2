"""R48 全量检修回归测试（round 48）。

覆盖本轮修复：
T1  配置脏数据归一化 + 顶层非对象自愈
T2  DialogView 虚拟滚动：用户停在底部时 17~60 条消息可见；上滚时抑制渲染并可跳回
T3  AIDialog 旧 worker 检测信号不再污染新对话
T4  ZZOI 抓取网络失败不再误触发零提交锁定
T5  各侧边栏退出按钮 FocusLockedError 不再 quit 绕过锁定
T6  ZZOI 锁定按钮态 + 关机退出拦截
T7  自定义主题最小键集不再 KeyError
T8  sidebar_float 热键主线程桥 + 侧边栏生命周期/无屏防护
T9  DialogView._send 同步异常恢复按钮
T10 ZzoiView 异步化 + 登录 cookie 迁移
T11 AIDialog 对话存档异步化 + 退出流程同步存档兜底
T12 helpers：日志自愈/原子写/多线程锁
T13 ContextManager/Supervisor/CheatDetector/SiteGuard 脏配置防御
T14 graph Unicode 数字误判 + 邻接表导入上限
T15 死信号接入 UI（analyze_failed / context_*）
T16 selected_svgs 真正消费到侧边栏图标 + SVG 文件库加载
T17 设置中心补齐系统集成/网站风险配置端口
T18 30 分钟自动 ZZOI 检查异步化

运行：python test_r48_overhaul.py
"""
import os
import sys
import inspect
import json
import tempfile
import time

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


# ==================== T1 配置脏数据归一化 ====================
print("T1 配置脏数据归一化")
from config.settings import AppSettings, ConfigManager
import config.settings as cs_mod

s = AppSettings.from_dict({
    "focus_duration_minutes": None,
    "screen_capture_interval_sec": "abc",
    "focus_lock_on_no_submission": "false",
    "kimi_enabled": None,
    "site_whitelist": "oops",
    "screen_custom_rect": None,
    "mail_receivers": "a@b.c",
    "theme_custom_data": ["x"],
    "ai_dialog_extra_rules": 123,
    "external_ai_remind_cooldown_min": None,
})
check("1.1 非法分钟数回退默认", s.focus_duration_minutes == 30 and isinstance(s.focus_duration_minutes, int))
check("1.2 非法秒数回退默认", s.screen_capture_interval_sec == 60)
check("1.3 'false' 字符串转 bool False", s.focus_lock_on_no_submission is False)
check("1.4 None bool 安全转换", isinstance(s.kimi_enabled, bool))
check("1.5 非 list 白名单回退默认", isinstance(s.site_whitelist, list) and s.site_whitelist)
check("1.6 非 list 矩形回退默认", s.screen_custom_rect == [0, 0, 1920, 1080])
check("1.7 非 list 收件人回退空", s.mail_receivers == [])
check("1.8 非 dict 主题数据回退 dict", s.theme_custom_data == {})
check("1.9 数字追加规则转 str", s.ai_dialog_extra_rules == "123")
check("1.10 非法冷却分钟回退默认", s.external_ai_remind_cooldown_min == 5)
load_src = inspect.getsource(ConfigManager._load)
check("1.11 _load 校验 config 顶层 dict", "isinstance(data, dict)" in load_src)
check("1.12 _load 校验 secrets 顶层 dict", "isinstance(secrets, dict)" in load_src)

s_big = AppSettings.from_dict({
    "focus_mode": None, "screen_capture_interval_sec": 10 ** 30,
    "mail_smtp_port": 10 ** 30, "site_risk_score_threshold": -5,
})
check("1.13 focus_mode 脏值归一到 oi", s_big.focus_mode == "oi", repr(s_big.focus_mode))
check("1.14 超大截图间隔夹取业务上限", s_big.screen_capture_interval_sec == 86400)
check("1.15 超大 SMTP 端口夹取 65535", s_big.mail_smtp_port == 65535)
check("1.16 负风险阈值夹取 0", s_big.site_risk_score_threshold == 0)

s_elems = AppSettings.from_dict({
    "screen_custom_rect": ["0", "0", "640", "480"],
    "mail_receivers": [1, "a@b.c", None],
    "site_whitelist": ["luogu.com", 3, "vijos.org"],
})
check("1.17 矩形元素字符串转 int", s_elems.screen_custom_rect == [0, 0, 640, 480])
check("1.18 收件人元素过滤并转 str", s_elems.mail_receivers == ["1", "a@b.c"])
check("1.19 网站名单过滤非字符串", s_elems.site_whitelist == ["luogu.com", "vijos.org"])

# ==================== T2 虚拟滚动 ====================
print("T2 DialogView 虚拟滚动")
from ui.dialog_view import DialogView, MessageBubble, RENDER_CHUNK


def visible_bubbles(dv):
    out = []
    for i in range(dv.messages_layout.count()):
        item = dv.messages_layout.itemAt(i)
        if item is None:
            continue
        w = item.widget()
        if isinstance(w, MessageBubble):
            out.append(w)
    return out


dv = DialogView()
for i in range(20):
    dv._add_message("user", f"msg{i}")
b = visible_bubbles(dv)
check("2.1 数据完整追加 20 条", len(dv._all_messages) == 20)
check("2.2 停留在底部时窗口继续滑动渲染", b[-1].text == "msg19", f"实际 {b[-1].text if b else '空'}")
check("2.3 窗口大小保持 RENDER_CHUNK", len(b) == RENDER_CHUNK)
check("2.4 更早未显示数正确", dv._render_start == 20 - RENDER_CHUNK, f"实际 {dv._render_start}")
check("2.5 加载更早按钮显示未显示条数", str(20 - RENDER_CHUNK) in dv.load_more_btn.text())

dv2 = DialogView()
for i in range(10):
    dv2._add_message("user", f"a{i}")
dv2._user_scrolled_up = True
for i in range(10, 14):
    dv2._add_message("user", f"a{i}")
b2 = visible_bubbles(dv2)
check("2.6 上滚时新消息不插入气泡", b2[-1].text == "a9", f"实际 {b2[-1].text if b2 else '空'}")
check("2.7 上滚时显示跳到底部", "跳到底部" in dv2.load_more_btn.text())
dv2._jump_to_bottom()
b3 = visible_bubbles(dv2)
check("2.8 跳到底部后渲染最新消息", b3[-1].text == "a13")
check("2.9 跳到底部后 _render_start 归零", dv2._render_start == 0)

# ==================== T3 旧 worker 信号污染 ====================
print("T3 AIDialog 旧 worker 信号隔离")
from core.ai_dialog import AIDialog
d = AIDialog()
got = []
d.chat_detected.connect(lambda msg: got.append(("chat", msg)))
d.code_output_detected.connect(lambda: got.append(("code",)))
d.new_algo_detected.connect(lambda msg: got.append(("algo", msg)))
d.mode_mismatch_suggested.connect(lambda mode: got.append(("mode", mode)))
d._ignore_worker = True
d._messages = [{"role": "user", "content": "new-dialog-msg"}]
d._on_worker_chat_detected("旧闲聊", "旧文本")
d._on_worker_code_output("旧文本")
d._on_worker_new_algo("旧算法提示")
d._on_worker_mode_mismatch("study")
check("3.1 旧 worker 闲聊不 pop 新对话消息", len(d._messages) == 1 and d._messages[0]["content"] == "new-dialog-msg")
check("3.2 旧 worker 检测信号不再外发", got == [], f"实际 {got}")
for sig, slot in ((d.chat_detected, None),):
    pass

# ==================== T4 ZZOI 网络失败不误锁 ====================
print("T4 ZZOI 网络失败不误锁")
import requests
from core.oj_tracker import ZzoiTracker
cfg = ConfigManager()
saved_lock = cfg.settings.focus_lock_on_no_submission
saved_uid = cfg.settings.zzoi_uid
try:
    cfg.settings.focus_lock_on_no_submission = True
    cfg.settings.zzoi_uid = "12345"
    zt = ZzoiTracker()
    zt._logged_in = True

    class _FakeSess:
        def get(self, *a, **k):
            raise requests.ConnectionError("down")
    zt._session = _FakeSess()

    class _Engine:
        def __init__(self):
            self.locked = []
        def lock_for_zzoi(self, reason):
            self.locked.append(reason)
    eng = _Engine()
    result = zt.daily_check(eng)
    check("4.1 网络失败 locked=False", result.get("locked") is False, f"实际 {result}")
    check("4.2 fetch_ok=False 被标记", result.get("fetch_ok") is False)
    check("4.3 未触发误锁", eng.locked == [], f"实际 {eng.locked}")
finally:
    cfg.settings.focus_lock_on_no_submission = saved_lock
    cfg.settings.zzoi_uid = saved_uid

# ==================== T5 侧边栏退出不绕过锁定 ====================
print("T5 侧边栏退出拦截")
for mod_name in ("ui.sidebar_v2", "ui.sidebar_rect", "ui.sidebar_full", "ui.sidebar_float", "ui.sidebar"):
    import importlib
    mod = importlib.import_module(mod_name)
    src = inspect.getsource(mod.Sidebar._exit_clicked)
    check(f"5.{mod_name}.1 捕获 FocusLockedError", "FocusLockedError" in src, f"{mod_name}")
    check(f"5.{mod_name}.2 锁定拦截分支无 QApplication.quit", "QApplication.quit()" not in src, f"{mod_name}")
    check(f"5.{mod_name}.3 finally 恢复按钮", "setEnabled(True)" in src, f"{mod_name}")

# ==================== T6 ZZOI 锁定按钮态与关机拦截 ====================
print("T6 ZZOI 锁定按钮态与关机拦截")
from ui.focus_view import FocusView, set_global_engine
from core.focus_engine import FocusEngine
old_engine = None
try:
    import ui.focus_view as fv
    old_engine = fv._GLOBAL_ENGINE
    eng = FocusEngine()
    set_global_engine(eng)
    view = FocusView()
    eng.start(duration_minutes=30, lock_reason="zzoi_no_submit")
    check("6.1 ZZOI 锁定隐藏正常退出", not view.normal_exit_btn.isVisible())
    check("6.2 ZZOI 锁定隐藏急事退出", not view.emergency_btn.isVisible())
    check("6.3 ZZOI 锁定隐藏关机退出", not view.shutdown_btn.isVisible())
    eng.force_release()
finally:
    set_global_engine(old_engine)
import core.exit_flow as ef
esrc = inspect.getsource(ef.request_emergency_shutdown)
check("6.4 关机退出检查 ZZOI 锁定", 'startswith("zzoi")' in esrc)
check("6.5 锁定关机被拦截并 return", "无法关机" in esrc and "return" in esrc)
fsrc = inspect.getsource(FocusView._sync_state_from_engine)
check("6.6 FocusView 重开同步锁定按钮态", "_apply_lock_ui" in fsrc)

# ==================== T7 自定义主题缺省键 ====================
print("T7 自定义主题最小键集")
from ui.themes import ThemeManager, _fill_theme_defaults
tm = ThemeManager()
saved_theme = cfg.settings.theme
saved_custom = cfg.settings.theme_custom_data
try:
    cfg.settings.theme = "custom"
    cfg.settings.theme_custom_data = {"bg": "#123456", "surface": "#223344", "text": "#eeeeee", "accent": "#ff0000"}
    tm._current = "custom"
    tm._custom = dict(cfg.settings.theme_custom_data)
    t = tm.current_theme
    check("7.1 补齐 bubble_ai_bg", "bubble_ai_bg" in t and "bubble_user_bg" in t)
    check("7.2 补齐 text_dim/border_light/code_bg", all(k in t for k in ("text_dim", "border_light", "code_bg")))
    bubble = MessageBubble("assistant", "hello")
    check("7.3 最小主题可构造气泡", bubble is not None)
finally:
    cfg.settings.theme = saved_theme
    cfg.settings.theme_custom_data = saved_custom
    tm._current = saved_theme
    tm._custom = saved_custom
themes_load_src = inspect.getsource(ThemeManager._load_from_cfg)
check("7.4 ThemeManager 校验 custom 为 dict", "isinstance(custom, dict)" in themes_load_src)

# ==================== T8 sidebar_float 热键桥与生命周期 ====================
print("T8 sidebar_float 热键桥与生命周期")
import ui.sidebar_float as sf
check("8.1 有 _FloatHotkeyBridge", hasattr(sf, "_FloatHotkeyBridge"))
check("8.2 热键经桥 QueuedConnection 投递",
      "Qt.QueuedConnection" in inspect.getsource(sf._dispatch_float_toggle)
      and "_dispatch_float_toggle" in inspect.getsource(sf.Sidebar._register_hotkey))
check("8.3 重复注册先停旧监听器", "stop()" in inspect.getsource(sf.Sidebar._register_hotkey))
for mod_name in ("ui.sidebar_rect", "ui.sidebar_full", "ui.sidebar_float", "ui.sidebar"):
    mod = importlib.import_module(mod_name)
    src = inspect.getsource(mod.Sidebar.closeEvent)
    check(f"8.{mod_name}.closeEvent 断开 focus 信号",
          "focus_started.disconnect" in src and "focus_ended.disconnect" in src)
for mod_name in ("ui.sidebar_v2", "ui.sidebar_rect", "ui.sidebar_full", "ui.sidebar", "ui.sidebar_float"):
    mod = importlib.import_module(mod_name)
    if mod_name == "ui.sidebar_float":
        geo_src = inspect.getsource(mod.Sidebar._set_initial_geometry) + inspect.getsource(mod.Sidebar._show_at_cursor)
    else:
        geo_src = inspect.getsource(mod.Sidebar._screen_geo) if hasattr(mod.Sidebar, "_screen_geo") else ""
    check(f"8.{mod_name}.无屏防护",
          "primaryScreen()" in geo_src and ("is None" in geo_src or "is not None" in geo_src),
          mod_name)

# ==================== T9 _send 同步异常恢复 ====================
print("T9 _send 同步异常恢复")
dv9 = DialogView()
class _Boom:
    def send(self, *a, **k):
        raise RuntimeError("boom")
dv9._dialog = _Boom()
dv9.input.setPlainText("hello")
dv9._send()
check("9.1 同步异常后发送按钮恢复", dv9.send_btn.isEnabled() and dv9.send_btn.text() == "发送")
check("9.2 同步异常后进度条隐藏", not dv9.progress.isVisible())
check("9.3 同步异常后思考提示隐藏", not dv9._thinking_label.isVisible())
check("9.4 同步异常写入错误气泡", any(w.text.startswith("[发送失败]") for w in visible_bubbles(dv9)))
dv9._dialog = None

# ==================== T10 ZzoiView 异步化与 cookie 迁移 ====================
print("T10 ZzoiView 异步化")
from ui.zzoi_view import ZzoiView, _ZzoiWorker, _LockRecorder
check("10.1 ZzoiView 有 QThread 调度", "QThread" in inspect.getsource(ZzoiView._start_task))
check("10.2 worker 登录返回 cookies", '"cookies"' in inspect.getsource(_ZzoiWorker.run))
class _Cookies:
    pass
class _Track:
    def __init__(self):
        self._session = type("S", (), {})()
        self._logged_in = False
view = ZzoiView()
view._tracker = _Track()
cookies = _Cookies()
view._on_worker_finished("login", {"ok": True, "cookies": cookies})
check("10.3 登录成功后全局登录态置真", view._tracker._logged_in is True)
check("10.4 登录成功后迁移真实 cookie", view._tracker._session.cookies is cookies)

# ==================== T11 对话存档异步化 ====================
print("T11 对话存档异步化")
from core.ai_dialog import _ArchiveWorker
check("11.1 有 _ArchiveWorker", hasattr(__import__("core.ai_dialog", fromlist=["_ArchiveWorker"]), "_ArchiveWorker"))
check("11.2 有 close_dialog_async", hasattr(AIDialog, "close_dialog_async"))
check("11.3 有 delete_dialog_async", hasattr(AIDialog, "delete_dialog_async"))
d11 = AIDialog()
record = {}
orig_start = d11._start_archive_worker
def fake_start(did, msgs, mode):
    record["did"], record["mode"], record["msgs"] = did, mode, msgs
d11._start_archive_worker = fake_start
d11._messages = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
old_did = d11._dialog_id
d11.close_dialog_async()
check("11.4 异步关闭调用归档 worker", record.get("mode") == "archive" and record.get("did") == old_did)
check("11.5 异步关闭立即开启新对话", d11.dialog_id() != old_did and d11.messages() == [])
d11._messages = [{"role": "user", "content": "z"}]
record.clear()
d11.delete_dialog_async()
check("11.6 异步删除调用摘要 worker", record.get("mode") == "delete")
d11._start_archive_worker = orig_start
esrc2 = inspect.getsource(ef.request_exit)
check("11.7 退出流程同步 close_dialog 兜底", "_get_global_dialog().close_dialog()" in esrc2)

# ==================== T12 helpers 自愈/原子写/锁 ====================
print("T12 helpers")
import utils.helpers as uh
check("12.1 save_json 原子写", "os.replace" in inspect.getsource(uh.save_json))
check("12.2 log_event 有线程锁", "_DAILY_LOG_LOCK" in inspect.getsource(uh.log_event))
check("12.3 get_today_log 结构自愈", "_normalize_daily_log" in inspect.getsource(uh.get_today_log))
tmpdir = tempfile.mkdtemp(prefix="oisys_r48_")
old_path = uh.daily_log_path
try:
    p = os.path.join(tmpdir, "daily.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"date": "20260816", "events": "bad", "submission_errors": [], "reminders": "x"}, f)
    uh.daily_log_path = lambda date_str=None: p
    data = uh.get_today_log()
    check("12.4 events 非 list 自愈为 list", isinstance(data.get("events"), list))
    check("12.5 reminders 非数字自愈为 0", data.get("reminders") == 0)
    check("12.6 submission_errors 非 dict 自愈", isinstance(data.get("submission_errors"), dict))
    # 12.7 数值字符串 reminders 归一为整数，而不是误归 0
    p_rem = os.path.join(tmpdir, "daily_rem.json")
    with open(p_rem, "w", encoding="utf-8") as f:
        json.dump({"date": "20260816", "events": [], "reminders": "2",
                   "submission_errors": {}}, f)
    uh.daily_log_path = lambda date_str=None: p_rem
    check("12.7 reminders 数值字符串自愈", uh.get_today_log().get("reminders") == 2)
    uh.daily_log_path = lambda date_str=None: p
    # 12.7 坏 detail 被复制后修改，必须落盘自愈
    p_detail = os.path.join(tmpdir, "daily_detail.json")
    with open(p_detail, "w", encoding="utf-8") as f:
        json.dump({"date": "20260816",
                   "events": [{"ts": "12:00:00", "type": "focus_start", "detail": "bad"}],
                   "submission_errors": {}, "reminders": 0}, f)
    uh.daily_log_path = lambda date_str=None: p_detail
    uh.get_today_log()
    with open(p_detail, "r", encoding="utf-8") as f:
        disk_detail = json.load(f)["events"][0]["detail"]
    check("12.7 坏 detail 自愈后写回磁盘", disk_detail == {}, f"实际 {disk_detail!r}")
    uh.daily_log_path = lambda date_str=None: p
finally:
    uh.daily_log_path = old_path

# ==================== T13 脏配置防御 ====================
print("T13 脏配置防御")
from core.context_manager import ContextManager, ContextBlock
from core.ai_supervisor import AISupervisor
from core.cheat_detector import CheatDetector
from core import site_guard

saved_max = cfg.settings.ai_dialog_max_context_blocks
saved_interval = cfg.settings.ai_supervisor_interval_rounds
saved_cooldown = cfg.settings.external_ai_remind_cooldown_min
try:
    cfg.settings.ai_dialog_max_context_blocks = "abc"
    cm = ContextManager()
    cm._score_with_flash = lambda blocks: None
    blocks = [ContextBlock("b1", "user", "x") for _ in range(3)]
    kept = cm.prune_with_flash(blocks)
    check("13.1 上下文块上限脏配置不崩溃", len(kept) == 3)

    cfg.settings.ai_supervisor_interval_rounds = "abc"
    sup = AISupervisor()
    ok = False
    try:
        sup.should_check()
        ok = True
    except Exception as e:
        print("    should_check 异常:", e)
    check("13.2 元监督间隔脏配置不崩溃", ok)

    cfg.settings.external_ai_remind_cooldown_min = None
    cd = CheatDetector()
    cd._external_ai_streak = 2
    cd._last_external_ai_at = 0.0
    ext = cd.detect_external_ai("chatgpt")
    check("13.3 外部 AI 冷却脏配置不崩溃且能提醒", isinstance(ext, dict) and ext.get("kind") == "external_ai")

    sg = site_guard.SiteGuard()
    sg._last_enforce_at = 0.0
    try:
        sg.enforce([])
        ok_sg = True
    except Exception as e:
        print("    site_guard 异常:", e)
        ok_sg = False
    check("13.4 SiteGuard 非 dict 结果不崩溃", ok_sg)
finally:
    cfg.settings.ai_dialog_max_context_blocks = saved_max
    cfg.settings.ai_supervisor_interval_rounds = saved_interval
    cfg.settings.external_ai_remind_cooldown_min = saved_cooldown

# ==================== T14 graph 极端输入 ====================
print("T14 graph 极端输入")
from ui.graph_renderer import parse_uvw_block, parse_graph_block
from ui.graph_editor import GraphScene, MAX_NODES, MAX_EDGES
try:
    r = parse_uvw_block("²\n1 2")
    ok_unicode = isinstance(r, dict)
except Exception as e:
    ok_unicode = False
    print("    unicode 异常:", e)
check("14.1 Unicode 数字不再让 parse 抛 ValueError", ok_unicode)
scene = GraphScene()
n0 = scene.add_node(0, 0, "keep")
huge = "A: " + " ".join(f"N{i}(1)" for i in range(MAX_EDGES + 10))
ok_limit = scene.from_adjacency(huge)
check("14.2 邻接表超边数拒绝且保留原图", ok_limit is False and n0 in scene._nodes)
check("14.3 邻接表超 100KB 拒绝", scene.from_adjacency("A: " + "B(1) " * 50000) is False and n0 in scene._nodes)

# ==================== T15 死信号接入 ====================
print("T15 死信号接入 UI")
import main as main_mod
check("15.1 analyze_failed 接入 toast", "analyze_failed.connect" in inspect.getsource(main_mod.main))
d_init_src = inspect.getsource(AIDialog.__init__)
check("15.2 context_pruned 转发 UI", "context_pruned.connect" in d_init_src)
check("15.3 block_referenced 转发 UI", "block_referenced.connect" in d_init_src)
check("15.4 block_blacklisted 转发 UI", "block_blacklisted.connect" in d_init_src)
bind_src = inspect.getsource(DialogView._bind_dialog)
check("15.5 DialogView 连接 context_pruned", "context_pruned.connect" in bind_src)
unbind_src = inspect.getsource(DialogView._unbind_dialog)
check("15.6 DialogView 断开 context_pruned", "context_pruned, self._on_context_pruned" in unbind_src)

# ==================== T16 selected_svgs 消费 ====================
print("T16 selected_svgs 消费")
from ui import icons as icons_mod
saved_selected = cfg.settings.selected_svgs
try:
    groups = icons_mod.get_all_svgs_grouped()
    all_names = {it["name"] for g in groups.values() for it in g}
    check("16.1 SVG 库加载文件选择（chat）", "chat-svgrepo-com" in all_names)
    cfg.settings.selected_svgs = ["chat-svgrepo-com", "bell-off-svgrepo-com"]
    btns = {k: svg for k, l, svg in icons_mod.get_sidebar_buttons()}
    check("16.2 勾选 chat 应用到对话按钮", btns["dialog"] != icons_mod.SVG_DIALOG)
    check("16.3 勾选 bell-off 应用到静音按钮", btns["mute"] != icons_mod.SVG_BOSS)
    cfg.settings.selected_svgs = []
    btns_default = icons_mod.get_sidebar_buttons()
    check("16.4 未勾选保持默认", btns_default[1][2] == icons_mod.SVG_DIALOG)
finally:
    cfg.settings.selected_svgs = saved_selected
for mod_name in ("ui.sidebar_v2", "ui.sidebar_rect", "ui.sidebar_full", "ui.sidebar_float", "ui.sidebar"):
    mod = importlib.import_module(mod_name)
    check(f"16.{mod_name}.侧边栏消费 get_sidebar_buttons", "get_sidebar_buttons()" in inspect.getsource(mod.Sidebar._setup_ui))

# ==================== T17 设置中心补齐端口 ====================
print("T17 设置中心补齐端口")
from ui.settings_view import SystemTab, SiteTab, SettingsView
sys_src = inspect.getsource(SystemTab)
for field in ("sidebar_style", "sidebar_float_hotkey", "watchdog_enabled",
              "watchdog_restart", "note_enabled", "note_base", "note_suffix",
              "ext_cooldown"):
    check(f"17.{field} 控件存在", field in sys_src)
site_src = inspect.getsource(SiteTab)
for field in ("news_list", "game_list", "risk_threshold"):
    check(f"17.{field} 控件存在", field in site_src)
settings_src = inspect.getsource(SettingsView._setup_ui)
check("17.系统集成 Tab 已挂载", 'self._add_tab(SystemTab(), "系统集成")' in settings_src)

# ==================== T18 自动 ZZOI 异步化 ====================
print("T18 自动 ZZOI 异步化")
main_src = inspect.getsource(main_mod)
check("18.1 有 _AutoZzoiWorker", "_AutoZzoiWorker" in main_src)
check("18.2 定时器走 _start_auto_zzoi_check", "_start_auto_zzoi_check" in main_src)
check("18.3 定时器不再主线程直调 daily_check",
      "zzoi_timer.timeout.connect(\n            lambda: zzoi_tracker.daily_check" not in main_src)

# ==================== T19 第二轮：自动 ZZOI 线程桥 ====================
print("T19 自动 ZZOI 线程桥")
check("19.1 有 _AutoZzoiBridge", "_AutoZzoiBridge" in main_src)
check("19.2 bridge slot 装饰", "@Slot(dict)" in main_src)
check("19.3 bridge 在主线程应用锁定", "bridge._apply" in main_src and "worker.finished.connect(bridge._apply)" in main_src)
check("19.4 thread/worker/bridge 全部持有引用", "entry = (thread, worker, bridge)" in main_src and "job_refs.append(entry)" in main_src)

# ==================== T20 第二轮：请求代次过滤迟到截图结果 ====================
print("T20 截图/关联页面请求代次")
dv20 = DialogView()
before_msgs = len(dv20._all_messages)
dv20._shot_request_id = 5
dv20._on_screenshot_analyzed(3, {"activity": "旧窗口迟到的结果", "efficiency": 90})
check("20.1 旧 request_id 截图结果被丢弃", len(dv20._all_messages) == before_msgs and dv20._shot_request_id == 5)
dv20._attach_request_id = 7
dv20._on_screen_context_ready(3, {"screen_summary": "旧结果"})
check("20.2 旧 request_id 关联结果被丢弃", dv20._pending_screen_context is None and dv20._attach_request_id == 7)
check("20.3 AIDialog 截图信号带 request_id",
      "screenshot_analyzed = Signal(int, dict)" in inspect.getsource(AIDialog))
check("20.4 DialogView 关闭时作废请求代次", "_shot_request_id = None" in inspect.getsource(DialogView.closeEvent))

# ==================== T21 第二轮：线程引用代次校验 ====================
print("T21 线程引用代次校验")
check("21.1 AIDialog 对话线程代次校验", "lambda t=self._thread: self._reset_thread_refs(t)" in inspect.getsource(AIDialog.send))
check("21.2 AIDialog 截图线程代次校验", "lambda t=self._shot_thread: self._reset_shot_thread_refs(t)" in inspect.getsource(AIDialog._start_shot_worker))
from core.screen_analyzer import ScreenAnalyzer
check("21.3 ScreenAnalyzer 线程代次校验", "lambda t=self._thread: self._reset_thread_refs(t)" in inspect.getsource(ScreenAnalyzer.analyze))
check("21.4 _reset_thread_refs 校验线程身份", "is not finished_thread" in inspect.getsource(AIDialog._reset_thread_refs))

# ==================== T22 第二轮：note.ms base URL / 损坏日志 / UVW 上限 ====================
print("T22 第二轮余项")
import core.log_sync as ls
check("22.1 note_ms API 消费配置 base url",
      "_note_ms_api_url" in inspect.getsource(ls.fetch_note_ms)
      and "_note_ms_base_url()" in inspect.getsource(ls._note_ms_api_url))
check("22.2 note_ms 不再硬编码官方域",
      'f"https://note.ms/api/notes/' not in inspect.getsource(ls.fetch_note_ms)
      and 'f"https://note.ms/api/notes/' not in inspect.getsource(ls.append_to_note_ms))
check("22.3 write_daily_markdown 防御非 dict detail",
      "isinstance(detail, dict)" in inspect.getsource(uh.write_daily_markdown))
from ui.graph_editor import GraphScene
scene22 = GraphScene()
n22 = scene22.add_node(0, 0, "keep")
uvw_huge = "\n".join(f"{i} {i+1}" for i in range(1, 6000))
t0 = time.time()
ok_huge = scene22.from_uvw(uvw_huge)
elapsed = time.time() - t0
check("22.4 from_uvw 超限快速拒绝", ok_huge is False and elapsed < 5, f"耗时 {elapsed:.2f}s")
check("22.5 from_uvw 超限不破坏原图", n22 in scene22._nodes)

# ==================== T23 终检验收修复 ====================
print("T23 终检验收修复")
from ui.sidebar_full import Sidebar as FullSidebar
import ui.sidebar_full as full_mod
check("23.1 sidebar_full 导入 QPointF", "QPointF" in inspect.getsource(full_mod))
full_sb = FullSidebar()
try:
    full_sb.show()
    app.processEvents()
    full_sb.grab()
    check("23.2 sidebar_full 绘制不崩溃", True)
finally:
    full_sb.close()

sub_tmp = tempfile.mkdtemp(prefix="oisys_r48_sub_")
old_path2 = uh.daily_log_path
try:
    p2 = os.path.join(sub_tmp, "daily.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump({"date": "20260816", "events": [],
                   "submission_errors": {"WA": "x", "TLE": "2"}, "reminders": 0}, f)
    uh.daily_log_path = lambda date_str=None: p2
    data2 = uh.get_today_log()
    check("23.3 submission_errors 值类型自愈",
          data2.get("submission_errors") == {"WA": 0, "TLE": 2}, str(data2.get("submission_errors")))
finally:
    uh.daily_log_path = old_path2

import core.ai_client as ac
from utils.exceptions import AICallError
saved_getcfg = ac._get_provider_config
class _FakeRespList:
    def raise_for_status(self):
        pass
    def json(self):
        return [1, 2]
class _FakeRequestsList:
    RequestException = requests.RequestException
    @staticmethod
    def post(*a, **k):
        return _FakeRespList()
saved_requests = ac.requests
try:
    ac._get_provider_config = lambda provider: {
        "base_url": "https://x.example", "model": "m", "api_key": "k", "enabled": True}
    ac.requests = _FakeRequestsList
    try:
        ac.chat([{"role": "user", "content": "x"}], provider="deepseek")
        ok_ai = False
    except AICallError:
        ok_ai = True
    check("23.4 AI 非 dict 响应转业务异常", ok_ai)
finally:
    ac._get_provider_config = saved_getcfg
    ac.requests = saved_requests

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
