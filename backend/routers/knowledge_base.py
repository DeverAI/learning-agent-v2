from fastapi import APIRouter, HTTPException, Query
from services.knowledge import search_local, list_docs, get_doc, delete_doc, search_and_enrich

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("")
async def list_knowledge():
    return {"docs": list_docs()}


@router.get("/search")
async def search_knowledge(q: str = Query(default="", max_length=2_000)):
    if not q:
        return {"found": False, "content": ""}
    result = search_local(q)
    return result


@router.post("/enrich")
async def enrich_knowledge(q: str = Query(default="", max_length=2_000)):
    if not q:
        raise HTTPException(status_code=400, detail="query required")
    content = await search_and_enrich(q)
    if content:
        return {"found": True, "content": content}
    return {"found": False, "content": ""}


@router.get("/{doc_id}")
async def get_knowledge(doc_id: str):
    doc = get_doc(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="不存在")
    return doc


@router.delete("/{doc_id}")
async def delete_knowledge(doc_id: str):
    if not delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="不存在")
    return {"message": "已删除"}
