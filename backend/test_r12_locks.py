"""R12：会话锁拆分的行为测试。

这些断言存在的意义：锁拆分是"能跑就算对"的典型 —— 拆错了照样能跑，
只是又变回"一次长对话堵住所有操作"。所以必须用行为断言钉住：

1. 三把锁是**不同的对象**（否则等于没拆）；
2. 前台回合互相串行（同一会话两条消息不能交错写会话文件）；
3. `fg_busy` 在回合进行中为真（删除/改名据此立刻 409，而不是卡死）；
4. `_save(guard_deleted=True)` 在文件被删后**拒绝回写**（不复活已删除的会话）；
5. 锁表能被回收（不随会话数无限累积）。

测试自己管理临时会话文件，不依赖 config 重定向 —— 避免与既有测试文件的
"首导入方生效"惯例互相干扰。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import session_locks as SL  # noqa: E402


# --------------------------------------------------------------------------
# 1. 三把锁必须是不同的对象
# --------------------------------------------------------------------------

def test_fg_file_bg_locks_are_distinct_objects():
    fg = SL.fg_lock("r12_a")
    fl = SL.file_lock("r12_a")
    bg = SL.bg_lock("r12_a")
    assert fg is not fl, "前台锁与文件锁是同一个对象 = 等于没拆"
    assert fg is not bg
    assert fl is not bg
    # 同名的重复获取必须拿到同一把（否则锁形同虚设）
    assert SL.fg_lock("r12_a") is fg
    assert SL.file_lock("r12_a") is fl
    assert SL.bg_lock("r12_a") is bg


def test_bg_lock_is_per_task_id():
    assert SL.bg_lock("task_1") is not SL.bg_lock("task_2")


# --------------------------------------------------------------------------
# 2. 前台回合串行
# --------------------------------------------------------------------------

def test_fg_lock_serializes_two_turns():
    order = []

    async def turn(tag):
        async with SL.fg_lock("r12_serial"):
            order.append(f"{tag}-in")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-out")

    async def main():
        await asyncio.gather(turn("a"), turn("b"))

    asyncio.run(main())
    assert len(order) == 4
    # 不允许出现 a-in / b-in 交错
    assert order[0].endswith("-in") and order[1].endswith("-out")
    assert order[2].endswith("-in") and order[3].endswith("-out")
    assert order[0][0] == order[1][0] and order[2][0] == order[3][0]


def test_file_lock_does_not_block_fg_lock():
    """关键：持着文件锁不应该挡住前台回合，反之亦然（这就是"拆开"的意义）。"""

    async def main():
        async with SL.file_lock("r12_indep"):
            # 文件锁被持有期间，前台锁必须仍然可获取
            got = False
            async with SL.fg_lock("r12_indep"):
                got = True
            return got

    assert asyncio.run(main()) is True


# --------------------------------------------------------------------------
# 3. fg_busy 的语义
# --------------------------------------------------------------------------

def test_fg_busy_false_when_idle_true_while_held():
    async def main():
        assert SL.fg_busy("r12_busy") is False
        async with SL.fg_lock("r12_busy"):
            assert SL.fg_busy("r12_busy") is True
            return True

    assert asyncio.run(main()) is True
    assert SL.fg_busy("r12_busy") is False


def test_fg_busy_true_for_waiters():
    """有排队者也算忙 —— 否则等待中的回合会被新锁绕过。"""
    async def main():
        holder = SL.fg_lock("r12_wait")

        async def hold():
            async with holder:
                await asyncio.sleep(0.08)

        async def wait():
            await asyncio.sleep(0.01)
            async with SL.fg_lock("r12_wait"):
                pass

        t1 = asyncio.create_task(hold())
        t2 = asyncio.create_task(wait())
        await asyncio.sleep(0.04)
        busy = SL.fg_busy("r12_wait")
        await asyncio.gather(t1, t2)
        return busy

    assert asyncio.run(main()) is True


# --------------------------------------------------------------------------
# 4. 删除守卫：不复活已删除的会话
# --------------------------------------------------------------------------

def test_save_guard_deleted_refuses_resurrection():
    from routers import sessions as S

    sid = "r12guardtest"
    p = S._session_path(sid)
    if os.path.exists(p):
        os.remove(p)
    try:
        S._save(sid, {"id": sid, "messages": []})
        assert os.path.exists(p)

        # 模拟"回合进行中用户删掉了会话"
        os.remove(p)

        with pytest.raises(S.SessionDeletedError):
            S._save(sid, {"id": sid, "messages": [
                {"role": "assistant", "content": "回合结束才回写"}]},
                guard_deleted=True)
        assert not os.path.exists(p), "会话被复活了"
    finally:
        if os.path.exists(p):
            os.remove(p)


def test_save_without_guard_still_writes():
    """不带守卫时保持原行为（create_session 等正常路径不受影响）。"""
    from routers import sessions as S

    sid = "r12noguard"
    p = S._session_path(sid)
    if os.path.exists(p):
        os.remove(p)
    try:
        S._save(sid, {"id": sid, "messages": []})
        assert os.path.exists(p)
        S._save(sid, {"id": sid, "messages": [{"role": "user", "content": "x"}]})
        assert os.path.exists(p)
    finally:
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------
# 5. 锁表可回收
# --------------------------------------------------------------------------

def test_lock_table_is_weakref_based():
    """钉住锁表的弱引用语义（写这条是因为我第一版断言的前提就是错的）。

    锁表用 `WeakValueDictionary`：**没有强引用时条目会立刻消失**。
    所以 `SL.fg_lock(sid)` 单纯调一下、把返回值丢掉，等于什么都没留下。

    这对调用方是有约束的，而 `fg_busy()` 的正确性正建立在它上面：
    "在跑的回合一定通过 `async with lock` 持有强引用" -> 此时查得到、且 `locked()` 为真；
    回合结束后强引用消失、条目自动回收 -> 查不到 = 不忙。
    """
    import gc
    sid = "r12_weak"
    SL.fg_lock(sid)          # 丢弃返回值
    gc.collect()
    assert sid not in SL.snapshot()["fg"], "弱引用表竟然留住了无人引用的锁"

    held = SL.fg_lock(sid)   # 持有引用
    assert sid in SL.snapshot()["fg"]
    del held
    gc.collect()
    assert sid not in SL.snapshot()["fg"]


def test_drop_fg_lock_if_idle_removes_key():
    sid = "r12_drop"
    lock = SL.fg_lock(sid)      # 必须持引用，否则弱引用表已经自己回收了
    assert sid in SL.snapshot()["fg"]
    assert SL.drop_fg_lock_if_idle(sid) is True
    assert sid not in SL.snapshot()["fg"]
    assert lock is not None


def test_drop_fg_lock_refuses_while_held():
    async def main():
        sid = "r12_drop_held"
        async with SL.fg_lock(sid):
            return SL.drop_fg_lock_if_idle(sid)

    assert asyncio.run(main()) is False
