"""R13：后台推理端生命周期的行为测试。

用**假处理器**注入，不调真实模型 —— 这样每条断言都是确定性的，
而且能精确构造"检查点之外长时间不返回"这种真实模型很难复现的情形。

钉住的四件事（都是踩过或差点踩到的）：
1. `spawn` 幂等，且**如实返回**是否真的启动了（不能假装续跑成功）；
2. 协作式取消在检查点生效，已产出的步骤保留；
3. **检查点之外卡住时，宽限期后必须硬杀** —— 否则用户点了取消毫无反应；
4. 服务重启后残留的 `running` 必须被对账成 `error`，
   否则前端永远显示"进行中"，用户等一个永远不结束的任务。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.database import init_db  # noqa: E402
from services import background_agent as BA  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    """确保 agent_tasks 表存在（用当前 ambient 引擎，不改配置，避免与别人打架）。"""
    asyncio.run(init_db())
    yield


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def test_create_task_and_spawn_idempotent(monkeypatch):
    async def fake(ckpt, task):
        await ckpt.tick("一步")
        await asyncio.sleep(0.2)
        return {"ok": True}

    BA.RUNNERS["__t_basic"] = fake
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_basic", title="基础", params={})
            assert t["status"] == "pending"
            assert t["cancellable"] is True
            assert t["resumable"] is False
            first = BA.spawn(t["task_id"])
            second = BA.spawn(t["task_id"])
            for _ in range(40):
                await asyncio.sleep(0.1)
                got = await BA.get_task(t["task_id"])
                if got["status"] in ("done", "error", "cancelled"):
                    break
            return first, second, got
        first, second, got = _run(main())
        assert first is True
        assert second is False, "已在跑还返回 True 会让调用方以为起了两个"
        assert got["status"] == "done"
        assert got["progress"] == 100.0
        assert got["output"] == {"ok": True}
    finally:
        BA.RUNNERS.pop("__t_basic", None)


def test_create_task_rejects_unknown_tool():
    async def main():
        with pytest.raises(ValueError):
            await BA.create_task(sid="s", tool="__nope", title="x", params={})
    _run(main())


def test_spawn_returns_false_for_resume_of_running_task(monkeypatch):
    """续跑时上一次还没停 -> 必须返回 False，让上层如实告诉用户，而不是假装成功。"""
    async def slow(ckpt, task):
        await ckpt.tick("开始")
        await asyncio.sleep(30)
        return {}

    BA.RUNNERS["__t_slow"] = slow
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_slow", title="慢", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.3)
            return BA.spawn(t["task_id"])
        assert _run(main()) is False
    finally:
        BA.RUNNERS.pop("__t_slow", None)


# --------------------------------------------------------------------------
# 协作式取消
# --------------------------------------------------------------------------

def test_checkpoint_tick_raises_on_cancel_flag(monkeypatch):
    async def fake(ckpt, task):
        await ckpt.tick("第一步")
        return {}

    BA.RUNNERS["__t_ck"] = fake
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_ck", title="检查点", params={})
            ck = BA.Checkpoint(t["task_id"])
            # 手动置取消标志，再 tick -> 必须抛 TaskCancelled
            from models.database import async_session
            from models.models import AgentTask
            async with async_session() as db:
                row = await db.get(AgentTask, t["task_id"])
                row.cancel_requested = True
                await db.commit()
            with pytest.raises(BA.TaskCancelled):
                await ck.tick("这一步不该被执行")
            return True
        assert _run(main()) is True
    finally:
        BA.RUNNERS.pop("__t_ck", None)


def test_cooperative_cancel_keeps_steps(monkeypatch):
    """在检查点被取消：状态 cancelled，且**已经记下的步骤不丢**。"""
    async def stepwise(ckpt, task):
        for i in range(50):
            await ckpt.tick(f"第 {i + 1} 步")
            await asyncio.sleep(0.05)
        return {}

    BA.RUNNERS["__t_coop"] = stepwise
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_coop", title="合作取消", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.4)
            await BA.request_cancel(t["task_id"])
            for _ in range(60):
                await asyncio.sleep(0.1)
                got = await BA.get_task(t["task_id"])
                if got["status"] == "cancelled":
                    return got
            return got
        got = _run(main())
        assert got["status"] == "cancelled"
        assert len(got["steps"]) >= 1, "取消把已完成的步骤也清掉了"
        assert got["resumable"] is True
    finally:
        BA.RUNNERS.pop("__t_coop", None)


# --------------------------------------------------------------------------
# 兜底硬取消：这是实测发现的缺陷，必须有回归
# --------------------------------------------------------------------------

def test_cancel_watchdog_hard_kills_stuck_task(monkeypatch):
    """模拟"一次长模型调用"——检查点之后长时间不返回。

    没有看门狗时：取消请求发出后任务会一直停在 running（实测卡了 120 秒以上），
    对用户就是"点了取消没反应"。
    """
    monkeypatch.setattr(BA, "CANCEL_GRACE_SECONDS", 1)

    async def stuck(ckpt, task):
        await ckpt.tick("开始调用模型")
        await asyncio.sleep(120)      # 检查点之外的长时间 I/O
        return {}

    BA.RUNNERS["__t_stuck"] = stuck
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_stuck", title="卡住", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.4)
            r = await BA.request_cancel(t["task_id"])
            assert r["ok"] is True
            assert r["status"] == "running"
            assert r["grace_seconds"] == 1
            assert "强制停止" in r["note"] or "停止" in r["note"]
            for _ in range(60):
                await asyncio.sleep(0.2)
                got = await BA.get_task(t["task_id"])
                if got["status"] == "cancelled":
                    return got
            return got
        got = _run(main())
        assert got["status"] == "cancelled", "卡住的任务没有被兜底停掉"
        assert "强制停止" in got["error"] or "硬取消" in got["error"]
    finally:
        BA.RUNNERS.pop("__t_stuck", None)


def test_cancel_pending_task_is_immediate(monkeypatch):
    """还没起跑的任务点取消应**立刻**变 cancelled，而不是等一个不存在的检查点。"""
    async def fake(ckpt, task):
        return {}

    BA.RUNNERS["__t_pending"] = fake
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_pending", title="未启动", params={})
            r = await BA.request_cancel(t["task_id"])
            return r, await BA.get_task(t["task_id"])
        r, got = _run(main())
        assert r["ok"] is True
        assert got["status"] == "cancelled"
    finally:
        BA.RUNNERS.pop("__t_pending", None)


# --------------------------------------------------------------------------
# 步骤状态不能谎报
# --------------------------------------------------------------------------

def test_step_status_is_running_while_in_flight(monkeypatch):
    """任务还在跑时，最后一步必须是 `running`。

    原先这里写死 `status: "done"`，于是界面会出现「写第 1 片 已完成 / 进度 75%」，
    而那片讲解词其实一个字都还没出来 —— 属"看起来做完了"的谎报。
    收尾时必须把最后一条 running 标成终态，否则任务结束了界面还挂着"进行中"。
    """
    async def two_steps(ckpt, task):
        await ckpt.tick("第一步")
        await ckpt.tick("第二步")
        await asyncio.sleep(0.8)
        return {}

    BA.RUNNERS["__t_step"] = two_steps
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_step", title="步骤", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.4)
            mid = await BA.get_task(t["task_id"])
            for _ in range(60):
                await asyncio.sleep(0.1)
                done = await BA.get_task(t["task_id"])
                if done["status"] in ("done", "error", "cancelled"):
                    break
            return mid, done
        mid, done = _run(main())
        assert len(mid["steps"]) >= 2, f"步骤没记全: {mid['steps']}"
        assert mid["steps"][-1]["status"] == "running", \
            f"在跑的最后一步被写成了 {mid['steps'][-1]['status']}"
        assert mid["steps"][-2]["status"] == "done", "上一步应该已经收尾为 done"
        assert done["status"] == "done"
        assert all(s["status"] == "done" for s in done["steps"]), \
            f"收尾没把 running 的步骤标终态: {done['steps']}"
    finally:
        BA.RUNNERS.pop("__t_step", None)


def test_finalize_marks_last_step_cancelled(monkeypatch):
    monkeypatch.setattr(BA, "CANCEL_GRACE_SECONDS", 1)

    async def stuck(ckpt, task):
        await ckpt.tick("开始调用模型")
        await asyncio.sleep(60)
        return {}

    BA.RUNNERS["__t_fin"] = stuck
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_fin", title="收尾", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.3)
            await BA.request_cancel(t["task_id"])
            for _ in range(60):
                await asyncio.sleep(0.2)
                got = await BA.get_task(t["task_id"])
                if got["status"] == "cancelled":
                    return got
            return got
        got = _run(main())
        assert got["status"] == "cancelled"
        assert all(s["status"] != "running" for s in got["steps"]), \
            f"取消后还有步骤挂着 running: {got['steps']}"
    finally:
        BA.RUNNERS.pop("__t_fin", None)

# --------------------------------------------------------------------------
# 重启对账
# --------------------------------------------------------------------------

def test_reconcile_marks_stale_running_as_error():
    async def main():
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            row = AgentTask(sid="s", tool="prepare_lesson", title="残留",
                            params={}, status="running", progress=37.0)
            db.add(row)
            await db.commit()
            tid = row.id
        n = await BA.reconcile_on_startup()
        got = await BA.get_task(tid)
        return n, got
    n, got = _run(main())
    assert n >= 1
    assert got["status"] == "error"
    assert "重启" in got["error"]
    assert got["resumable"] is True, "中断的任务应当可续跑"


# --------------------------------------------------------------------------
# 续跑
# --------------------------------------------------------------------------

def test_resume_rejects_done_task(monkeypatch):
    async def quick(ckpt, task):
        await ckpt.tick("一步")
        return {}

    BA.RUNNERS["__t_done"] = quick
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_done", title="已完成", params={})
            BA.spawn(t["task_id"])
            for _ in range(40):
                await asyncio.sleep(0.1)
                if (await BA.get_task(t["task_id"]))["status"] == "done":
                    break
            return await BA.resume_task(t["task_id"])
        r = _run(main())
        assert r["ok"] is False
        assert "已完成" in r["reason"]
    finally:
        BA.RUNNERS.pop("__t_done", None)


def test_resume_restarts_cancelled_task(monkeypatch):
    calls = {"n": 0}

    async def once(ckpt, task):
        calls["n"] += 1
        await ckpt.tick(f"第 {calls['n']} 次运行")
        if calls["n"] == 1:
            await asyncio.sleep(30)      # 第一次卡住，等看门狗
        return {"runs": calls["n"]}

    monkeypatch.setattr(BA, "CANCEL_GRACE_SECONDS", 1)
    BA.RUNNERS["__t_resume"] = once
    try:
        async def main():
            t = await BA.create_task(sid="s", tool="__t_resume", title="续跑", params={})
            BA.spawn(t["task_id"])
            await asyncio.sleep(0.3)
            await BA.request_cancel(t["task_id"])
            for _ in range(60):
                await asyncio.sleep(0.2)
                if (await BA.get_task(t["task_id"]))["status"] == "cancelled":
                    break
            # 等旧的句柄彻底退出，spawn 才会重新起
            for _ in range(40):
                r = await BA.resume_task(t["task_id"])
                if r["ok"]:
                    break
                await asyncio.sleep(0.2)
            for _ in range(60):
                await asyncio.sleep(0.2)
                got = await BA.get_task(t["task_id"])
                if got["status"] == "done":
                    return r, got
            return r, got
        r, got = _run(main())
        assert r["ok"] is True, f"续跑失败: {r}"
        assert got["status"] == "done"
        assert got["output"]["runs"] == 2, "续跑没有真的重跑处理器"
    finally:
        BA.RUNNERS.pop("__t_resume", None)
