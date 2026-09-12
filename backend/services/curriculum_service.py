"""课程体系：初中知识点的加载、校验、灌库，以及「今天该补什么」。

来源与依据
----------
2026-09-12 实测确认：这个项目里**从来没有任何学科知识点体系**，
`storage/knowledge_graph.json` 文件根本不存在，知识树只有 7 个由笔记临时生成的节点。
而用户的目标是「撑过初三知识补漏 + 自招」，补漏要回答的第一个问题是
**「我这个知识点不会，该先补哪个前置」** —— 没有体系与前置边就无从回答。

因此本模块负责把 `data/curriculum_cn_junior.json` 灌进知识图谱，
并在此基础上给出「今日补漏清单」。

三条不妥协的约定
----------------
1. **写错要报错，不要静默丢边。** `validate()` 逐条检查 `prereq` 里的 label 是否存在于
   同一份文件；有拼写错误就明确列出来。静默丢掉一条前置边，用户会得到一个**错误的补漏顺序**，
   比直接报错危险得多。
2. **没有前置边就说没有。** `prerequisites_of()` 只走 `prerequisite` 边，
   图里没有就返回空并让调用方如实交代 —— 绝不替用户编一个学习顺序。
3. **掌握度是估计值。** `build_review_plan()` 用海马体的掌握度排序，但它在返回体里
   明确标注 `mastery_source="estimated_decayed"`，不假装是实测分数。
"""

from __future__ import annotations

import json
import os
from typing import Optional

from logger import get_logger, log_error
from services import knowledge_graph as KG

logger = get_logger()

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRICULUM_FILE = os.path.join(BACKEND_DIR, "data", "curriculum_cn_junior.json")

CURRICULUM_TAG = "curriculum_v1"

# 难度分层（与数据文件里的 band 字段一致，按由易到难排序）
BANDS = ["基础", "中档", "难", "自招"]


class CurriculumError(Exception):
    pass


# --------------------------------------------------------------------------
# 加载与校验
# --------------------------------------------------------------------------

def load_curriculum(path: Optional[str] = None) -> dict:
    p = path or CURRICULUM_FILE
    if not os.path.exists(p):
        raise CurriculumError(f"课程体系文件不存在：{p}")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise CurriculumError(f"课程体系文件解析失败：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("subjects"), list):
        raise CurriculumError("课程体系文件结构不对：需要 {\"subjects\": [...]}")
    return data


def validate(curriculum: dict) -> dict:
    """校验体系数据。返回 {"ok":bool,"errors":[...],"warnings":[...],"stats":{...}}。

    重点抓三类问题：
    - `prereq` 引用了**不存在的 label**（拼写错误）—— 必须报错；
    - 同一学科内 label 重复 —— 报错（会导致节点被覆盖，依赖关系错乱）；
    - 自环（A 的前置是 A）—— 报错；
    - 前置成环（A->B->A）—— 报错，否则拓扑排序会死循环。
    """
    errors: list[str] = []
    warnings: list[str] = []
    stats = {"subjects": 0, "nodes": 0, "edges": 0, "bands": {}}

    for subj in curriculum.get("subjects") or []:
        name = str(subj.get("subject") or "").strip()
        nodes = subj.get("nodes") or []
        if not name:
            errors.append("有一个学科没有 subject 名")
            continue
        stats["subjects"] += 1
        labels = [str(n.get("label") or "").strip() for n in nodes]
        dupes = {l for l in labels if labels.count(l) > 1 and l}
        if dupes:
            errors.append(f"[{name}] label 重复：{sorted(dupes)}")
        label_set = set(labels)
        stats["nodes"] += len(nodes)
        for n in nodes:
            label = str(n.get("label") or "").strip()
            band = str(n.get("band") or "")
            stats["bands"][band] = stats["bands"].get(band, 0) + 1
            if band and band not in BANDS:
                warnings.append(f"[{name}] {label} 的 band 不在 {BANDS}：{band}")
            for pre in (n.get("prereq") or []):
                pre = str(pre).strip()
                stats["edges"] += 1
                if pre == label:
                    errors.append(f"[{name}] {label} 的前置是它自己（自环）")
                elif pre not in label_set:
                    errors.append(f"[{name}] {label} 的前置「{pre}」在体系里不存在（拼写错误？）")

    # 成环检测（只在同一学科内做；跨学科的边一律按 label 全库查）
    all_labels = {}
    for subj in curriculum.get("subjects") or []:
        for n in (subj.get("nodes") or []):
            all_labels[str(n.get("label") or "").strip()] = list(n.get("prereq") or [])
    cycles = _find_cycles(all_labels)
    for c in cycles:
        errors.append("前置关系成环：" + " -> ".join(c))

    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


def _find_cycles(adj: dict) -> list:
    """找前置关系的环。adj: label -> [prereq labels]。返回若干条环路径。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in adj}
    found: list = []
    stack: list = []

    def visit(u: str):
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, []):
            if v not in color:
                continue
            if color[v] == GRAY:
                i = stack.index(v)
                found.append(stack[i:] + [v])
                if len(found) >= 5:      # 够了就停，避免病态图刷屏
                    return
            elif color[v] == WHITE:
                visit(v)
        stack.pop()
        color[u] = BLACK

    for k in list(color):
        if color[k] == WHITE:
            visit(k)
            if len(found) >= 5:
                break
    return found


# --------------------------------------------------------------------------
# 灌库
# --------------------------------------------------------------------------

def flatten(curriculum: dict) -> tuple[list, list]:
    """把体系文件摊平成 (nodes, edges)。prereq 边方向为 前置 -> 该知识点。"""
    nodes, edges = [], []
    for subj in curriculum.get("subjects") or []:
        subject = str(subj.get("subject") or "").strip()
        for n in (subj.get("nodes") or []):
            label = str(n.get("label") or "").strip()
            if not label:
                continue
            nodes.append({
                "label": label,
                "subject": subject,
                "grade": str(n.get("grade") or ""),
                "module": str(n.get("module") or ""),
                "band": str(n.get("band") or "基础"),
                "description": str(n.get("description") or ""),
            })
            for pre in (n.get("prereq") or []):
                edges.append({"from": str(pre).strip(), "to": label,
                              "relation": "prerequisite"})
    return nodes, edges


def seed(force: bool = False) -> dict:
    """校验并灌库。校验不过**直接抛错**，不写任何东西。"""
    cur = load_curriculum()
    report = validate(cur)
    if not report["ok"]:
        raise CurriculumError("课程体系校验未通过，未写入图谱：\n  - "
                              + "\n  - ".join(report["errors"][:20]))
    nodes, edges = flatten(cur)
    if force:
        removed = KG.remove_curriculum(CURRICULUM_TAG)
    else:
        removed = {"nodes_removed": 0, "edges_removed": 0}
    stats = KG.merge_curriculum(nodes, edges, tag=CURRICULUM_TAG)
    graph_stats = KG.get_graph_stats()
    return {
        "ok": True,
        "validation": report,
        "removed_before_reseed": removed,
        "merge": stats,
        "graph_now": graph_stats,
    }


# --------------------------------------------------------------------------
# 查询：先补什么
# --------------------------------------------------------------------------

def prerequisites_of(label: str, depth: int = 5) -> dict:
    """「要掌握 X，之前应先掌握什么」。找不到节点 / 没有前置边都如实返回。"""
    node = KG.find_node_by_label(label)
    if not node:
        return {"found": False, "label": label,
                "note": "图谱里没有这个知识点（可能是名字不一致，或体系还没灌库）"}
    result = KG.get_prerequisites(node["id"], depth=depth)
    # 拍平成一个从"最该先学"到"最靠近目标"的顺序（层级由远到近）
    ordered = []
    for level in reversed(result.get("levels") or []):
        for item in level:
            if item["id"] not in [x["id"] for x in ordered]:
                ordered.append(item)
    result["label"] = label
    result["study_order"] = ordered          # 越靠前越该先学
    if not result.get("levels"):
        result["note"] = ("图谱里**没有**记录这个知识点的前置关系，"
                          "所以给不出「先补什么」——不要凭空推断顺序。")
    return result


def list_subject(subject: str = "", grade: str = "", band: str = "") -> dict:
    """按条件列出体系里的知识点（用于给用户看"这一章都有什么"）。"""
    graph = KG._load_graph()
    items = []
    for nid, n in graph["nodes"].items():
        if not n.get("is_curriculum"):
            continue
        if subject and n.get("subject") != subject:
            continue
        if grade and n.get("grade") != grade:
            continue
        if band and n.get("band") != band:
            continue
        items.append({"id": nid, "label": n.get("label", ""),
                      "subject": n.get("subject", ""), "grade": n.get("grade", ""),
                      "module": n.get("module", ""), "band": n.get("band", "")})
    order = {b: i for i, b in enumerate(BANDS)}
    grade_order = ["七上", "七下", "八上", "八下", "九上", "九下"]
    gorder = {g: i for i, g in enumerate(grade_order)}
    items.sort(key=lambda x: (gorder.get(x["grade"], 99), order.get(x["band"], 99), x["label"]))
    return {"total": len(items), "items": items}


# --------------------------------------------------------------------------
# 今日补漏清单
# --------------------------------------------------------------------------

async def build_review_plan(subject: str = "", limit: int = 10, grade: str = "") -> dict:
    """把「课程体系 + 掌握度 + 错题」合成一份可执行的补漏清单。

    排序依据（**逐条标注是「事实」还是「估计」**）：
    1. 有错题的知识点优先（事实：题库里确实有做错过/被标记的题）；
    2. 已记录掌握度的，按掌握度从低到高（**估计值**，来自海马体的衰减曲线）；
    3. 没有记录的，按体系的 grade/band 从易到难（事实：体系里就是这么标的）。

    `grade` 可限定年级（如「九上」）。**不给默认值是有意的**：
    默认从七上开始是对的（补漏本来就该从最早的缺口补），
    但初三学生通常只想看当下的，所以让他显式说，而不是替他决定。

    返回体里明确区分 `evidence` 字段，避免"估计值"被当成"实测分数"用。

    **async**：内部要读数据库，而它会被 async 端点/prod handler 调用；
    用 `asyncio.run` 包同步版会在已有事件循环里直接抛 RuntimeError。
    """
    import asyncio

    from services.hippocampus_service import get_teaching_context

    curriculum_nodes = list_subject(subject=subject, grade=grade)["items"]
    if not curriculum_nodes:
        return {
            "total": 0, "items": [],
            "note": ("课程体系里没有匹配的知识点 —— 要么体系还没灌库"
                     "（跑 `POST /api/knowledge-graph/seed-curriculum`），"
                     "要么这个学科还没录。**这不是「你都会了」。**"),
        }

    # 海马体掌握度（估计值）。同步函数 -> 丢线程，别卡住事件循环
    try:
        ctx = await asyncio.to_thread(get_teaching_context, "")
        topics = ctx.get("topics") or {}
    except Exception as exc:
        log_error("curriculum.plan", f"read hippocampus failed: {exc}")
        topics = {}

    # 错题/标记题（事实）
    wrong_by_tag: dict = {}
    try:
        from sqlalchemy import or_, select
        from models.database import async_session
        from models.models import Question

        async with async_session() as db:
            r = await db.execute(
                select(Question).where(
                    or_(Question.status == "error",
                        Question.audit_flags.is_not(None))
                ).limit(500)
            )
            for q in r.scalars().all():
                flags = q.audit_flags if isinstance(q.audit_flags, list) else []
                if not flags and q.status != "error":
                    continue
                for t in (q.knowledge_tags or []):
                    wrong_by_tag[t] = wrong_by_tag.get(t, 0) + 1
    except Exception as exc:
        logger.warning("curriculum.plan: collecting wrong questions failed: %s", exc)

    band_order = {b: i for i, b in enumerate(BANDS)}
    grade_order = {g: i for i, g in enumerate(["七上", "七下", "八上", "八下", "九上", "九下"])}

    items = []
    for n in curriculum_nodes:
        label = n["label"]
        mastery = None
        src = None
        for topic, v in topics.items():
            if topic and (topic in label or label in topic):
                try:
                    mastery = float(v.get("mastery") or 0.0)
                except (TypeError, ValueError):
                    mastery = 0.0
                src = "estimated_decayed"      # 衰减后的估计值，不是实测分
                break
        wrong = wrong_by_tag.get(label, 0)
        items.append({
            "id": n["id"], "label": label, "subject": n["subject"],
            "grade": n["grade"], "module": n["module"], "band": n["band"],
            "mastery": mastery, "mastery_source": src,
            "wrong_question_count": wrong,
            "evidence": ("has_wrong_questions" if wrong else
                         ("has_mastery_record" if mastery is not None else
                          "curriculum_only")),
        })

    def sort_key(x):
        return (
            0 if x["wrong_question_count"] else 1,
            x["mastery"] if x["mastery"] is not None else 1.0,
            grade_order.get(x["grade"], 99),
            band_order.get(x["band"], 99),
            x["label"],
        )

    items.sort(key=sort_key)
    trimmed = items[:max(1, min(int(limit or 10), 50))]
    return {
        "total": len(items),
        "returned": len(trimmed),
        "subject": subject,
        "grade": grade,
        "items": trimmed,
        "mastery_source_note": ("掌握度来自海马体、是按遗忘曲线**衰减后的估计值**，"
                                "不是实测分数；没有记录的知识点 mastery 为 null，"
                                "表示「没有数据」而不是「掌握度为零」。"),
        "evidence_note": ("evidence=has_wrong_questions 表示题库里确实有该知识点的错题/标记题（事实）；"
                          "curriculum_only 表示只有体系标签、没有任何个人数据。"),
    }
