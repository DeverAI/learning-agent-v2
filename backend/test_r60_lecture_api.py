# -*- coding: utf-8 -*-
"""round 60 离线回归：讲题服务（plan 生成/跨题压缩/清单）+ Agent 试卷意图。"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
config.STORAGE_DIR = tempfile.mkdtemp(prefix="la_r60_")

import models.database as dbm
import routers.ocr, routers.papers, routers.sessions, routers.lecture  # noqa: F401 模型全注册
dbm.reset_engine()
asyncio.run(dbm.init_db())

from models.database import async_session
from models.models import Paper, Question
from services import lecture_service as ls

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


async def seed():
    async with async_session() as db:
        qids = []
        for i in range(3):
            q = Question(id=f"r60_q{i+1}", folder_path=f"q{i+1}", raw_image_path="x.jpg",
                         status="done", is_resolved=True, bank="default", subject="math",
                         ocr_text=f"题目{i+1}", question_html=f"<p>题目{i+1}</p>",
                         standard_answer=f"答案{i+1}")
            db.add(q)
            qids.append(q.id)
        await db.flush()
        p = Paper(id="r60_paper", title="检查卷", subject="math", grade="初二",
                  question_ids=qids,
                  question_order=[{"id": q, "number": i + 1} for i, q in enumerate(qids)])
        db.add(p)
        await db.commit()
        return qids


qids = asyncio.run(seed())


# ---- T1 清单 ----
print("\n[T1] 清单")
async def t1():
    async with async_session() as db:
        papers = await ls.list_paper_summaries(db)
        qs = await ls.list_question_summaries(db)
        check("1.试卷清单含新卷", any(p["id"] == "r60_paper" for p in papers))
        check("1.题目清单含 done 题", len(qs) == 3)
asyncio.run(t1())


# ---- T2 单题计划（mock AI）----
print("\n[T2] 单题计划")
async def t2():
    async with async_session() as db:
        async def fake_json(messages, **kw):
            return {"steps": [{"title": "题意分析", "speech": "看已知条件",
                       "placement_hint": "top_right"},
                      {"title": "解法", "speech": "套公式", "placement_hint": "bad_hint"}],
            "summary": "本题收尾"}
        ls.ai_service.deepseek_json = fake_json
        plan = await ls.build_question_plan(db, "r60_q1")
        check("2.steps 归一且非法 hint 回落", len(plan["steps"]) == 2
              and plan["steps"][1]["placement_hint"] == "mid_left")
        check("2.summary 透传", plan["summary"] == "本题收尾")

        # 跨题压缩：prev_summary 进 prompt
        captured = {}
        async def cap(messages, **kw):
            captured["msgs"] = messages
            return {"steps": [{"title": "解", "speech": "s"}], "summary": "下一题收尾"}
        ls.ai_service.deepseek_json = cap
        await ls.build_question_plan(db, "r60_q2", prev_summary="上一题已讲完")
        user = captured["msgs"][1]["content"]
        check("2.prev_summary 注入 prompt", "上一题已讲完" in user)

        # 失败降级
        async def boom(messages, **kw):
            raise RuntimeError("down")
        ls.ai_service.deepseek_json = boom
        plan3 = await ls.build_question_plan(db, "r60_q3")
        check("2.AI 失败降级两步直读", len(plan3["steps"]) == 2
              and plan3["steps"][0]["title"] == "题目")
asyncio.run(t2())


# ---- T3 整卷连讲（跨题压缩）----
print("\n[T3] 整卷连讲")
async def t3():
    async with async_session() as db:
        summaries = []
        async def fake_json(messages, **kw):
            user = messages[1]["content"]
            tag = "prev" if "前情提要" in user else "noprev"
            summaries.append(tag)
            return {"steps": [{"title": "s", "speech": "x"}],
                    "summary": f"第{len(summaries)+1}题收尾"}
        ls.ai_service.deepseek_json = fake_json
        data = await ls.build_paper_plans(db, "r60_paper")
        check("3.整卷 3 题全部生成", data["count"] == 3)
        check("3.跨题摘要逐题传递（第 2/3 题带前情，第 1 题不带）",
              summaries[0] == "noprev" and summaries[1] == "prev"
              and summaries[2] == "prev")
asyncio.run(t3())


# ---- T4 Agent 试卷意图（工具注册表）----
# 2026-09-11 重构后：分支不再是 sessions.py 里的 `if itype == "..."` 源码串，
# 而是 agent_tools.py 里的具名处理器 + agent_core.REGISTRY 里的一条表项。
# 因此这里改成**按语义断言**（工具是否注册、有无确认闸、处理器是否可调用），
# 而不是继续 grep 源码字符串 —— 旧的字符串断言正是"改一次结构就误报"的脆弱写法。
print("\n[T4] Agent 试卷意图（工具注册表）")
from services import agent_tools as _at
_at.register_all()
from services.agent_core import get as _tool_get
_src_handlers = open(os.path.join("services", "agent_tools.py"), encoding="utf-8").read()
_src_router = open(os.path.join("routers", "sessions.py"), encoding="utf-8").read()

check("4.edit_paper 已注册（原先不在分类器 prompt 里 => 分支不可达）",
      _tool_get("edit_paper") is not None)
check("4.delete_paper 已注册（同上，原先同样不可达）",
      _tool_get("delete_paper") is not None)
check("4.edit_paper 带确认闸（原注释声称有、实际没有，现已补上）",
      bool(_tool_get("edit_paper").requires_confirm))
check("4.delete_paper 处理器可调用",
      callable(getattr(_tool_get("delete_paper"), "handler", None)))
check("4.delete_paper 走「用户原话确认语」闸（不信任分类器给的 confirm）",
      _tool_get("delete_paper").requires_confirm == "确认删除试卷"
      and 'c.data.get("confirm"' not in _src_handlers)
check("4.delete_paper 题目保留语义（paper_id=None 解绑）",
      "paper_id=None" in _src_handlers)
check("4.分类器 prompt 改为由注册表生成（消除手写失同步）",
      "build_intent_prompt()" in _src_router)


# ---- T5 路由注册 ----
print("\n[T5] 路由注册")
from main import app
def iter_routes(routes):
    for r in routes:
        if hasattr(r, "original_router"):
            yield from iter_routes(r.original_router.routes)
        elif hasattr(r, "routes") and not hasattr(r, "methods"):
            yield from iter_routes(r.routes)
        else:
            yield r
paths = {f"{m} {getattr(r,'path','')}" for r in iter_routes(app.routes)
         for m in (getattr(r, "methods", None) or set())
         if getattr(r, "path", "").startswith("/api/lecture")}
check("5.GET /api/lecture/papers 注册", "GET /api/lecture/papers" in paths)
check("5.POST /api/lecture/plan-paper 注册", "POST /api/lecture/plan-paper" in paths)
check("5.POST /api/lecture/plan/{question_id} 注册",
      "POST /api/lecture/plan/{question_id}" in paths)
print(f"\n===== R60 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
