import os
import json
import math
import time
import html
import ast
import re
import asyncio
import weakref
import tempfile
from datetime import timezone
from fastapi import HTTPException
from services.coord_engine import eval_expression, calc_all_points, to_svg
from services.diagram_components import render_spec as _render_components
from config import QUESTIONS_DIR, load_settings
from logger import get_logger, log_error

logger = get_logger()

_VALID_QUESTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_FUNCTION_YX_RE = re.compile(r"y\s*=\s*[^,\n]*x\b", re.IGNORECASE)


def _is_valid_question_id(question_id: str) -> bool:
    if not question_id or not isinstance(question_id, str):
        return False
    return bool(_VALID_QUESTION_ID_RE.fullmatch(question_id))


def _sanitize_svg_color(raw_color: str, default: str = "#222") -> str:
    """仅允许 #RGB/#RRGGBB 或安全 CSS 颜色名，防止 SVG 属性注入。"""
    if not raw_color:
        return default
    c = str(raw_color).strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", c) or re.fullmatch(r"#[0-9a-f]{6}", c):
        return c
    safe_names = {
        "red", "green", "blue", "yellow", "orange", "purple", "cyan", "magenta",
        "lime", "pink", "teal", "lavender", "brown", "beige", "maroon", "mint",
        "olive", "coral", "navy", "grey", "gray", "black", "white", "gold", "silver",
        "none", "transparent",
    }
    if c in safe_names:
        return c
    return default


def _sanitize_svg_style(raw_style: str) -> str:
    if raw_style == "dashed":
        return "dashed"
    return "solid"


def _svg_mode() -> str:
    """Returns 'coord' (old mode) or 'direct' (new mode, default)."""
    try:
        s = load_settings()
        return s.get("svg_mode", "direct")
    except Exception:
        return "direct"


_SCHEMATIC_KEYWORDS = ("坐标系", "折线", "售价", "销量", "价格", "斜率")


def _is_extreme_slope_chart(data_points: list[tuple]) -> bool:
    """判断数据点是否包含极端斜率（如销量-价格关系图）。"""
    if len(data_points) < 2:
        return False
    xs = [p[0] for p in data_points]
    ys = [p[1] for p in data_points]
    x_range = max(xs) - min(xs)
    y_range = max(ys) - min(ys)
    if x_range == 0:
        return y_range != 0
    if y_range / x_range > 50:
        return True
    max_slope = 0.0
    for (x1, y1), (x2, y2) in zip(data_points, data_points[1:]):
        dx = x2 - x1
        if dx == 0:
            return True
        max_slope = max(max_slope, abs((y2 - y1) / dx))
    return max_slope > 50


def _schematic_compress_y(data_points: list[tuple]) -> list[tuple]:
    """对 y 值进行示意化压缩，返回 (x, compressed_y, original_y)。"""
    ys = [p[1] for p in data_points]

    def _log_compress(y: float) -> float:
        if y >= 0:
            return math.log1p(y)
        return -math.log1p(-y)

    log_vals = [_log_compress(y) for y in ys]
    max_diff = max(
        (abs(log_vals[i] - log_vals[i - 1]) for i in range(1, len(log_vals))),
        default=0,
    )
    range_v = max(log_vals) - min(log_vals) or 1
    # 若 log 压缩后仍跨度过大，则归一化到 0-200 固定范围
    if max_diff > 50 or range_v > 100:
        min_v = min(log_vals)
        compressed = [(v - min_v) / range_v * 200 for v in log_vals]
    else:
        compressed = log_vals

    return [(data_points[i][0], compressed[i], ys[i]) for i in range(len(data_points))]


def _format_schematic_num(value: float) -> str:
    """格式化示意化图表中的数值标签。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.3g}"


def _render_schematic_line_chart(points_spec: list[dict], k: float = 50,
                                 width: int = 400, height: int = 300) -> str:
    """使用压缩后的 y 坐标绘制示意化折线图，并在数据点旁标注真实值。"""
    data_points = [
        (eval_expression(str(p.get("x", "0")), k),
         eval_expression(str(p.get("y", "0")), k))
        for p in points_spec
    ]
    compressed = _schematic_compress_y(data_points)

    xs = [p[0] for p in compressed]
    cs = [p[1] for p in compressed]
    min_x, max_x = min(xs), max(xs)
    min_c, max_c = min(cs), max(cs)

    pad = 40
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad
    x_range = (max_x - min_x) or 1
    c_range = (max_c - min_c) or 1

    def svg_x(x: float) -> float:
        return pad + (x - min_x) / x_range * plot_w

    def svg_y(c: float) -> float:
        return height - pad - (c - min_c) / c_range * plot_h

    lines = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
    )
    lines.append(f'<rect width="{width}" height="{height}" fill="white"/>')

    left, right = pad, width - pad
    bottom, top = height - pad, pad
    # 坐标轴
    lines.append(
        f'<line x1="{left:.1f}" y1="{bottom:.1f}" x2="{right:.1f}" y2="{bottom:.1f}" '
        'style="stroke:#333;stroke-width:2"/>'
    )
    lines.append(
        f'<line x1="{left:.1f}" y1="{bottom:.1f}" x2="{left:.1f}" y2="{top:.1f}" '
        'style="stroke:#333;stroke-width:2"/>'
    )

    # 折线
    pts = " ".join(f"{svg_x(x):.1f},{svg_y(c):.1f}" for x, c, _ in compressed)
    lines.append(
        f'<polyline points="{pts}" style="stroke:#222;stroke-width:2;fill:none"/>'
    )

    # 数据点与真实值标注
    for x, c, y in compressed:
        px, py = svg_x(x), svg_y(c)
        lines.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="#222"/>')
        label = _format_schematic_num(y)
        lines.append(
            f'<text x="{px:.1f}" y="{py - 8:.1f}" font-size="10" fill="#333" '
            f'text-anchor="middle">{label}</text>'
        )

    # y 轴刻度（基于压缩后的值）
    tick_count = 5
    for i in range(tick_count + 1):
        ratio = i / tick_count
        c_val = min_c + ratio * c_range
        y_pos = svg_y(c_val)
        lines.append(
            f'<line x1="{left:.1f}" y1="{y_pos:.1f}" x2="{left - 5:.1f}" y2="{y_pos:.1f}" '
            'style="stroke:#333;stroke-width:1"/>'
        )
        lines.append(
            f'<text x="{left - 8:.1f}" y="{y_pos + 3:.1f}" font-size="9" fill="#333" '
            f'text-anchor="end">{_format_schematic_num(c_val)}</text>'
        )

    # x 轴刻度
    for i in range(tick_count + 1):
        ratio = i / tick_count
        x_val = min_x + ratio * x_range
        x_pos = svg_x(x_val)
        lines.append(
            f'<line x1="{x_pos:.1f}" y1="{bottom:.1f}" x2="{x_pos:.1f}" y2="{bottom + 5:.1f}" '
            'style="stroke:#333;stroke-width:1"/>'
        )
        lines.append(
            f'<text x="{x_pos:.1f}" y="{bottom + 16:.1f}" font-size="9" fill="#333" '
            f'text-anchor="middle">{_format_schematic_num(x_val)}</text>'
        )

    # 示意图说明
    lines.append(
        f'<text x="{width / 2:.1f}" y="{top - 10:.1f}" font-size="12" fill="#666" '
        'text-anchor="middle">示意图，非真实比例</text>'
    )
    lines.append('</svg>')
    return "\n".join(lines)


# 失败占位的签名文本。assembler.assemble 在「无 template 且无 components」时返回一张
# 灰底卡片（带 viewBox 和 <text>），只打 WARNING 日志、不抛错。仅靠「viewBox + 绘图标签」
# 的正则存在性判断**必然放行**它（探针实测 _has_drawing_content(占位) == True），于是占位
# 被当正式图落盘并返回成功 URL（FreqErr [失败占位伪成功]）。
_FAILURE_PLACEHOLDER_MARKERS = ("无可用组件，无法生成示意图",)


def _is_failure_placeholder(svg: str) -> bool:
    """是否为已知的失败占位卡片。

    与空画布不同：占位带 viewBox 与 <text>，正则质检看不出来，必须按签名文本判。
    """
    text = str(svg or "")
    return any(marker in text for marker in _FAILURE_PLACEHOLDER_MARKERS)


def _is_experiment_svg(svg: str, spec: dict | None = None) -> bool:
    """判断这张图是不是「物化实验图」。

    用途：质量自检里数学图会按「只用黑白灰」判配色，而实验图允许彩色（液体、
    火焰、试剂）。判据取三选一，宁可判成实验图（少报）也不要把彩色实验图误判成
    数学图缺陷 —— 误杀合法图比放过可疑图更糟。
    """
    text = str(svg or "")
    if "#f5f0e8" in text:          # 实验图的米白画布底色
        return True
    if isinstance(spec, dict) and (spec.get("template") or spec.get("components")):
        return True
    return False


_STYLE_ALLOWED_PROPS = frozenset({
    "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "stroke-miterlimit",
    "fill", "fill-opacity", "fill-rule",
    "opacity", "font-size", "font-family", "font-style", "font-weight",
    "text-anchor", "dominant-baseline", "letter-spacing",
    "marker-start", "marker-mid", "marker-end",
    "vector-effect",
})
# 允许在值里出现 url(#...) 的属性（只可能是箭头标记引用）
_STYLE_VALUE_URL_PROPS = frozenset({"marker-start", "marker-mid", "marker-end"})
_STYLE_UNSAFE_TOKENS = (
    "javascript:", "data:", "http:", "https:", "//",
    "@import", "expression(", "behavior:", "-moz-binding",
)


def sanitize_style_attr(value: str) -> str:
    """保留 `style` 属性里**白名单内的 CSS 声明**，其余丢弃。

    ## 为什么不能像原来那样把 `style` 整条删掉（2026-09-12 实测）

    坐标推理 / 折线示意这两条路径产出的图形，**笔画全部写在 `style` 里**：

        <line x1="40" y1="270" x2="360" y2="30"
              style="stroke:#333;stroke-width:2;fill:none"/>

    而 `_sanitize_svg` 的属性白名单里没有 `style`，于是被整条删除，落盘变成：

        <line x1="40" y1="270" x2="360" y2="30" />

    没有 `stroke` 的 `<line>` 在 SVG 里等价于 `stroke:none` —— **线的完全不显示**。
    学生看到的是一张只有字母和刻度数字的白图，而接口、数据库、前端全都报成功。
    这是"教错学生"级别的静默失败，且**任何现有测试都发现不了**（没人渲染它）。

    现在改为按 CSS 属性白名单保留：笔画、颜色、字号、定位全部留下，
    危险构造（外部 URL、`expression()`、`@import`、`behavior:`）逐条丢弃。
    """
    out = []
    for part in str(value or "").split(";"):
        if ":" not in part:
            continue
        prop, _, val = part.partition(":")
        prop = prop.strip().lower()
        val = val.strip()
        if not prop or not val or prop not in _STYLE_ALLOWED_PROPS:
            continue
        low = val.lower().replace(" ", "")
        if any(tok in low for tok in _STYLE_UNSAFE_TOKENS):
            continue
        if "url(" in low:
            if prop not in _STYLE_VALUE_URL_PROPS or not re.fullmatch(
                    r"url\(#[a-zA-Z0-9_.:-]+\)", val):
                continue
        out.append(f"{prop}:{val}")
    return ";".join(out)


class DiagramService:

    def __init__(self):
        # 按 svg 文件路径保护写操作，避免同一文件并发写入导致损坏/时间戳不一致
        # 使用 WeakValueDictionary 防止长期运行后锁对象无限累积
        self._file_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._file_locks_lock = asyncio.Lock()
        # R23：最近一次质量自检结果（路径 -> 问题列表），供 /api/diagram/check 读取。
        self._quality_issues: dict[str, list[str]] = {}

    async def _acquire_file_lock(self, svg_path: str) -> asyncio.Lock:
        async with self._file_locks_lock:
            lock = self._file_locks.get(svg_path)
            if lock is None:
                lock = asyncio.Lock()
                self._file_locks[svg_path] = lock
            return lock

    async def _write_svg(self, svg_path: str, svg: str, spec: dict = None):
        """原子写 SVG、可选 spec 和时间戳，避免并发写入损坏。
        所有落盘 SVG 均经过 _sanitize_svg 二次消毒，防止 AI/用户输入注入事件或脚本。
        """
        svg = self._sanitize_svg(svg)
        async with await self._acquire_file_lock(svg_path):
            directory = os.path.dirname(svg_path)
            os.makedirs(directory, exist_ok=True)
            temp_paths = []
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as stream:
                    stream.write(svg)
                    stream.flush()
                    os.fsync(stream.fileno())
                    svg_temp = stream.name
                    temp_paths.append(svg_temp)
                spec_temp = None
                if spec is not None:
                    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as stream:
                        json.dump(spec, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                        spec_temp = stream.name
                        temp_paths.append(spec_temp)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as stream:
                    stream.write(str(int(time.time())))
                    stream.flush()
                    os.fsync(stream.fileno())
                    ts_temp = stream.name
                    temp_paths.append(ts_temp)
                os.replace(svg_temp, svg_path)
                if spec_temp:
                    os.replace(spec_temp, svg_path + ".spec.json")
                os.replace(ts_temp, svg_path + ".ts")
            finally:
                for temp_path in temp_paths:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            logger.warning("Failed to remove temporary SVG file: %s", temp_path)

        # ── 质量自检（R23 接线，此前 _validate_svg_quality 是**零调用点**的死代码）──
        # 为什么必须挂在统一落盘口：R22 实测过「消毒把 style 删掉 → 图上一条线都看不见」，
        # 而接口 200 / 数据库 done / 前端正常显示，只有肉眼能发现。
        # 为什么**只观测不拒收**：判据里的数学图配色白名单对彩色实验图并不适用，
        # 直接拿它当门禁会把合法实验图拦掉。这里记录并暴露给 /api/diagram/check。
        try:
            issues = self._validate_svg_quality(
                svg_path, is_experiment=_is_experiment_svg(svg, spec))
            self._quality_issues[svg_path] = list(issues)
            if issues:
                logger.warning("SVG 质量自检 %d 项问题 %s: %s",
                               len(issues), svg_path, "；".join(issues[:5]))
        except Exception as exc:  # 自检自身绝不拖垮落盘
            logger.warning("SVG 质量自检执行失败 %s: %s", svg_path, exc)
        return svg_path

    def validate_diagram(self, question_id: str, index: int = 0) -> list[str]:
        """对已落盘的示意图跑质量自检，返回问题列表（供 /api/diagram/check 使用）。"""
        path = os.path.join(QUESTIONS_DIR, question_id, f"diagram_{index}.svg")
        if not os.path.exists(path):
            return ["SVG文件不存在"]
        cached = self._quality_issues.get(path)
        if cached is not None:
            return list(cached)
        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(65536)
        except OSError:
            head = ""
        issues = self._validate_svg_quality(
            path, is_experiment=_is_experiment_svg(head, None))
        self._quality_issues[path] = list(issues)
        return issues

    async def save_reference_svg(self, question_id: str, svg_code: str) -> dict:
        """校验并保存 OCR 视觉模型复刻的原题参考 SVG。

        参考图与解题阶段生成的 diagram_N.svg 分离，避免重新绘图覆盖原题事实。
        返回 {path, svg}，path 为可直接展示的受控 storage URL。
        """
        if not _is_valid_question_id(question_id):
            raise ValueError("非法的 question_id")
        if not isinstance(svg_code, str):
            raise ValueError("参考 SVG 不是文本")
        raw = svg_code.strip()
        if len(raw) > 100_000:
            raise ValueError("参考 SVG 超过 100KB 限制")

        # 允许模型使用 markdown 代码块，但只接受首个完整 SVG 文档。
        match = re.search(r"<svg\b[\s\S]*?</svg>", raw, flags=re.IGNORECASE)
        if not match:
            raise ValueError("视觉模型未返回完整 SVG")
        cleaned = self._sanitize_svg(match.group(0))

        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise ValueError(f"参考 SVG XML 无效: {exc}") from exc

        def _local_name(value: str) -> str:
            return value.rsplit("}", 1)[-1]

        if _local_name(root.tag).lower() != "svg":
            raise ValueError("参考图根节点必须是 svg")

        view_box = root.attrib.get("viewBox", "")
        try:
            vb = [float(v) for v in re.split(r"[\s,]+", view_box.strip()) if v]
        except (TypeError, ValueError):
            vb = []
        if len(vb) != 4 or vb[2] <= 0 or vb[3] <= 0 or vb[2] > 10_000 or vb[3] > 10_000:
            raise ValueError("参考 SVG 必须包含有效且合理的 viewBox")

        allowed_tags = {
            "svg", "g", "path", "line", "polyline", "polygon", "rect",
            "circle", "ellipse", "text", "tspan",
        }
        allowed_attrs = {
            "xmlns", "viewBox", "width", "height", "preserveAspectRatio",
            "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
            "d", "points", "transform", "fill", "stroke", "stroke-width",
            "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity",
            "font-size", "font-family", "font-style", "font-weight", "text-anchor",
            "dominant-baseline", "dx", "dy", "vector-effect",
        }
        safe_colors = {"black", "white", "none", "currentcolor", "#000", "#000000", "#fff", "#ffffff"}

        def _clean_tree(parent):
            for child in list(parent):
                tag = _local_name(child.tag).lower()
                if tag not in allowed_tags:
                    parent.remove(child)
                    continue
                for attr in list(child.attrib):
                    if _local_name(attr) not in allowed_attrs:
                        del child.attrib[attr]
                for color_attr in ("fill", "stroke"):
                    if color_attr in child.attrib:
                        value = child.attrib[color_attr].strip().lower()
                        if value not in safe_colors:
                            child.attrib[color_attr] = "black"
                _clean_tree(child)

        for attr in list(root.attrib):
            if _local_name(attr) not in allowed_attrs:
                del root.attrib[attr]
        for color_attr in ("fill", "stroke"):
            if color_attr in root.attrib and root.attrib[color_attr].strip().lower() not in safe_colors:
                root.attrib[color_attr] = "black"
        _clean_tree(root)
        root.set("viewBox", " ".join(f"{v:g}" for v in vb))
        root.set("width", f"{vb[2]:g}")
        root.set("height", f"{vb[3]:g}")
        root.set("preserveAspectRatio", "xMidYMid meet")
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        validated = ET.tostring(root, encoding="unicode")
        if not self._has_drawing_content(validated):
            raise ValueError("参考 SVG 不包含有效绘图元素")

        folder = os.path.join(QUESTIONS_DIR, question_id)
        os.makedirs(folder, exist_ok=True)
        svg_path = os.path.join(folder, "reference.svg")
        await self._write_svg(svg_path, validated)
        return {
            "path": f"/storage/questions/{question_id}/reference.svg",
            "svg": validated,
        }

    def _write_timestamp(self, svg_path: str):
        """Write a .ts timestamp file alongside the SVG file.
        优先从SVG注释中提取时间戳，确保与SVG内嵌一致。
        """
        try:
            ts = int(time.time())
            # 尝试从SVG注释 <!-- ts:... --> 中提取（只读前64KB避免大文件）
            if os.path.exists(svg_path):
                with open(svg_path, "r", encoding="utf-8") as f:
                    svg_text = f.read(65536)
                import re
                m = re.search(r'<!--\s*ts:(\d+)\s*-->', svg_text)
                if m:
                    ts = int(m.group(1))
            ts_path = svg_path + ".ts"
            with open(ts_path, "w", encoding="utf-8") as f:
                f.write(str(ts))
        except Exception as e:
            logger.warning("Failed to write timestamp for %s: %s", svg_path, e)

    async def generate_diagram(self, question_id: str, prompt: str, index: int = 0, spec_override: dict = None) -> str:
        """
        生成示意图。新版默认流程（direct）：
        1. DeepSeek 先推理验证图形逻辑
        2. 直接生成完整可渲染 SVG 代码
        旧版（coord）：坐标→连线→SVG 逐步模式

        若传入 spec_override（来自编辑器手动摆放），优先按该 spec 渲染并落盘，
        不走 AI 生成，以保证用户编辑的装置图被完整保存。
        """
        if not _is_valid_question_id(question_id):
            raise HTTPException(400, f"非法的 question_id: {question_id}")
        folder = os.path.join(QUESTIONS_DIR, question_id)
        os.makedirs(folder, exist_ok=True)
        svg_path = os.path.join(folder, f"diagram_{index}.svg")

        # ─── 编辑器手动保存优先路径 ───
        if spec_override and isinstance(spec_override, dict) and spec_override.get("components"):
            try:
                from services.diagram_components import render_spec
                from services.diagram_components.assembler import normalize_spec
                spec_override, normalize_warnings = normalize_spec(spec_override)
                for warning in normalize_warnings:
                    logger.warning("spec_override normalized for %s/%d: %s", question_id, index, warning)
                svg, err = render_spec(spec_override)
                if err or not svg:
                    logger.warning("spec_override render failed for %s/%d: %s", question_id, index, err)
                    raise HTTPException(500, f"spec_override 渲染失败: {err}")
                # 保留编辑器传入的完整字段，避免 viewBox/template/custom_ports/title 等信息丢失
                spec_to_save = {
                    "components": spec_override.get("components", []),
                    "uf": spec_override.get("uf", {"parent": {}, "ratio": {}}),
                    "connection_lines": spec_override.get("connection_lines", []),
                }
                for extra_key in ["viewBox", "template", "custom_ports", "title", "params"]:
                    if extra_key in spec_override:
                        spec_to_save[extra_key] = spec_override[extra_key]
                await self._write_svg(svg_path, svg, spec=spec_to_save)
                logger.info("Saved spec_override diagram for %s/%d", question_id, index)
                return f"/storage/questions/{question_id}/diagram_{index}.svg"
            except HTTPException:
                raise
            except Exception as e:
                logger.warning("spec_override save failed for %s/%d: %s", question_id, index, e)
                raise HTTPException(500, f"保存手动编辑的图失败: {e}")

        # 函数图像优先走精确渲染链路
        func_path = await self._generate_function_graph(question_id, prompt, index)
        if func_path:
            return func_path

        mode = _svg_mode()
        logger.info("Diagram mode: %s for %s/%d", mode, question_id, index)

        if mode == "direct":
            # New mode: reasoning-first direct SVG generation
            result = await self._generate_svg_reasoning_first(question_id, prompt, index)
            if result:
                return result
            logger.info("direct SVG mode failed for %s/%d, falling back to coord mode", question_id, index)

        # Try component-based approach for chemistry/physics/structured diagrams
        if "化学" in prompt or "实验" in prompt or "装置" in prompt or "物理" in prompt or "力" in prompt or "滑轮" in prompt:
            result = await self._generate_component_based(question_id, prompt, index)
            if result:
                return result

        # Default/Old mode: coordinate-based or fallback
        try:
            spec = await self._infer_coordinates(question_id, prompt, index)
            if spec and spec.get("points"):
                points = spec["points"]
                lines = spec.get("lines", [])
                circles = spec.get("circles", [])

                if not lines and len(points) >= 2:
                    lines = self._auto_lines(points)

                k = spec.get("params", {}).get("k", {}).get("default", 50)

                # 对价格/销量等折线图的极端斜率进行示意化绘制
                if any(kw in prompt for kw in _SCHEMATIC_KEYWORDS) and len(points) >= 2:
                    data_points = [
                        (eval_expression(str(p.get("x", "0")), k),
                         eval_expression(str(p.get("y", "0")), k))
                        for p in points
                    ]
                    if _is_extreme_slope_chart(data_points):
                        svg = _render_schematic_line_chart(points, k)
                        await self._write_svg(svg_path, svg)
                        self._report_tools_needed(lines, points)
                        return f"/storage/questions/{question_id}/diagram_{index}.svg"

                coords = calc_all_points(points, k)
                svg = to_svg(coords, lines, circles=circles, show_points=True)

                await self._write_svg(svg_path, svg)
                self._report_tools_needed(lines, points)
                return f"/storage/questions/{question_id}/diagram_{index}.svg"

        except Exception as e:
            logger.info("Coordinate inference failed for diagram %s/%d, falling back to direct SVG: %s",
                        question_id, index, e)
            self._note_agent_need(f"坐标推理失败 (diagram {index}): {e}")

        # Final fallback: old direct SVG generation
        return await self._generate_svg_direct(question_id, prompt, index)

    async def _generate_svg_reasoning_first(self, question_id: str, prompt: str, index: int) -> str:
        """New mode: AI first reasons about diagram validity, then generates complete SVG."""
        from services.ai_service import ai_service
        folder = os.path.join(QUESTIONS_DIR, question_id)
        svg_path = os.path.join(folder, f"diagram_{index}.svg")

        # Get image + visual hint in one DB call
        raw_b64, raw_mime, visual_hint = await self._get_image_and_hint(question_id)

        reasoning_prompt = (
            "你是图形生成专家。生成SVG示意图步骤：**先推理打草稿，再输出SVG**。\n\n"
            "## 第一步：推理（必须输出）\n"
            "1. 图形类型（几何证明/函数图像/物理化学装置）\n"
            "2. 列出关键点精确坐标，如 A(50,150), B(300,150)，画布 400x300\n"
            "3. 列出线段：实线/虚线，以及所有文字标签的位置\n\n"
            "## 第二步：SVG（严格按推理输出）\n"
            "【极简与观感要求】\n"
            "1. 必须极其简单、清晰、直观。去除所有不必要的装饰、颜色和复杂的填充！\n"
            "2. viewBox='0 0 400 300'。不需要背景矩形，默认透明或纯白即可。\n"
            "3. 线条使用纯黑或深灰（stroke=\"#222\" stroke-width=\"2\"），辅助线使用虚线（stroke-dasharray=\"4,4\" stroke=\"#666\"）。\n"
            "4. 文字标签（如 A, B, C）使用 <text font-size=\"16\" font-family=\"sans-serif\" fill=\"#111\">，必须放置在图形外侧，绝对禁止与线条重叠！\n"
            "5. 直角使用简单的路径 ┐，不要画复杂的直角符号。平行符号用简单的箭头。\n"
            "6. 物理/化学图也必须保持极简的线框风格（黑白线条），不要尝试画写实的彩色仪器！\n\n"
            f"【图形需求】{prompt}\n"
            f"{visual_hint}\n\n"
            "先输出推理草稿（坐标+线段+标签），然后直接输出简洁优美的SVG代码。"
        )
        try:
            svg_code = await ai_service.deepseek_chat(
                [{"role": "user", "content": reasoning_prompt}],
                max_tokens=16384, temperature=0.3, scope="diagram"
            )
            import re as _re
            svg_match = _re.search(r'<svg[\s\S]*?</svg>', svg_code, _re.IGNORECASE)
            if svg_match:
                svg_code = svg_match.group(0)

            if '<svg' in svg_code and '</svg>' in svg_code:
                # Security sanitization
                svg_code = self._sanitize_svg(svg_code)
                if not self._has_drawing_content(svg_code):
                    raise ValueError("模型返回的 SVG 不含有效绘图元素")
                await self._write_svg(svg_path, svg_code)

                # GLM vision review for math diagrams (if original image exists)
                # 视觉复核走 MiMo-first 统一助手（Fact.md 模型分工定规，2026-09-09）
                if raw_b64:
                    try:
                        review = await ai_service.vision_mimo_first(raw_b64, (
                            "查看原题图片，并对照下面待检查 SVG 的代码，检查示意图是否正确。\n"
                            "检查：1.标注字母齐全 2.比例/位置正确 3.直角/虚线等符号\n"
                            f"【待检查 SVG】\n{svg_code[:12000]}\n"
                            '正确→{"verdict":"pass"}  需修正→{"verdict":"fix","issues":"问题"}'
                        ), raw_mime)
                        if review.get("verdict") == "fix":
                            issues = review.get("issues", "")
                            logger.info("GLM: math diagram %s/%d needs fix: %s", question_id, index, issues[:100])
                            # DeepSeek re-renders based on GLM's text feedback
                            fix_prompt = (
                                "你是图形生成专家。先推理坐标再输出SVG。\n"
                                "1.列出所有点坐标 2.列出所有线段 3.列出所有标签 4.输出SVG\n"
                                f"【需要修正的问题】{issues}\n"
                                f"【原需求】{prompt[:300]}\n"
                                "重新生成修正版SVG。务必保持**极简风格**（黑白线条，无多余装饰，文字清晰不重叠）。"
                            )
                            svg_code2 = await ai_service.deepseek_chat(
                                [{"role": "user", "content": fix_prompt}],
                                max_tokens=16384, temperature=0.3, scope="diagram"
                            )
                            svg_match2 = _re.search(r'<svg[\s\S]*?</svg>', svg_code2, _re.IGNORECASE)
                            if svg_match2:
                                fixed_svg = self._sanitize_svg(svg_match2.group(0))
                                if self._has_drawing_content(fixed_svg):
                                    await self._write_svg(svg_path, fixed_svg)
                    except Exception as e:
                        logger.info("GLM review skipped for math %s/%d: %s", question_id, index, e)

                return f"/storage/questions/{question_id}/diagram_{index}.svg"
            else:
                logger.warning("SVG reasoning-first produced invalid SVG for %s/%d", question_id, index)
        except Exception as e:
            logger.warning("SVG reasoning-first failed for %s/%d: %s", question_id, index, e)
        return ""  # signals to fallback

    async def _generate_function_graph(self, question_id: str, prompt: str, index: int) -> str:
        """函数图像 AI 生成 + 精确渲染链路。"""
        # 仅当 prompt 明显涉及函数图像时才走精确渲染链路，避免把普通几何证明题误路由进来
        keywords = ("函数", "函数图像", "f(x)", "抛物线", "绝对值")
        geometry_only_keywords = ("三角形", "四边形", "正方形", "矩形", "菱形", "梯形",
                                  "平行四边形", "多边形", "圆", "全等", "相似")
        has_function_hint = any(kw in prompt for kw in keywords)
        has_geometry_only = any(kw in prompt for kw in geometry_only_keywords)
        if not has_function_hint and not _FUNCTION_YX_RE.search(prompt):
            return ""
        # 如果 prompt 里只有纯几何关键词且没有函数关键词，即使匹配 y=...x 也不走函数图像链路
        if has_geometry_only and not has_function_hint:
            return ""

        from services.ai_service import ai_service
        folder = os.path.join(QUESTIONS_DIR, question_id)
        os.makedirs(folder, exist_ok=True)
        svg_path = os.path.join(folder, f"diagram_{index}.svg")

        user_prompt = (
            "你是函数图像绘图专家。请根据题目描述输出函数图像的数学描述 JSON。\n"
            "规则：\n"
            "1. function_expr 是 Python 可解析的表达式，使用 x 作为变量，例如 \"abs(x**2 - 2*x - 3)\"、\"x**2 + 1\"、\"math.sin(x)\"（可用 math 函数）。\n"
            "2. x_range / y_range 可选；如不提供，后端会自动计算。\n"
            "3. geometries 数组描述需要在函数图像上叠加的几何元素：\n"
            '   - {"type":"point","x":...,"y":...,"label":"A","color":"#c62828"}\n'
            '   - {"type":"segment","x1":...,"y1":...,"x2":...,"y2":...,"style":"dashed"|"solid","color":"#2e7d32"}\n'
            '   - {"type":"circle","x":...,"y":...,"r":...,"style":"dashed"|"solid","color":"#1565c0"}\n'
            '   - {"type":"polygon","points":[[x1,y1],[x2,y2],...],"style":"dashed"|"solid","color":"#6a1b9a"}\n'
            '   - {"type":"text","x":...,"y":...,"text":"...","font_size":12}\n'
            "4. 坐标使用真实数学坐标（不是像素坐标）。\n"
            "输出格式：{\"function_expr\":\"...\",\"x_range\":[-3,3],\"y_range\":[-1,5],\"geometries\":[...]}\n"
            "如果不需要图或无法判断，返回 {\"skip\":true}。\n\n"
            f"题目描述：{prompt}"
        )
        try:
            spec = await ai_service.deepseek_json(
                [{"role": "user", "content": user_prompt}],
                max_tokens=4096, scope="diagram"
            )
        except Exception as e:
            logger.warning("Function graph AI failed for %s/%d: %s", question_id, index, e)
            return ""

        if not spec or not isinstance(spec, dict):
            return ""
        if spec.get("skip"):
            return ""

        svg = self._render_function_graph_spec(spec)
        if not svg:
            return ""

        try:
            await self._write_svg(svg_path, svg)
            return f"/storage/questions/{question_id}/diagram_{index}.svg"
        except Exception as e:
            logger.warning("Function graph save failed for %s/%d: %s", question_id, index, e)
            return ""

    def _render_function_graph_spec(self, spec: dict) -> str:
        """根据结构化 spec 精确渲染函数图像 SVG。"""

        try:
            expr = str(spec.get("function_expr", "")).strip()
            if not expr:
                return ""

            width = 400
            height = 300
            pad = 40
            plot_w = width - 2 * pad
            plot_h = height - 2 * pad

            def _to_range(v):
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    try:
                        low = float(v[0]) if v[0] is not None else None
                    except Exception:
                        low = None
                    try:
                        high = float(v[1]) if v[1] is not None else None
                    except Exception:
                        high = None
                    if low is not None and not math.isfinite(low):
                        low = None
                    if high is not None and not math.isfinite(high):
                        high = None
                    return [low, high]
                return [None, None]

            x_range = _to_range(spec.get("x_range"))
            y_range = _to_range(spec.get("y_range"))

            # 默认 x 范围
            if x_range[0] is None or x_range[1] is None:
                x_range = [-5.0, 5.0]

            # AST 白名单解析并只编译一次，避免 200 次采样重复解析
            try:
                tree = ast.parse(expr, mode="eval")
            except SyntaxError as e:
                raise ValueError("invalid expression") from e

            allowed_nodes = (
                ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
                ast.Call, ast.Attribute, ast.Name, ast.Constant, ast.Load,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
                ast.USub, ast.UAdd, ast.Not,
                ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                ast.And, ast.Or,
            )
            allowed_math_funcs = {
                "sin", "cos", "tan", "asin", "acos", "atan",
                "sinh", "cosh", "tanh",
                "sqrt", "exp", "log", "log10", "log2",
                "pi", "e", "tau", "inf", "nan",
                "ceil", "floor", "fabs", "gcd",
            }

            for node in ast.walk(tree):
                if not isinstance(node, allowed_nodes):
                    raise ValueError(f"disallowed node: {type(node).__name__}")
                if isinstance(node, ast.Name):
                    if node.id not in ("x", "abs", "math"):
                        raise ValueError(f"disallowed name: {node.id}")
                if isinstance(node, ast.Attribute):
                    if not (isinstance(node.value, ast.Name) and node.value.id == "math"):
                        raise ValueError("only math.* attributes allowed")
                    if node.attr not in allowed_math_funcs:
                        raise ValueError(f"disallowed math function: {node.attr}")
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "math"):
                            raise ValueError("only math.* calls allowed")
                        if node.func.attr not in allowed_math_funcs:
                            raise ValueError(f"disallowed math call: {node.func.attr}")
                    elif isinstance(node.func, ast.Name):
                        if node.func.id != "abs":
                            raise ValueError("only abs() calls allowed")
                    else:
                        raise ValueError("disallowed call target")

            _compiled_expr = compile(tree, "<string>", "eval")

            def safe_eval(x: float):
                return eval(_compiled_expr, {"__builtins__": {}}, {"x": x, "math": math, "abs": abs})

            def sample_function(xr):
                n = 200
                pts = []
                y_vals = []
                x_min, x_max = xr
                if x_max <= x_min:
                    x_max = x_min + 1.0
                for i in range(n):
                    x = x_min + i * (x_max - x_min) / (n - 1)
                    try:
                        y = safe_eval(x)
                        if not isinstance(y, (int, float)):
                            continue
                        if not math.isfinite(y):
                            continue
                        pts.append((float(x), float(y)))
                        y_vals.append(float(y))
                    except Exception:
                        continue
                return pts, y_vals

            points, y_vals = sample_function(x_range)
            if not points:
                return ""

            # 自动 y 范围
            if (y_range[0] is None or y_range[1] is None) and y_vals:
                y_min_auto = min(y_vals)
                y_max_auto = max(y_vals)
                y_span = y_max_auto - y_min_auto or 1.0
                y_margin = 0.15 * y_span
                y_lo = y_min_auto - y_margin
                y_hi = y_max_auto + y_margin
                if y_range[0] is None:
                    y_range[0] = y_lo
                if y_range[1] is None:
                    y_range[1] = y_hi

            # 收集几何关键点，用于扩展范围
            geo_xs = []
            geo_ys = []
            geometries = spec.get("geometries") or []
            for g in geometries:
                if not isinstance(g, dict):
                    continue
                try:
                    gtype = g.get("type", "")
                    if gtype in ("point", "text"):
                        geo_xs.append(float(g.get("x")))
                        geo_ys.append(float(g.get("y")))
                    elif gtype == "segment":
                        geo_xs.extend([float(g.get("x1")), float(g.get("x2"))])
                        geo_ys.extend([float(g.get("y1")), float(g.get("y2"))])
                    elif gtype == "circle":
                        cx = float(g.get("x"))
                        cy = float(g.get("y"))
                        r = float(g.get("r"))
                        geo_xs.extend([cx - r, cx + r])
                        geo_ys.extend([cy - r, cy + r])
                    elif gtype == "polygon":
                        for p in g.get("points", []):
                            geo_xs.append(float(p[0]))
                            geo_ys.append(float(p[1]))
                except Exception:
                    continue

            def extend_range(rng, vals, auto: bool):
                if not vals or not auto:
                    return rng
                vals = [v for v in vals if isinstance(v, (int, float)) and math.isfinite(v)]
                if not vals:
                    return rng
                lo, hi = min(vals), max(vals)
                span = hi - lo or 1.0
                margin = 0.15 * span
                new_lo = min(rng[0], lo - margin) if rng[0] is not None else lo - margin
                new_hi = max(rng[1], hi + margin) if rng[1] is not None else hi + margin
                return [new_lo, new_hi]

            x_was_auto = _to_range(spec.get("x_range"))[0] is None
            y_was_auto = _to_range(spec.get("y_range"))[0] is None
            x_range = extend_range(x_range, geo_xs, x_was_auto)
            y_range = extend_range(y_range, geo_ys, y_was_auto)

            if y_range[0] is None or y_range[1] is None:
                y_range = [-1.0, 1.0]

            # 如果 x 范围因几何点扩展，重新采样函数
            if x_was_auto and geo_xs:
                points, y_vals = sample_function(x_range)
                if y_was_auto and y_vals:
                    y_range = extend_range(y_range, y_vals, True)

            x_span = x_range[1] - x_range[0] or 1.0
            y_span = y_range[1] - y_range[0] or 1.0

            def svg_x(x: float) -> float:
                return pad + (x - x_range[0]) / x_span * plot_w

            def svg_y(y: float) -> float:
                return height - pad - (y - y_range[0]) / y_span * plot_h

            ts = int(time.time())
            lines = []
            lines.append(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
                f'width="{width}" height="{height}">'
            )
            lines.append(f'<rect width="{width}" height="{height}" fill="white"/>')

            # 网格
            try:
                nx, ny = 8, 6
                x_step = x_span / nx
                y_step = y_span / ny
                if x_step > 0 and y_step > 0:
                    for i in range(nx + 1):
                        x_val = x_range[0] + i * x_step
                        sx = svg_x(x_val)
                        lines.append(
                            f'<line x1="{sx:.1f}" y1="{pad:.1f}" x2="{sx:.1f}" y2="{height - pad:.1f}" '
                            'stroke="#e0e0e0" stroke-width="1" stroke-dasharray="2,2"/>'
                        )
                    for i in range(ny + 1):
                        y_val = y_range[0] + i * y_step
                        sy = svg_y(y_val)
                        lines.append(
                            f'<line x1="{pad:.1f}" y1="{sy:.1f}" x2="{width - pad:.1f}" y2="{sy:.1f}" '
                            'stroke="#e0e0e0" stroke-width="1" stroke-dasharray="2,2"/>'
                        )
            except Exception:
                pass

            # 坐标轴
            origin_x = svg_x(0) if x_range[0] <= 0 <= x_range[1] else (pad if 0 > x_range[1] else width - pad)
            origin_y = svg_y(0) if y_range[0] <= 0 <= y_range[1] else (height - pad if 0 < y_range[0] else pad)

            # x 轴
            lines.append(
                f'<line x1="{pad:.1f}" y1="{origin_y:.1f}" x2="{width - pad - 6:.1f}" y2="{origin_y:.1f}" '
                'stroke="#333" stroke-width="2"/>'
            )
            lines.append(
                f'<polygon points="{width - pad:.1f},{origin_y:.1f} {width - pad - 6:.1f},{origin_y - 3:.1f} '
                f'{width - pad - 6:.1f},{origin_y + 3:.1f}" fill="#333"/>'
            )
            # y 轴
            lines.append(
                f'<line x1="{origin_x:.1f}" y1="{height - pad:.1f}" x2="{origin_x:.1f}" y2="{pad + 6:.1f}" '
                'stroke="#333" stroke-width="2"/>'
            )
            lines.append(
                f'<polygon points="{origin_x:.1f},{pad:.1f} {origin_x - 3:.1f},{pad + 6:.1f} '
                f'{origin_x + 3:.1f},{pad + 6:.1f}" fill="#333"/>'
            )

            # 函数曲线
            if points:
                pts = " ".join(f"{svg_x(x):.1f},{svg_y(y):.1f}" for x, y in points)
                lines.append(
                    f'<polyline points="{pts}" fill="none" stroke="#1565c0" stroke-width="2"/>'
                )

            # 几何元素
            def _geo_float(v):
                try:
                    f = float(v)
                    if not math.isfinite(f):
                        return None
                    return f
                except Exception:
                    return None

            def _geo_point_pair(p):
                if not isinstance(p, (list, tuple)) or len(p) < 2:
                    return None, None
                x = _geo_float(p[0])
                y = _geo_float(p[1])
                if x is None or y is None:
                    return None, None
                return x, y

            for g in geometries:
                if not isinstance(g, dict):
                    continue
                gtype = g.get("type", "")
                try:
                    if gtype == "point":
                        x = _geo_float(g.get("x"))
                        y = _geo_float(g.get("y"))
                        if x is None or y is None:
                            continue
                        label = str(g.get("label", ""))
                        color = _sanitize_svg_color(str(g.get("color", "#c62828")), default="#c62828")
                        sx, sy = svg_x(x), svg_y(y)
                        lines.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="4" fill="{color}"/>')
                        if label:
                            lines.append(
                                f'<text x="{sx + 7:.1f}" y="{sy - 7:.1f}" font-size="12" fill="{color}" '
                                f'font-family="sans-serif">{html.escape(label, quote=True)}</text>'
                            )
                    elif gtype == "segment":
                        x1 = _geo_float(g.get("x1"))
                        y1 = _geo_float(g.get("y1"))
                        x2 = _geo_float(g.get("x2"))
                        y2 = _geo_float(g.get("y2"))
                        if None in (x1, y1, x2, y2):
                            continue
                        style = _sanitize_svg_style(str(g.get("style", "solid")))
                        color = _sanitize_svg_color(str(g.get("color", "#2e7d32")), default="#2e7d32")
                        dash = ' stroke-dasharray="5,5"' if style == "dashed" else ""
                        lines.append(
                            f'<line x1="{svg_x(x1):.1f}" y1="{svg_y(y1):.1f}" x2="{svg_x(x2):.1f}" y2="{svg_y(y2):.1f}" '
                            f'stroke="{color}" stroke-width="2"{dash}/>'
                        )
                    elif gtype == "circle":
                        x = _geo_float(g.get("x"))
                        y = _geo_float(g.get("y"))
                        r = _geo_float(g.get("r"))
                        if x is None or y is None or r is None or r < 0:
                            continue
                        style = _sanitize_svg_style(str(g.get("style", "solid")))
                        color = _sanitize_svg_color(str(g.get("color", "#1565c0")), default="#1565c0")
                        dash = ' stroke-dasharray="5,5"' if style == "dashed" else ""
                        sx, sy = svg_x(x), svg_y(y)
                        x_scale = plot_w / x_span
                        y_scale = plot_h / y_span
                        sr = r * ((x_scale + y_scale) / 2)
                        sr = min(max(sr, 0.0), max(width, height) * 2)
                        lines.append(
                            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{sr:.1f}" fill="none" stroke="{color}" '
                            f'stroke-width="2"{dash}/>'
                        )
                    elif gtype == "polygon":
                        pts_raw = g.get("points", [])
                        if not pts_raw:
                            continue
                        style = _sanitize_svg_style(str(g.get("style", "solid")))
                        color = _sanitize_svg_color(str(g.get("color", "#6a1b9a")), default="#6a1b9a")
                        dash = ' stroke-dasharray="5,5"' if style == "dashed" else ""
                        pts = []
                        valid_poly = True
                        for p in pts_raw:
                            px, py = _geo_point_pair(p)
                            if px is None:
                                valid_poly = False
                                break
                            pts.append(f"{svg_x(px):.1f},{svg_y(py):.1f}")
                        if not valid_poly:
                            continue
                        lines.append(
                            f'<polygon points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2"{dash}/>'
                        )
                    elif gtype == "text":
                        x = _geo_float(g.get("x"))
                        y = _geo_float(g.get("y"))
                        if x is None or y is None:
                            continue
                        text = str(g.get("text", ""))
                        try:
                            font_size = int(g.get("font_size", 12))
                        except (TypeError, ValueError):
                            font_size = 12
                        font_size = max(6, min(72, font_size))
                        sx, sy = svg_x(x), svg_y(y)
                        lines.append(
                            f'<text x="{sx:.1f}" y="{sy:.1f}" font-size="{font_size}" fill="#333" '
                            f'font-family="sans-serif">{html.escape(text, quote=True)}</text>'
                        )
                except Exception:
                    continue

            lines.append(f'<!-- ts:{ts} -->')
            lines.append('</svg>')
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Function graph render failed: %s", e)
            return ""

    async def _get_image_and_hint(self, question_id: str) -> tuple[str, str, str]:
        """Single DB call: returns (raw_image_base64, mime_type, visual_hint_text)."""
        try:
            from models.database import async_session
            from models.models import Question
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if not q:
                    return "", "", ""
                b64, mime = "", ""
                if q.raw_image_path and os.path.exists(q.raw_image_path):
                    import base64 as _b64
                    ext = os.path.splitext(q.raw_image_path)[1].lower()
                    mime = {
                        ".png": "image/png", ".webp": "image/webp",
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    }.get(ext, "image/jpeg")
                    with open(q.raw_image_path, "rb") as f:
                        b64 = _b64.b64encode(f.read()).decode()
                hint_parts = []
                if q.diagram_description:
                    hint_parts.append(f"【示意图描述】{q.diagram_description}")
                return b64, mime, "\n".join(hint_parts)
        except Exception as e:
            logger.debug("Failed to get image for %s: %s", question_id, e)
            return "", "", ""

    async def _generate_component_based(self, question_id: str, prompt: str, index: int) -> str:
        """Component: Physics/Chem→AI组件拼装→GLM审查→修正"""
        from services.ai_service import ai_service
        from services.diagram_components import assemble, available_components, available_templates
        folder = os.path.join(QUESTIONS_DIR, question_id)
        svg_path = os.path.join(folder, f"diagram_{index}.svg")
        raw_b64, raw_mime, visual_hint = await self._get_image_and_hint(question_id)
        is_chem_phys = any(kw in prompt for kw in ["化学","实验","装置","物理","力","滑轮","斜面"])

        # Build component catalog for AI prompt (with port details)
        all_comps = available_components()
        comp_list = ", ".join([f"{c['type']}({c['name']})" for c in all_comps])
        tpl_list = ", ".join([f"{t['id']}({t['name']})" for t in available_templates()])
        # Port catalog: show each component's available ports
        port_catalog_parts = []
        for c in all_comps:
            if c["ports"]:
                port_catalog_parts.append(f"{c['type']}: {c['ports']}")
        port_catalog = "; ".join(port_catalog_parts)

        # Try GLM vision first (if image available)
        spec_json = None
        if is_chem_phys and raw_b64:
            logger.info("Component: GLM vision for %s/%d", question_id, index)
            try:
                hint = (
                    "查看图片中的实验装置，输出组装JSON。\n"
                    f"可用组件: {comp_list}\n"
                    f"可用模板: {tpl_list}\n"
                    f"组件端口: {port_catalog}\n\n"
                    "策略：\n"
                    "1. 如果是标准实验装置（蒸馏/过滤/电解水等），使用 template 字段引用预置模板\n"
                    "2. 如果是非标准装置，使用 components 逐一定义每个组件的位置\n"
                    "3. 需要标注的部件用 label 字段\n"
                    "4. 有特殊标记的部分用 highlight(red/blue/green/yellow)\n"
                    "5. connections中使用端口对接时，**优先使用上面列出的已有端口**\n"
                    "6. 如果已有端口无法满足连接需求，可以用 custom_ports 自创端口：\n"
                    '   custom_ports: {"组件type": [{"id":"端口名","dx":x偏移,"dy":y偏移,"dir":"方向"}]}\n'
                    "   dir可选: up/down/left/right，dx/dy是相对于组件左上角的偏移量\n"
                    "输出JSON示例：\n"
                    '  {"template":"distillation","title":"蒸馏装置"} 或\n'
                    '  {"viewBox":"0 0 400 300","components":[{"type":"beaker","x":50,"y":80,"label":"烧杯"}],'
                    '"connections":[["beaker","top","funnel","bottom"]],'
                    '"custom_ports":{"beaker":[{"id":"side_left","dx":-5,"dy":20,"dir":"left"}]}}'
                )
                spec_json = await ai_service.vision_mimo_first(raw_b64, hint, raw_mime, parse_json=True)
            except Exception as e:
                logger.warning("GLM vision failed for %s/%d: %s", question_id, index, e)

        # If GLM failed or no image → use DeepSeek text
        if spec_json is None:
            logger.info("Component: DeepSeek text for %s/%d", question_id, index)
            ds_prompt = (
                "你是实验装置/物理示意图专家。输出组装JSON。\n"
                f"可用组件: {comp_list}\n"
                f"可用模板: {tpl_list}\n"
                f"组件端口: {port_catalog}\n\n"
                f"{prompt[:400]}\n{visual_hint[:200]}\n\n"
                "策略：\n"
                "1. 标准实验装置→用 template 字段\n"
                "2. 选项：template字段, 或 components列表\n"
                "3. 用 label 标注、highlight 高亮、connection_lines 画连接线\n"
                "4. connections端口对接时，**优先使用已有端口**（见上面端口列表）\n"
                "5. 如果已有端口不满足需求，可用 custom_ports 自创新端口：\n"
                '   "custom_ports":{"组件type":[{"id":"名称","dx":x,"dy":y,"dir":"up/down/left/right"}]}\n'
                '输出JSON: {"template":"distillation"} 或 {"components":[...],"connections":[...],"custom_ports":{...}}'
            )
            try:
                spec_json = await ai_service.deepseek_json(
                    [{"role":"user","content":ds_prompt}], max_tokens=8192, scope="diagram"
                )
            except Exception as e:
                logger.warning("DeepSeek component failed: %s", e)
                return ""

        if not spec_json:
            return ""

        # Render via new assembler
        try:
            svg_str = assemble(spec_json)
            # 必须显式校验再落盘：空 spec 会让 assemble 返回「无可用组件」占位卡片，
            # 而它带 viewBox+<text>、能骗过旧正则质检 → 被当成功图写入并返回 URL。
            if _is_failure_placeholder(svg_str) or not self._has_drawing_content(svg_str):
                raise ValueError("assembler 产出占位/空内容（无有效组件）")
            await self._write_svg(svg_path, svg_str)
        except Exception as e:
            logger.warning("Assembler failed for %s/%d: %s, fallback to old render", question_id, index, e)
            svg_str, err = _render_components(spec_json)
            if err:
                logger.warning("Old render also failed: %s", err)
                return ""
            if _is_failure_placeholder(svg_str) or not self._has_drawing_content(svg_str):
                logger.warning("Old render also produced placeholder/empty for %s/%d", question_id, index)
                return ""
            await self._write_svg(svg_path, svg_str)

        # GLM vision review: original image vs rendered SVG
        # 视觉复核走 MiMo-first 统一助手（Fact.md 模型分工定规，2026-09-09）
        if raw_b64:
            try:
                review = await ai_service.vision_mimo_first(raw_b64, (
                    "查看原题图片，评估生成的SVG实验装置图是否正确。\n"
                    f"【当前组件】{json.dumps(spec_json, ensure_ascii=False)[:1200]}\n"
                    '正确→{"verdict":"pass"}  需修正→{"verdict":"fix","issues":"具体问题描述"}'
                ), raw_mime)
                if review.get("verdict") == "fix":
                    issues = review.get("issues", "")
                    logger.info("GLM: component diagram %s/%d needs fix: %s", question_id, index, issues[:100])
                    fix_spec = None
                    try:
                        fix_spec = await ai_service.deepseek_json([{"role":"user","content":(
                            f"修正组件拼装JSON。审查意见：{issues}\n原需求：{prompt[:200]}\n当前：{json.dumps(spec_json, ensure_ascii=False)[:1000]}"
                        )}], max_tokens=8192, scope="diagram")
                    except Exception as _fe:
                        logger.warning("GLM fix spec failed for %s/%d: %s", question_id, index, _fe)
                    if fix_spec:
                        try:
                            svg_str2 = assemble(fix_spec)
                            # 重画同样要校验：占位/空内容不应覆盖首版成果
                            if _is_failure_placeholder(svg_str2) or not self._has_drawing_content(svg_str2):
                                raise ValueError("重画产出占位/空内容，保留首版")
                            await self._write_svg(svg_path, svg_str2)
                        except Exception as _re:
                            logger.warning("Re-assemble failed (保留首版): %s", _re)
            except Exception as e:
                logger.info("GLM review skipped: %s", e)

        logger.info("Diagram saved: %s/%d", question_id, index)
        return f"/storage/questions/{question_id}/diagram_{index}.svg"

    async def _infer_coordinates(self, question_id: str, prompt: str, index: int) -> dict:
        """Ask DeepSeek to infer coordinates for the diagram."""
        from services.ai_service import ai_service

        system = (
            "你是几何坐标推理专家。先推算所有坐标，再输出JSON。\n\n"
            "【推理步骤】\n"
            "1. 确定坐标系原点和轴向（通常A(0,0)，x轴沿AB方向）\n"
            "2. 推算每个关键点的坐标表达式（用k表示比例，k默认50px）\n"
            "3. 确定线的样式（solid实线/dashed虚线）和连接关系\n"
            "4. 检查角度、比例是否合理，点是否重叠\n\n"
            "【规则】\n"
            "1. 用比例常数 k 表示长度（默认 k=50 像素）\n"
            "2. 角度使用度数制\n"
            "3. 坐标表达式使用：+ - * / ( ) k sin(deg) cos(deg) tan(deg) sqrt(x)\n"
            "4. lines中的style可选: solid(实线), dashed(虚线), dotted(点线), segment(线段)\n"
            "5. 所有点必须标注字母（如 A、B、C、O 等），SVG中 show_points=true\n"
            "6. 如果题目没有图或不需要图，返回空points数组\n\n"
            '输出JSON: {"reasoning":"建系过程+各点推算","points":[{"id":"A","x":"0","y":"0"},...],"lines":[{"from":"A","to":"B","style":"solid"},...],"circles":[{"center":"O","radius":50,"style":"solid"}或{"center":"O","edge":"B"}],"params":{"k":{"default":50}}}'
        )

        # Try to get GLM's visual description if an image exists
        visual_hint = ""
        try:
            from models.database import async_session
            from models.models import Question
            from sqlalchemy import select
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if q and q.diagram_description:
                    visual_hint = f"\n【图的描述】{q.diagram_description}"
                if q and q.raw_image_path and os.path.exists(q.raw_image_path):
                    visual_hint += f"\n【有原始图片可参考】{q.raw_image_path}"
        except Exception as e:
            logger.debug("Failed to get visual hint for diagram %s: %s", question_id, e)

        result = await ai_service.deepseek_json([
            {"role": "system", "content": system},
            {"role": "user", "content": f"题目：{prompt}{visual_hint}\n请推理坐标。"}
        ], max_tokens=8192, scope="diagram")

        return result

    def _auto_lines(self, points: list) -> list:
        """Auto-generate lines connecting consecutive points."""
        lines = []
        for i in range(len(points) - 1):
            lines.append({"from": points[i]["id"], "to": points[i+1]["id"], "style": "solid"})
        # Close polygon if 3+ points
        if len(points) >= 3:
            lines.append({"from": points[-1]["id"], "to": points[0]["id"], "style": "solid"})
        return lines

    async def _generate_svg_direct(self, question_id: str, prompt: str, index: int) -> str:
        """Fallback: old method - DeepSeek generates SVG directly."""
        from services.ai_service import ai_service

        folder = os.path.join(QUESTIONS_DIR, question_id)
        os.makedirs(folder, exist_ok=True)
        svg_path = os.path.join(folder, f"diagram_{index}.svg")

        system = (
            "你是示意图生成专家。先推理坐标再生成SVG。\n"
            "步骤：1.列出所有点坐标(A(50,150)...) 2.列出所有线段(实线/虚线) 3.列出所有标签 4.输出SVG\n"
            "要求：viewBox='0 0 400 300'，透明或白底，极简黑白线条stroke:#222，虚线stroke:#666 stroke-dasharray:4,4\n"
            "字母用<text font-size='16' font-family='sans-serif'>，必须避免与线条重叠！\n"
            "绝对禁止任何多余的色块填充和复杂装饰，越简单清晰越好。"
        )
        svg_code = await ai_service.deepseek_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": f"生成示意图：{prompt}\n直接输出SVG代码，不要markdown包裹。"}
        ], max_tokens=16384, scope="diagram")

        import re
        match = re.search(r'<svg[\s\S]*?</svg>', svg_code, re.IGNORECASE)
        if match:
            svg_code = match.group(0)
            svg_code = self._sanitize_svg(svg_code)
            if self._has_drawing_content(svg_code):
                await self._write_svg(svg_path, svg_code)
                return f"/storage/questions/{question_id}/diagram_{index}.svg"

        logger.warning("Direct SVG generation produced invalid output for %s/%d", question_id, index)
        return ""

    async def regenerate_diagram(self, question_id: str, prompt: str,
                                  index: int, diagram_type: str = "geometry") -> str:
        """Regenerate a specific diagram."""
        return await self.generate_diagram(question_id, prompt, index)

    async def insert_diagram(self, question_id: str, prompt: str, target_diagram_index: int) -> str:
        """在已有图上插入新组件（不改变已有组件位置）"""
        if not _is_valid_question_id(question_id):
            raise HTTPException(400, f"非法的 question_id: {question_id}")
        from services.ai_service import ai_service
        from services.diagram_components import assemble, available_components

        folder = os.path.join(QUESTIONS_DIR, question_id)
        os.makedirs(folder, exist_ok=True)

        existing_svg_path = os.path.join(folder, f"diagram_{target_diagram_index}.svg")
        spec_path = existing_svg_path + ".spec.json"

        # 1. 读取已有图的 spec 信息
        existing_spec = None
        if os.path.exists(spec_path):
            try:
                with open(spec_path, "r", encoding="utf-8") as f:
                    existing_spec = json.load(f)
            except Exception as e:
                logger.warning("Failed to read spec %s: %s", spec_path, e)

        # 2. 如果没有 spec，调用AI分析已有SVG生成spec
        if existing_spec is None and os.path.exists(existing_svg_path):
            try:
                with open(existing_svg_path, "r", encoding="utf-8") as f:
                    svg_content = f.read()
                analysis_prompt = (
                    "分析下面SVG图中的所有组件，输出组件拼装JSON。\n"
                    "识别每个图形元素的类型、坐标、尺寸和文字标签。\n"
                    "可用基础类型：rect, rect_fill, circle, line, text, arrow\n"
                    f"SVG代码（前4000字符）：\n{svg_content[:4000]}\n\n"
                    '输出JSON格式：{"viewBox":[0,0,400,300],"components":[{"type":"rect","x":0,"y":0,"w":50,"h":50,"label":"A"},...],"connection_lines":[]}'
                )
                analysis = await ai_service.deepseek_json(
                    [{"role": "user", "content": analysis_prompt}],
                    max_tokens=8192, scope="diagram"
                )
                if analysis and analysis.get("components"):
                    existing_spec = analysis
            except Exception as e:
                logger.warning("AI SVG analysis failed for %s: %s", existing_svg_path, e)

        if existing_spec is None:
            existing_spec = {"viewBox": [0, 0, 400, 300], "components": [], "connection_lines": []}

        # 3. 生成要插入的新组件描述
        all_comps = available_components()
        comp_list = ", ".join([f"{c['type']}({c['name']})" for c in all_comps])

        new_parts_prompt = (
            f"在已有示意图上插入新组件。需求：{prompt}\n"
            f"可用组件（含基础图形）：{comp_list}, 以及 rect, rect_fill, circle, line, text, arrow\n"
            "仅输出要新增的组件JSON列表，不要包含已有组件。坐标根据原有图布局合理放置。\n"
            '输出JSON格式：{"components":[{"type":"...","x":...,"y":...,"label":"..."}],"connection_lines":[]}'
        )
        try:
            new_parts = await ai_service.deepseek_json(
                [{"role": "user", "content": new_parts_prompt}],
                max_tokens=8192, scope="diagram"
            )
        except Exception as e:
            logger.warning("Failed to generate new components for insert: %s", e)
            return ""

        if not new_parts or not new_parts.get("components"):
            logger.warning("No new components generated for insert, prompt=%s", prompt[:80])
            return ""

        # 4. 合并 spec（保持已有组件位置不变）
        merged = dict(existing_spec)
        existing_components = list(existing_spec.get("components", []))
        new_components = new_parts.get("components", [])
        merged["components"] = existing_components + new_components

        existing_lines = list(existing_spec.get("connection_lines", []))
        new_lines = new_parts.get("connection_lines", [])
        merged["connection_lines"] = existing_lines + new_lines

        # 5. 调用 assemble 生成新 SVG
        try:
            svg_str = assemble(merged)
            if _is_failure_placeholder(svg_str) or not self._has_drawing_content(svg_str):
                logger.warning("Insert assemble produced placeholder/empty for %s", question_id)
                return ""
        except Exception as e:
            logger.warning("Assemble failed for insert %s: %s", question_id, e)
            return ""

        new_index, reservation = self._reserve_diagram_index(folder)
        new_svg_path = os.path.join(folder, f"diagram_{new_index}.svg")
        try:
            await self._write_svg(new_svg_path, svg_str, spec=merged)
        finally:
            try:
                os.remove(reservation)
            except FileNotFoundError:
                pass

        logger.info("Insert diagram saved: %s/%d (inserted into %d)", question_id, new_index, target_diagram_index)
        return f"/storage/questions/{question_id}/diagram_{new_index}.svg"

    async def get_diagram_spec(self, question_id: str, index: int) -> tuple[dict | None, bool]:
        """读取 diagram_{index}.spec.json，返回 (spec, fresh)。
        spec 结构不完整或不存在时返回 (None, False)，由端点映射为 404。
        """
        folder = os.path.join(QUESTIONS_DIR, question_id)
        spec_path = os.path.join(folder, f"diagram_{index}.spec.json")
        if not os.path.isfile(spec_path):
            return None, False
        spec = None
        try:
            with open(spec_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                spec = loaded
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning("Failed to read diagram spec %s: %s", spec_path, e)
            return None, False
        if spec is None or not isinstance(spec.get("components"), list):
            logger.warning("Diagram spec %s has invalid structure", spec_path)
            return None, False
        fresh = await self.check_diagram_freshness(question_id, index)
        return spec, fresh

    async def check_diagram_freshness(self, question_id: str, diagram_index: int) -> bool:
        """检查图是否最新（时间戳校验）"""
        from models.database import async_session
        from models.models import Question

        folder = os.path.join(QUESTIONS_DIR, question_id)
        svg_path = os.path.join(folder, f"diagram_{diagram_index}.svg")
        ts_path = svg_path + ".ts"

        # 检查时间戳文件是否存在
        if not os.path.exists(ts_path):
            logger.info("Timestamp missing for %s/%d", question_id, diagram_index)
            return False

        # 读取时间戳
        try:
            with open(ts_path, "r", encoding="utf-8") as f:
                ts_str = f.read().strip()
            ts = int(ts_str)
        except (ValueError, IOError, OSError) as e:
            logger.warning("Failed to read timestamp %s: %s", ts_path, e)
            return False

        # 与最新数据对比（检查 question 的 updated_at）
        try:
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if q and q.updated_at:
                    # updated_at 以 naive UTC 存储；timestamp() 对 naive 值按本地时区解释，
                    # 必须显式补 UTC 才能与 time.time() 的 epoch 比较
                    q_ts = int(q.updated_at.replace(tzinfo=timezone.utc).timestamp())
                    if q_ts > ts:
                        logger.info("Diagram %s/%d stale: question updated at %d > diagram %d",
                                    question_id, diagram_index, q_ts, ts)
                        return False
        except Exception as e:
            logger.warning("Failed to check freshness for %s/%d: %s", question_id, diagram_index, e)
            # 如果数据库查询失败，默认认为图是有效的
            pass

        return True

    # ---- AGENT_NEEDS feedback ----
    def _sanitize_svg(self, svg_code: str) -> str:
        """用 XML 标签与属性白名单净化 SVG，并拒绝所有外部资源。"""
        import xml.etree.ElementTree as ET

        raw = str(svg_code or "").strip()
        match = re.search(r"<svg\b[\s\S]*?</svg>", raw, flags=re.IGNORECASE)
        if not match and ("&amp;" in raw or "&lt;" in raw or "&gt;" in raw or "&quot;" in raw):
            # 仅在「原样提取失败」时才兼容模型把整个 SVG 当 HTML 实体转义回来的畸形输入。
            # 绝不能无条件 unescape：那会把合法转义（&amp;/&lt;）解回裸字符，使本可正常解析的
            # SVG 变成 ParseError 并被下面的分支降级为空画布（静默数据损坏）。
            # 见 FreqErr.md [SVG 无条件 unescape]。
            raw = html.unescape(raw)
            match = re.search(r"<svg\b[\s\S]*?</svg>", raw, flags=re.IGNORECASE)
        if not match:
            raise ValueError("SVG 文档不完整")
        try:
            root = ET.fromstring(match.group(0))
        except ET.ParseError:
            # 模型偶尔会给出缺少引号、重复属性或未声明 xlink 的畸形 XML。
            # 不能尝试用正则“修好”并继续展示（容易留下解析器差异型 XSS），
            # 统一降级为空的安全画布，由上层质量检查决定是否重试。
            return (
                '<svg xmlns="http://www.w3.org/2000/svg" '
                'viewBox="0 0 400 300" width="400" height="300"></svg>'
            )

        def local_name(value: str) -> str:
            return value.rsplit("}", 1)[-1]

        allowed_tags = {
            "svg", "g", "defs", "marker", "clippath", "path", "line", "polyline",
            "polygon", "rect", "circle", "ellipse", "text", "tspan",
        }
        allowed_attrs = {
            "xmlns", "viewBox", "width", "height", "preserveAspectRatio", "id",
            "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
            "d", "points", "transform", "fill", "stroke", "stroke-width", "stroke-linecap",
            "stroke-linejoin", "stroke-dasharray", "opacity", "fill-opacity", "stroke-opacity",
            "font-size", "font-family", "font-style", "font-weight", "text-anchor",
            "dominant-baseline", "dx", "dy", "vector-effect", "orient", "markerWidth",
            "markerHeight", "refX", "refY", "markerUnits", "clip-path", "marker-start",
            "marker-mid", "marker-end",
        }
        local_url_attrs = {"clip-path", "marker-start", "marker-mid", "marker-end", "fill", "stroke"}

        def clean(parent):
            for child in list(parent):
                if local_name(child.tag).lower() not in allowed_tags:
                    parent.remove(child)
                    continue
                for raw_attr in list(child.attrib):
                    attr = local_name(raw_attr)
                    value = str(child.attrib[raw_attr]).strip()
                    lowered = value.lower().replace(" ", "")
                    if attr == "style":
                        # 不能整条删（会连笔画一起删掉，见 sanitize_style_attr 的说明）
                        kept = sanitize_style_attr(value)
                        if kept:
                            child.attrib[raw_attr] = kept
                        else:
                            del child.attrib[raw_attr]
                    elif attr not in allowed_attrs:
                        del child.attrib[raw_attr]
                    elif "url(" in lowered:
                        if attr not in local_url_attrs or not re.fullmatch(r"url\(#[a-zA-Z0-9_.:-]+\)", value):
                            del child.attrib[raw_attr]
                    elif any(token in lowered for token in ("javascript:", "data:", "http:", "https:", "//")):
                        del child.attrib[raw_attr]
                clean(child)

        if local_name(root.tag).lower() != "svg":
            raise ValueError("SVG 根节点无效")
        def clean_attributes(element):
            for raw_attr in list(element.attrib):
                attr = local_name(raw_attr)
                value = str(element.attrib[raw_attr]).strip()
                lowered = re.sub(r"\s+", "", value).lower()
                if attr == "style":
                    kept = sanitize_style_attr(value)
                    if kept:
                        element.attrib[raw_attr] = kept
                    else:
                        del element.attrib[raw_attr]
                elif attr not in allowed_attrs:
                    del element.attrib[raw_attr]
                elif "url(" in lowered:
                    if attr not in local_url_attrs or not re.fullmatch(
                        r"url\(#[a-zA-Z0-9_.:-]+\)", value
                    ):
                        del element.attrib[raw_attr]
                elif any(token in lowered for token in (
                    "javascript:", "data:", "http:", "https:", "//"
                )):
                    del element.attrib[raw_attr]

        clean_attributes(root)
        clean(root)
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        return ET.tostring(root, encoding="unicode")

    def _next_diagram_index(self, folder: str) -> int:
        """扫描文件夹，找到下一个可用的 diagram index，避免覆盖已有文件"""
        max_idx = -1
        if os.path.isdir(folder):
            for fname in os.listdir(folder):
                if fname.startswith("diagram_") and fname.endswith(".svg"):
                    try:
                        idx = int(fname[len("diagram_"):-len(".svg")])
                        max_idx = max(max_idx, idx)
                    except ValueError:
                        continue
        return max_idx + 1

    def _reserve_diagram_index(self, folder: str) -> tuple[int, str]:
        """跨协程/进程原子预留 diagram index，防止并发插入互相覆盖。"""
        index = 0
        while index < 10_000:
            svg_path = os.path.join(folder, f"diagram_{index}.svg")
            reservation = svg_path + ".reserve"
            if os.path.exists(svg_path):
                index += 1
                continue
            try:
                fd = os.open(reservation, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                if os.path.exists(svg_path):
                    os.remove(reservation)
                    index += 1
                    continue
                return index, reservation
            except FileExistsError:
                index += 1
        raise RuntimeError("示意图数量超过安全上限")

    @staticmethod
    def _has_drawing_content(svg: str) -> bool:
        """拒绝空画布/失败占位，只有真实绘图元素才可作为成功结果。"""
        text = str(svg or "")
        # 先排除失败占位：它含 viewBox 与 <text>，纯正则存在性判断会放行。
        if _is_failure_placeholder(text):
            return False
        return bool(
            re.search(r"\bviewBox\s*=", text, flags=re.IGNORECASE)
            and re.search(
                r"<(?:path|line|polyline|polygon|rect|circle|ellipse|text)\b",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _note_agent_need(self, message: str):
        """记录能力缺口，不在项目根目录自动制造额外文档。"""
        logger.info("Agent capability need: %s", str(message)[:1000])

    def _get_svg_size(self, svg_path: str) -> tuple[int, int]:
        """读取SVG文件的viewBox宽度和高度，用于img标签防止压缩。"""
        import re
        try:
            if not os.path.exists(svg_path):
                return (0, 0)
            with open(svg_path, "r", encoding="utf-8") as f:
                content = f.read(32768)  # first 32KB is enough
            # Match viewBox="x y w h"
            m = re.search(r'viewBox\s*=\s*["\']?\s*[\d.-]+\s+[\d.-]+\s+([\d.]+)\s+([\d.]+)', content)
            if m:
                return (int(float(m.group(1))), int(float(m.group(2))))
            # Fallback: match width/height attributes on root <svg>
            m2 = re.search(r'<svg[^>]*\swidth\s*=\s*["\']?(\d+)', content)
            m3 = re.search(r'<svg[^>]*\sheight\s*=\s*["\']?(\d+)', content)
            if m2 and m3:
                return (int(m2.group(1)), int(m3.group(1)))
        except Exception as e:
            logger.debug("Failed to get SVG size for %s: %s", svg_path, e)
        return (0, 0)

    def _validate_svg_quality(self, svg_path: str, is_experiment: bool = False) -> list[str]:
        """验证SVG示意图质量，返回问题列表。数学图检查全黑，实验图检查米白背景。"""
        import re
        issues = []
        try:
            if not os.path.exists(svg_path):
                return ["SVG文件不存在"]
            with open(svg_path, "r", encoding="utf-8") as f:
                content = f.read(65536)
            # 1. 检查 viewBox
            if 'viewBox' not in content and 'viewbox' not in content:
                issues.append("缺少viewBox属性")
            # 2. 检查是否有绘图元素
            has_shapes = bool(re.search(r'<(?:line|circle|rect|path|polygon|polyline|ellipse|text)', content))
            if not has_shapes:
                issues.append("SVG中无绘图元素(line/circle/rect/path/text)")
            # 3. 颜色检查
            if is_experiment:
                # 实验图应有米白色背景
                if 'fill="white"' not in content and 'fill="#fff"' not in content and 'fill="#f5f0e8"' not in content:
                    issues.append("实验图缺少白色/米色背景")
            else:
                # 数学图应全黑线+白底，不允许彩色填充
                bad_colors = re.findall(r'fill\s*=\s*["\'](?!none|#fff|#FFF|white|#222|#333|#444|#555|#666|#999|#aaa|transparent)', content)
                if bad_colors:
                    issues.append(f"数学图包含非标准填充色: {', '.join(set(bad_colors))}")
                bad_strokes = re.findall(r'stroke\s*=\s*["\'](?!#222|#333|#444|#555|#666|#999|#aaa|#000)', content)
                if bad_strokes:
                    issues.append(f"数学图包含非标准描边色: {', '.join(set(bad_strokes))}")
            # 4. 检查文本是否可能重叠（粗略：检查text标签是否太多太密）
            text_count = len(re.findall(r'<text\b', content))
            if text_count > 20:
                # Too many text labels may indicate overlap
                pass  # just a heuristic, not a hard error
            # 4a. 检查text标签位置是否可能重叠（相邻<text>的x,y坐标过近）
            text_positions = re.findall(r'<text\b[^>]*\s+x\s*=\s*["\']?([\d.]+)[^>]*\s+y\s*=\s*["\']?([\d.]+)', content)
            if len(text_positions) >= 2:
                for i in range(len(text_positions)):
                    for j in range(i + 1, min(i + 3, len(text_positions))):
                        try:
                            x1, y1 = float(text_positions[i][0]), float(text_positions[i][1])
                            x2, y2 = float(text_positions[j][0]), float(text_positions[j][1])
                            dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                            if dist < 8:  # closer than 8px → likely overlap
                                issues.append(f"文字位置可能重叠: text#{i}与text#{j}距离仅{dist:.0f}px")
                                break
                        except (ValueError, IndexError):
                            pass
            # 5. 检查是否有script/foreignObject等安全标签（使用sanitize）
            if '<script' in content.lower() or '<foreignobject' in content.lower():
                issues.append("SVG含不安全的script/foreignObject标签")
            # 6. 检查虚线是否正确使用 stroke-dasharray
            if 'stroke-dasharray' in content:
                # Verify dashed lines have nonzero dasharray values
                dash_matches = re.findall(r'stroke-dasharray\s*=\s*["\']?\s*([\d.,\s]+)', content)
                for dm in dash_matches:
                    dm_clean = re.sub(r'\s+', ' ', dm.strip()).replace(',', ' ')
                    values = [v for v in dm_clean.split() if v and float(v) > 0]
                    if not values:
                        issues.append(f"虚线stroke-dasharray值无效: {dm[:30]}")
            # 7. 检查箭头是否正确使用 marker-end
            arrow_lines = re.findall(r'<line\b[^>]*marker-end', content)
            arrow_paths = re.findall(r'<path\b[^>]*marker-end', content)
            if (arrow_lines or arrow_paths) and '<defs>' not in content:
                issues.append("箭头引用了marker-end但缺少<defs>定义")
        except Exception as e:
            issues.append(f"SVG验证异常: {str(e)[:100]}")
        return issues

    def _report_tools_needed(self, lines: list, points: list):
        """Check if any needed features are missing and report to AGENT_NEEDS."""
        missing = []
        # Check for angle markers
        for li in lines:
            if li.get("mark") == "angle" and "center" not in li:
                missing.append("angle_mark(center, start, end, radius) — 角度标注")
            if li.get("mark") == "right_angle":
                missing.append("right_angle_mark(vertex, a, b, size) — 直角标记")
            if li.get("mark") == "parallel":
                missing.append("parallel_mark(line1_start, line1_end, count) — 平行标记")

        # Check for unsupported expression functions
        for p in points:
            for coord in [str(p.get("x", "")), str(p.get("y", ""))]:
                for fn in ["log2", "log1p", "erf", "gamma", "lgamma", "modf", "frexp"]:
                    if fn in coord:
                        # 缺 id 时不能让 KeyError 作废已成功落盘的高质量坐标图
                        missing.append(f"表达式函数 {fn}() — 坐标 {p.get('id', '?')}:{coord[:50]}")

        for m in missing:
            self._note_agent_need(m)


diagram_service = DiagramService()
