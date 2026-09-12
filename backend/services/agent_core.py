"""Agent 内核：工具注册表 = 双端架构的通用底座。

来源：`Agent双端架构设计.md` §4「通用底座的落点：工具注册表」。

## 为什么要有这个模块（本模块修掉的真实缺陷）

2026-09-11 之前，同一个工具的定义散落在**三处手写内容**里：

1. `routers/sessions.py` 意图分类的 system prompt —— 手写的中文长串
2. `routers/sessions.py` 的 `STEP_LABELS` —— 手写的中文标签
3. `routers/sessions.py` 的 `if itype == "..."` 分支 —— 真正的实现

三处一旦失同步，后果不是"报错"而是**静默失效**。实测已经发生了：
`edit_paper` / `delete_paper` 两个分支有实现（round 60 加的）、有注释，
但**既不在 prompt 里、也不在 STEP_LABELS 里** —— 分类器永远不会吐出这两个 type，
所以这两个分支**从来不可达**，是死分支，而且没人会发现。

本模块把三者收敛成一张表：prompt 与标签**由表生成**，分支**按表分发**。
从根上消除失同步，而不是靠"记得同步改三处"。

## 双端架构的落点

`Tool` 上的 `foreground` / `cancellable` / `needs_net` / `timeout` 是**数据**，
前后台分流是查表，不是再写一堆 `if`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    """一个可被 Agent 调用的工具。

    字段全是**声明式元数据**：意图分类器的 prompt、前端步骤标签、
    前后台分流、确认闸都从这些字段派生，不再各写一份。
    """

    name: str
    label: str                      # 中文显示名（步骤 / 工具卡片）
    description: str                # 供意图分类器判别边界（写清与相邻工具的差异）
    params_hint: str = ""           # data 字段说明，进分类器 prompt
    foreground: bool = True         # True=前台同步执行；False=可丢后台
    cancellable: bool = False       # 是否允许被协作式取消
    needs_net: bool = False         # 是否联网（决定超时与重试策略）
    # 秒。**0 = 不设超时（默认）**。
    #
    # 为什么不默认 120：`asyncio.wait_for` 超时走的是**硬取消**，会在写文件/写 DB
    # 中途抛 CancelledError，留下半条数据 —— 这正是本项目明确禁止的
    # （Techniques.md §12 的"原子写 + 状态机"纪律，以及 Agent双端架构设计.md §5.3
    # "取消用协作式，不用 Task.cancel() 打底"）。
    # 所以超时是**逐工具显式开启**的：只给那些处理器自己写成可安全中断的（检查点式）工具设。
    timeout: int = 0
    requires_confirm: str = ""      # 非空=写操作确认语，用户须复述该短语才执行
    composes: tuple[str, ...] = ()  # 复合工具声明它由哪些原子工具组成
    aliases: tuple[str, ...] = ()   # 分类器可能吐出的同义词
    handler: Optional[Callable[["Ctx"], Awaitable[dict]]] = field(
        default=None, compare=False
    )


@dataclass
class Ctx:
    """一次工具调用所需的全部上下文。

    处理器统一收 `Ctx`，不再各自去摸 `_load` / `_save` / 全局单例 ——
    这样处理器可以被独立调用与测试。
    """

    sid: str
    req: Any                        # ChatMsg（取 .message）
    session: dict                   # 会话 JSON（可写）
    messages: list                  # 已追加 user 消息的完整历史
    steps: list                     # 计划步骤（回退用）
    data: dict                      # 分类器给出的 data
    itype: str                      # 归一化后的工具名
    style: str = ""                 # 用户排版偏好
    profile: dict = field(default_factory=dict)
    step_callback: Optional[Callable] = None
    save: Callable[[], None] = lambda: None      # 由分发器注入，落盘会话
    ai: Any = None                                # AIService 单例

    @property
    def message(self) -> str:
        return getattr(self.req, "message", "")

    # -- 会话写回 ---------------------------------------------------------
    def set_reply(self, reply: str) -> None:
        """把助手回复追加进会话内存（**不落盘**）。

        分开的原因：个别处理器需要在落盘**之前**再读一次
        `session["messages"]`（例如首轮自动起标题），
        写死不落盘会逼它们绕开这个方法，又回到各写一份。
        """
        self.session["messages"] = self.messages
        self.session["messages"].append({"role": "assistant", "content": reply})

    def commit(self, reply: str) -> None:
        """把助手回复追加进会话并落盘。

        这是**唯一**的会话写回口。原先每个分支各写三行
        （`s["messages"] = messages` / `.append(...)` / `_save(...)`），
        20 个分支就是 20 份重复，漏一处就丢消息。
        """
        self.set_reply(reply)
        self.save()

    # -- 返回体组装 -------------------------------------------------------
    def finish(self, reply: str, action: Optional[dict] = None,
               tool_calls: bool = False, extra: Optional[dict] = None) -> dict:
        out: dict = {"reply": reply, "steps": final_steps(self.sid, self.steps)}
        if action is not None:
            out["action"] = action
        if tool_calls:
            from services.ai_service import agent_tool_calls_get
            out["tool_calls"] = agent_tool_calls_get(self.sid)
        if extra:
            out.update(extra)
        return out


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

REGISTRY: dict[str, Tool] = {}
_ALIAS_INDEX: dict[str, str] = {}


def register(tool: Tool) -> Tool:
    """注册一个工具（模块导入时调用）。重名直接报错 —— 静默覆盖会让 prompt 与实现错位。"""
    if tool.name in REGISTRY:
        raise ValueError(f"工具名重复注册: {tool.name}")
    REGISTRY[tool.name] = tool
    _ALIAS_INDEX[tool.name] = tool.name
    for a in tool.aliases:
        _ALIAS_INDEX.setdefault(a, tool.name)
    return tool


def normalize_type(raw: Any) -> str:
    """把分类器吐出的 type 归一化成注册表里的工具名。

    认不出就返回空串，由调用方决定兜底 —— 这里**不替调用方决定**用 chat 还是报错。
    """
    if not isinstance(raw, str):
        return ""
    return _ALIAS_INDEX.get(raw.strip(), "")


def get(name: str) -> Optional[Tool]:
    return REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return list(REGISTRY.values())


def foreground_tools() -> list[Tool]:
    return [t for t in REGISTRY.values() if t.foreground]


def background_tools() -> list[Tool]:
    return [t for t in REGISTRY.values() if not t.foreground]


def step_labels() -> dict[str, str]:
    """步骤标签表，由注册表生成（原先手写在 sessions.py）。"""
    return {t.name: t.label for t in REGISTRY.values()}


# --------------------------------------------------------------------------
# 由表生成意图分类 prompt
# --------------------------------------------------------------------------

# `need` 是兜底工具，按语义必须排在最后：分类器从上往下匹配，
# 放在前面会把所有想不明白的话都吸进来。
_TAIL_TOOLS = ("need",)


def build_intent_prompt() -> str:
    """生成意图分类器的 system prompt。

    原先这段是手写的一整串中文，与分支失同步（已导致 edit_paper/delete_paper 死掉）。
    现在由 `REGISTRY` 生成 —— **不可能再失同步**。
    """
    tools = [t for t in REGISTRY.values() if t.name not in _TAIL_TOOLS]
    tools += [t for t in REGISTRY.values() if t.name in _TAIL_TOOLS]

    type_list = "/".join(f"{t.name}({t.description})" for t in tools)
    parts = [
        '分析意图返回JSON: {"type":"类型","data":{},'
        '"steps":[{"parent":"父步骤","child":"子步骤","status":"running","time":""}]}。',
        f"类型: {type_list}",
    ]
    specs = [f"{t.name}时data含{t.params_hint}。" for t in tools if t.params_hint]
    if specs:
        parts.append("".join(specs))
    parts.append(
        "steps为可选字段，描述Agent计划执行的层级步骤；parent为父步骤名，"
        "child为子步骤名，status为running/done/error，time可留空由后端填充。"
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 公共小工具
# --------------------------------------------------------------------------

def final_steps(sid: str, fallback: list) -> list:
    """优先返回 AI 执行过程中收集到的真实步骤，没有则回退到计划步骤。"""
    from services.ai_service import agent_steps_get
    actual = agent_steps_get(sid)
    return actual if actual else fallback


async def run_with_timeout(tool: Tool, ctx: Ctx) -> dict:
    """执行工具处理器；仅当工具**显式声明**了 `timeout > 0` 时才套超时。

    `timeout == 0`（默认）时是一句直调，行为与重构前完全一致 ——
    重构不该顺手给所有工具加上一个会硬取消写盘的护栏。

    超时**不吞**：抛 `asyncio.TimeoutError` 由调用方转成用户可见的失败，
    避免"看着成功其实没做成"。
    """
    if tool.handler is None:
        raise RuntimeError(f"工具 {tool.name} 未注册处理器")
    if tool.timeout <= 0:
        return await tool.handler(ctx)
    return await asyncio.wait_for(tool.handler(ctx), timeout=tool.timeout)


def registry_snapshot() -> list[dict]:
    """给前端 / 调试端点用的只读快照（不含 handler）。"""
    return [
        {
            "name": t.name, "label": t.label, "description": t.description,
            "foreground": t.foreground, "cancellable": t.cancellable,
            "needs_net": t.needs_net, "timeout": t.timeout,
            "requires_confirm": bool(t.requires_confirm),
            "composes": list(t.composes),
        }
        for t in REGISTRY.values()
    ]
