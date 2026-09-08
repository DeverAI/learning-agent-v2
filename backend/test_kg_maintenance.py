"""知识图谱/知识树后端维护逻辑回归测试。

覆盖：
1. remove_note_from_graph：末位来源删除节点、共享来源清片段降权重
2. 间接关系推断在簇重建之后执行（新节点能拿到 cluster 并建立弱关联）
3. 图谱文件损坏时先留档 .corrupt，不再静默用空结构覆盖
4. 删除笔记 / 自动整理合并后同步清理知识图谱（子进程隔离真实数据）
"""

import json
import os
import subprocess
import sys
import tempfile


BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


def _with_temp_graph(func):
    """把 kg.GRAPH_FILE 指向临时文件执行断言，结束后恢复。"""
    import services.knowledge_graph as kg

    fd, tmp = tempfile.mkstemp(prefix="kg_test_", suffix=".json")
    os.close(fd)
    os.remove(tmp)
    old_file = kg.GRAPH_FILE
    kg.GRAPH_FILE = tmp
    try:
        func(kg)
    finally:
        kg.GRAPH_FILE = old_file
        for path in (tmp, tmp + ".corrupt"):
            if os.path.exists(path):
                os.remove(path)


def test_remove_note_deletes_node_and_clears_snippet():
    def run(kg):
        extracted = {"concepts": [{"label": "勾股定理", "type": "concept"}], "relations": []}
        kg.add_note_to_graph("n1", "t", "内容一", [], extracted)
        kg.add_note_to_graph("n2", "t", "内容二", [], extracted)
        graph = kg._load_graph()
        nid = "knode_勾股定理"
        assert nid in graph["nodes"]
        assert graph["nodes"][nid]["source_notes"] == ["n1", "n2"]
        assert graph["nodes"][nid]["note_snippets"]["n2"] == "内容二"

        # 移除共享来源之一：节点保留、权重下降、该笔记摘要被清除
        kg.remove_note_from_graph("n2")
        graph = kg._load_graph()
        assert nid in graph["nodes"]
        assert graph["nodes"][nid]["source_notes"] == ["n1"]
        assert "n2" not in graph["nodes"][nid]["note_snippets"]
        assert "n1" in graph["nodes"][nid]["note_snippets"]

        # 移除最后一个来源：节点连同边一起删除
        kg.remove_note_from_graph("n1")
        graph = kg._load_graph()
        assert nid not in graph["nodes"]
        assert graph["edges"] == []

    _with_temp_graph(run)


def test_indirect_relation_inferred_for_new_cluster_member():
    def run(kg):
        extracted = {
            "concepts": [
                {"label": "相似三角形", "type": "concept"},
                {"label": "对应边成比例", "type": "term"},
                {"label": "对应角相等", "type": "term"},
            ],
            "relations": [
                {"from": "对应边成比例", "to": "相似三角形", "relation": "is_a"},
                {"from": "对应角相等", "to": "相似三角形", "relation": "is_a"},
            ],
        }
        kg.add_note_to_graph("n1", "t", "内容", [], extracted)
        graph = kg._load_graph()
        b = "knode_对应边成比例"
        c = "knode_对应角相等"
        linked = any(
            {e.get("from"), e.get("to")} == {b, c} for e in graph["edges"]
        )
        assert linked, f"同簇新节点之间应推断出弱关联，实际边: {graph['edges']}"

    _with_temp_graph(run)


def test_corrupt_graph_file_backed_up_not_wiped():
    def run(kg):
        payload = '{"nodes": {"knode_old": {"label": "旧概念"'
        with open(kg.GRAPH_FILE, "w", encoding="utf-8") as f:
            f.write(payload)

        graph = kg._load_graph()
        assert graph["nodes"] == {}
        assert graph["clusters"] == {}

        backup = kg.GRAPH_FILE + ".corrupt"
        assert os.path.exists(backup)
        with open(backup, "r", encoding="utf-8") as f:
            assert f.read() == payload

    _with_temp_graph(run)


def test_refresh_note_snippet_updates_and_clears():
    def run(kg):
        extracted = {"concepts": [{"label": "欧姆定律", "type": "concept"}], "relations": []}
        kg.add_note_to_graph("n1", "t", "旧内容旧内容", [], extracted)
        nid = "knode_欧姆定律"

        # 更新正文 → 摘要同步刷新
        kg.refresh_note_snippet("n1", "新内容新内容")
        graph = kg._load_graph()
        assert graph["nodes"][nid]["note_snippets"]["n1"] == "新内容新内容"
        assert graph["nodes"][nid]["source_notes"] == ["n1"]

        # 超长内容截断到 200 字符
        kg.refresh_note_snippet("n1", "x" * 500)
        graph = kg._load_graph()
        assert len(graph["nodes"][nid]["note_snippets"]["n1"]) == 200

        # 正文清空 → 摘要条目移除
        kg.refresh_note_snippet("n1", "")
        graph = kg._load_graph()
        assert "n1" not in graph["nodes"][nid]["note_snippets"]

        # 未知笔记 ID：无引用即无变更，不应报错也不应写盘
        mtime_before = os.path.getmtime(kg.GRAPH_FILE)
        kg.refresh_note_snippet("ghost", "内容")
        assert os.path.getmtime(kg.GRAPH_FILE) == mtime_before

    _with_temp_graph(run)


def test_load_graph_rejects_wrong_value_types():
    def run(kg):
        with open(kg.GRAPH_FILE, "w", encoding="utf-8") as f:
            json.dump({"nodes": {}, "edges": {"bad": 1}, "clusters": [],
                       "keyword_index": "oops"}, f)
        graph = kg._load_graph()
        assert graph["nodes"] == {}
        assert graph["edges"] == []
        assert graph["clusters"] == {}
        assert graph["keyword_index"] == {}
        # 错型数据被拒后应可正常写入新图谱
        summary = kg.add_note_to_graph("n1", "t", "c", [],
                                       {"concepts": [{"label": "测试概念"}], "relations": []})
        assert summary["nodes_added"] == 1

    _with_temp_graph(run)


def test_keyword_index_rebuild_dedupes_alias():
    def run(kg):
        extracted = {
            "concepts": [
                {"label": "勾股定理", "type": "concept",
                 "aliases": ["毕氏定理", "勾股定理 "]},
            ],
            "relations": [],
        }
        kg.add_note_to_graph("n1", "t", "c", [], extracted)
        # 添加并移除另一节点，使 remove_note_from_graph 走到索引重建分支
        kg.add_note_to_graph("n2", "t", "c", [],
                             {"concepts": [{"label": "临时概念"}], "relations": []})
        kg.remove_note_from_graph("n2")
        graph = kg._load_graph()
        nid = "knode_勾股定理"
        kw = kg._normalize_keyword("勾股定理")
        assert graph["keyword_index"].get(kw) is not None
        assert graph["keyword_index"][kw].count(nid) == 1, graph["keyword_index"][kw]

    _with_temp_graph(run)


def test_edit_note_refreshes_graph_snippet():
    """端到端：PUT /full 与 PATCH 修改正文后，图谱摘要片段应同步更新。"""
    script = r"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.getcwd())
import config
tmp = tempfile.mkdtemp(prefix="lh_kgedit_")
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
from services.knowledge_graph import add_note_to_graph, _load_graph

extracted = {"concepts": [{"label": "动量守恒", "type": "concept"}], "relations": []}

with TestClient(app_main.app, headers={"X-Auth-Token": "Ntmhzsgtc"}) as client:
    r = client.post("/api/notes", json={"title": "笔记C", "content": "原始内容"})
    assert r.status_code == 200, r.text
    nid_api = r.json()["id"]
    add_note_to_graph(nid_api, "笔记C", "原始内容", [], extracted)
    node_id = "knode_动量守恒"

    # PUT /full 更新正文
    r1 = client.put(f"/api/notes/{nid_api}/full", json={"content": "完整更新后的正文"})
    assert r1.status_code == 200, r1.text
    g = _load_graph()
    assert g["nodes"][node_id]["note_snippets"][nid_api] == "完整更新后的正文", g["nodes"][node_id]

    # PUT（部分更新）修改正文
    r2 = client.put(f"/api/notes/{nid_api}", json={"content": "补丁更新后的正文"})
    assert r2.status_code == 200, r2.text
    g = _load_graph()
    assert g["nodes"][node_id]["note_snippets"][nid_api] == "补丁更新后的正文", g["nodes"][node_id]

print("OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, cwd=BACKEND_DIR, timeout=180,
                            encoding="utf-8", errors="replace")
    assert "OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr[-1500:]}"


def test_delete_note_and_auto_organize_clean_graph():
    """端到端：删除笔记与自动整理合并都要清理图谱贡献。"""
    script = r"""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.getcwd())
import config
tmp = tempfile.mkdtemp(prefix="lh_kg_")
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
from services.knowledge_graph import add_note_to_graph, _load_graph

extracted = {"concepts": [{"label": "全等三角形", "type": "concept"}], "relations": []}

with TestClient(app_main.app, headers={"X-Auth-Token": "Ntmhzsgtc"}) as client:
    r1 = client.post("/api/notes", json={"title": "笔记A", "content": "AAA",
                                         "knowledge_tags": ["全等三角形"], "subject": "数学"})
    r2 = client.post("/api/notes", json={"title": "笔记B", "content": "BBB",
                                         "knowledge_tags": ["全等三角形"], "subject": "数学"})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    id_a, id_b = r1.json()["id"], r2.json()["id"]

    add_note_to_graph(id_a, "笔记A", "AAA", ["全等三角形"], extracted)
    add_note_to_graph(id_b, "笔记B", "BBB", ["全等三角形"], extracted)
    graph = _load_graph()
    nid = "knode_全等三角形"
    assert sorted(graph["nodes"][nid]["source_notes"]) == sorted([id_a, id_b])

    # 自动整理：两篇同标签笔记应合并（保留 updated_at 最新的一篇），
    # 被删笔记的图谱贡献同步清理
    org = client.post("/api/notes/auto-organize")
    assert org.status_code == 200, org.text
    graph = _load_graph()
    survivors = set(graph["nodes"][nid]["source_notes"])
    assert len(survivors) == 1 and survivors <= {id_a, id_b}, survivors
    kept_id = survivors.pop()

    # 删除最后一篇：知识点失去全部来源，节点应被移除
    dele = client.delete(f"/api/notes/{kept_id}")
    assert dele.status_code == 200, dele.text
    graph = _load_graph()
    assert nid not in graph["nodes"], graph["nodes"].keys()

print("OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, cwd=BACKEND_DIR, timeout=180,
                            encoding="utf-8", errors="replace")
    assert "OK" in result.stdout, f"stdout={result.stdout}\nstderr={result.stderr[-1500:]}"
