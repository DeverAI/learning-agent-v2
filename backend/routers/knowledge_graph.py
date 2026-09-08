"""知识图谱 API：知识点提取、关系网络查询、知识簇浏览。"""

import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from logger import get_logger

logger = get_logger()

router = APIRouter(prefix="/api/knowledge-graph", tags=["knowledge-graph"])


class ExtractRequest(BaseModel):
    text: str = Field(min_length=10, max_length=50000)
    subject_hint: str = Field(default="", max_length=64)


class ExtractResponse(BaseModel):
    concepts: list
    relations: list


class AddNoteRequest(BaseModel):
    note_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=50000)
    knowledge_tags: list[str] = Field(default_factory=list)


@router.get("/stats")
async def graph_stats():
    """获取知识图谱统计。"""
    from services.knowledge_graph import get_graph_stats
    return get_graph_stats()


@router.get("/search")
async def search_knowledge(q: str = "", limit: int = 10):
    """搜索知识点。"""
    if not q or not q.strip():
        raise HTTPException(400, "查询不能为空")
    from services.knowledge_graph import search_graph
    return {"results": search_graph(q.strip(), limit=min(max(1, limit), 50))}


@router.get("/related/{node_id}")
async def related_network(node_id: str, depth: int = 1):
    """获取指定知识点的关联网络。"""
    from services.knowledge_graph import get_related_nodes
    result = get_related_nodes(node_id, depth=min(max(1, depth), 3))
    if not result.get("center"):
        raise HTTPException(404, "知识点不存在")
    return result


@router.get("/node/{node_id}")
async def node_detail(node_id: str):
    """获取知识点详情（含关联笔记）。"""
    from services.knowledge_graph import get_node_detail
    result = await get_node_detail(node_id)
    if not result.get("found"):
        raise HTTPException(404, "知识点不存在")
    return result
@router.get("/clusters")
async def cluster_list():
    """获取所有知识簇摘要。"""
    from services.knowledge_graph import get_cluster_list
    return {"clusters": get_cluster_list()}


@router.post("/extract")
async def extract_knowledge(req: ExtractRequest):
    """AI 从文本中提取知识点和关系（不保存，预览用）。"""
    from services.knowledge_graph import extract_knowledge_from_text
    result = await extract_knowledge_from_text(req.text, req.subject_hint)
    return result


@router.post("/add-note")
async def add_note_to_graph(req: AddNoteRequest):
    """将笔记内容提取并合并到知识图谱。"""
    from services.knowledge_graph import extract_knowledge_from_text, add_note_to_graph

    # 先提取
    extracted = await extract_knowledge_from_text(req.content, "")
    if not extracted.get("concepts"):
        raise HTTPException(400, "未能从笔记中提取到有效知识点")

    # 合并到图谱
    summary = add_note_to_graph(
        note_id=req.note_id,
        title=req.title,
        content=req.content,
        knowledge_tags=req.knowledge_tags,
        extracted=extracted,
    )
    return {"status": "ok", "extracted": extracted, "summary": summary}


@router.delete("/note/{note_id}")
async def remove_note(note_id: str):
    """从知识图谱中移除笔记的贡献。"""
    from services.knowledge_graph import remove_note_from_graph
    remove_note_from_graph(note_id)
    return {"status": "ok"}


@router.delete("/clear")
async def clear_graph():
    """清空知识图谱（危险操作）。"""
    from services.knowledge_graph import GRAPH_FILE
    import os
    if os.path.exists(GRAPH_FILE):
        os.remove(GRAPH_FILE)
    return {"status": "cleared"}
