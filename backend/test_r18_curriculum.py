# -*- coding: utf-8 -*-
"""R18：课程体系与前置依赖的行为测试。

这一组要钉住的是**「知识补漏」能不能给出可靠顺序**这件事，所以重点不在"能跑"，
而在"错了会怎样"：

1. 体系数据写错（前置 label 拼错、label 重复、自环、成环）**必须报错而不是静默丢边** ——
   静默丢一条前置边，用户拿到的是一个**错误的补漏顺序**，比直接报错危险得多；
2. 图里没有前置边时**必须说"没有记录"**，不能编一个顺序出来；
3. 边的去重键必须带 `relation`，否则同一对节点之间的第二种关系会消失；
4. 掌握度只能标成**估计值**，且"没有数据"不能被当成"掌握度为零"。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import curriculum_service as C  # noqa: E402
from services import knowledge_graph as KG  # noqa: E402


@pytest.fixture()
def graph_file(tmp_path, monkeypatch):
    p = tmp_path / "knowledge_graph.json"
    monkeypatch.setattr(KG, "GRAPH_FILE", str(p))
    return str(p)


def _cur(subjects):
    return {"version": 1, "subjects": subjects}


# --------------------------------------------------------------------------
# 校验：写错必须报错
# --------------------------------------------------------------------------

def test_validate_ok_on_real_curriculum_file():
    """仓库里那份真数据必须始终校验通过（改了数据先跑这条）。"""
    rep = C.validate(C.load_curriculum())
    assert rep["ok"] is True, rep["errors"]
    assert rep["stats"]["nodes"] > 100, "体系节点数太少，疑似数据被截断"
    assert rep["stats"]["edges"] > 100, "前置边太少，疑似 prereq 被清空"


def test_validate_detects_missing_prereq_label():
    bad = _cur([{"subject": "数学", "nodes": [
        {"label": "一元二次方程", "band": "中档", "prereq": ["被写错的知识点"]},
    ]}])
    rep = C.validate(bad)
    assert rep["ok"] is False
    assert any("不存在" in e for e in rep["errors"]), rep["errors"]


def test_validate_detects_duplicate_label():
    bad = _cur([{"subject": "数学", "nodes": [
        {"label": "勾股定理", "band": "中档", "prereq": []},
        {"label": "勾股定理", "band": "中档", "prereq": []},
    ]}])
    rep = C.validate(bad)
    assert rep["ok"] is False
    assert any("重复" in e for e in rep["errors"]), rep["errors"]


def test_validate_detects_self_loop():
    bad = _cur([{"subject": "数学", "nodes": [
        {"label": "有理数", "band": "基础", "prereq": ["有理数"]},
    ]}])
    rep = C.validate(bad)
    assert rep["ok"] is False
    assert any("自环" in e for e in rep["errors"]), rep["errors"]


def test_validate_detects_cycle():
    bad = _cur([{"subject": "数学", "nodes": [
        {"label": "A", "band": "基础", "prereq": ["C"]},
        {"label": "B", "band": "基础", "prereq": ["A"]},
        {"label": "C", "band": "基础", "prereq": ["B"]},
    ]}])
    rep = C.validate(bad)
    assert rep["ok"] is False
    assert any("成环" in e for e in rep["errors"]), rep["errors"]


def test_seed_refuses_to_write_when_validation_fails(graph_file):
    """校验不过就**一个字节都不写**，不能写一半。"""
    bad = _cur([{"subject": "数学", "nodes": [
        {"label": "有理数", "band": "基础", "prereq": ["不存在的"]},
    ]}])
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "bad.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(bad, f, ensure_ascii=False)
    monkey = C.CURRICULUM_FILE
    C.CURRICULUM_FILE = p
    try:
        with pytest.raises(C.CurriculumError):
            C.seed()
    finally:
        C.CURRICULUM_FILE = monkey
    assert not os.path.exists(graph_file), "校验失败却把图写出来了"


# --------------------------------------------------------------------------
# 灌库：幂等 + 前置查询
# --------------------------------------------------------------------------

@pytest.fixture()
def seeded(graph_file):
    cur = _cur([{"subject": "数学", "nodes": [
        {"label": "有理数", "grade": "七上", "module": "数与式", "band": "基础", "prereq": []},
        {"label": "数轴", "grade": "七上", "module": "数与式", "band": "基础", "prereq": ["有理数"]},
        {"label": "一元一次方程", "grade": "七上", "module": "方程", "band": "中档",
         "prereq": ["数轴", "整式加减"]},
        {"label": "整式加减", "grade": "七上", "module": "数与式", "band": "基础", "prereq": ["有理数"]},
    ]}])
    nodes, edges = C.flatten(cur)
    KG.merge_curriculum(nodes, edges, tag="test_cur")
    return graph_file


def test_merge_is_idempotent(graph_file):
    nodes = [{"label": "有理数", "subject": "数学", "grade": "七上", "band": "基础"},
             {"label": "数轴", "subject": "数学", "grade": "七上", "band": "基础"}]
    edges = [{"from": "有理数", "to": "数轴", "relation": "prerequisite"}]
    first = KG.merge_curriculum(nodes, edges, tag="t2")
    second = KG.merge_curriculum(nodes, edges, tag="t2")
    assert first["edges_added"] == 1
    assert first["nodes_added"] == 2
    assert second["edges_added"] == 0, "重复灌库又加了一遍边"
    assert second["nodes_added"] == 0
    assert KG.get_graph_stats()["total_edges"] == 1


def test_prerequisites_ordered_levels(seeded):
    r = C.prerequisites_of("一元一次方程")
    assert r["found"] is True
    assert r["has_prerequisite_edges"] is True
    # 第 0 层是直接前置
    lvl0 = {x["label"] for x in r["levels"][0]}
    assert lvl0 == {"数轴", "整式加减"}, r["levels"]
    # 第 1 层是前置的前置
    lvl1 = {x["label"] for x in r["levels"][1]}
    assert lvl1 == {"有理数"}, r["levels"]
    # study_order 越靠前越先学 -> 有理数一定在最前
    assert r["study_order"][0]["label"] == "有理数", r["study_order"]


def test_prerequisites_says_none_when_no_edges(seeded):
    """有理数没有前置边 —— 必须如实说"没有记录"，不能编。"""
    r = C.prerequisites_of("有理数")
    assert r["found"] is True
    assert r["levels"] == []
    assert r["has_prerequisite_edges"] is False
    assert "没有" in r["note"]


def test_prerequisites_unknown_label_says_not_found(graph_file):
    r = C.prerequisites_of("这个知识点不存在")
    assert r["found"] is False
    assert "没有这个知识点" in r["note"]


def test_remove_curriculum(seeded):
    before = KG.get_graph_stats()
    assert before["total_nodes"] > 0
    KG.remove_curriculum("test_cur")
    after = KG.get_graph_stats()
    assert after["total_nodes"] == 0, after
    assert after["total_edges"] == 0, after


# --------------------------------------------------------------------------
# 边去重必须带 relation（这是我修掉的真实缺陷）
# --------------------------------------------------------------------------

def test_two_relations_between_same_pair_both_kept(graph_file):
    """同一对节点之间的两种关系都要留下 —— 旧代码按 (from,to) 去重，第二种被吞掉。"""
    KG.merge_curriculum(
        [{"label": "A", "subject": "测试"}, {"label": "B", "subject": "测试"}],
        [{"from": "A", "to": "B", "relation": "part_of"},
         {"from": "A", "to": "B", "relation": "related"}],
        tag="t")
    g = KG._load_graph()
    rels = {(e["from"], e["to"], e["relation"]) for e in g["edges"]}
    assert (KG._make_node_id("A"), KG._make_node_id("B"), "part_of") in rels
    assert (KG._make_node_id("A"), KG._make_node_id("B"), "related") in rels, \
        "第二种关系被静默丢掉了"


# --------------------------------------------------------------------------
# 今日补漏清单
# --------------------------------------------------------------------------

def test_review_plan_on_empty_graph_says_not_curriculum(graph_file):
    r = asyncio.run(C.build_review_plan())
    assert r["total"] == 0
    assert "不是「你都会了」" in r["note"], r["note"]


def test_review_plan_marks_estimated_mastery(seeded):
    r = asyncio.run(C.build_review_plan(limit=10))
    assert r["total"] >= 4
    assert "估计值" in r["mastery_source_note"], r["mastery_source_note"]
    for it in r["items"]:
        # 没有记录时必须显式 None（"没有数据"），不能是 0.0（"掌握度为零"）
        if it["evidence"] == "curriculum_only":
            assert it["mastery"] is None, it
        # 有记录时来源必须标成"衰减后的估计值"，不能让人误当成实测分
        if it["mastery"] is not None:
            assert it["mastery_source"] == "estimated_decayed", it
        assert it["evidence"] in ("has_wrong_questions", "has_mastery_record", "curriculum_only")
