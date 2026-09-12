import os
import re
import json
import uuid
import html
import tempfile
import asyncio
from datetime import datetime, timezone
from typing import Optional
from config import STORAGE_DIR, _atomic_write_json, ENABLE_FOCUS_BLACKBOARD, QUESTIONS_DIR
import shutil
from logger import get_logger, log_error
from services.hippocampus_service import (
    get_teaching_context, update_mastery, update_study_session, add_weak_point,
)
from services.face_analysis_service import analyze_face, merge_emotion_report
from services.ai_service import ai_service

logger = get_logger()

FOCUS_DIR = os.path.join(STORAGE_DIR, "focus_sessions")
os.makedirs(FOCUS_DIR, exist_ok=True)
_FOCUS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_focus_locks: dict[str, asyncio.Lock] = {}

# 黑板板书常量
_BOARD_PAGE_MAX_ENTRIES = 12   # 单页条目上限，超出自动落新页
_BOARD_TEXT_MAX_LEN = 600      # 单条板书文字上限
_BOARD_OPS_MAX = 8             # 每段讲解的板书指令上限
_BOARD_FUNCTION_MAX = 2        # 每段函数图上限
_BOARD_DIAGRAM_MAX = 1         # 每段 AI 示意图上限
_BOARD_REFERENCE_MAX = 1       # 每段引用图上限
_BOARD_SVGS_MAX = 200          # 单会话板书 SVG 总量上限（防无限增长）
_BOARD_ASSET_RE = re.compile(r"^board_\d{1,4}\.(svg|jpg|png)$|^ref_\d{1,4}\.(svg|jpg|png|jpeg)$")
# 取号专用（只从"名字里取数字"，不依赖固定切片）：
# _BOARD_ASSET_RE 是"合法资产名"白名单，同时放行 board_N.* 与 ref_N.*；
# 而取号必须只在 board_N.* 里进行，否则 int("") 会抛异常并让该会话板书永久失效。
_BOARD_NUM_RE = re.compile(r"^board_(\d{1,4})\.(?:svg|jpg|png)$")
_REF_NUM_RE = re.compile(r"^ref_(\d{1,4})\.(?:svg|jpg|png|jpeg)$")


def _next_ref_index(session_id: str) -> int:
    """该会话下一个可用的引用图序号。

    原实现用"每段调用内"的局部计数器 reference_used，导致每段第一张引用图都写成 ref_0.*，
    旧页 entries 仍按该名引用 → 文件被覆盖后学生翻回上一页看到的是最新一题的图。
    """
    folder = os.path.join(FOCUS_DIR, session_id)
    if not os.path.isdir(folder):
        return 0
    used = set()
    for _f in os.listdir(folder):
        _m = _REF_NUM_RE.fullmatch(_f)
        if _m:
            used.add(int(_m.group(1)))
    n = 0
    while n in used:
        n += 1
    return n

# 教学 Prompt 模板
TEACHING_SYSTEM_PROMPT = """你是一位经验丰富的私人教师，名字叫「学习 Agent」。你的教学风格是：
1. 每次只讲一个知识点或一小段内容，控制在 100-200 字
2. 讲解末尾必须输出「对吧」作为检查点标记
3. 根据学生的状态调整讲解策略：
   - 学生理解时：继续推进，适当加深
   - 学生困惑时：换一种方式解释，用更简单的例子
   - 学生走神时：插入互动问题或有趣的例子
   - 学生疲劳时：建议休息或总结已讲内容
4. 语言简洁、生动，避免长篇大论
5. 适当使用比喻和例子帮助理解

输出格式：纯文本讲解内容，末尾带「对吧」。不要使用 Markdown 标题。"""

BOARD_TEACHING_SYSTEM_PROMPT = """你是一位经验丰富的私人教师，名字叫「学习 Agent」。你有一块黑板可以边讲边写板书。你的教学风格是：
1. 每次只讲一个知识点或一小段内容，讲解文本控制在 100-200 字
2. 讲解末尾必须输出「对吧」作为检查点标记
3. 根据学生的状态调整讲解策略：理解时推进；困惑时换例重述；走神时互动；疲劳时休息或总结
4. 语言简洁、生动，适当使用比喻和例子

输出严格 JSON（不要任何额外文字、不要 Markdown 代码块）：
{"content": "讲解文本，末尾带「对吧」", "board": {"ops": [...]}}

board 与 ops 是可选字段（本段不需要板书时省略 board）。ops 支持的指令：
- {"op":"write","kind":"text","content":"板书文字（Markdown/LaTeX，不超过200字）"}：写一段要点或推导
- {"op":"write","kind":"function","spec":{"expr":"x**2 - 2*x + 1","x_min":-5,"x_max":5,"title":"标题"}}：画函数图。expr 是 Python 表达式（幂用 **，可用 math.sin 等），x_min/x_max 是数字
- {"op":"write","kind":"diagram","description":"示意图的文字描述"}：复杂示意图（较慢，每段最多 1 张，仅在函数图画不了时用）
- {"op":"write","kind":"shape","spec":{"shape":"triangle|rect|circle|axes","title":"标题"}}：简单几何图形（快，确定性渲染；triangle 画带标注三角形、rect 画矩形、circle 画圆、axes 画坐标系）
- {"op":"reference"}：把当前题目的原题图（reference.svg）或原题照片直接贴上黑板（讲几何/图形题时优先引用，每段最多 1 次）
- {"op":"erase","last":N}：擦掉当前页最后 N 条；{"op":"erase","entry":序号}：擦指定条目（从 0 开始）
- {"op":"clear"}：清空当前页；{"op":"newpage"}：另起一页
- {"op":"snapshot","label":"4-8字标签"}：在关键节点保存课堂回看快照（如推导完成、例题讲完）

板书使用原则：
- 每段最多 8 条指令。板书写关键步骤、结论和图，不要把讲解原文照抄上去
- 当前页空间不足时系统会自动开新页，不必担心内容丢失
- 讲函数、几何、实验流程等需要图的内容时优先画图辅助；重要结论写完可顺手 snapshot
- 题目自带原题图时（系统会提示），优先用 reference 引用原题图，不要用 diagram 重画"""

CHECKPOINT_SYSTEM_PROMPT = """你是一位教育心理学专家。根据以下学生状态报告，决定下一步教学策略。

学生状态：{emotion_report}

请输出严格的 JSON 格式（不要任何额外文字）：
{{"action": "continue|rephrase|practice|rest|end", "reason": "简短原因", "difficulty_adjust": -1|0|1}}

其中：
- continue: 学生理解，继续下一段
- rephrase: 学生困惑，换一种方式重述
- practice: 学生理解但需要巩固，插入练习题
- rest: 学生疲劳，建议休息
- end: 主题讲完或学生状态不适合继续，结束本主题
- difficulty_adjust: 难度调整（-1 降低，0 不变，1 提高）"""


def _focus_path(sid: str) -> str:
    if not isinstance(sid, str) or not _FOCUS_ID_RE.fullmatch(sid):
        raise ValueError("会话 ID 无效")
    root = os.path.realpath(FOCUS_DIR)
    path = os.path.realpath(os.path.join(root, f"{sid}.json"))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("会话 ID 无效")
    return path


def _load_focus(sid: str) -> Optional[dict]:
    try:
        p = _focus_path(sid)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, IOError, OSError, ValueError) as e:
        log_error("focus", f"load_focus {sid} failed: {e}")
    return None


def _save_focus(sid: str, data: dict):
    try:
        _atomic_write_json(_focus_path(sid), data)
    except Exception as e:
        log_error("focus", f"save_focus {sid} failed: {e}")
        raise


def _get_focus_lock(sid: str) -> asyncio.Lock:
    if sid not in _focus_locks:
        _focus_locks[sid] = asyncio.Lock()
    return _focus_locks[sid]


def _cleanup_focus_lock(sid: str, lock: asyncio.Lock):
    """会话结束后清理锁，防止内存泄漏。

    仅当无持有者、无排队等待者且仍是当前映射对象时才移除，
    避免正在等待旧锁的协程被新请求新建的锁绕过。
    """
    if (len(_focus_locks) > 256 and not lock.locked()
            and not getattr(lock, "_waiters", None)
            and _focus_locks.get(sid) is lock):
        _focus_locks.pop(sid, None)


def _validate_focus_sid(sid: str) -> None:
    """在获取锁之前校验 sid：非法 ID 直接拒绝，不留下永久锁对象。"""
    _focus_path(sid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== 黑板板书 =====

def _board_new() -> dict:
    return {"pages": [{"created_at": _now_iso(), "entries": []}], "snapshots": []}


def board_asset_path(session_id: str, name: str) -> str:
    """校验并返回板书 SVG 资源的绝对路径。非法名/越界路径抛 ValueError。"""
    _focus_path(session_id)
    if not isinstance(name, str) or not _BOARD_ASSET_RE.fullmatch(name):
        raise ValueError("板书资源名无效")
    root = os.path.realpath(os.path.join(FOCUS_DIR, session_id))
    path = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("板书资源路径无效")
    return path


def _save_board_svg(session_id: str, svg: str) -> str:
    """原子保存板书 SVG（O_EXCL 独占创建取号，对齐 diagram_service 惯例），返回资产名 board_N.svg。"""
    folder = os.path.join(FOCUS_DIR, session_id)
    os.makedirs(folder, exist_ok=True)
    existing = [f for f in os.listdir(folder) if _BOARD_ASSET_RE.fullmatch(f)]
    # 取号只认 board_N.*。原先用固定切片 `f[6:-4]`，只对 "board_N.svg" 成立；而白名单
    # _BOARD_ASSET_RE 同时放行 "ref_N.*"，"ref_0.svg"[6:-4] 是**空串** → int("") 抛 ValueError。
    # 该异常发生在 max() 求值过程中，会被上层每个 op 的 except 吞掉，后果是：
    # **只要本会话落过一张引用图，之后每一段板书图形都只会塞一条失败文字，图形渲染能力
    # 在该会话内永久失效**（已用切片算术实测确证）。改用正则捕获组，名字变体不再影响取号。
    board_nums = []
    for _f in existing:
        _m = _BOARD_NUM_RE.fullmatch(_f)
        if _m:
            board_nums.append(int(_m.group(1)))
    n = max(board_nums, default=0) + 1
    if n > _BOARD_SVGS_MAX:
        raise ValueError("板书图形数量已达上限")
    while n <= _BOARD_SVGS_MAX:
        name = f"board_{n}.svg"
        path = board_asset_path(session_id, name)
        if os.path.exists(path):
            n += 1  # 并发取号冲突：已被占用则顺延
            continue
        # 原子落盘：先写同目录临时文件，再 os.replace 到目标名。
        # 绝不能在最终文件名上直接写（旧实现 O_CREAT|O_EXCL 后直接 f.write）——进程若在
        # 写入中途被杀会留下**截断的** board_N.svg，而 O_EXCL 语义让它永不被覆盖、
        # 取号还会把它算作已存在，形成永久坏资产。对齐 config._atomic_write_json 惯例。
        fd, tmp = tempfile.mkstemp(prefix=".board_tmp_", suffix=".svg", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(svg)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return name
    raise ValueError("板书图形数量已达上限")


async def _render_board_function(spec) -> str:
    """函数图 spec → SVG（确定性渲染，AST 白名单沙箱）。失败抛 ValueError。

    护栏：expr 长度与嵌套幂/超大常量限制（防 9**9**9 类表达式拖死事件循环），
    渲染放线程池 + 5s 超时（safe_eval 为同步采样，卡死时不阻塞服务）。
    """
    from services.diagram_service import diagram_service as _ds
    if not isinstance(spec, dict):
        raise ValueError("function spec 无效")
    expr = str(spec.get("expr", "")).strip().replace("^", "**")
    if not expr:
        raise ValueError("函数表达式为空")
    if len(expr) > 200:
        raise ValueError("函数表达式过长")
    if expr.count("**") > 1:
        raise ValueError("不允许嵌套幂运算")
    if re.search(r"\d{7,}", expr):
        raise ValueError("不允许超大数值常量")
    try:
        norm = {
            "function_expr": expr,
            "x_range": [float(spec.get("x_min", -5)), float(spec.get("x_max", 5))],
        }
    except (TypeError, ValueError):
        raise ValueError("x_min/x_max 必须是数字")
    if norm["x_range"][0] >= norm["x_range"][1]:
        raise ValueError("x_min 必须小于 x_max")
    try:
        svg = await asyncio.wait_for(
            asyncio.to_thread(_ds._render_function_graph_spec, norm), timeout=5.0
        )
    except asyncio.TimeoutError:
        raise ValueError("函数图渲染超时")
    if not svg or "<svg" not in svg:
        raise ValueError("函数图渲染为空")
    return svg


async def _render_board_diagram(description: str) -> str:
    """AI 按文字描述生成单个教学 SVG（纯文本生成任务，走 v4-pro 推理）。失败抛 ValueError。"""
    from services.diagram_service import diagram_service as _ds
    desc = str(description or "").strip()
    if not desc:
        raise ValueError("示意图描述为空")
    prompt = (
        "你是数学/物理示意图绘制专家。根据描述生成一张教学用 SVG 示意图。\n"
        "硬性要求：输出单个闭合的 <svg>...</svg>，不要任何其他文字；白底黑线；"
        "只使用基本图元（line/rect/circle/ellipse/path/polygon/polyline/text 及 g 分组）；"
        "必须带 viewBox；宽高不超过 640x480；图形准确、文字清晰；"
        "禁止 script/动画/事件属性/外部引用。\n"
        f"描述：{desc[:300]}"
    )
    raw = await ai_service.deepseek_chat(
        [{"role": "user", "content": prompt}], max_tokens=4096, scope="focus"
    )
    m = re.search(r"<svg[\s\S]*</svg>", raw or "")
    if not m:
        raise ValueError("AI 未返回闭合 SVG")
    svg = _ds._sanitize_svg(m.group(0))
    if not _ds._has_drawing_content(svg):
        raise ValueError("SVG 无有效绘图内容")
    return svg


def _render_board_shape(spec) -> str:
    """简单几何图形 → SVG（确定性渲染，零 AI 调用——round 60）。失败抛 ValueError。

    spec: {"shape": "triangle|rect|circle|axes", "title": "标题",
           "labels": ["甲","乙","丙"] (可选，顶点/边标注)}"""
    from services.diagram_service import diagram_service as _ds
    if not isinstance(spec, dict):
        raise ValueError("shape spec 无效")
    shape = str(spec.get("shape", "")).strip().lower()
    # 拼接前必须转义：本函数是「新增渲染函数自行拼接未转义文本」的典型
    # （FreqErr [组件 label 未转义]）。未转义的 & 或 < 会让下游 _sanitize_svg 的
    # ET 解析失败 → 整图被静默降级为空画布；而空画布又会因标题的 <text> 骗过
    # _has_drawing_content，最终把空板书当成功写入并计入 svg_ok。
    title = html.escape(str(spec.get("title", "")).strip()[:40])
    labels = [html.escape(str(l).strip()[:8]) for l in (spec.get("labels") or []) if str(l).strip()]
    if shape == "triangle":
        pts = "20,160 280,160 150,20"
        txt = "".join(f'<text x="{x}" y="{y}" font-size="16" text-anchor="middle">{t}</text>'
                      for x, y, t in [(20, 180, labels[0] if labels else "A"),
                                      (280, 180, labels[1] if len(labels) > 1 else "B"),
                                      (150, 14, labels[2] if len(labels) > 2 else "C")])
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">'
               f'<polygon points="{pts}" fill="none" stroke="black" stroke-width="2"/>{txt}</svg>')
    elif shape == "rect":
        lbl = labels[0] if labels else ""
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">'
               f'<rect x="40" y="40" width="220" height="120" fill="none" stroke="black" stroke-width="2"/>'
               f'<text x="150" y="30" font-size="16" text-anchor="middle">{lbl}</text></svg>')
    elif shape == "circle":
        lbl = labels[0] if labels else "O"
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">'
               f'<circle cx="150" cy="100" r="80" fill="none" stroke="black" stroke-width="2"/>'
               f'<circle cx="150" cy="100" r="2" fill="black"/>'
               f'<text x="158" y="96" font-size="16">{lbl}</text></svg>')
    elif shape == "axes":
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200">'
               '<line x1="30" y1="170" x2="280" y2="170" stroke="black" stroke-width="2"/>'
               '<line x1="30" y1="170" x2="30" y2="20" stroke="black" stroke-width="2"/>'
               '<polygon points="280,166 288,170 280,174" fill="black"/>'
               '<polygon points="26,26 30,18 34,26" fill="black"/>'
               '<text x="270" y="188" font-size="14">x</text>'
               '<text x="12" y="28" font-size="14">y</text>'
               '<text x="16" y="186" font-size="14">O</text></svg>')
    else:
        raise ValueError(f"不支持的简单图形: {shape}")
    svg = _ds._sanitize_svg(svg)
    # 先判内容、后补标题。顺序反了的话，标题的 <text> 会让「被消毒降级的空画布」
    # 通过 _has_drawing_content，失败被计为成功（FreqErr [失败占位伪成功]）。
    if not _ds._has_drawing_content(svg):
        raise ValueError("图形无内容")
    if title:
        # title 已在上方 html.escape；追加的是纯文本节点（不含 <>&），置于消毒之后安全。
        svg = svg.replace("</svg>",
                          f'<text x="150" y="195" font-size="13" text-anchor="middle" fill="#555">{title}</text></svg>')
    return svg


async def _apply_board_ops(session: dict, ops) -> dict:
    """执行一段讲解附带的板书指令，返回执行摘要（注入下一段 prompt）。"""
    sid = str(session.get("id", ""))
    board = session.get("board")
    if not isinstance(board, dict):
        board = _board_new()
        session["board"] = board
    pages = board.setdefault("pages", [])
    if not pages:
        pages.append({"created_at": _now_iso(), "entries": []})
    board.setdefault("snapshots", [])

    # F18 修复：引用图**不是** SVG。原实现把复制过来的 JPEG/PNG 也记进 svg_ok，
    # 而该摘要会被注入下一段 prompt 与前端，任何用 svg_ok 做质检/上限判断的逻辑都会误判。
    # 单独记 ref_ok。
    summary = {"written": 0, "svg_ok": 0, "svg_failed": 0, "erased": 0,
               "newpage": 0, "snapshot": 0, "skipped": 0, "ref_ok": 0}
    function_used = 0
    diagram_used = 0
    reference_used = 0
    _ops_all = ops if isinstance(ops, list) else []
    if len(_ops_all) > _BOARD_OPS_MAX:
        # 静默容量截断：第 9 条起的指令**既不执行也不计数**（summary["skipped"] 不含它们），
        # 摘要与日志都不体现 → AI 被要求"每段最多 8 条"，一旦超限学生端就表现为
        # "AI 说写了却什么都没发生"，事后完全无从判断。这里至少留下可见痕迹。
        logger.warning("板书指令超过上限被丢弃: session=%s %d -> %d 条",
                       sid, len(_ops_all), _BOARD_OPS_MAX)
    for op in _ops_all[:_BOARD_OPS_MAX]:
        if not isinstance(op, dict):
            summary["skipped"] += 1
            continue
        kind = str(op.get("op", ""))
        page = pages[-1]
        try:
            if kind == "write":
                wk = op.get("kind")
                if wk == "text":
                    _raw = str(op.get("content", "")).strip()
                    content = _raw[:_BOARD_TEXT_MAX_LEN]
                    if len(_raw) > _BOARD_TEXT_MAX_LEN:
                        # 静默容量截断修正（FreqErr [静默容量截断]）：原实现在这里无声切片，
                        # 超长板书会被从中间砍断且不留痕迹，学生看到"讲到一半没了"。
                        # 注：系统提示要求 AI「不超过200字」，比这个上限更严，正常不该触发。
                        #
                        # ⚠ 2026-09-11 自查修正：本分支最初误写 `session_id`（那是别的函数的形参），
                        # 而本函数内是 `sid` → NameError 被 per-op 的 except 吞掉 →
                        # **超长文字不是被截断而是整条丢失**（把一个"静默截断"改成了"静默丢失"）。
                        # 自审发现并修正，勿再引用未绑定名。
                        logger.warning(
                            "板书文字超过上限被截断: session=%s %d -> %d 字",
                            sid, len(_raw), _BOARD_TEXT_MAX_LEN,
                        )
                    if not content:
                        summary["skipped"] += 1
                        continue
                    page["entries"].append({"kind": "text", "content": content})
                    summary["written"] += 1
                elif wk == "function":
                    if function_used >= _BOARD_FUNCTION_MAX:
                        summary["skipped"] += 1
                        continue
                    try:
                        svg = await _render_board_function(op.get("spec"))
                        name = _save_board_svg(sid, svg)
                        title = str((op.get("spec") or {}).get("title", ""))[:60]
                        page["entries"].append({"kind": "svg", "asset": name, "title": title})
                        summary["svg_ok"] += 1
                        function_used += 1
                    except Exception as e:
                        page["entries"].append({"kind": "text", "content": f"[板书图形生成失败：{str(e)[:80]}]"})
                        summary["svg_failed"] += 1
                elif wk == "shape":
                    # 简单几何图形（round 60）：确定性渲染零 AI 调用
                    try:
                        svg = _render_board_shape(op.get("spec"))
                        name = _save_board_svg(sid, svg)
                        title = str((op.get("spec") or {}).get("title", ""))[:60]
                        page["entries"].append({"kind": "svg", "asset": name, "title": title})
                        summary["svg_ok"] += 1
                    except Exception as e:
                        page["entries"].append({"kind": "text", "content": f"[图形生成失败：{str(e)[:80]}]"})
                        summary["svg_failed"] += 1
                elif wk == "diagram":
                    if diagram_used >= _BOARD_DIAGRAM_MAX:
                        summary["skipped"] += 1
                        continue
                    try:
                        svg = await _render_board_diagram(str(op.get("description", "")))
                        name = _save_board_svg(sid, svg)
                        page["entries"].append({"kind": "svg", "asset": name, "title": str(op.get("description", ""))[:60]})
                        summary["svg_ok"] += 1
                        diagram_used += 1
                    except Exception as e:
                        page["entries"].append({"kind": "text", "content": f"[板书图形生成失败：{str(e)[:80]}]"})
                        summary["svg_failed"] += 1
                else:
                    summary["skipped"] += 1
            elif kind == "reference":
                # 引用图（round 60）：把题目原题图 reference.svg / 原题照片贴上黑板
                if reference_used >= _BOARD_REFERENCE_MAX:
                    summary["skipped"] += 1
                    continue
                qid_ref = str(session.get("question_id", "")).strip()
                if not qid_ref:
                    summary["skipped"] += 1
                    continue
                qdir = os.path.join(QUESTIONS_DIR, qid_ref)
                src = None
                for cand in ("reference.svg", "original.jpg", "original.png", "original.jpeg"):
                    cpath = os.path.join(qdir, cand)
                    if os.path.isfile(cpath):
                        src = cpath
                        break
                if not src:
                    page["entries"].append({"kind": "text", "content": "[原题图不存在]"})
                    summary["skipped"] += 1
                    continue
                ext = os.path.splitext(src)[1].lower()
                # 会话级唯一化：`reference_used` 是**每段调用内**的局部计数器，原实现让每段
                # 第一张引用图都写同一个 ref_0.*，而旧页 entries 仍按该名引用 → 文件被覆盖后
                # 学生翻回上一页看到的是最新一题的图。改为扫目录取下一个可用号。
                name = f"ref_{_next_ref_index(sid)}{ext}"
                dst = board_asset_path(sid, name)
                tmp = None
                try:
                    # 原子落盘（对齐本文件 _save_board_svg 的纪律）：先在**同目录**写临时文件
                    # 再 os.replace。原实现直接 shutil.copyfile 到最终名，进程在复制中被杀会留下
                    # **截断的** ref_N.jpg，且无任何校验，随后被 entries.asset 引用并由
                    # /board/asset 端点返回给学生。
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    fd, tmp = tempfile.mkstemp(prefix=".ref_tmp_", suffix=ext or ".bin",
                                               dir=os.path.dirname(dst))
                    with os.fdopen(fd, "wb") as wf, open(src, "rb") as rf:
                        shutil.copyfileobj(rf, wf, 256 * 1024)
                    os.replace(tmp, dst)
                    tmp = None
                    page["entries"].append({"kind": "image", "asset": name,
                                            "title": "原题图"})
                    # F18：引用图是 JPEG/PNG，不是 SVG —— 计入 svg_ok 会让"板书图形"统计虚高
                    summary["ref_ok"] += 1
                    reference_used += 1
                except OSError as e:
                    if tmp:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                    logger.warning("reference copy failed: %s", e)
                    summary["skipped"] += 1
            elif kind == "erase":
                entries = page["entries"]
                if "last" in op:
                    try:
                        n = min(max(int(op.get("last", 0)), 0), len(entries))
                    except (TypeError, ValueError):
                        n = 0
                    if n:
                        del entries[len(entries) - n:]
                        summary["erased"] += n
                    else:
                        summary["skipped"] += 1
                elif "entry" in op:
                    try:
                        idx = int(op.get("entry", -1))
                    except (TypeError, ValueError):
                        idx = -1
                    if 0 <= idx < len(entries):
                        entries.pop(idx)
                        summary["erased"] += 1
                    else:
                        summary["skipped"] += 1
                else:
                    summary["skipped"] += 1
            elif kind == "clear":
                summary["erased"] += len(page["entries"])
                page["entries"] = []
            elif kind == "newpage":
                pages.append({"created_at": _now_iso(), "entries": []})
                summary["newpage"] += 1
            elif kind == "snapshot":
                label = str(op.get("label", "")).strip()[:24] or f"课堂快照{len(board['snapshots']) + 1}"
                board["snapshots"].append({"page": len(pages), "label": label, "time": _now_iso(), "by": "ai"})
                summary["snapshot"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:
            logger.warning("board op %s failed: %s", kind, str(e)[:120])
            summary["skipped"] += 1
        # 页满兜底：超出单页上限的条目自动滚入新页，保证 AI 书写不丢失
        while len(pages[-1]["entries"]) > _BOARD_PAGE_MAX_ENTRIES:
            overflow = pages[-1]["entries"][_BOARD_PAGE_MAX_ENTRIES:]
            pages[-1]["entries"] = pages[-1]["entries"][:_BOARD_PAGE_MAX_ENTRIES]
            pages.append({"created_at": _now_iso(), "entries": overflow})

    return {
        "written": summary["written"], "svg_ok": summary["svg_ok"],
        "svg_failed": summary["svg_failed"], "erased": summary["erased"],
        "newpage": summary["newpage"], "snapshot": summary["snapshot"],
        "skipped": summary["skipped"], "ref_ok": summary["ref_ok"],
        "page": len(pages), "page_entries": len(pages[-1]["entries"]),
        "page_max": _BOARD_PAGE_MAX_ENTRIES, "snapshots": len(board["snapshots"]),
    }


async def save_board_snapshot(session_id: str, label: str = "") -> dict:
    """学生手动保存当前页快照（回看引用）。"""
    _validate_focus_sid(session_id)
    if not ENABLE_FOCUS_BLACKBOARD:
        raise ValueError("黑板功能未启用")
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            board = session.get("board")
            if not isinstance(board, dict) or not board.get("pages"):
                raise ValueError("没有板书可保存")
            clean = str(label or "").strip()[:24] or f"课堂快照{len(board.get('snapshots', [])) + 1}"
            snap = {"page": len(board["pages"]), "label": clean, "time": _now_iso(), "by": "student"}
            board.setdefault("snapshots", []).append(snap)
            session["board"] = board
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return {"snapshot": snap}
    finally:
        _cleanup_focus_lock(session_id, lock)


async def start_session(mode: str, topic: str = "", question_id: str = "") -> dict:
    """启动一个新的专注模式会话"""
    sid = str(uuid.uuid4())

    # 获取教学上下文（海马体数据）
    teaching_ctx = get_teaching_context(topic)

    session = {
        "id": sid,
        "mode": mode,
        "topic": topic,
        "question_id": question_id,
        "status": "preparing",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "segments": [],
        "teaching_strategy": {
            "pace": "normal",
            "style": teaching_ctx.get("meta", {}).get("preferred_style", "conceptual"),
            "difficulty_level": 3,
        },
        "total_checkpoints": 0,
        "passed_checkpoints": 0,
    }
    if ENABLE_FOCUS_BLACKBOARD:
        session["board"] = _board_new()

    _save_focus(sid, session)

    # 生成第一段讲解
    first_segment = await _generate_teaching_segment(session, teaching_ctx)
    session["segments"].append(first_segment)
    session["status"] = "teaching"
    session["updated_at"] = _now_iso()
    _save_focus(sid, session)

    return session


async def _generate_teaching_segment(session: dict, teaching_ctx: dict) -> dict:
    """生成一段讲解内容（黑板开启时输出 JSON 并执行板书指令）"""
    topic = session.get("topic", "")
    mode = session.get("mode", "topic")
    strategy = session.get("teaching_strategy", {})
    segment_index = len(session.get("segments", []))

    # 构建 Prompt（黑板开启用 JSON 版提示词）
    use_board = bool(ENABLE_FOCUS_BLACKBOARD)
    system = BOARD_TEACHING_SYSTEM_PROMPT if use_board else TEACHING_SYSTEM_PROMPT

    # 构建用户上下文
    user_parts = []
    if topic:
        user_parts.append(f"当前学习主题：{topic}")

    # 添加海马体上下文
    meta = teaching_ctx.get("meta", {})
    if meta:
        user_parts.append(f"学生理解力基线：{meta.get('baseline_understanding', 0.5)}")
        user_parts.append(f"学生记忆力基线：{meta.get('baseline_memory', 0.5)}")
        user_parts.append(f"偏好讲解风格：{meta.get('preferred_style', 'conceptual')}")

    # 添加已有主题记忆
    topics_memory = teaching_ctx.get("topics", {})
    if topics_memory:
        for t, td in topics_memory.items():
            mastery = td.get("mastery", 0)
            weak = td.get("weak_points", [])
            user_parts.append(f"主题「{t}」掌握度 {mastery:.0%}，薄弱点：{', '.join(weak) if weak else '无'}")

    # F10 修复：把学生上一段的作答喂进讲解上下文。
    # 原实现里 checkpoint_response.voice_text **只落盘、不参与任何决策**（全文件仅形参+写入两处），
    # 学生"答题"对系统没有任何作用，却计入 total_checkpoints。这里接上回路，
    # 让「根据学生状态决定下一段」这条设计真正生效。
    _last_answer = ""
    for _seg in reversed(session.get("segments", [])):
        _cpr = _seg.get("checkpoint_response") or {}
        if str(_cpr.get("voice_text") or "").strip():
            _last_answer = str(_cpr["voice_text"]).strip()[:500]
            break
    if _last_answer:
        user_parts.append(
            f"\n学生上一段作答：{_last_answer}\n"
            "请据此调整本段：答对则推进，答错或含糊则先补基础、不要直接跳到下一知识点。"
        )

    # 添加已有讲解历史
    segments = session.get("segments", [])
    if segments:
        user_parts.append("\n已讲内容摘要：")
        for seg in segments[-3:]:  # 最近 3 段
            content = seg.get("content", "")[:100]
            user_parts.append(f"- {content}...")
        # 板书上下文：当前页占用 + 上一段执行摘要，供 AI 管理空间（擦旧/翻页）
        if use_board:
            board = session.get("board") or {}
            pages = board.get("pages") or []
            if pages:
                user_parts.append(
                    f"\n当前板书：共 {len(pages)} 页，"
                    f"当前页 {len(pages[-1].get('entries', []))}/{_BOARD_PAGE_MAX_ENTRIES} 条，"
                    f"快照 {len(board.get('snapshots', []))} 个。"
                )
            last_summary = segments[-1].get("board_summary")
            if last_summary:
                user_parts.append(f"上一段板书执行摘要：{json.dumps(last_summary, ensure_ascii=False)}")

    # 添加策略
    difficulty = strategy.get("difficulty_level", 3)
    pace = strategy.get("pace", "normal")
    user_parts.append(f"\n当前难度等级：{difficulty}/5，节奏：{pace}")

    if segment_index == 0:
        user_parts.append("\n请开始第一段讲解。")
    else:
        user_parts.append("\n请继续下一段讲解。")

    user_content = "\n".join(user_parts)

    try:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        board_ops = None
        if use_board:
            data = await ai_service.deepseek_json(messages, max_tokens=3000, scope="focus")
            if not isinstance(data, dict):
                raise ValueError("讲解返回非对象")
            content = str(data.get("content", "")).strip()
            if not content:
                raise ValueError("讲解内容为空")
            board_data = data.get("board")
            if isinstance(board_data, dict):
                board_ops = board_data.get("ops")
        else:
            raw_reply = await ai_service.deepseek_chat(messages, max_tokens=1024, scope="focus")
            content = raw_reply.strip() if raw_reply else ""

        # 确保末尾有「对吧」
        if "对吧" not in content[-10:]:
            content = content + "\n\n对吧"

        # 执行板书指令（失败只影响板书，不阻断教学）
        board_summary = None
        if use_board and board_ops:
            try:
                board_summary = await _apply_board_ops(session, board_ops)
            except Exception as be:
                log_error("focus", f"apply_board_ops failed: {str(be)[:200]}")

        segment = {
            "index": segment_index,
            "content": content,
            "has_checkpoint": True,
            "checkpoint_response": None,
        }
        if board_summary is not None:
            segment["board_summary"] = board_summary
        return segment
    except Exception as e:
        log_error("focus", f"generate_teaching_segment failed: {e}")
        # 失败段必须可见且可重试（Design「可恢复」原则，M1）：
        # 标记 failed=True，学生提交检查点时按重试处理（弹出失败段重新生成，
        # 不计入 total/passed_checkpoints，避免伪段落污染掌握度统计）。
        return {
            "index": segment_index,
            "content": "抱歉，这一段讲解生成失败了。请点击确认按钮重试。",
            "has_checkpoint": True,
            "checkpoint_response": None,
            "failed": True,
        }


async def submit_checkpoint(
    session_id: str,
    voice_text: str = "",
    emotion_report: dict = None,
    webcam_image: str = "",
    voice_features: dict = None,
    segment_index: int = None,
) -> dict:
    """提交检查点响应，获取下一段讲解。

    segment_index：学生响应的讲解段序号（前端从 currentSegment.index 传入）。
    缺省时保持旧行为（取最新段）；显式传入时做幂等校验——该段已响应过则拒绝
    （功能检查轮 F8-2：前端超时重试/连点会把新教学段误挂成检查点响应，
    total_checkpoints 虚增并污染掌握度统计）。"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            return await _submit_checkpoint_locked(session_id, voice_text, emotion_report,
                                                   webcam_image, voice_features,
                                                   segment_index)
    finally:
        _cleanup_focus_lock(session_id, lock)


async def _submit_checkpoint_locked(
    session_id: str,
    voice_text: str = "",
    emotion_report: dict = None,
    webcam_image: str = "",
    voice_features: dict = None,
    segment_index: int = None,
) -> dict:
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")

    if session.get("status") not in ("teaching", "checkpoint"):
        raise ValueError(f"当前状态 {session.get('status')} 不允许提交检查点")

    segments = session.get("segments", [])
    if segment_index is not None:
        if not isinstance(segment_index, int) or segment_index < 0 or segment_index >= len(segments):
            raise ValueError(f"讲解段序号无效：{segment_index}")
        target_index = segment_index
        if segments[target_index].get("checkpoint_response"):
            raise ValueError("该讲解段已提交过检查点响应，请等待下一段讲解开始")
    else:
        target_index = len(segments) - 1
    if target_index < 0:
        raise ValueError("没有可响应的讲解段")

    # 0. 失败段的提交视为重试请求（M1，2026-09-09）：弹出失败段并重新生成，
    #    不做表情分析、不计检查点统计、不触发 pause/simplify 建议。
    if segments[target_index].get("failed"):
        segments.pop(target_index)
        teaching_ctx = get_teaching_context(session.get("topic", ""))
        retry_segment = await _generate_teaching_segment(session, teaching_ctx)
        session["segments"].append(retry_segment)
        session["status"] = "teaching"
        session["updated_at"] = _now_iso()
        _save_focus(session_id, session)
        return {
            "session": session,
            "action": "retry",
            "segment": retry_segment,
        }

    # 1+2. 情感报告：优先采用调用方提供的（接口本就声明支持该通道），否则服务端自行分析。
    # F9 修复：原实现把 emotion_report 收下后**全链路零读取**（grep 只有形参与透传两处），
    # 最终仍由 webcam_image 重新分析 —— 即"客户端自带情感报告"这条通道从未生效。
    if emotion_report:
        combined_report = dict(emotion_report)
    else:
        face_report = None
        if webcam_image:
            face_report = await analyze_face(webcam_image)
        if face_report and voice_features:
            combined_report = merge_emotion_report(face_report, voice_features)
        elif face_report:
            combined_report = face_report
        elif voice_features:
            # 只有语音特征、没有表情数据：**不知道**学生是否理解，不能默认判为 engaged
            # （engaged 落在上层"计为通过"的分支）。标 unknown + degraded 如实表达"数据不足"。
            combined_report = {
                "overall_state": "unknown",
                "confidence": 0.0,
                "suggestion": "continue",
                "face_expression": "unknown",
                "voice_features": voice_features,
                "indicators": [],
                "degraded": True,
            }
        else:
            # 既无表情也无语音：纯文字作答或未采集。同样不得默认判为通过。
            combined_report = {
                "overall_state": "unknown",
                "confidence": 0.0,
                "suggestion": "continue",
                "face_expression": "unknown",
                "voice_features": {},
                "indicators": [],
                "degraded": True,
            }

    # 3. 记录检查点响应
    checkpoint_response = {
        "voice_text": voice_text or "",
        "emotion_report": combined_report,
        "timestamp": _now_iso(),
    }

    session["segments"][target_index]["checkpoint_response"] = checkpoint_response
    session["total_checkpoints"] = session.get("total_checkpoints", 0) + 1

    # 只有**未降级**的真实评估才计为通过。
    # 原实现只看 overall_state ∈ (understanding, engaged)，而"无摄像头/分析失败"的兜底值
    # 恰好是 engaged → 不用摄像头即 100% 通过，掌握度固定 +0.1 并写进长期记忆（已确证）。
    if (not combined_report.get("degraded")) and \
       combined_report.get("overall_state") in ("understanding", "engaged"):
        session["passed_checkpoints"] = session.get("passed_checkpoints", 0) + 1

    # 4. 根据情感报告决定下一步
    suggestion = combined_report.get("suggestion", "continue")

    if suggestion == "pause":
        session["status"] = "paused"
        session["updated_at"] = _now_iso()
        _save_focus(session_id, session)
        return {
            "session": session,
            "action": "pause",
            "message": "建议休息一下，准备好了再继续。",
        }

    if suggestion == "simplify":
        # 降低难度，重述当前段
        strategy = session.get("teaching_strategy", {})
        strategy["difficulty_level"] = max(1, strategy.get("difficulty_level", 3) - 1)
        session["teaching_strategy"] = strategy
        # F11 修复：把"这一段没讲明白"记成知识薄弱点。
        # 原实现里写入 weak_points 的唯一函数 `add_weak_point` 全仓**无任何调用者**
        # （profile.json 里 weak_points 恒为 []），于是教学 prompt 中的"薄弱点"永远是"无"，
        # 针对薄弱点的复习策略不可能被触发——记忆驱动的个性化教学只是宣称。
        try:
            _seg_text = str(session["segments"][target_index].get("content") or "")[:40]
            add_weak_point(session.get("topic", ""), f"第{target_index + 1}段未掌握：{_seg_text}")
        except Exception as _e:
            log_error("focus", f"add_weak_point failed: {_e}")

    # 5. 生成下一段讲解
    teaching_ctx = get_teaching_context(session.get("topic", ""))
    next_segment = await _generate_teaching_segment(session, teaching_ctx)
    session["segments"].append(next_segment)
    session["status"] = "teaching"
    session["updated_at"] = _now_iso()
    _save_focus(session_id, session)

    return {
        "session": session,
        "action": "continue",
        "segment": next_segment,
    }


async def get_session_state(session_id: str) -> dict:
    """获取会话当前状态"""
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")
    return session


async def pause_session(session_id: str) -> dict:
    """暂停会话"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            session["status"] = "paused"
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return session
    finally:
        _cleanup_focus_lock(session_id, lock)


async def resume_session(session_id: str) -> dict:
    """恢复会话"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            session["status"] = "teaching"
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return session
    finally:
        _cleanup_focus_lock(session_id, lock)


async def end_session(session_id: str) -> dict:
    """结束会话，更新海马体"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            return await _end_session_locked(session_id)
    finally:
        # 在锁释放后有界清理；等待中的协程持有旧锁引用不受影响，
        # 新请求会创建新锁，与旧锁的等待者互不干扰（end 为终态，影响面有限）
        _cleanup_focus_lock(session_id, lock)


async def _end_session_locked(session_id: str) -> dict:
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")

    session["status"] = "completed"
    session["updated_at"] = _now_iso()

    # 更新海马体（失败不影响会话结束）
    try:
        topic = session.get("topic", "")
        if topic:
            total_checkpoints = session.get("total_checkpoints", 0)
            passed_checkpoints = session.get("passed_checkpoints", 0)

            # 真实学习时长：会话 JSON 里本就有 created_at/updated_at（ISO 字符串），
            # 原先从未被使用，而是写入「检查点数 × 2」这个与时间无关的合成值。
            # 时间戳缺失/异常时传 None，让下游按"无数据"处理（不编造分钟数）。
            real_minutes = None
            try:
                _t0 = datetime.fromisoformat(str(session.get("created_at") or "").replace("Z", "+00:00"))
                _t1 = datetime.fromisoformat(str(session.get("updated_at") or _now_iso()).replace("Z", "+00:00"))
                if _t1 >= _t0:
                    real_minutes = max(1, int((_t1 - _t0).total_seconds() // 60))
            except Exception:
                real_minutes = None

            if total_checkpoints <= 0:
                # 一次检查点都没有 → 没有任何评估依据。
                # 不提升掌握度（原实现会因 pass_rate=0 落到 0.02 分支，凭空加分），
                # 通过率传 None 表示"本次无数据"，不参与滑动平均。
                update_study_session(topic, minutes=real_minutes, checkpoint_pass_rate=None)
            else:
                pass_rate = min(1.0, passed_checkpoints / total_checkpoints)
                if pass_rate >= 0.8:
                    mastery_delta = 0.1
                elif pass_rate >= 0.5:
                    mastery_delta = 0.05
                else:
                    mastery_delta = 0.02
                update_mastery(topic, mastery_delta, reason="focus_session")
                update_study_session(topic, minutes=real_minutes, checkpoint_pass_rate=pass_rate)
    except Exception as e:
        log_error("focus", f"hippocampus update failed: {e}")
    finally:
        _save_focus(session_id, session)

    return session


def list_sessions(limit: int = 20) -> list:
    """列出最近的专注模式会话（按修改时间倒序）"""
    sessions = []
    try:
        all_files = [f for f in os.listdir(FOCUS_DIR) if f.endswith(".json")]
        # 按文件修改时间排序（最近的在前）
        all_files.sort(key=lambda f: os.path.getmtime(os.path.join(FOCUS_DIR, f)), reverse=True)
        files = all_files[:limit]
        for fname in files:
            sid = fname[:-5]
            data = _load_focus(sid)
            if data:
                sessions.append({
                    "id": data.get("id", sid),
                    "mode": data.get("mode", ""),
                    "topic": data.get("topic", ""),
                    "status": data.get("status", ""),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "total_checkpoints": data.get("total_checkpoints", 0),
                    "passed_checkpoints": data.get("passed_checkpoints", 0),
                })
    except OSError as e:
        log_error("focus", f"list_sessions failed: {e}")
    return sessions
