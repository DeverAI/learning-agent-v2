# -*- coding: utf-8 -*-
"""R17：Agent 侧读知识图谱的行为测试。

## 为什么专门写这一组

`h_knowledge_lookup` 里曾经写成 `[n for n in nodes if n.get("id") == node_id]`，
而 `knowledge_graph` 的 `nodes` 是 **dict**（node_id -> node）而不是 list。
遍历 dict 拿到的是 key（字符串），`str.get` 直接 AttributeError。

这个 bug 有个恶劣性质：**图谱为空时完全看不出来** ——
空图谱会走"一个节点都没有"的提前返回，永远碰不到那行代码。
服务器上 `knowledge_graph.json` 本来就是不存在的，所以怎么点都是"友好提示"。

因此本组测试**必须用非空图谱**，否则等于没测。
"""
import asyncio
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import agent_tools  # noqa: E402
from services import knowledge_graph as KG  # noqa: E402
from services.agent_core import Ctx  # noqa: E402


class FakeReq:
    def __init__(self, msg):
        self.message = msg


def mk_ctx(msg, data=None, itype="knowledge_lookup"):
    return Ctx(sid="__graphtest__", req=FakeReq(msg),
               session={"id": "__graphtest__", "messages": []},
               messages=[{"role": "user", "content": msg}],
               steps=[], data=data or {}, itype=itype, save=lambda: None)


GRAPH_FIXTURE = {
    "nodes": {
        "knode_勾股定理": {"label": "勾股定理", "type": "concept", "core": True,
                           "weight": 5, "source_notes": ["n1"]},
        "knode_直角三角形": {"label": "直角三角形", "type": "concept", "core": True,
                             "weight": 4, "source_notes": ["n1"]},
    },
    "edges": [{"from": "knode_勾股定理", "to": "knode_直角三角形",
               "relation": "related", "weight": 2}],
    "clusters": {},
    "keyword_index": {"勾股定理": ["knode_勾股定理"],
                      "直角三角形": ["knode_直角三角形"]},
}


@pytest.fixture()
def graph_file(tmp_path, monkeypatch):
    p = tmp_path / "knowledge_graph.json"
    p.write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(KG, "GRAPH_FILE", str(p))
    return str(p)


@pytest.fixture()
def empty_graph(tmp_path, monkeypatch):
    p = tmp_path / "knowledge_graph.json"   # 故意不创建
    monkeypatch.setattr(KG, "GRAPH_FILE", str(p))
    return str(p)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 非空图谱：这才是会崩的那种输入
# --------------------------------------------------------------------------

def test_search_on_nonempty_graph_does_not_crash(graph_file):
    r = _run(agent_tools.h_knowledge_lookup(mk_ctx("查一下勾股定理", {"query": "勾股定理"})))
    assert "勾股定理" in r["reply"]
    assert r["action"]["data"]["matched"], "非空图谱上应该匹配到节点"


def test_lookup_by_node_id_does_not_crash(graph_file):
    """按 node_id 直查 —— 旧代码就是死在这一行（遍历 dict 拿到的 key 没有 .get）。"""
    r = _run(agent_tools.h_knowledge_lookup(
        mk_ctx("看一下这个知识点", {"node_id": "knode_勾股定理"})))
    assert r["action"]["data"]["center"]["id"] == "knode_勾股定理", r["action"]["data"]


def test_related_mode_does_not_crash(graph_file):
    """关联网络模式会遍历 nodes 建 label 表，旧代码同样会崩。"""
    r = _run(agent_tools.h_knowledge_lookup(
        mk_ctx("它和什么有关", {"node_id": "knode_勾股定理", "mode": "related"})))
    assert "直角三角形" in r["reply"], r["reply"]


def test_detail_mode(graph_file):
    r = _run(agent_tools.h_knowledge_lookup(
        mk_ctx("详情", {"node_id": "knode_直角三角形", "mode": "detail"})))
    assert "直角三角形" in r["reply"]


def test_no_match_on_nonempty_graph_is_distinguishable(graph_file):
    """有数据但匹配不上 -> 必须说"没匹配"，不是"图谱是空的"。"""
    r = _run(agent_tools.h_knowledge_lookup(mk_ctx("查个不存在的东西", {"query": "zzz不存在"})))
    d = r["action"]["data"]
    assert d.get("empty") is not True
    assert d.get("matched") == []
    assert "没有匹配" in r["reply"]


# --------------------------------------------------------------------------
# 空图谱：状态必须与"没匹配上"可区分
# --------------------------------------------------------------------------

def test_empty_graph_reports_empty_not_no_match(empty_graph):
    r = _run(agent_tools.h_knowledge_lookup(mk_ctx("查一下", {"query": "勾股定理"})))
    d = r["action"]["data"]
    assert d.get("empty") is True
    assert "一个节点都还没有" in r["reply"]


# --------------------------------------------------------------------------
# related_content 的知识图谱来源同样走 dict
# --------------------------------------------------------------------------

def test_related_content_graph_source_nonempty(graph_file):
    r = _run(agent_tools.h_related_content(mk_ctx("找勾股定理的相关内容", {"topic": "勾股定理"})))
    srcs = {s["key"]: s for s in r["action"]["data"]["sources"]}
    assert srcs["graph"]["status"] == "ok", srcs["graph"]
    assert srcs["graph"]["count"] >= 1


def test_related_content_graph_source_empty(empty_graph):
    r = _run(agent_tools.h_related_content(mk_ctx("找点东西", {"topic": "勾股定理"})))
    srcs = {s["key"]: s for s in r["action"]["data"]["sources"]}
    # 图谱文件不存在 -> empty（"来源本身是空的"），不是 no_match
    assert srcs["graph"]["status"] == "empty", srcs["graph"]
