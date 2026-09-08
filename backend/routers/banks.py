from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, distinct, delete
from sqlalchemy.ext.asyncio import AsyncSession
from models.database import get_db
from models.models import Question
from config import QUESTIONS_DIR
import os, shutil, re
from logger import get_logger

logger = get_logger()
router = APIRouter(prefix="/api/banks", tags=["banks"])

_BANK_NAME_RE = re.compile(r"^[\w\-\u4e00-\u9fa5]{1,32}$")

def _validate_bank_name(name: str):
    if not isinstance(name, str) or not _BANK_NAME_RE.match(name):
        raise HTTPException(400, detail="题库名称只允许 1-32 位中文、字母、数字、下划线或中划线")


@router.get("")
async def list_banks(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(func.distinct(Question.bank)))
    banks = sorted([row[0] for row in r.fetchall() if row[0]])
    return {"banks": banks}


@router.get("/{bank_name}/tags")
async def bank_tags(bank_name: str, db: AsyncSession = Depends(get_db)):
    _validate_bank_name(bank_name)
    r = await db.execute(
        select(Question.knowledge_tags)
        .where(Question.bank == bank_name)
        .where(Question.status == "done")
    )
    tags = set()
    for row in r.fetchall():
        for t in (row[0] or []):
            tags.add(t)
    return {"tags": sorted(tags)}


@router.post("/{bank_name}/tags/add")
async def bank_add_tag(bank_name: str, tag: str = Query(..., min_length=1, max_length=100),
                        db: AsyncSession = Depends(get_db)):
    _validate_bank_name(bank_name)
    if not tag.strip():
        raise HTTPException(400, "标签不能为空")
    # 与 remove_tag 对称：给题库全部题目统一加标签，而不是只改第一道题
    r = await db.execute(
        select(Question).where(Question.bank == bank_name).where(Question.status == "done")
    )
    questions = r.scalars().all()
    if not questions:
        raise HTTPException(404, "该题库无题目，请先上传题目")
    count = 0
    for q in questions:
        tags = list(q.knowledge_tags or [])
        if tag not in tags:
            tags.append(tag)
            q.knowledge_tags = tags
            count += 1
    await db.commit()
    return {"message": f"标签「{tag}」已加入题库「{bank_name}」的 {count} 道题"}


@router.delete("/{bank_name}/tags/remove")
async def bank_remove_tag(bank_name: str, tag: str = Query(..., min_length=1, max_length=100),
                           db: AsyncSession = Depends(get_db)):
    _validate_bank_name(bank_name)
    r = await db.execute(
        select(Question).where(Question.bank == bank_name)
    )
    count = 0
    for q in r.scalars().all():
        if tag in (q.knowledge_tags or []):
            q.knowledge_tags = [t for t in q.knowledge_tags if t != tag]
            count += 1
    await db.commit()
    return {"message": f"已从 {count} 道题中移除标签「{tag}」"}


@router.post("/{bank_name}/rename")
async def rename_bank(bank_name: str, new_name: str = Query(..., min_length=1, max_length=32),
                       db: AsyncSession = Depends(get_db)):
    _validate_bank_name(bank_name)
    if not _BANK_NAME_RE.match(new_name.strip()):
        raise HTTPException(400, detail="新题库名称只允许 1-32 位中文、字母、数字、下划线或中划线")
    if not new_name.strip():
        raise HTTPException(400, "新名称不能为空")
    new_name = new_name.strip()
    if new_name == bank_name:
        return {"message": f"题库「{bank_name}」名称未变化", "count": 0}
    r = await db.execute(
        select(Question).where(Question.bank == bank_name)
    )
    questions = r.scalars().all()
    if not questions:
        raise HTTPException(404, "题库不存在或没有题目")
    # 目标名已存在时明确提示合并风险，避免静默合并两个题库
    dup = await db.execute(
        select(Question.id).where(Question.bank == new_name).limit(1)
    )
    merging = dup.first() is not None
    for q in questions:
        q.bank = new_name
    await db.commit()
    message = f"已将 {len(questions)} 道题移至「{new_name}」"
    if merging:
        message += "（目标题库已存在，两库已合并）"
    return {"message": message, "count": len(questions)}


@router.delete("/{bank_name}")
async def delete_bank(bank_name: str, db: AsyncSession = Depends(get_db)):
    _validate_bank_name(bank_name)
    if bank_name == "default":
        raise HTTPException(400, "不能删除默认题库")
    r = await db.execute(
        select(Question).where(Question.bank == bank_name)
    )
    count = 0
    for q in r.scalars().all():
        q.bank = "default"
        count += 1
    await db.commit()
    return {"message": f"已将 {count} 道题移至默认题库", "count": count}
