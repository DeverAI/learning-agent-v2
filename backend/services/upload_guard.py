# -*- coding: utf-8 -*-
"""上传体大小护栏：分块读 + 累计上限。

来源：`FUTURE.md`「优化方向」登记项 ——
「上传图片『先全量 read 后验大小』改分块读累计（ocr/correction/search 三处同款），
降低恶意超大上传的内存峰值」。

问题：三处原本都是 `raw = await file.read()` 先把整个请求体读进内存，再判
`len(raw) > MAX`。**判定只是事后拒绝，内存峰值已经发生**——一个 500MB 的
上传体在判定之前就把内存吃满，单用户本地服务会被直接打死。

改法：分块读、边读边累计，一旦超限立刻 413 中止。内存峰值封顶在
`CHUNK_SIZE + max_size`，与上传体实际大小无关。

三处共用本模块，避免"同一规则写三份"（教训见 FreqErr `[敏感字段判据不一致]`）。
"""
from fastapi import HTTPException

# 256KB：足够摊平 async read 的调度开销，又不会在小文件上浪费分配
CHUNK_SIZE = 256 * 1024


async def read_upload_limited(file, max_size: int, *, too_large_detail: str,
                              empty_detail: str = "上传内容为空",
                              require_nonempty: bool = True) -> bytes:
    """分块读取上传文件并校验总量。

    :param file: FastAPI `UploadFile`
    :param max_size: 允许的最大字节数，超过即 413
    :param too_large_detail: 超限时的用户可见文案（各处既有文案不同，保持原样）
    :param empty_detail: 内容为空时的 400 文案
    :param require_nonempty: 是否把"空内容"视为错误
    """
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            # 立即中止：不再继续读，也不把已读分块拼成大对象
            raise HTTPException(413, detail=too_large_detail)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if require_nonempty and not raw:
        raise HTTPException(400, detail=empty_detail)
    return raw
