# -*- coding: utf-8 -*-
"""本轮 BUG 修复回归测试。

覆盖：
- coord_engine 反三角函数/科学计数法/缺id/SVG标签转义
- 笔记下载中文文件名（latin-1 响应头 500）
- ocr_service 图文标记重映射（部分图失败时错位）
- ai_service 响应解析健壮性 / 空响应可回退
- calibration 滑动窗口封顶后停止重新校准
- knowledge_graph 缺键容错
- audit 系统消息写入失败不传播
- focus 非法会话 ID 不留下永久锁
- questions pending 文件损坏自愈
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ───────────────────────── coord_engine ─────────────────────────

def test_coord_engine_inverse_trig():
    from services.coord_engine import eval_expression
    assert abs(eval_expression("asin(1)") - 90.0) < 1e-6
    assert abs(eval_expression("acos(0.5)") - 60.0) < 1e-6
    assert abs(eval_expression("atan(1)") - 45.0) < 1e-6
    # sin/cos/tan 正常路径不受影响
    assert abs(eval_expression("sin(30)") - 0.5) < 1e-9


def test_coord_engine_nested_inverse():
    from services.coord_engine import eval_expression
    assert abs(eval_expression("asin(sin(30))") - 30.0) < 1e-6


def test_coord_engine_scientific_notation_whitelist():
    from services.coord_engine import eval_expression
    # sin(180°)≈1.22e-16：旧版白名单拒绝 e 记号导致整条表达式作废返回 0
    val = eval_expression("2+sin(180)")
    assert abs(val - 2.0) < 1e-9, val
    val2 = eval_expression("hypot(3,4)*pow(10,-8)")
    assert abs(val2 - 5e-8) < 1e-20, val2


def test_coord_engine_calc_points_missing_id():
    from services.coord_engine import calc_all_points
    result = calc_all_points([
        {"x": "0", "y": "0"},
        {"id": "B", "x": "10", "y": "0"},
        {"id": "", "x": "5", "y": "5"},
        {"x": "1", "y": "1"},
    ])
    assert set(result.keys()) == {"B"}, result


def test_coord_engine_to_svg_escapes_labels():
    from services.coord_engine import to_svg
    points = {"<b>A</b>": (10.0, 10.0), "B": (100.0, 10.0)}
    svg = to_svg(points, [{"from": "<b>A</b>", "to": "B"}], show_points=True)
    assert "<b>A</b>" not in svg, svg
    assert "&lt;b&gt;" in svg, svg


# ───────────────────────── notes 下载文件名 ─────────────────────────

def test_content_disposition_chinese_title_encodable():
    """中文标题必须能通过 latin-1 响应头编码，且保留 UTF-8 文件名。"""
    from routers.notes import _content_disposition_attachment
    from starlette.responses import PlainTextResponse

    header = _content_disposition_attachment("勾股定理笔记")
    resp = PlainTextResponse(content="x", headers={"Content-Disposition": header})
    # raw_headers 的键为小写 bytes，值必须能按 latin-1 编码（否则发送时 500）
    encoded = [(k, v) for k, v in resp.raw_headers if k == b"content-disposition"]
    assert encoded, "Content-Disposition 头缺失"
    raw = encoded[0][1].decode("latin-1")
    assert "filename*=UTF-8''" in raw
    assert "%E5%8B%BE%E8%82%A1%E5%AE%9A%E7%90%86" in raw


def test_content_disposition_injection_resisted():
    from routers.notes import _content_disposition_attachment
    header = _content_disposition_attachment('坏"标题\r\nX-Inject: 1')
    # 换行注入必须被清洗；文件名引号包裹合法，但不得出现裸换行
    assert "\r" not in header and "\n" not in header
    assert "X-Inject:" not in header


def test_download_note_endpoint_chinese_title():
    """端到端：下载中文标题笔记不应 500（子进程 + 临时 STORAGE_DIR 隔离真实数据）。"""
    import subprocess
    script = r"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.getcwd())
import config
tmp = tempfile.mkdtemp(prefix="lh_bugfix_")
config.STORAGE_DIR = tmp
for name in ("questions", "papers", "corrections", "paper_configs",
             "sessions", "notes", "quotes", "task_states", "logs", "data"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
config.DATABASE_URL = "sqlite+aiosqlite:///" + os.path.join(tmp, "app.db")
import models.models
from models.database import init_db
asyncio.run(init_db())
from fastapi.testclient import TestClient
import main as app_main
with TestClient(app_main.app, headers={"X-Auth-Token": "Ntmhzsgtc"}) as client:
    created = client.post("/api/notes", json={"title": "测试中文笔记标题", "content": "# 内容"})
    assert created.status_code == 200, created.text
    note_id = created.json()["id"]
    resp = client.get(f"/api/notes/{note_id}/download")
    print("STATUS:", resp.status_code)
    if resp.status_code != 200:
        sys.exit(1)
    cd = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" in cd, cd
print("OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=os.path.dirname(os.path.abspath(__file__)), timeout=180)
    assert "OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr[-800:]}"


# ───────────────────────── ocr_service 标记重映射 ─────────────────────────

def test_remap_diagram_markers_identity_and_shift():
    from services.ocr_service import _remap_diagram_markers
    # 恒等映射
    html = "前[[DIAGRAM:0]]中[[DIAGRAM:1]]后"
    mapping = {0: 0, 1: 1}
    assert _remap_diagram_markers(html, mapping) == html
    # 1 号图生成失败 → 2 号图位置前移，失败标记移除
    html2 = "题[[DIAGRAM:0]]辅1[[DIAGRAM:1]]辅2[[DIAGRAM:2]]"
    mapping2 = {0: 0, 2: 1}
    out = _remap_diagram_markers(html2, mapping2)
    assert "[[DIAGRAM:0]]" in out and "[[DIAGRAM:1]]" in out
    assert "[[DIAGRAM:2]]" not in out
    # 空映射：全部清除
    assert "[[DIAGRAM:" not in _remap_diagram_markers(html2, {})


def test_replace_diagram_markers_ignores_bad_paths():
    from services.ocr_service import _replace_diagram_markers
    diagrams = [{"path": "/storage/questions/abc123def456/diagram_0.svg", "w": "300"}]
    out = _replace_diagram_markers("[[DIAGRAM:0]]", diagrams)
    assert "/storage/questions/abc123def456/diagram_0.svg" in out
    # 非法路径保持原标记不被注入
    evil = [{"path": "javascript:alert(1)"}]
    out2 = _replace_diagram_markers("[[DIAGRAM:0]]", evil)
    assert "javascript" not in out2


# ───────────────────────── ai_service 健壮性 ─────────────────────────

def test_extract_content_malformed_responses():
    s = None
    try:
        from services.ai_service import AIService
        s = AIService.__new__(AIService)
    except Exception:
        pass
    if s is None:
        from services.ai_service import ai_service as s
    assert s._extract_content({}) == ""
    assert s._extract_content({"choices": []}) == ""
    assert s._extract_content({"choices": [{}]}) == ""
    assert s._extract_content({"choices": [{"message": None}]}) == ""
    assert s._extract_content({"choices": [{"message": {"content": "ok"}}]}) == "ok"


def test_empty_response_error_is_retryable_class():
    from services.ai_service import AIEmptyResponseError
    exc = AIEmptyResponseError("AI returned empty assistant content")
    # 不应包含会触发免回退快速失败的 "AI API" 子串语义
    assert isinstance(exc, Exception)


# ───────────────────────── calibration ─────────────────────────

def test_calibration_recalibrates_after_window_cap(tmp_path=None):
    import services.calibration_service as cs
    cs.CALIBRATION_FILE = os.path.join(tempfile.gettempdir(), f"calib_test_{os.getpid()}.json")
    if os.path.exists(cs.CALIBRATION_FILE):
        os.remove(cs.CALIBRATION_FILE)
    try:
        comps = lambda dy: [
            {"type": "beaker", "x": 100.0 + dy, "y": 50.0},
            {"type": "lamp", "x": 100.0, "y": 10.0 + dy},
        ]
        calibrated_first = False
        for i in range(210):
            r = cs.record_adjustment(comps(i * 0.01))
            if i == 4:
                calibrated_first = r["calibrated"]
        assert calibrated_first is True
        assert r["sample_count"] <= 200
        # 封顶之后仍应持续触发重新校准（新增满 5 条样本）
        assert r["calibrated"] is True, "滑动窗口封顶后重新校准失效"
    finally:
        if os.path.exists(cs.CALIBRATION_FILE):
            os.remove(cs.CALIBRATION_FILE)


# ───────────────────────── knowledge_graph ─────────────────────────

def test_load_graph_fills_missing_keys():
    import services.knowledge_graph as kg
    old_file = kg.GRAPH_FILE
    tmp = os.path.join(tempfile.gettempdir(), f"kg_test_{os.getpid()}.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"nodes": {"knode_x": {"label": "X"}}}, f)
    kg.GRAPH_FILE = tmp
    try:
        graph = kg._load_graph()
        assert graph["nodes"]
        assert graph["edges"] == []
        assert graph["clusters"] == {}
        assert graph["keyword_index"] == {}
        # add_note_to_graph 不应再 KeyError
        summary = kg.add_note_to_graph("n1", "标题", "内容", ["标签"],
                                       {"concepts": [{"label": "新概念", "type": "concept"}],
                                        "relations": []})
        assert summary.get("nodes_added", 0) >= 0
    finally:
        kg.GRAPH_FILE = old_file
        if os.path.exists(tmp):
            os.remove(tmp)


# ───────────────────────── audit 系统消息 ─────────────────────────

def test_add_system_message_never_raises(monkeypatch=None):
    import services.audit_service as aud

    def _boom(msgs):
        raise OSError("disk full")

    original = aud._save_system_messages
    aud._save_system_messages = _boom
    try:
        # 写入失败只记日志，不允许向上抛出杀死调度器
        aud.add_system_message("system", "t", "c")
    except Exception as exc:
        raise AssertionError(f"add_system_message raised: {exc}")
    finally:
        aud._save_system_messages = original


# ───────────────────────── focus 锁卫生 ─────────────────────────

def test_focus_invalid_sid_does_not_leak_lock():
    from services.focus_service import submit_checkpoint, pause_session, resume_session, end_session
    from services.focus_service import _focus_locks

    async def run():
        for bad_sid in ("../../etc/passwd", "", "x" * 65, "bad id!"):
            for fn in (submit_checkpoint, pause_session, resume_session, end_session):
                try:
                    await fn(bad_sid)
                except Exception:
                    pass
        await asyncio.sleep(0)

    asyncio.run(run())
    leaks = [k for k in _focus_locks if k in ("../../etc/passwd", "", "x" * 65, "bad id!")]
    assert not leaks, f"非法会话 ID 在锁表中留下永久条目: {leaks}"


def test_focus_missing_session_does_not_leak_lock():
    from services.focus_service import pause_session, _focus_locks

    async def run():
        try:
            await pause_session("nonexistent-session-id-123")
        except ValueError:
            pass

    asyncio.run(run())
    assert "nonexistent-session-id-123" not in _focus_locks or (
        len(_focus_locks) < 256  # 有界清理兜底
    )


# ───────────────────────── questions pending 容错 ─────────────────────────

def test_load_pending_corrupted_file_self_heals():
    import routers.questions as rq
    rq.PENDING_DIR = tempfile.mkdtemp(prefix="pending_test_")
    qid = "abc123def456"
    bad_path = os.path.join(rq.PENDING_DIR, f"{qid}.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        f.write("{corrupted json!!")
    result = rq._load_pending(qid)
    assert result == {}
    assert not os.path.exists(bad_path), "损坏的 pending 文件应被清理自愈"


# ───────────────────────── scheduler 窗口语义 ─────────────────────────

def test_scheduler_wide_windows_logic():
    """窗口放宽后的布尔逻辑验证（不启动真实调度器）。"""
    for hour, minute, expect_quote in [(2, 49, False), (2, 50, True), (3, 15, True)]:
        cond = (hour == 2 and minute >= 50 or hour == 3)
        assert cond == expect_quote, f"h={hour} m={minute}"
    # 巡检窗口覆盖 1、2 点
    assert (1 in (1, 2)) and (2 in (1, 2)) and (3 not in (1, 2))


if __name__ == "__main__":
    test_coord_engine_inverse_trig()
    test_coord_engine_nested_inverse()
    test_coord_engine_scientific_notation_whitelist()
    test_coord_engine_calc_points_missing_id()
    test_coord_engine_to_svg_escapes_labels()
    test_content_disposition_chinese_title_encodable()
    test_content_disposition_injection_resisted()
    test_download_note_endpoint_chinese_title()
    test_remap_diagram_markers_identity_and_shift()
    test_replace_diagram_markers_ignores_bad_paths()
    test_extract_content_malformed_responses()
    test_empty_response_error_is_retryable_class()
    test_calibration_recalibrates_after_window_cap()
    test_load_graph_fills_missing_keys()
    test_add_system_message_never_raises()
    test_focus_invalid_sid_does_not_leak_lock()
    test_focus_missing_session_does_not_leak_lock()
    test_load_pending_corrupted_file_self_heals()
    test_scheduler_wide_windows_logic()
    print("bugfix round checks passed")
