# -*- coding: utf-8 -*-
"""round 58 实机验证（真服务器 + 真 AI + TTS）：恢复后重跑。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from core.lecture_engine import LectureEngine  # noqa: E402
from core.screen_paint import ScreenPaintOverlay  # noqa: E402
from core.tts_player import TTSPlayer  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def main():
    # 验证专用：注入服务器访问密码（从桥文件读取，不落 settings）
    import base64
    import json as _json
    bridge = os.path.join(os.environ.get("TEMP", "."), "la_probe", "la_auth.json")
    if os.path.exists(bridge):
        pwd = base64.b64decode(_json.load(open(bridge))["p"]).decode()
        from config.settings import ConfigManager as _CM
        _CM().settings.sync_server_password = pwd
        print("[0] 已注入服务器访问密码（长度 %d）" % len(pwd))

    overlay = ScreenPaintOverlay()
    tts = TTSPlayer()
    eng = LectureEngine(tts=tts, overlay=overlay)

    papers_out, errors = [], []
    eng.papers_ready.connect(lambda p: papers_out.append(p))
    eng.error.connect(lambda m: errors.append(m))

    print("[1] 拉取服务器试卷列表 ...")
    eng.fetch_papers()
    deadline = time.time() + 20
    while time.time() < deadline and not papers_out and not errors:
        app.processEvents()
        time.sleep(0.1)
    if not papers_out:
        print(f"  [FAIL] {errors}")
        return 1
    papers = papers_out[0]
    print(f"  拉到 {len(papers)} 份试卷")
    check("服务器试卷列表拉取成功（可为空）", papers_out and not errors)

    q = None
    if papers:
        print("[2] 加载第一份有题的试卷 ...")
        paper_out = []
        eng.paper_ready.connect(lambda p: paper_out.append(p))
        for p in papers:
            eng.fetch_paper(p["id"])
            deadline = time.time() + 15
            while time.time() < deadline and not paper_out:
                app.processEvents()
                time.sleep(0.1)
            if paper_out and paper_out[0]["question_order"]:
                q = paper_out[0]["question_order"][0]
                print(f"  试卷「{paper_out[0]['title']}」共 {len(paper_out[0]['question_order'])} 题，选第 {q['number']} 题")
                break
    if q is None:
        print("[2b] 无试卷 → 从题库直接选一道已完成题（回退路径）")
        import requests as _rq
        r = _rq.get(f"{eng._base()}/api/questions", headers=eng._headers(), timeout=20)
        items = r.json() if isinstance(r.json(), list) else []
        done_items = [it for it in items if it.get("status") == "done"]
        if not done_items:
            print("  [FAIL] 题库无已完成题目可讲")
            return 1
        q = {"id": done_items[0]["id"], "number": 1}
        print(f"  题库直选 {q['id']}")
    check("选中可讲解题目", q is not None)

    print("[3] 题目详情 + 真 AI 讲解计划 ...")
    question = eng.fetch_question(q["id"])
    if not question:
        print("  [FAIL] 题目详情失败")
        return 1
    steps = []
    eng.plan_ready.connect(lambda s, sm: steps.extend(s))
    eng.build_plan(question)
    deadline = time.time() + 90
    while time.time() < deadline and not steps:
        app.processEvents()
        time.sleep(0.2)
    print(f"  讲解 {len(steps)} 步: {[s['title'] for s in steps][:6]}")
    check("AI 讲解计划生成", len(steps) >= 2)
    hints = [s.get("placement_hint") for s in steps]
    check("steps 带 placement_hint", all(h for h in hints))
    print(f"  hints: {hints}")

    print("[4] 逐步讲解（放置代理 + 卡片 + TTS）...")
    step_events = []
    eng.step_started.connect(lambda i, t, s: (step_events.append(i),
                                              print(f"    第{i+1}步 [{t}] placement={steps[i].get('placement_hint')}")))
    finished = []
    eng.lecture_finished.connect(lambda: finished.append(1))
    eng.start_lecture(steps)
    deadline = time.time() + 240
    while time.time() < deadline and not finished:
        app.processEvents()
        time.sleep(0.2)
    check("逐步讲解完成", len(step_events) == len(steps) and finished)

    print("[5] 清理")
    eng.shutdown()

    print("\n===== R58 实机验证 =====")
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
