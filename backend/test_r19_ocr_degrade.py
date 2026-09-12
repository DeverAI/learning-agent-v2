# -*- coding: utf-8 -*-
"""R19：识别链路的"降级留痕"必须活到用户看见。

## 为什么专门写这一组

原先有两处识别降级只写进 `storage/task_states/{qid}.json`：
- 整页切题失败、静默降级成"一道题"
- 多图一题时某张图 OCR 失败、题干/手写作答静默缺失

而该文件在任务**正常完成时会被 `_clear_task_state` 直接删掉**，所以这两件事
**永远不会出现在用户面前** —— 他看到的是"已完成"。

改成写进 DB 的 `audit_flags` 之后，还有一个**更隐蔽的坑**：
`run_auto_rewrite` 里有一个"保留哪些标记"的白名单，不在名单里的标记会被悄悄清掉
（表现为"标记出现过一会儿又没了"）。所以新标记类型必须同时进白名单，
否则这次修复等于白做。本组测试就是钉住这一点。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import audit_service as AS  # noqa: E402

BACKEND = os.path.dirname(os.path.abspath(__file__))

# 本次新增的两类"降级留痕"
NEW_FLAGS = ("split_fallback", "ocr_partial_failure")


def _read(rel):
    with open(os.path.join(BACKEND, rel), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# 1. 标记类型必须双处登记
# --------------------------------------------------------------------------

def test_new_flags_declared_in_flag_types():
    """没有中文名的话，仪表盘消息会退化成显示英文键名。"""
    for t in NEW_FLAGS:
        assert t in AS.FLAG_TYPES, f"{t} 未登记进 FLAG_TYPES"
        assert AS.FLAG_TYPES[t].strip(), f"{t} 的中文名为空"


def test_new_flags_are_preserved_by_rewrite():
    """关键：不在保留名单里的标记会被 run_auto_rewrite 悄悄清掉。"""
    for t in NEW_FLAGS:
        assert t in AS._PRESERVED_FLAG_TYPES, (
            f"{t} 不在 _PRESERVED_FLAG_TYPES 里 —— 自动重写会把它清掉，"
            f"用户会看到'标记出现过又没了'")


def test_preserved_tuple_covers_previous_hardcoded_triple():
    """改动不能把原来保住的三类弄丢。"""
    for t in ("missing_diagram", "question_challenge_low", "question_challenge_high"):
        assert t in AS._PRESERVED_FLAG_TYPES


def test_rewrite_uses_the_single_source_of_truth():
    """重写过滤必须引用 `_PRESERVED_FLAG_TYPES`，不能再写死一份列表。"""
    src = _read("services/audit_service.py")
    assert "_PRESERVED_FLAG_TYPES" in src
    # 旧的写死三元组写法不应再出现
    legacy = 'f.get("type") in (\n                                    "missing_diagram"'
    assert legacy not in src, "仍然存在写死的保留列表"


# --------------------------------------------------------------------------
# 2. 两处降级都必须真的写进 DB（不是只写日志/状态文件）
# --------------------------------------------------------------------------

def test_split_fallback_writes_db_flag():
    src = _read("routers/ocr.py")
    assert "flag_question" in src, "切题降级没有写 DB 标记"
    assert '"split_fallback"' in src, "切题降级没有用 split_fallback 类型"
    # 留痕失败必须被单独记日志，不能连着把识别也带崩
    assert "Failed to write split_fallback flag" in src


def test_partial_ocr_failure_writes_db_flag():
    src = _read("services/ocr_service.py")
    assert "ocr_failures" in src, "没有收集识别失败的图片"
    assert '"ocr_partial_failure"' in src, "没有写 ocr_partial_failure 标记"
    # 必须挂在"判为 done"的那一支（识别失败但内容仍可用），而不是把它降级成 error
    m = re.search(r"q\.status = \"done\"\s*\n\s*# 识别有降级", src)
    assert m, "ocr_partial_failure 没有挂在 done 分支上"


def test_failed_image_is_recorded_in_both_loops():
    """题干图与辅助图两条循环都要记，漏一条就有一半失败静默。"""
    src = _read("services/ocr_service.py")
    assert src.count("ocr_failures.append") >= 2, "两条 OCR 循环没有都记录失败"


# --------------------------------------------------------------------------
# 3. 前端必须看得见
# --------------------------------------------------------------------------

def test_frontend_renders_degraded_badges():
    js = _read("static/js/questions.js")
    assert "_degradedBadge" in js, "列表页没有降级徽标"
    assert "'split_fallback'" in js, "没有渲染 切题降级 徽标"
    assert "'ocr_partial_failure'" in js, "没有渲染 识别不全 徽标"
    # 徽标要真的被用上（定义了但没接到列表渲染里等于没做）
    assert js.count("_degradedBadge(q)") >= 2, "徽标函数没有被列表渲染调用"


def test_frontend_surfaces_rejected_questions():
    """后端返回了 rejected，前端不能只看 message 就说"已启动 N 道"。"""
    qjs = _read("static/js/questions.js")
    assert "rejected" in qjs, "questions.js 丢弃了 rejected"
    bu = _read("templates/batch_upload.html")
    assert "rejected" in bu, "batch_upload.html 丢弃了 rejected"
    assert "_rej" in bu
