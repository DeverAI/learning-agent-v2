"""知识图谱服务：从笔记中剥离核心知识点，构建关系网络。

功能：
1. AI 提取知识点（关键词、概念、人物、作品、时代等）
2. 知识点关系构建（共现关联、语义归类、层级关系）
3. 知识网络检索（搜一个知识点，返回关联网络）
4. 知识簇/并查集（相近的知识点自动归为一簇）
"""

import json
import os
import re
import time
import hashlib
from collections import defaultdict
from config import STORAGE_DIR, _atomic_write_json
from logger import get_logger

logger = get_logger()

GRAPH_FILE = os.path.join(STORAGE_DIR, "knowledge_graph.json")

# ── 数据结构 ──
# {
#   "nodes": {
#     "node_id": {
#       "label": "显示名称",
#       "type": "concept|person|work|era|term|event|location|feature",
#       "core": true/false,
#       "aliases": ["别名1", "别名2"],
#       "description": "简要描述",
#       "detail": "详细描述（可包含更丰富的内容）",
#       "source_notes": ["note_id1", "note_id2"],
#       "note_snippets": {"note_id": "内容摘要片段"},
#       "cluster": "cluster_id",
#       "weight": 5,
#       "created_at": "2026-08-18 19:00:00"
#     }
#   },
#   "edges": [
#     {"from": "node_id1", "to": "node_id2", "relation": "类型", "weight": 3}
#   ],
#   "clusters": {
#     "cluster_id": {"label": "簇名称", "nodes": ["n1","n2"], "tags": ["tag1"], "core_nodes": ["n1"]}
#   },
#   "keyword_index": {
#     "关键词": ["node_id1", "node_id2"]
#   }
# }

_NODE_TYPES = {"concept", "person", "work", "era", "term", "event", "location", "feature"}
_CORE_TYPES = {"concept", "term"}
_RELATION_TYPES = {
    "related": "相关",
    "is_a": "属于",
    "part_of": "包含",
    "created_by": "创作者",
    "contemporary": "同时期",
    "influenced": "影响",
    "belongs_to": "归属于",
    "compared_with": "对比",
    # 时序关系（2026-09-12 新增）。在此之前的 8 种关系全是**非时序**的，
    # 所以"学 X 之前要先会什么"在图里无法表达 —— 知识补漏就缺了排序依据。
    "prerequisite": "前置",
    "successor": "后继",
}
# 时序关系集合：做"先补什么"的拓扑排序时只认这些边。
TEMPORAL_RELATIONS = {"prerequisite", "successor"}


def _load_graph() -> dict:
    """加载知识图谱，不存在则返回空结构。

    对旧版本/手工编辑过的文件补齐缺失键，
    避免 add_note_to_graph 硬取 clusters/keyword_index 时 KeyError。
    """
    graph = {"nodes": {}, "edges": [], "clusters": {}, "keyword_index": {}}
    if os.path.exists(GRAPH_FILE):
        try:
            with open(GRAPH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
                # 各键同时校验类型：手工编辑产生的错型数据会在后续 .get()/遍历处崩溃
                for key, expected_type in (("nodes", dict), ("edges", list),
                                           ("clusters", dict), ("keyword_index", dict)):
                    value = data.get(key)
                    if isinstance(value, expected_type):
                        graph[key] = value
        except (json.JSONDecodeError, IOError) as exc:
            # 文件损坏/不可读时先留档，避免下一次 _save_graph 用空结构覆盖掉全部数据
            try:
                if os.path.exists(GRAPH_FILE):
                    backup = GRAPH_FILE + ".corrupt"
                    with open(GRAPH_FILE, "rb") as src, open(backup, "wb") as dst:
                        dst.write(src.read())
                    logger.warning("Knowledge graph file unreadable (%s); backed up to %s", exc, backup)
            except OSError:
                logger.warning("Knowledge graph file unreadable and backup failed: %s", exc)
    return graph


def _save_graph(graph: dict):
    """原子写入知识图谱。"""
    _atomic_write_json(GRAPH_FILE, graph)


def _make_node_id(label: str) -> str:
    """为知识点生成稳定的 node_id。"""
    normalized = re.sub(r"\s+", "", label.strip().lower())[:32]
    return f"knode_{normalized}"


def _normalize_keyword(kw: str) -> str:
    """标准化关键词用于索引查找。"""
    return re.sub(r"\s+", "", kw.strip().lower())


def _get_existing_concepts_context() -> str:
    """获取已有概念列表作为 AI 提取的上下文。"""
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    if not nodes:
        return ""
    sorted_nodes = sorted(nodes.items(), key=lambda x: x[1].get("weight", 1), reverse=True)[:30]
    lines = []
    for nid, node in sorted_nodes:
        label = node.get("label", "")
        ntype = node.get("type", "")
        desc = node.get("description", "")[:50]
        lines.append(f"  - {nid}: {label}（{ntype}）{desc}")
    return "\n\n已有知识图谱概念（仅供参考，尽量与已有概念建立关联）：\n" + "\n".join(lines)


# ── AI 提取 ──

async def extract_knowledge_from_text(text: str, subject_hint: str = "") -> dict:
    """从文本中 AI 提取知识点和关系。

    会参考已有知识图谱中的概念，帮助 AI 建立新旧知识之间的关联。

    返回:
    {
        "concepts": [
            {"label": "阅微草堂笔记", "type": "work", "aliases": ["阅微草堂"],
             "description": "清代纪晓岚创作的笔记小说集", "merge_with": "existing_node_id"}
        ],
        "relations": [
            {"from": "阅微草堂笔记", "to": "纪晓岚", "relation": "created_by"},
            {"from": "阅微草堂笔记", "to": "清代笔记小说", "relation": "belongs_to"}
        ]
    }
    """
    from services.ai_service import ai_service

    truncated = text[:6000]
    if len(text) > 6000:
        logger.warning("Knowledge extraction input truncated from %d to 6000 chars", len(text))

    # 加载已有概念作为上下文
    existing_context = _get_existing_concepts_context()

    system_prompt = """你是一个知识图谱构建专家。请从用户提供的文本中提取核心知识点和它们之间的关系。""" + ("""已有知识图谱中的概念（请尽量与已有概念建立关联，如果新知识点与已有概念相同或高度相关，请在 merge_with 中标注已有概念的 node_id）：""") + existing_context + """

输出纯 JSON（不要 markdown 包裹），格式：
{
  "concepts": [
    {
      "label": "知识点显示名称",
      "type": "类型（concept/人物person/作品work/时代era/术语term/事件event/地点location）",
      "aliases": ["别名1", "别名2"],
      "description": "一句话描述",
      "merge_with": "如果与已有概念重复/高度相关，填写已有概念的node_id，否则为空字符串"
    }
  ],
  "relations": [
    {
      "from": "知识点名称（与concepts中label精确匹配）",
      "to": "知识点名称（与concepts中label精确匹配）",
      "relation": "关系类型"
    }
  ]
}

关系类型说明：
- related: 一般关联
- is_a: 属于（如"阅微草堂笔记 is_a 笔记小说"）
- part_of: 包含（如"清代 part_of 中国史"）
- created_by: 创作者（如"阅微草堂笔记 created_by 纪晓岚"）
- contemporary: 同时期（如"纪晓岚 contemporary 蒲松龄"）
- influenced: 影响
- belongs_to: 归属于（如"阅微草堂笔记 belongs_to 清代文学"）
- compared_with: 对比参照（如"阅微草堂笔记 compared_with 聊斋志异"）

规则：
- concepts 5-15 个，聚焦核心知识点，不要太多太碎
- 人物、作品、时代、概念要区分 type
- relations 要准确，只提取文本中明确提到的关系
- description 简洁，不超过 30 字
- 如果文本中提到的概念与已有概念相关，务必在 relations 中建立关系
- 只输出 JSON，不要其他文字"""

    user_msg = f"学科提示: {subject_hint}\n\n文本内容:\n{truncated}" if subject_hint else truncated

    try:
        # 统一走 ai_service：尊重配置的模型名/自定义 scope API，并享受统一的错误处理
        result = await ai_service.deepseek_json(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_msg}],
            temperature=0.3, max_tokens=4096, scope="knowledge_extract",
        )
        if not isinstance(result, dict):
            return {"concepts": [], "relations": []}
        concepts = result.get("concepts", [])
        relations = result.get("relations", [])
        # 校验格式
        valid_concepts = [c for c in (concepts or [])
                          if isinstance(c, dict) and c.get("label")]
        valid_relations = [r for r in (relations or [])
                           if isinstance(r, dict) and r.get("from") and r.get("to")]
        return {"concepts": valid_concepts, "relations": valid_relations}
    except Exception as e:
        logger.warning("Knowledge extraction failed: %s", e)
        return {"concepts": [], "relations": []}


# ── 图谱操作 ──

def add_note_to_graph(note_id: str, title: str, content: str,
                      knowledge_tags: list, extracted: dict) -> dict:
    """将 AI 提取的知识点合并到知识图谱中。

    返回变更摘要: {"nodes_added": 3, "edges_added": 2, "clusters_updated": 1}
    """
    graph = _load_graph()
    nodes = graph["nodes"]
    edges = graph["edges"]
    clusters = graph["clusters"]
    kw_index = graph["keyword_index"]

    concepts = extracted.get("concepts", [])
    relations = extracted.get("relations", [])
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    nodes_added = 0
    edges_added = 0
    merge_mappings = {}  # new_label -> existing_nid

    # 添加知识点节点
    for concept in concepts:
        label = concept.get("label", "").strip()
        if not label:
            continue
        nid = _make_node_id(label)
        node_type = concept.get("type", "concept")
        if node_type not in _NODE_TYPES:
            node_type = "concept"
        is_core = node_type in _CORE_TYPES
        aliases = concept.get("aliases", []) or []
        desc = (concept.get("description") or "")[:200]
        detail = (concept.get("detail") or concept.get("description") or "")[:2000]
        merge_with = concept.get("merge_with", "").strip()

        # 如果 AI 标注了 merge_with 且目标节点存在，则合并到已有节点
        if merge_with and merge_with in nodes:
            merge_mappings[label] = merge_with
            nodes[merge_with]["weight"] = nodes[merge_with].get("weight", 1) + 1
            if note_id and note_id not in nodes[merge_with].get("source_notes", []):
                nodes[merge_with].setdefault("source_notes", []).append(note_id)
            for alias in aliases:
                if alias not in nodes[merge_with].get("aliases", []):
                    nodes[merge_with].setdefault("aliases", []).append(alias)
            # 更新 detail（追加新描述）
            if detail and detail != nodes[merge_with].get("description", ""):
                existing_detail = nodes[merge_with].get("detail", "")
                if detail not in existing_detail:
                    nodes[merge_with]["detail"] = (existing_detail + "\n" + detail)[:2000]
            # 保存笔记片段
            if note_id:
                snippet = content[:200] if content else ""
                if snippet:
                    nodes[merge_with].setdefault("note_snippets", {})[note_id] = snippet
            for name in [label] + aliases:
                kw = _normalize_keyword(name)
                if kw:
                    if kw not in kw_index:
                        kw_index[kw] = []
                    if merge_with not in kw_index[kw]:
                        kw_index[kw].append(merge_with)
            continue

        if nid in nodes:
            nodes[nid]["weight"] = nodes[nid].get("weight", 1) + 1
            if note_id and note_id not in nodes[nid].get("source_notes", []):
                nodes[nid].setdefault("source_notes", []).append(note_id)
            for alias in aliases:
                if alias not in nodes[nid].get("aliases", []):
                    nodes[nid].setdefault("aliases", []).append(alias)
            # 更新 detail
            if detail and detail != nodes[nid].get("description", ""):
                existing_detail = nodes[nid].get("detail", "")
                if detail not in existing_detail:
                    nodes[nid]["detail"] = (existing_detail + "\n" + detail)[:2000]
            # 保存笔记片段
            if note_id:
                snippet = content[:200] if content else ""
                if snippet:
                    nodes[nid].setdefault("note_snippets", {})[note_id] = snippet
        else:
            nodes[nid] = {
                "label": label,
                "type": node_type,
                "core": is_core,
                "aliases": aliases,
                "description": desc,
                "detail": detail,
                "source_notes": [note_id] if note_id else [],
                "note_snippets": {note_id: content[:200]} if note_id and content else {},
                "weight": 1,
                "created_at": now,
            }
            nodes_added += 1

        all_names = [label] + aliases
        for name in all_names:
            kw = _normalize_keyword(name)
            if kw not in kw_index:
                kw_index[kw] = []
            if nid not in kw_index[kw]:
                kw_index[kw].append(nid)

    # 添加关系边
    for rel in relations:
        from_label = rel.get("from", "").strip()
        to_label = rel.get("to", "").strip()
        relation_type = rel.get("relation", "related")
        if not from_label or not to_label:
            continue
        # 解析节点 ID（优先使用 merge_mapping，否则按 label 查找）
        from_id = merge_mappings.get(from_label, _make_node_id(from_label))
        to_id = merge_mappings.get(to_label, _make_node_id(to_label))
        # 如果计算的 ID 不在 nodes 中，尝试通过关键词索引查找
        if from_id not in nodes:
            kw = _normalize_keyword(from_label)
            candidates = kw_index.get(kw, [])
            if candidates:
                from_id = candidates[0]
        if to_id not in nodes:
            kw = _normalize_keyword(to_label)
            candidates = kw_index.get(kw, [])
            if candidates:
                to_id = candidates[0]
        if from_id not in nodes or to_id not in nodes:
            continue

        # 检查是否已存在相同关系
        # 去重键必须**带上 relation**。原先只比 (from,to)，于是同一对节点之间的
        # 第二种关系会被静默丢掉（体重 +1 了，关系却没增加）——
        # 例如先写入「A 包含 B」，再写「A 与 B 相关」，后者直接消失。
        exists = False
        for edge in edges:
            if (edge.get("from") == from_id and edge.get("to") == to_id
                    and edge.get("relation", "related") == relation_type):
                edge["weight"] = edge.get("weight", 1) + 1
                exists = True
                break
        if not exists:
            edges.append({
                "from": from_id,
                "to": to_id,
                "relation": relation_type,
                "weight": 1,
            })
            edges_added += 1

    # 先重建知识簇（基于并查集的连通分量），让新节点拿到 cluster 字段，
    # 间接关系推断才能按“同簇”找到邻居
    _rebuild_clusters(graph)

    # 推断间接关系：同一簇内的新节点与邻近节点建立弱关联
    _infer_indirect_relations(graph, merge_mappings, concepts)

    _save_graph(graph)
    return {
        "nodes_added": nodes_added,
        "edges_added": edges_added,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "total_clusters": len(clusters),
    }


def _infer_indirect_relations(graph: dict, merge_mappings: dict, concepts: list):
    """推断间接关系：新加入的知识点与同一簇内已有知识点建立弱关联。

    例如：新知识点 A 连接到簇中的 B，而 B 已连接到 C，
    则自动推断 A 与 C 的间接关系（related，权重较低）。
    """
    nodes = graph["nodes"]
    edges = graph["edges"]
    new_labels = {c.get("label", "").strip() for c in concepts if c.get("label")}
    new_ids = set()
    for label in new_labels:
        nid = merge_mappings.get(label, _make_node_id(label))
        if nid in nodes:
            new_ids.add(nid)

    if not new_ids:
        return

    # 收集每个新节点所在的簇
    for nid in new_ids:
        cluster_id = nodes[nid].get("cluster")
        if not cluster_id:
            continue
        cluster_nodes = graph.get("clusters", {}).get(cluster_id, {}).get("nodes", [])
        # 与同簇中权重最高的 3 个节点建立弱关联
        related = sorted(
            [n for n in cluster_nodes if n != nid and n in nodes],
            key=lambda x: nodes[x].get("weight", 1),
            reverse=True,
        )[:3]
        for rid in related:
            # 检查是否已有直接连接
            already_connected = False
            for edge in edges:
                if (edge.get("from") == nid and edge.get("to") == rid) or \
                   (edge.get("from") == rid and edge.get("to") == nid):
                    already_connected = True
                    break
            if not already_connected:
                edges.append({
                    "from": nid,
                    "to": rid,
                    "relation": "related",
                    "weight": 1,
                })


def _rebuild_clusters(graph: dict):
    """基于并查集重建知识簇。"""
    nodes = graph["nodes"]
    edges = graph["edges"]

    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # 初始化并查集
    for nid in nodes:
        parent[nid] = nid

    # 合并有关系的节点
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f in nodes and t in nodes:
            union(f, t)

    # 收集簇
    clusters_raw = defaultdict(list)
    for nid in nodes:
        root = find(nid)
        clusters_raw[root].append(nid)

    # 构建簇信息
    clusters = {}
    for root, members in clusters_raw.items():
        # 找核心节点中权重最高的作为簇标签，否则取权重最高的节点
        core_members = [n for n in members if nodes[n].get("core")]
        if core_members:
            best = max(core_members, key=lambda n: nodes[n].get("weight", 1))
        else:
            best = max(members, key=lambda n: nodes[n].get("weight", 1))
        types = set(nodes[n].get("type", "concept") for n in members)
        # 用 root 的哈希做簇 ID：root 常含长中文前缀，直接截断会碰撞互相覆盖
        cluster_id = f"cluster_{hashlib.sha1(root.encode('utf-8')).hexdigest()[:16]}"
        clusters[cluster_id] = {
            "label": nodes[best]["label"],
            "nodes": members,
            "core_nodes": core_members,
            "size": len(members),
            "types": list(types),
        }
        # 更新节点的 cluster 字段
        for nid in members:
            nodes[nid]["cluster"] = cluster_id

    graph["clusters"] = clusters


# ── 查询 ──

def merge_curriculum(nodes: list, edges: list, *, tag: str = "curriculum") -> dict:
    """把一份**课程体系**（知识点 + 前置边）合并进知识图谱。

    与 `add_note_to_graph` 的区别（都是有意为之，不是重复实现）：
    - 节点带 `subject/grade/module/band` 等体系元数据，便于按学科/年级/难度筛；
    - 边**按 (from,to,relation) 去重**，不会把两条不同关系合成一条；
    - **不跑** `_infer_indirect_relations`：体系边是精确的教学依赖，不是"同簇推断"，
      混进推断关系会把"先补什么"的顺序搞脏；
    - `source_notes` 里记 `tag`，方便日后整体撤回（`remove_curriculum(tag)`）。

    幂等：重复调用只会更新节点属性、不会重复加边。
    返回 {"nodes_added","nodes_updated","edges_added","edges_skipped"}。
    """
    graph = _load_graph()
    g_nodes = graph["nodes"]
    g_edges = graph["edges"]
    kw_index = graph["keyword_index"]

    stats = {"nodes_added": 0, "nodes_updated": 0, "edges_added": 0, "edges_skipped": 0}
    label_to_id: dict = {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 1) 节点 ----
    for item in nodes:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        nid = _make_node_id(label)
        label_to_id[label] = nid
        if nid in g_nodes:
            node = g_nodes[nid]
            stats["nodes_updated"] += 1
        else:
            node = {
                "id": nid, "label": label, "type": "concept",
                "description": str(item.get("description") or "")[:200],
                "detail": "", "aliases": [], "weight": 1,
                "source_notes": [tag] if tag else [],
                "created_at": now, "updated_at": now,
            }
            g_nodes[nid] = node
            stats["nodes_added"] += 1
        # 体系元数据总是覆盖为最新（便于改版后重灌）
        for key in ("subject", "grade", "module", "band"):
            if item.get(key):
                node[key] = item[key]
        node["is_curriculum"] = True
        node["updated_at"] = now
        if tag and tag not in (node.get("source_notes") or []):
            node.setdefault("source_notes", []).append(tag)
        for name in [label] + list(item.get("aliases") or []):
            kw = _normalize_keyword(name)
            kw_index.setdefault(kw, [])
            if nid not in kw_index[kw]:
                kw_index[kw].append(nid)

    # ---- 2) 边 ----
    # 先把所有节点登记完再连边，避免"先出现的边指向后出现的节点"这种顺序依赖
    for rel in edges:
        from_id = label_to_id.get(str(rel.get("from") or "").strip())
        to_id = label_to_id.get(str(rel.get("to") or "").strip())
        if not from_id or not to_id:
            # 端点不在本次体系里：跳过并计数，由调用方决定是否当成错误
            stats["edges_skipped"] += 1
            continue
        relation = str(rel.get("relation") or "related")
        if relation not in _RELATION_TYPES:
            stats["edges_skipped"] += 1
            continue
        dup = False
        for edge in g_edges:
            if (edge.get("from") == from_id and edge.get("to") == to_id
                    and edge.get("relation", "related") == relation):
                dup = True
                break
        if dup:
            stats["edges_skipped"] += 1
            continue
        g_edges.append({"from": from_id, "to": to_id, "relation": relation,
                        "weight": 1, "source": tag, "created_at": now})
        stats["edges_added"] += 1

    _rebuild_clusters(graph)
    _save_graph(graph)
    return stats


def remove_curriculum(tag: str = "curriculum") -> dict:
    """撤回某次体系灌入：删掉带该 tag 的边，以及只属于该 tag 的节点。"""
    graph = _load_graph()
    before_nodes = len(graph["nodes"])
    before_edges = len(graph["edges"])
    graph["edges"] = [e for e in graph["edges"] if e.get("source") != tag]
    keep = {}
    for nid, node in graph["nodes"].items():
        srcs = [s for s in (node.get("source_notes") or []) if s != tag]
        if node.get("is_curriculum") and not srcs:
            continue          # 只挂在这个 tag 上的体系节点，一并删掉
        node["source_notes"] = srcs
        keep[nid] = node
    graph["nodes"] = keep
    # 关键词索引里同步剔除已删节点
    for kw, ids in list(graph["keyword_index"].items()):
        alive = [i for i in ids if i in keep]
        if alive:
            graph["keyword_index"][kw] = alive
        else:
            graph["keyword_index"].pop(kw, None)
    _rebuild_clusters(graph)
    _save_graph(graph)
    return {"nodes_removed": before_nodes - len(graph["nodes"]),
            "edges_removed": before_edges - len(graph["edges"])}


def find_node_by_label(label: str) -> dict:
    """按 label / 别名找节点。找不到返回 {}（调用方据此给"没有"而不是编一个）。"""
    graph = _load_graph()
    key = _normalize_keyword(label)
    for nid in graph["keyword_index"].get(key, []):
        node = graph["nodes"].get(nid)
        if node:
            return {**node, "id": nid}
    return {}


def get_prerequisites(node_id: str, depth: int = 5) -> dict:
    """顺着 `prerequisite` 边回溯，给出"要掌握 X 之前应先掌握什么"。

    返回 `{"target":..., "levels":[[...], [...], ...], "unresolved":[...]}`：
    - `levels[0]` 是直接前置，`levels[1]` 是前置的前置，以此类推；
    - 去重后同一节点只出现在**最早**的那一层（最短距离），避免同一知识点重复出现在多层；
    - 环会在去重中被自然截断（已访问过的不再展开）。

    **诚实性要求**：图里没有前置边的节点，返回的 `levels` 就是空的 ——
    调用方必须如实说"没有记录前置关系"，不能自己编一个学习顺序出来。
    """
    graph = _load_graph()
    nodes = graph["nodes"]
    if node_id not in nodes:
        return {"target": node_id, "found": False, "levels": [], "unresolved": [],
                "note": "节点不在图谱中"}

    # 反向索引：to -> [from...]（只认 prerequisite 方向）
    incoming: dict = {}
    for e in graph["edges"]:
        if e.get("relation") != "prerequisite":
            continue
        incoming.setdefault(e.get("to"), []).append(e.get("from"))

    seen = {node_id}
    levels = []
    frontier = [node_id]
    for _ in range(max(1, int(depth or 1))):
        nxt = []
        for cur in frontier:
            for pre in incoming.get(cur, []):
                if pre in seen or pre not in nodes:
                    continue
                seen.add(pre)
                nxt.append(pre)
        if not nxt:
            break
        levels.append([{"id": n, "label": nodes[n].get("label", ""),
                        "subject": nodes[n].get("subject", ""),
                        "grade": nodes[n].get("grade", "")} for n in nxt])
        frontier = nxt

    return {
        "target": {"id": node_id, "label": nodes[node_id].get("label", "")},
        "found": True,
        "levels": levels,
        "total_prerequisites": sum(len(l) for l in levels),
        "has_prerequisite_edges": bool(incoming.get(node_id)),
    }


def search_graph(query: str, limit: int = 10) -> list:
    """搜索知识图谱中的节点，返回匹配的节点列表。"""
    graph = _load_graph()
    nodes = graph["nodes"]
    kw_index = graph["keyword_index"]

    q = _normalize_keyword(query)
    matched_ids = set()

    # 精确匹配
    if q in kw_index:
        matched_ids.update(kw_index[q])

    # 模糊匹配
    for kw, ids in kw_index.items():
        if q in kw or kw in q:
            matched_ids.update(ids)

    results = []
    for nid in matched_ids:
        if nid in nodes:
            node = nodes[nid].copy()
            node["id"] = nid
            results.append(node)

    results.sort(key=lambda x: x.get("weight", 1), reverse=True)
    return results[:limit]


def get_related_nodes(node_id: str, depth: int = 1) -> dict:
    """获取与指定节点关联的知识网络。

    返回:
    {
        "center": {...},
        "nodes": [...],
        "edges": [...],
        "cluster": {...}
    }
    """
    graph = _load_graph()
    nodes = graph["nodes"]
    edges = graph["edges"]

    if node_id not in nodes:
        return {"center": None, "nodes": [], "edges": [], "cluster": None}

    center = nodes[node_id].copy()
    center["id"] = node_id

    # BFS 查找关联节点
    visited = {node_id}
    current_level = {node_id}
    related_edges = []

    for _ in range(depth):
        next_level = set()
        for edge in edges:
            f, t = edge.get("from"), edge.get("to")
            if f in current_level and t not in visited:
                visited.add(t)
                next_level.add(t)
                related_edges.append(edge)
            elif t in current_level and f not in visited:
                visited.add(f)
                next_level.add(f)
                related_edges.append(edge)
        current_level = next_level
        if not current_level:
            break

    # 也添加同簇的其他节点
    cluster_id = nodes[node_id].get("cluster")
    if cluster_id and cluster_id in graph.get("clusters", {}):
        for nid in graph["clusters"][cluster_id].get("nodes", []):
            if nid not in visited:
                visited.add(nid)

    result_nodes = []
    for nid in visited:
        if nid in nodes:
            n = nodes[nid].copy()
            n["id"] = nid
            result_nodes.append(n)

    # 收集所有相关边
    result_edges = []
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f in visited and t in visited:
            result_edges.append(edge)

    cluster_info = None
    if cluster_id and cluster_id in graph.get("clusters", {}):
        cluster_info = graph["clusters"][cluster_id].copy()

    return {
        "center": center,
        "nodes": result_nodes,
        "edges": result_edges,
        "cluster": cluster_info,
    }


def get_cluster_list() -> list:
    """获取所有知识簇摘要。"""
    graph = _load_graph()
    clusters = graph.get("clusters", {})
    result = []
    for cid, info in clusters.items():
        result.append({
            "id": cid,
            "label": info.get("label", ""),
            "size": info.get("size", 0),
            "types": info.get("types", []),
        })
    result.sort(key=lambda x: x["size"], reverse=True)
    return result


def get_graph_stats() -> dict:
    """获取知识图谱统计信息。"""
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    core_count = sum(1 for n in nodes.values() if n.get("core"))
    return {
        "total_nodes": len(nodes),
        "total_edges": len(graph.get("edges", [])),
        "total_clusters": len(graph.get("clusters", {})),
        "total_keywords": len(graph.get("keyword_index", {})),
        "core_nodes": core_count,
        "support_nodes": len(nodes) - core_count,
    }


async def get_node_detail(node_id: str) -> dict:
    """获取知识点详情，包含关联笔记信息。"""
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    if node_id not in nodes:
        return {"found": False}

    node = nodes[node_id].copy()
    node["id"] = node_id

    # 获取关联笔记的详细信息（异步获取；单条失败仅跳过该条）
    note_details = []
    for nid in node.get("source_notes", []):
        try:
            result = await _fetch_note_summary(nid)
            if result:
                note_details.append(result)
        except Exception:
            pass

    node["source_note_details"] = note_details

    # 获取关联节点
    related = []
    for edge in graph.get("edges", []):
        f, t = edge.get("from"), edge.get("to")
        other_id = None
        if f == node_id:
            other_id = t
        elif t == node_id:
            other_id = f
        if other_id and other_id in nodes:
            rn = nodes[other_id].copy()
            rn["id"] = other_id
            rn["relation"] = edge.get("relation", "related")
            related.append(rn)

    # 按 core 优先、weight 降序排列
    related.sort(key=lambda x: (x.get("core", False), x.get("weight", 1)), reverse=True)
    node["related_nodes"] = related
    node["found"] = True
    return node


async def _fetch_note_summary(note_id: str) -> dict:
    """异步获取笔记摘要。"""
    from models.database import async_session
    from models.models import Note
    from sqlalchemy import select
    async with async_session() as db:
        note = await db.get(Note, note_id)
        if not note:
            return None
        return {
            "id": note.id,
            "title": note.title or "未命名",
            "subject": note.subject or "",
            "grade": note.grade or "",
            "knowledge_tags": note.knowledge_tags or [],
            "excerpt": (note.content or "")[:200],
        }


def refresh_note_snippet(note_id: str, content: str):
    """笔记正文修改后同步刷新图谱中该笔记的摘要片段（不做 AI 重提取）。

    只更新 note_snippets；source_notes 与权重保持不变。
    """
    if not note_id:
        return
    graph = _load_graph()
    nodes = graph.get("nodes", {})
    snippet = (content or "")[:200]
    changed = False
    for node in nodes.values():
        snippets = node.get("note_snippets")
        if not isinstance(snippets, dict) or note_id not in snippets:
            continue
        if snippet:
            if snippets[note_id] != snippet:
                snippets[note_id] = snippet
                changed = True
        else:
            snippets.pop(note_id, None)
            changed = True
    if changed:
        _save_graph(graph)


def remove_note_from_graph(note_id: str):
    """从知识图谱中移除某个笔记的贡献（笔记删除时调用）。"""
    graph = _load_graph()
    nodes = graph["nodes"]

    changed = False
    for nid in list(nodes.keys()):
        source_notes = nodes[nid].get("source_notes", [])
        if note_id in source_notes:
            source_notes.remove(note_id)
            if not source_notes:
                # 没有来源了，删除节点
                del nodes[nid]
                changed = True
            else:
                nodes[nid]["source_notes"] = source_notes
                nodes[nid]["weight"] = max(1, nodes[nid].get("weight", 1) - 1)
                # 同步移除该笔记的摘要片段，避免详情页展示已删笔记的内容
                snippets = nodes[nid].get("note_snippets")
                if isinstance(snippets, dict):
                    snippets.pop(note_id, None)

    if changed:
        # 清理边和索引
        graph["edges"] = [e for e in graph.get("edges", [])
                          if e.get("from") in nodes and e.get("to") in nodes]
        graph["keyword_index"] = {}
        for nid, node in nodes.items():
            # 用集合归一化，别名与标签规范化相同时不会重复追加同一节点
            names = {_normalize_keyword(name)
                     for name in [node.get("label", "")] + list(node.get("aliases", []) or [])}
            names.discard("")
            for kw in names:
                bucket = graph["keyword_index"].setdefault(kw, [])
                if nid not in bucket:
                    bucket.append(nid)
        _rebuild_clusters(graph)

    _save_graph(graph)
