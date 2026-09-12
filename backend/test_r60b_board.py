# -*- coding: utf-8 -*-
"""round 60 离线回归：黑板简单图形（shape）+ 引用图（reference）。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
config.STORAGE_DIR = tempfile.mkdtemp(prefix="la_r60b_")

import models.database as dbm
import routers.ocr, routers.papers, routers.sessions  # noqa: F401
dbm.reset_engine()
asyncio.run(dbm.init_db())

from models.database import async_session
from models.models import Question
import services.focus_service as fs

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


async def main():
    # 种一道带 reference.svg 的题目
    qid = "r60b_q1"
    qdir = os.path.join(config.QUESTIONS_DIR, qid)
    os.makedirs(qdir, exist_ok=True)
    ref_svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
               '<circle cx="50" cy="50" r="40" fill="none" stroke="black"/></svg>')
    with open(os.path.join(qdir, "reference.svg"), "w", encoding="utf-8") as f:
        f.write(ref_svg)
    async with async_session() as db:
        db.add(Question(id=qid, folder_path=qdir, raw_image_path="",
                        status="done", is_resolved=True, source_type="manual",
                        bank="default", ocr_text="圆的题目"))
        await db.commit()

    sess = await fs.start_session(mode="question", topic="", question_id=qid)
    sid = sess["id"]

    # ---- T1 shape 确定性渲染 ----
    print("\n[T1] shape 简单图形")
    for shape in ("triangle", "rect", "circle", "axes"):
        summary = await fs._apply_board_ops(sess, [
            {"op": "write", "kind": "shape",
             "spec": {"shape": shape, "title": f"测试{shape}"}}])
        check(f"1.{shape} 渲染成功", summary["svg_ok"] >= 1)
    # 无效 shape
    summary = await fs._apply_board_ops(sess, [
        {"op": "write", "kind": "shape", "spec": {"shape": "hexagon"}}])
    check("1.无效 shape 降级为失败文字", summary["svg_failed"] >= 1)

    # SVG 内容验证（triangle 有 polygon）
    board_dir = os.path.join(config.STORAGE_DIR, "focus_sessions", sid)
    assets = os.listdir(board_dir) if os.path.isdir(board_dir) else []
    tri_assets = [a for a in assets if a.endswith(".svg")]
    check("1.SVG 文件已保存", len(tri_assets) >= 4)
    if tri_assets:
        content = open(os.path.join(board_dir, tri_assets[0]),
                       encoding="utf-8").read()
        check("1.SVG 含有效绘图元素", "<polygon" in content or "<rect" in content
              or "<circle" in content or "<line" in content)

    # ---- T2 引用图 ----
    print("\n[T2] 引用图 reference")
    summary2 = await fs._apply_board_ops(sess, [{"op": "reference"}])
    check("2.reference 成功复制原题图", summary2["svg_ok"] >= 1)
    board_dir2 = os.path.join(config.STORAGE_DIR, "focus_sessions", sid)
    img_assets = [a for a in os.listdir(board_dir2)
                  if a.startswith("ref_")]
    check("2.reference 图片文件存在", len(img_assets) >= 1)
    src = open(os.path.join(board_dir2, img_assets[0]),
               encoding="utf-8").read()
    check("2.引用图内容 = 原题 reference.svg", "<circle" in src)

    # 再次引用（新调用 reference_used 重置 → 正常执行）
    summary3 = await fs._apply_board_ops(sess, [{"op": "reference"}])
    check("2.新调用引用重置（每段独立限额）", summary3["svg_ok"] >= 1)

    # 无 question_id 的会话 → skipped
    sess2 = await fs.start_session(mode="topic", topic="纯讲解")
    summary4 = await fs._apply_board_ops(sess2, [{"op": "reference"}])
    check("2.无题目会话 reference → skipped", summary4["skipped"] >= 1)

    await fs.end_session(sid)
    await fs.end_session(sess2["id"])

asyncio.run(main())
print(f"\n===== R60b 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
