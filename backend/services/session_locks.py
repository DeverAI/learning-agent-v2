"""会话锁拆分（来源：`Agent双端架构设计.md` §6「锁拆分（必须第一步做）」）。

## 为什么要拆

2026-09-11 之前是**整会话一把锁**：`session_chat` 一进函数就持锁，
一直持到 AI 回答结束（可能是几十秒到几分钟）。后果是"一次长对话把所有相关操作都堵住"：

- 用户想给这个会话改个名 -> 卡住
- 用户想删掉这个会话 -> 卡住
- 后台任务想更新自己的状态 -> 卡住（一旦接入后台推理端，这里立刻变成死锁点）

用户的原话是「不要一个并发死磕到底不然用户会看到卡死」。
所以拆锁不是洁癖，是那条需求的**前置**。

## 拆成三把

| 锁 | 粒度 | 覆盖范围 |
|----|------|----------|
| `fg_lock(sid)` | 会话 | **只**串行同一个会话的两条前台消息 |
| `file_lock(sid)` | 会话 | **只**覆盖会话 JSON 的读改写临界区（毫秒级） |
| `bg_lock(task_id)` | 任务 | 后台任务自身的状态更新串行 |

**原则：前台不等后台；短临界区不背长任务的锅。**

## 一个必须说明的取舍

拆锁之后，`rename_session` / `delete_session` **不再等待**正在进行的对话回合 ——
因为"等待"正是用户说的卡死。它们改为：

- 先看该会话有没有前台回合在跑（`fg_busy`）；
- 有 -> **立刻**返回 409 并说明原因（不阻塞、不含糊）；
- 无 -> 在 `file_lock` 下完成读改写。

为什么不干脆并行写？因为对话回合结束时会整体回写会话 JSON，
若期间改过标题就会被**静默回滚**（丢失更新）。与其悄悄丢掉用户的改名，
不如明确告诉他"正在对话中，稍后再改"。

`delete` 的竞态另有 `routers/sessions.py::_save` 的 `guard_deleted` 兜底：
对话回合结束回写时若发现文件已被删除，不再把会话"复活"。

## 锁表用弱引用

沿用项目既有做法（`Techniques.md` §3）：锁对象无人引用时自动回收，
避免"每个会话 ID 留一把锁"随时间无限累积。
"""

from __future__ import annotations

import asyncio
import weakref

_FG_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
_FILE_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()
_BG_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _get(store, key: str) -> asyncio.Lock:
    lock = store.get(key)
    if lock is None:
        # 事件循环单线程：setdefault 不会被打断；
        # 全程持强引用，确保不会被 weakref 在返回前回收。
        lock = store.setdefault(key, asyncio.Lock())
    return lock


def fg_lock(sid: str) -> asyncio.Lock:
    """前台回合锁：串行同一个会话的两条前台消息。"""
    return _get(_FG_LOCKS, sid)


def file_lock(sid: str) -> asyncio.Lock:
    """会话文件锁：只覆盖 JSON 读改写的短临界区。"""
    return _get(_FILE_LOCKS, sid)


def bg_lock(task_id: str) -> asyncio.Lock:
    """后台任务锁：串行该任务自身的状态更新。"""
    return _get(_BG_LOCKS, task_id)


def fg_busy(sid: str) -> bool:
    """该会话是否有前台回合正在跑（不阻塞，立刻返回）。"""
    lock = _FG_LOCKS.get(sid)
    if lock is None:
        return False
    if lock.locked():
        return True
    # 锁没被持有但有人在排队等：也算忙，否则排队者会被新锁绕过
    return bool(getattr(lock, "_waiters", None))


def drop_fg_lock_if_idle(sid: str) -> bool:
    """无人持有且无人排队时移除锁键，避免锁表随会话数累积。

    返回是否真的移除了。删除会话后调用。
    """
    lock = _FG_LOCKS.get(sid)
    if lock is None:
        return False
    if lock.locked() or getattr(lock, "_waiters", None):
        return False
    if _FG_LOCKS.get(sid) is lock:
        _FG_LOCKS.pop(sid, None)
        return True
    return False


def snapshot() -> dict:
    """只读状态快照，供诊断端点/测试使用。"""
    return {
        "fg": sorted(_FG_LOCKS.keys()),
        "file": sorted(_FILE_LOCKS.keys()),
        "bg": sorted(_BG_LOCKS.keys()),
    }
