"""后台推理端：任务创建 / 执行循环 / 协作式取消 / 状态落盘 / 续跑。

来源：`Agent双端架构设计.md`
- §3.1 一个内核两种执行模式
- §5.3 取消机制：协作式优先
- §5.4 任务状态机（`cancelled` **不是终点**，可续跑）
- §7.1 状态落库（`AgentTask`）

## 为什么必须协作式取消

`asyncio.Task.cancel()` 会在**任意** await 点抛 `CancelledError` ——
如果正好落在写文件或写 DB 中途，就留下半条数据。项目已有明确纪律
（`Techniques.md` §12：JSON 原子写、状态更新有序）。所以：

- 正常路径：任务在自己的**检查点**上主动问"要不要停"（`Checkpoint.tick`），
  被取消时抛 `TaskCancelled`，由执行器收尾成 `cancelled` 并保住已产出；
- 兜底路径：`cancel()` 只用于任务卡死在非检查点的网络 I/O 时，由超时触发。

## 状态机

```
pending -> running -> (done | cancelled | error)
              ^          │
              └─ 续跑 ───┘（cancelled/error 且已有部分产出时可续跑）
```

## 重启对账

服务重启后，数据库里可能残留 `running` 的任务行 —— 但跑它的协程已经随进程消失了。
不对账的话前端会永远显示"进行中"，用户会一直等一个永远不会结束的任务。
所以启动时必须 `reconcile_on_startup()` 把它们标成 `error`。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from logger import get_logger, log_error
from models.database import async_session
from models.models import AgentTask
from services.session_locks import bg_lock

logger = get_logger()

MAX_CONCURRENCY = 5

# 请求取消后，最多再等这么多秒；到点仍没停就**兜底硬杀**。
#
# 为什么需要它（这是实测出来的，不是理论）：协作式取消只在**检查点**生效，
# 而一次 LLM 调用本身可能跑几分钟。实测中取消请求发出后任务又跑了 120 秒
# 仍停在 running —— 对用户来说就是"点了取消没反应"，正是他要避免的卡死感。
# 设计文档 §5.3 本来就把 `Task.cancel()` 定为"卡死在非检查点 I/O 时超时强杀"的兜底，
# 这里把那个"超时"落到具体数值上。
CANCEL_GRACE_SECONDS = 25

ACTIVE_STATUSES = ("pending", "running")
TERMINAL_STATUSES = ("done", "cancelled", "error")

# 进度是**估计值**：跑到 95% 就封顶（未完成前不显示 100），
# 只有真的 done 才置 100。「容量诚实」：不拿估计值冒充完成度。
_RUNNING_PROGRESS_CAP = 95.0

_sem: Optional[asyncio.Semaphore] = None
_sem_loop = None
_running: dict[str, asyncio.Task] = {}


class TaskCancelled(Exception):
    """协作式取消。由 `Checkpoint.tick()` 在检查点抛出，执行器收尾为 cancelled。"""

    def __init__(self, task_id: str):
        super().__init__(f"task {task_id} cancelled")
        self.task_id = task_id


class TaskNotFound(Exception):
    pass


# --------------------------------------------------------------------------
# 检查点
# --------------------------------------------------------------------------

class Checkpoint:
    """任务的协作式取消 + 进度上报句柄。

    任务处理器在每个可安全中断的位置调用 `await ckpt.tick("这一步在干什么")`：
    - 会检查取消标志，被取消则抛 `TaskCancelled`（**此时中断是安全的**）；
    - 会把这步写进 `steps` 并按已知总数推进 `progress`；
    - 会更新 `updated_at`，让前端的心跳看起来是活的。
    """

    def __init__(self, task_id: str, total_steps: int = 0):
        self.task_id = task_id
        self.total = max(0, int(total_steps or 0))
        self.done = 0
        self._ticks: list[dict] = []

    def set_total(self, total_steps: int) -> None:
        self.total = max(0, int(total_steps or 0))

    async def cancelled(self) -> bool:
        async with async_session() as db:
            t = await db.get(AgentTask, self.task_id)
            # 任务行消失 = 视为已取消（不要再往一个不存在的任务上写东西）
            return (t is None) or bool(t.cancel_requested)

    async def tick(self, label: str) -> None:
        if await self.cancelled():
            raise TaskCancelled(self.task_id)
        self.done += 1
        await self._persist_step(label)

    async def note(self, label: str) -> None:
        """只记一条步骤，不推进进度（用于并行/附属动作）。"""
        await self._persist_step(label)

    async def _persist_step(self, label: str) -> None:
        """写一条步骤。

        **状态必须是 running，不能是 done**：这一步刚开始，还没做完。
        原先这里写死 `status: "done"`，于是界面上会出现
        "写第 1 片 ✓ / 进度 75%" —— 而那片讲解词其实一个字都还没写出来。
        这是"看起来做完了"的谎报，比不显示更糟。
        收尾由 `_finalize_steps()` 负责（任务结束或下一步开始时把上一步标 done）。
        """
        import time as _t
        entry = {"child": str(label)[:120], "status": "running",
                 "time": _t.strftime("%H:%M:%S")}
        progress = 0.0
        if self.total > 0:
            progress = min(_RUNNING_PROGRESS_CAP, self.done / self.total * 100.0)
        async with bg_lock(self.task_id):
            async with async_session() as db:
                t = await db.get(AgentTask, self.task_id)
                if t is None:
                    raise TaskCancelled(self.task_id)
                steps = list(t.steps or [])
                # 上一条还挂着 running -> 它已经过去了（否则不会走到这里），标 done
                if steps and steps[-1].get("status") == "running":
                    steps[-1] = {**steps[-1], "status": "done"}
                steps.append(entry)
                t.steps = steps[-200:]        # 有上限，避免无限膨胀
                t.progress = progress
                t.updated_at = _now()
                await db.commit()


async def _finalize_steps(task_id: str, status: str) -> None:
    """收尾时把最后一条 running 的步骤改成终态。

    不做的话，任务已经 cancelled/done 了，界面上最后一步还挂着"进行中"。
    """
    if status not in TERMINAL_STATUSES:
        return
    final = "done" if status == "done" else ("error" if status == "error" else "cancelled")
    async with bg_lock(task_id):
        async with async_session() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                return
            steps = list(t.steps or [])
            if steps and steps[-1].get("status") == "running":
                steps[-1] = {**steps[-1], "status": final}
                t.steps = steps
                t.updated_at = _now()
                await db.commit()


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 处理器注册
# --------------------------------------------------------------------------

async def _run_prepare_lesson(ckpt: Checkpoint, task: AgentTask) -> dict:
    """备课：把课稿骨架逐片填满（可中断、可续跑）。"""
    from services import lesson_service
    params = task.params or {}
    lesson_id = str(params.get("lesson_id") or "")
    if not lesson_id:
        raise ValueError("prepare_lesson 缺少 lesson_id")
    return await lesson_service.generate_lesson(
        lesson_id, ckpt,
        section_count=int(params.get("section_count") or lesson_service.DEFAULT_SECTION_COUNT))


RUNNERS: dict[str, Callable[[Checkpoint, AgentTask], Awaitable[dict]]] = {
    "prepare_lesson": _run_prepare_lesson,
}


def _semaphore() -> asyncio.Semaphore:
    """并发闸。**跟随当前事件循环重建**。

    `asyncio.Semaphore` 会绑定到首次使用它的事件循环；若进程里换了循环
    （测试里的多次 `asyncio.run`、开发期 reload），旧闸会抛
    "bound to a different event loop"。生产只有一个循环，但在这里做对很便宜。
    """
    global _sem, _sem_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(MAX_CONCURRENCY)
        _sem_loop = loop
    return _sem


# --------------------------------------------------------------------------
# 生命周期
# --------------------------------------------------------------------------

async def create_task(*, sid: str, tool: str, title: str, params: dict) -> dict:
    if tool not in RUNNERS:
        raise ValueError(f"不支持的后台工具: {tool}")
    t = AgentTask(sid=str(sid or ""), tool=tool, title=str(title or "")[:200],
                  params=dict(params or {}), status="pending", progress=0.0,
                  steps=[], output={}, partial={}, cancel_requested=False)
    async with async_session() as db:
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return _task_dict(t)


def spawn(task_id: str) -> bool:
    """把任务丢进事件循环执行。已在跑则返回 False（幂等，且调用方据此如实告知用户）。"""
    # 顺手清掉已结束的句柄，避免 `_running` 随任务数增长
    for k in [k for k, v in list(_running.items()) if v.done()]:
        _running.pop(k, None)
    cur = _running.get(task_id)
    if cur is not None and not cur.done():
        return False
    _running[task_id] = asyncio.create_task(_guarded_run(task_id))
    return True


async def _guarded_run(task_id: str) -> None:
    try:
        async with _semaphore():
            await _run(task_id)
    except TaskCancelled:
        await _set_state(task_id, status="cancelled")
    except asyncio.CancelledError:
        # 兜底硬取消。**不覆盖已写入的终态**：看门狗一般已经先写了更具体的
        # 原因（"协作取消超时，已强制停止"），这里覆盖掉会让排查失去线索。
        await _set_state_if_not_terminal(
            task_id, status="cancelled", error="进程内硬取消（兜底路径）")
        raise
    except Exception as exc:
        log_error("background_agent", f"task {task_id} failed: {exc}")
        await _set_state(task_id, status="error", error=str(exc)[:500])
    finally:
        _running.pop(task_id, None)


async def _run(task_id: str) -> None:
    async with async_session() as db:
        t = await db.get(AgentTask, task_id)
        if t is None:
            return
        if t.cancel_requested:
            await _set_state(task_id, status="cancelled", error="启动前已请求取消")
            return
        tool = t.tool
    runner = RUNNERS.get(tool)
    if runner is None:
        await _set_state(task_id, status="error", error=f"未注册的后台工具: {tool}")
        return
    await _set_state(task_id, status="running", progress=1.0)
    ckpt = Checkpoint(task_id)
    try:
        output = await runner(ckpt, t)
    except TaskCancelled:
        await _set_state(task_id, status="cancelled")
        return
    except Exception as exc:
        # 保留 partial：让"续跑"有东西可接
        log_error("background_agent", f"runner {tool} failed on {task_id}: {exc}")
        await _set_state(task_id, status="error", error=str(exc)[:500],
                         partial={"steps_done": ckpt.done})
        return
    await _set_state(task_id, status="done", progress=100.0,
                     output=output if isinstance(output, dict) else {"result": output})


async def _set_state(task_id: str, *, status: str, progress: Optional[float] = None,
                     output: Optional[dict] = None, error: str = "",
                     partial: Optional[dict] = None) -> None:
    async with bg_lock(task_id):
        async with async_session() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                return
            t.status = status
            if progress is not None:
                t.progress = float(progress)
            if output is not None:
                t.output = output
            if error:
                t.error = error[:2000]
            if partial is not None:
                t.partial = partial
            t.updated_at = _now()
            await db.commit()
    # 任务进入终态后，最后一条还挂着 running 的步骤要收尾（否则界面上永远"进行中"）
    await _finalize_steps(task_id, status)


async def _set_state_if_not_terminal(task_id: str, *, status: str, error: str = "") -> None:
    """只在任务还没进终态时才写；已终态则原样保留（保住先写下的原因）。"""
    async with bg_lock(task_id):
        async with async_session() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                return
            if t.status in TERMINAL_STATUSES:
                return
            t.status = status
            if error:
                t.error = error[:2000]
            t.updated_at = _now()
            await db.commit()


async def request_cancel(task_id: str) -> dict:
    """请求取消。**协作式**：立即置标志，任务在下一个检查点停下。

    对用户必须说清这一点 —— 不能让他以为点了取消就立刻停了。
    """
    async with bg_lock(task_id):
        async with async_session() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                raise TaskNotFound(task_id)
            if t.status in TERMINAL_STATUSES:
                return {"ok": False, "status": t.status,
                        "reason": f"任务已{t.status}，无法取消"}
            t.cancel_requested = True
            if t.status == "pending":
                # 还没起跑：直接落成 cancelled（没有"下一个检查点"可等）
                t.status = "cancelled"
                t.error = "启动前已请求取消"
            t.updated_at = _now()
            await db.commit()
            status_now = t.status
    if status_now == "running":
        # 起看门狗：协作式取消没能及时生效时兜底硬杀（见 CANCEL_GRACE_SECONDS 说明）
        asyncio.create_task(_cancel_watchdog(task_id))
        return {
            "ok": True, "status": status_now,
            "note": (f"已请求取消。任务会在下一个检查点停下；"
                     f"若当前这一步（例如一次模型调用）本身耗时较长，"
                     f"最多 {CANCEL_GRACE_SECONDS} 秒后会被强制停止。"
                     f"已经写好的内容会保留。"),
            "grace_seconds": CANCEL_GRACE_SECONDS,
        }
    return {
        "ok": True, "status": status_now,
        "note": "尚未开始，已直接取消。",
    }


async def _cancel_watchdog(task_id: str) -> None:
    """请求取消后的兜底：宽限期内没停就硬杀。

    硬杀前**再确认一次**取消标志仍在（用户可能已点了续跑），
    避免把续跑中的任务误杀。
    """
    await asyncio.sleep(CANCEL_GRACE_SECONDS)
    handle = _running.get(task_id)
    if handle is None or handle.done():
        return
    async with async_session() as db:
        t = await db.get(AgentTask, task_id)
        if t is None or not t.cancel_requested or t.status != "running":
            return
    logger.warning("background_agent: hard-cancelling task %s after %ss grace",
                   task_id, CANCEL_GRACE_SECONDS)
    await _set_state(task_id, status="cancelled",
                     error=f"协作取消超时（{CANCEL_GRACE_SECONDS}秒）未停下，已强制停止")
    handle.cancel()


async def resume_task(task_id: str) -> dict:
    """续跑：`cancelled` / `error` 的任务清掉取消标志，重新入队。

    已产出的部分**不清空**（课稿里已写好的讲解词原样保留），
    处理器会跳过已完成的部分继续做。
    """
    async with bg_lock(task_id):
        async with async_session() as db:
            t = await db.get(AgentTask, task_id)
            if t is None:
                raise TaskNotFound(task_id)
            if t.status in ("done",):
                return {"ok": False, "reason": "任务已完成，无需续跑"}
            if t.status in ("pending", "running"):
                return {"ok": False, "reason": f"任务正在{t.status}，无需续跑"}
            t.status = "pending"
            t.cancel_requested = False
            t.error = ""
            t.updated_at = _now()
            await db.commit()
    started = spawn(task_id)
    if not started:
        # 上一次运行还没停（例如硬取消尚未生效）。**如实说**，不要假装已续跑。
        return {"ok": False, "status": "pending",
                "reason": "上一次运行还没有完全停止，请稍等几秒再试"}
    return {"ok": True, "note": "已重新入队，已完成的部分不会重做。"}


async def reconcile_on_startup() -> int:
    """服务启动时对账：把残留的 `running`/`pending` 任务标成 `error`。

    不做的后果（这条是真实故障，不是理论）：服务重启 -> 跑任务的协程随进程消失
    -> 数据库里的行还是 `running` -> 前端永远显示"进行中" ->
    用户一直等一个**永远不会结束**的任务，而且看不到任何错误。
    """
    from sqlalchemy import select as _sel
    async with async_session() as db:
        r = await db.execute(_sel(AgentTask).where(AgentTask.status.in_(ACTIVE_STATUSES)))
        rows = list(r.scalars().all())
        for t in rows:
            t.status = "error"
            t.error = "服务重启，任务中断（已产出的部分保留，可续跑）"
            t.updated_at = _now()
        if rows:
            await db.commit()
    if rows:
        logger.warning("background_agent: reconciled %d stale tasks on startup", len(rows))
    return len(rows)


async def get_task(task_id: str) -> dict:
    async with async_session() as db:
        t = await db.get(AgentTask, task_id)
        if t is None:
            raise TaskNotFound(task_id)
        return _task_dict(t, full=True)


async def list_tasks(*, sid: str = "", status: str = "", limit: int = 50,
                     offset: int = 0) -> dict:
    from sqlalchemy import func as _func, select as _sel
    limit = max(1, min(int(limit or 50), 200))
    async with async_session() as db:
        q = _sel(AgentTask).order_by(AgentTask.created_at.desc())
        cnt = _sel(_func.count(AgentTask.id))
        if sid:
            q = q.where(AgentTask.sid == sid)
            cnt = cnt.where(AgentTask.sid == sid)
        if status:
            q = q.where(AgentTask.status == status)
            cnt = cnt.where(AgentTask.status == status)
        total = (await db.execute(cnt)).scalar() or 0
        r = await db.execute(q.offset(max(0, int(offset or 0))).limit(limit))
        items = [_task_dict(t) for t in r.scalars().all()]
        return {"total": total, "returned": len(items), "tasks": items}


def running_count() -> int:
    return sum(1 for t in _running.values() if not t.done())


def _task_dict(t: AgentTask, full: bool = False) -> dict:
    d = {
        "task_id": t.id,
        "sid": t.sid or "",
        "tool": t.tool or "",
        "title": t.title or "",
        "status": t.status or "pending",
        "progress": float(t.progress or 0.0),
        "cancel_requested": bool(t.cancel_requested),
        "error": t.error or "",
        "steps": list(t.steps or []),
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
        "updated_at": t.updated_at.strftime("%Y-%m-%d %H:%M:%S") if t.updated_at else "",
        "cancellable": (t.status in ACTIVE_STATUSES),
        "resumable": (t.status in ("cancelled", "error")),
    }
    if full:
        d["params"] = dict(t.params or {})
        d["output"] = dict(t.output or {})
        d["partial"] = dict(t.partial or {})
    return d
