# -*- coding: utf-8 -*-
"""手欠用户模拟测试：并发双击、乱序操作、垃圾输入。

在子进程中以临时 STORAGE_DIR 启动应用（与 test_endpoint_contract 相同的隔离策略），
模拟真实用户乱点行为，断言任何情况下都不出现 500 或状态损坏。
"""
import os
import subprocess
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

_ABUSE_SCRIPT = r'''
import asyncio, json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
os.chdir(os.getcwd())

import config
tmp = tempfile.mkdtemp(prefix="lh_abuse_")
config.STORAGE_DIR = tmp
for name in ("questions", "papers", "corrections", "paper_configs",
             "sessions", "notes", "quotes", "task_states", "logs", "data"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
config.DATABASE_URL = "sqlite+aiosqlite:///" + os.path.join(tmp, "app.db")

import models.models
from models.database import init_db
asyncio.run(init_db())

import httpx
import main as app_main

AUTH = {"X-Auth-Token": "Ntmhzsgtc"}
failures = []


async def fire(client, method, url, extra_headers=None, **kw):
    headers = {**AUTH, **(extra_headers or {})}
    try:
        if method == "GET":
            return await client.get(url, headers=headers, **kw)
        if method == "POST":
            return await client.post(url, headers=headers, **kw)
        if method == "PUT":
            return await client.put(url, headers=headers, **kw)
        if method == "DELETE":
            return await client.delete(url, headers=headers, **kw)
    except Exception as exc:
        return exc
    raise ValueError(method)


def check(label, results, allowed=(200, 201, 400, 404, 409, 413, 422)):
    """并发结果里不允许 5xx 与传输层异常。"""
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            failures.append(f"{label}[{i}] transport error: {type(r).__name__}: {r}")
        elif r.status_code >= 500:
            failures.append(f"{label}[{i}] status={r.status_code} body={r.text[:200]}")
        elif r.status_code not in allowed:
            print(f"  note: {label}[{i}] unexpected-but-tolerated status {r.status_code}")


async def run():
    transport = httpx.ASGITransport(app=app_main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # ── 1. 双击创建笔记 ──
        rs = await asyncio.gather(*[
            fire(client, "POST", "/api/notes", json={"title": f"并发{i}", "content": "内容"})
            for i in range(8)
        ])
        check("POST /api/notes x8", rs)

        # ── 2. 并发上传会话 + 同一会话乱点 ──
        rs = await asyncio.gather(*[
            fire(client, "POST", "/api/ocr/session/create", data={"title": f"卷{i}"})
            for i in range(4)
        ])
        check("session/create x4", rs)
        sid = rs[0].json().get("id") if not isinstance(rs[0], Exception) else None
        if sid:
            # 空文件上传（缺 files 字段）
            rs = await asyncio.gather(*[
                fire(client, "POST", f"/api/ocr/session/{sid}/upload", data={"upload_mode": m})
                for m in ("one_per_image", "multi", "auto_split", "", "garbage_mode")
            ])
            check("session/upload missing files x5", rs)
            # 不存在的会话乱删乱传
            rs = await asyncio.gather(*[
                fire(client, "DELETE", f"/api/ocr/session/{sid}"),
                fire(client, "GET", f"/api/ocr/session/{sid}"),
                fire(client, "POST", "/api/ocr/session/notanid/upload"),
                fire(client, "GET", f"/api/ocr/session/{sid}/summary"),
            ])
            check("session misc chaos", rs)

        # ── 3. 笔记下载/编辑乱点（中文+emoji+注入标题）──
        weird_titles = ["emoji🎯笔记", "<script>alert(1)</script>", "a" * 300,
                        "../../etc/passwd", "\\r\\n注入", "正常数学笔记"]
        created_ids = []
        for t in weird_titles[:3]:
            r = await fire(client, "POST", "/api/notes", json={"title": t, "content": "x"})
            if not isinstance(r, Exception) and r.status_code == 200:
                created_ids.append(r.json().get("id"))
        for nid in created_ids:
            rs = await asyncio.gather(*[
                fire(client, "GET", f"/api/notes/{nid}/download"),
                fire(client, "GET", f"/api/notes/{nid}/download"),
                fire(client, "PUT", f"/api/notes/{nid}", json={"title": "改名", "sort_order": -99999}),
                fire(client, "PUT", f"/api/notes/{nid}", json={"sort_order": 10**9}),
            ])
            check(f"note {nid[:6]} download/edit chaos", rs)

        # ── 4. 垃圾 ID 全家桶 ──
        bad_ids = ["notanid", "%2E%2E%2Fsettings", "a" * 200, "..\\..\\settings.json",
                   "null", "0", "-1"]
        paths = [
            ("GET", "/api/questions/{i}"), ("GET", "/api/notes/{i}"),
            ("GET", "/api/papers/{i}"), ("GET", "/api/sessions/{i}"),
            ("DELETE", "/api/sessions/{i}"), ("GET", "/api/prompts/{i}"),
        ]
        rs = []
        labels = []
        for bid in bad_ids:
            for method, tpl in paths:
                rs.append(await fire(client, method, tpl.format(i=bid)))
                labels.append(f"{method} {tpl.format(i=bid)}")
        for lbl, r in zip(labels, rs):
            if isinstance(r, Exception):
                failures.append(f"{lbl}: transport {type(r).__name__}")
            elif r.status_code >= 500:
                failures.append(f"{lbl}: status={r.status_code}")

        # ── 5. 超长/畸形消息体 ──
        huge = "字" * 60000
        r = await fire(client, "POST", "/api/log-frontend-error",
                       json={"error": huge * 3, "stack": huge})
        if not isinstance(r, Exception) and r.status_code >= 500:
            failures.append(f"log-frontend-error huge: {r.status_code}")
        r = await fire(client, "POST", "/api/log-paper-error",
                       json={"action": "x" * 100, "paper_id": "../evil", "error": "e"})
        if not isinstance(r, Exception) and r.status_code >= 500:
            failures.append(f"log-paper-error evil path: {r.status_code}")

        # ── 6. focus 模式乱点（随机 ID 刷锁）──
        random_sids = [f"random-{n}" for n in range(30)]
        rs = []
        for s in random_sids:
            rs.append(await fire(client, "GET", f"/api/focus/sessions/{s}/state"))
        check("focus random ids x30", rs, allowed=(400, 404))
        rs = []
        for s in random_sids:
            rs.append(await fire(client, "POST", f"/api/focus/sessions/{s}/pause"))
        check("focus pause random ids x30", rs, allowed=(400, 404))

        # ── 7. 组卷参数乱填 ──
        garbage_configs = [
            {"question_count": -5},
            {"subject": "", "grade": None},
            {"knowledge_tags": "不是列表"},
            {},
        ]
        rs = []
        for gc in garbage_configs:
            rs.append(await fire(client, "POST", "/api/configs", json=gc))
        check("configs garbage", rs, allowed=(200, 201, 400, 422))

        # ── 8. 系统消息乱删 ──
        rs = await asyncio.gather(*[
            fire(client, "DELETE", "/api/system-messages/all"),
            fire(client, "DELETE", "/api/system-messages/999999"),
            fire(client, "DELETE", "/api/system-messages/%2E%2E%2Fetc"),
            fire(client, "DELETE", "/api/system-messages/-1"),
        ])
        check("system-messages delete chaos", rs)

        # ── 9. 首页组件布局乱存 ──
        rs = []
        for layout in ([{"widget": "x"}], [], [[[[1]]]], [{"widgets": object}]):
            try:
                payload = json.dumps(layout, default=str)
            except Exception:
                continue
            rs.append(await fire(client, "POST", "/api/home-widgets/layout",
                                 content=payload,
                                 extra_headers={"Content-Type": "application/json"}))
        check("home-widgets layout garbage", rs, allowed=(200, 201, 400, 422))

        # ── 10. 并发双击上传同一会话：question_ids 不得丢失更新 ──
        r = await fire(client, "POST", "/api/ocr/session/create",
                       data={"title": "并发上传一致性"})
        sid2 = r.json().get("id")
        png = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)

        def _files(n):
            return [("files", (f"img{n}.png", png, "image/png"))]

        results = await asyncio.gather(*[
            client.post(f"/api/ocr/session/{sid2}/upload", headers=AUTH,
                        files=_files(i)) for i in range(6)
        ])
        codes = [r.status_code for r in results]
        if any(c >= 500 for c in codes):
            failures.append(f"concurrent upload got 5xx: {codes}")
        # 查询会话汇总，确认 question_ids 数量与成功上传数一致（无丢失覆盖）
        r2 = await fire(client, "GET", f"/api/ocr/session/{sid2}")
        summary = r2.json() if not isinstance(r2, Exception) else {}
        qids = summary.get("session", {}).get("question_ids") or summary.get("question_ids") or []
        ok_uploads = sum(1 for c in codes if c == 200)
        if ok_uploads and len(qids) < ok_uploads:
            failures.append(
                f"concurrent upload lost updates: uploaded_ok={ok_uploads} "
                f"session_qids={len(qids)} ({qids})"
            )


asyncio.run(run())

if failures:
    print("ABUSE FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ABUSE OK")
'''


def test_abuse_simulation():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", _ABUSE_SCRIPT],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=BACKEND_DIR, timeout=300, env=env,
    )
    assert "ABUSE OK" in (result.stdout or ""), (
        f"exit={result.returncode}\nstdout={(result.stdout or '')[-1500:]}\nstderr={(result.stderr or '')[-2500:]}"
    )


if __name__ == "__main__":
    test_abuse_simulation()
    print("abuse simulation passed")
