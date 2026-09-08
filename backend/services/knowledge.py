import os
import json
import re
from services.ai_service import ai_service
from logger import get_logger, log_error
from config import STORAGE_DIR, _atomic_write_json

logger = get_logger()

# 知识库文档目录必须从 config.STORAGE_DIR 派生（测试隔离的唯一来源），
# 不允许用 __file__ 自行推导数据目录，否则端点测试会污染真实数据。
def _knowledge_dir() -> str:
    return os.path.join(STORAGE_DIR, "knowledge")


_DOC_ID_RE = re.compile(r"^[\w\u4e00-\u9fff-]{1,80}$")


def _ensure_dir():
    os.makedirs(_knowledge_dir(), exist_ok=True)


def _slugify(text: str) -> str:
    slug = re.sub(r'[^\w\u4e00-\u9fff]+', '_', text).strip('_')
    if not slug:
        import hashlib
        slug = "doc_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return slug[:60]


def _doc_path(doc_id: str) -> str:
    if not isinstance(doc_id, str) or not _DOC_ID_RE.fullmatch(doc_id):
        raise ValueError("知识库文档 ID 无效")
    root = os.path.realpath(_knowledge_dir())
    path = os.path.realpath(os.path.join(root, f"{doc_id}.json"))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("知识库文档 ID 无效")
    return path


def search_local(query: str) -> dict:
    """搜索本地知识库。返回 {'found': bool, 'content': str, 'source': str}"""
    _ensure_dir()
    kdir = _knowledge_dir()
    if not os.path.exists(kdir):
        return {"found": False, "content": "", "source": ""}

    query_lower = query.lower()
    best_score = 0
    best = {"found": False, "content": "", "source": ""}

    for filename in os.listdir(kdir):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(kdir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                logger.warning("Knowledge doc %s is not an object, skipped", filename)
                continue
            keywords = doc.get("keywords", [])
            title = doc.get("title", "")
            content = doc.get("content", "")
            if not isinstance(keywords, list):
                keywords = []
            title = str(title or "").lower()
            content = str(content or "").lower()
            score = 0

            for kw in keywords:
                if not isinstance(kw, str):
                    continue
                if kw.lower() in query_lower or query_lower in kw.lower():
                    score += 3
            if title and (title in query_lower or query_lower in title):
                score += 5

            for word in query_lower.split():
                if word in content:
                    score += 1

            if score > best_score:
                best_score = score
                best = {"found": True, "content": str(doc.get("content", "") or ""),
                        "source": filename.replace(".json", "")}
        except Exception as e:
            logger.warning("Failed to parse knowledge doc %s: %s", filename, e)
            continue

    return best


async def search_and_enrich(query: str) -> str:
    local = search_local(query)
    if local["found"] and local["content"]:
        logger.info("Knowledge base local hit: %s", local['source'])
        return local["content"]

    logger.info("Knowledge base miss, searching GLM: %.80s...", query)
    try:
        result = await ai_service.zhipuai_search(query)
        if result:
            _save_doc(query, result)
            return result
    except Exception as e:
        log_error("knowledge", f"GLM search failed: {e}")

    return ""


def _save_doc(query: str, content: str):
    _ensure_dir()
    keywords = _extract_keywords(query, content)
    title = query.strip()[:80]
    slug = _slugify(title)
    doc = {
        "title": title,
        "keywords": keywords,
        "content": content,
    }
    kdir = _knowledge_dir()
    path = os.path.join(kdir, f"{slug}.json")
    # 避免覆盖
    counter = 1
    while os.path.exists(path):
        path = os.path.join(kdir, f"{slug}_{counter}.json")
        counter += 1
    _atomic_write_json(path, doc)
    logger.info("Knowledge doc saved: %s", path)


def _extract_keywords(query: str, content: str) -> list:
    words = set()
    for w in re.findall(r'[\u4e00-\u9fff\w]{2,}', query):
        words.add(w)
    for w in re.findall(r'[\u4e00-\u9fff]{2,4}', content[:2000]):
        words.add(w)
    return list(words)[:20]


def list_docs() -> list[dict]:
    _ensure_dir()
    docs = []
    kdir = _knowledge_dir()
    if not os.path.exists(kdir):
        return docs
    for fn in sorted(os.listdir(kdir), reverse=True):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(kdir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            docs.append({
                "id": fn.replace(".json", ""),
                "title": d.get("title", ""),
                "keywords": d.get("keywords", [])[:8],
                "size": len(d.get("content", "")),
            })
        except Exception as e:
            logger.warning("Failed to read knowledge doc %s: %s", fn, e)
    return docs


def get_doc(doc_id: str) -> dict:
    try:
        path = _doc_path(doc_id)
    except ValueError:
        return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.warning("Failed to read knowledge doc %s: %s", doc_id, e)
        return {}


def delete_doc(doc_id: str) -> bool:
    try:
        path = _doc_path(doc_id)
    except ValueError:
        return False
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True
