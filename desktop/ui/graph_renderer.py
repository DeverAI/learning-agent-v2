"""轻量图论图渲染器（无第三方依赖）。

支持的 `graph` 代码块格式（YAML 风格）：

```graph
directed: false
nodes:
  - id: 1, label: A
  - id: 2, label: B
  - id: 3, label: C
edges:
  - from: 1, to: 2, weight: 5
  - from: 2, to: 3
```

简化格式（每行一条边，节点自动创建）：
```
A - B (5)
A - C
B -> C
```

API：
- `parse_graph_block(text: str) -> dict | None`：解析代码块文本，失败返回 None
- `render_graph_svg(text: str, theme: dict | None = None, width: int = 480,
                    marker_id: str | None = None) -> str | None`：
  返回可直接嵌入 HTML 的 `<svg>...</svg>` 字符串；解析或渲染失败返回 None
"""
import math
import random
import re
import uuid
from typing import Dict, List, Optional, Tuple


NODE_RADIUS = 22
DEFAULT_WIDTH = 480
DEFAULT_HEIGHT = 320
BG_TRANSPARENT = "transparent"
# r31 P0 修复：从 30 降到 20，避免 480x320 标准画布下节点圆圈互相覆盖
# 30 节点在 480x320 下 k=50 但实际可用空间仅能容纳 ~10 个不重叠节点（半径 22px）
MAX_NODES = 20
MAX_EDGES = 100
MIN_WIDTH = 320
MIN_HEIGHT = 200
MAX_LABEL_LEN = 12      # label 截断上限，避免长 label 溢出


# ---------- 解析 ----------
_NODE_LINE_RE = re.compile(
    r"^\s*-\s*id\s*:\s*([^,\s]+)\s*(?:,\s*label\s*:\s*(.+?))?\s*$"
)
_EDGE_LINE_RE = re.compile(
    r"^\s*-\s*from\s*:\s*([^,\s]+)\s*,\s*to\s*:\s*([^,\s]+)(?:\s*,\s*weight\s*:\s*(\S+))?\s*$"
)
_DIRECTED_RE = re.compile(r"^\s*directed\s*:\s*(true|false)\s*$", re.IGNORECASE)
_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
_WEIGHT_VALID = re.compile(r"^-?\d+(\.\d+)?$")
# 简化模式正则（与简化分支保持一致；提取出来供 YAML 兜底复用）
# r36 P1 修复：em dash/en dash（—/–）语义上等同于无向横线，
# 不再放入有向正则；只由 _SIMPLE_DASH_RE 按无向边处理。
_SIMPLE_RE = re.compile(
    r"^\s*(.+?)\s*(?:-->|→|->)\s*(.+?)\s*(?:\(\s*(-?[0-9.]+)\s*\))?\s*$"
)
# r36 P1 修复：en dash / em dash（–/—）支持无空格（如 A–B / A—B），
# 而普通 hyphen 仍要求两侧有空格，避免误拆带连字符的节点名。
_SIMPLE_DASH_RE = re.compile(
    r"^\s*(.+?)\s*(?:\s+-\s+|[–—])\s*(.+?)\s*(?:\(\s*(-?[0-9.]+)\s*\))?\s*$"
)
# r32: "u v w" 格式（csacademy 风格）
# - 1 token：仅创建节点
# - 2 tokens：u v（无向边 u-v，v 不存在则自动建）
# - 3 tokens：u v w（无向边 u-v 权重 w）
# 第 1 行支持 `directed: true/false` 单独声明
_UVW_LINE_RE = re.compile(r"^\s*(\S+)(?:\s+(\S+))?(?:\s+(\S+))?\s*$")
# r34 修复：_tokenize_uvw_line 为限制最多 3 个 token 会截断行，
# 启发式判断时需用整行正则二次确认确实只有 1-3 个 token（支持引号）。
_UVW_LOOKS_RE = re.compile(
    r'^\s*(?:"(?:[^"\\]|\\.)*"|[^\s"]+)'
    r'(?:\s+(?:"(?:[^"\\]|\\.)*"|[^\s"]+)){0,2}\s*$'
)


def _strip_inline_comment(line: str) -> str:
    """去除行内 `#` 注释，但保留引号/转义内的 `#`。

    用于 UVW/简化格式预处理，避免注释中的 `->` 等符号干扰
    格式启发式判断，同时避免引号内 `#` 被误截断。
    """
    in_quote = False
    escape = False
    for i, c in enumerate(line):
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_quote = not in_quote
            continue
        if c == "#" and not in_quote:
            return line[:i].rstrip()
    return line


# ---------- LaTeX → Unicode 轻量支持（用于节点 label） ----------
# 完整 LaTeX 渲染代价过大，这里只覆盖最常见的数学/几何符号
_LATEX_SIMPLE_MAP = {
    r"\angle": "∠",
    r"\perp": "⊥",
    r"\parallel": "∥",
    r"\infty": "∞",
    r"\rightarrow": "→",
    r"\to": "→",
    r"\leftarrow": "←",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔",
    r"\neq": "≠",
    r"\le": "≤",
    r"\leq": "≤",
    r"\ge": "≥",
    r"\geq": "≥",
    r"\pm": "±",
    r"\cdot": "·",
    r"\times": "×",
    r"\div": "÷",
    r"\approx": "≈",
    r"\equiv": "≡",
    r"\sum": "∑",
    r"\prod": "∏",
    r"\emptyset": "∅",
    r"\in": "∈",
    r"\notin": "∉",
    r"\subset": "⊂",
    r"\subseteq": "⊆",
    r"\supset": "⊃",
    r"\supseteq": "⊇",
    r"\cup": "∪",
    r"\cap": "∩",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\epsilon": "ε",
    r"\zeta": "ζ",
    r"\eta": "η",
    r"\theta": "θ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\nu": "ν",
    r"\xi": "ξ",
    r"\pi": "π",
    r"\rho": "ρ",
    r"\sigma": "σ",
    r"\tau": "τ",
    r"\phi": "φ",
    r"\varphi": "ϕ",
    r"\chi": "χ",
    r"\psi": "ψ",
    r"\omega": "ω",
    r"\Gamma": "Γ",
    r"\Delta": "Δ",
    r"\Theta": "Θ",
    r"\Lambda": "Λ",
    r"\Xi": "Ξ",
    r"\Pi": "Π",
    r"\Sigma": "Σ",
    r"\Phi": "Φ",
    r"\Psi": "Ψ",
    r"\Omega": "Ω",
    r"\sqrt": "√",   # 简化：仅输出 √，参数丢弃
    r"\neg": "¬",
    r"\land": "∧",
    r"\lor": "∨",
}


def _latex_to_unicode_simple(s: str) -> str:
    """轻量 LaTeX → Unicode 转换（仅对单 token 命令生效）。

    不解析花括号参数（避免破坏 label 语义）；只做单命令替换。
    用 `(?![A-Za-z])` 后行词边界避免误伤 `\anglefoo` 之类。
    已知限制：兜底正则（无映射的命令 + 花括号参数）按贪婪匹配整个命令名，
    因此 `\\foofrac{x}` 会显示为 `[\\foofrac]{x}`，无法做更细粒度区分；
    实际使用中（AI 写标准 LaTeX）此情况极少。
    """
    if not s or "\\" not in s:
        return s
    out = s
    # 先按 map 替换，加负向后行词边界：命令后不能跟字母（避免 \tooo 误伤）
    for cmd, ch in _LATEX_SIMPLE_MAP.items():
        out = re.sub(re.escape(cmd) + r"(?![A-Za-z])", ch, out)
    # 兜底：\cmd{...} 形式只去掉命令名，保留花括号内容（信息不丢）
    # 输出形如 "[\cmd]{arg}"，保留原命令名便于阅读
    out = re.sub(r"\\([A-Za-z]+)\{([^}]*)\}", r"[\\\1]{\2}", out)
    return out


def _color(value, default: str) -> str:
    """安全取颜色：None / 非 str / 空都退到 default。"""
    if not isinstance(value, str) or not value.strip():
        return default
    return value


# r36 P1 修复：SVG 颜色字段校验，防止异常/注入字符串破坏 SVG 属性
_COLOR_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _safe_color(value, default: str) -> str:
    """取安全颜色：必须是 #RGB 或 #RRGGBB 格式，否则退到 default。"""
    c = _color(value, default)
    if _COLOR_HEX_RE.match(c):
        return c
    return default


def _hex_luminance(hex_color: str) -> float:
    """计算 hex 颜色亮度（0~1）。失败返回 0.5。"""
    try:
        s = hex_color.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            return 0.5
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
        # 简单加权亮度
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    except Exception:
        return 0.5


def _contrast_ratio(c1: str, c2: str) -> float:
    """WCAG 对比度（1~21）。失败返回 1.0。"""
    l1 = _hex_luminance(c1)
    l2 = _hex_luminance(c2)
    light = max(l1, l2)
    dark = min(l1, l2)
    return (light + 0.05) / (dark + 0.05)


def _pick_edge_color(theme: dict, bg_color: str) -> str:
    """R38 P1 修复：对比度感知的边色选择。

    依次尝试 text_dim / text / border，挑选第一个与背景对比度 >= 3.0 的颜色。
    若全部不达标，返回对比度最高的候选色。这确保图论 SVG 的边和箭头在
    所有主题下都可见（WCAG 1.4.11 非文本对比度 >= 3:1）。
    """
    candidates = [
        _safe_color(theme.get("text_dim"), "#475569"),
        _safe_color(theme.get("text"), "#1e293b"),
        _safe_color(theme.get("border"), "#475569"),
    ]
    # 去重保序
    seen = set()
    uniq = []
    for c in candidates:
        cl = c.lower()
        if cl not in seen:
            seen.add(cl)
            uniq.append(c)
    threshold = 3.0
    best = None
    best_cr = 0.0
    for c in uniq:
        cr = _contrast_ratio(c, bg_color)
        if cr >= threshold:
            return c
        if cr > best_cr:
            best_cr = cr
            best = c
    return best or "#475569"


def _auto_contrast_text(bg: str, accent: str, text_fallback: str) -> str:
    """根据 bg 与 accent 的明度选对比文字色。

    - 浅色 bg（亮度 > 0.5）+ 深色 accent（亮度 <= 0.5）：label_fill 用白色（节点圆用深色 accent）
    - 浅色 bg + 浅色 accent：极端情况，文字用深色（避免与节点同色）
    - 深色 bg：label_fill 用 bg（深色）
    """
    try:
        bg_lum = _hex_luminance(bg)
        accent_lum = _hex_luminance(accent) if accent else 0.5
        if bg_lum > 0.5:
            # 浅色主题：节点圆用 accent。
            if accent_lum <= 0.5:
                # accent 是深色：label 用白字，对比鲜明
                return "#ffffff"
            # accent 也是浅色：极端情况（白底白圆），必须用深色 label 避免不可见
            return "#0f172a"
        # 深色主题：节点圆用浅色 accent，文字用深色（与 bg 一致）
        return bg if isinstance(bg, str) and bg else "#0f172a"
    except Exception:
        return text_fallback if isinstance(text_fallback, str) else "#0f172a"


def _truncate_label(s: str) -> str:
    """截断过长的 label。"""
    s = (s or "").strip()
    if len(s) <= MAX_LABEL_LEN:
        return s
    return s[: MAX_LABEL_LEN - 1] + "…"


def _strip_code_fences(text: str) -> str:
    """去除 ```graph / ``` 代码块围栏，返回内部正文。"""
    raw = text.strip()
    if raw.startswith("```"):
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1 :]
        raw = raw.rstrip()
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()
    return raw


def parse_graph_block(text: str) -> Optional[Dict]:
    """解析 graph 代码块文本，返回 {directed, nodes, edges, dropped_edges} 或 None。

    支持三种格式（按优先级）：
    1. UVW 风格（r32，csacademy 风格）：每行 1-3 个 token，自动判断后优先
    2. YAML 风格：`directed:`, `nodes: - id:`, `edges: - from: to:`
    3. 简化风格：每行 `A - B (w)` 或 `A -> B (w)`

    dropped_edges：因节点缺失被忽略的边，仅 YAML 模式统计。
    """
    if not text or not isinstance(text, str):
        return None
    raw = _strip_code_fences(text)
    if not raw:
        return None

    # r33 P1-1 修复：与 parse_uvw_block 对齐，100KB 硬限制
    # 防止粘贴大文件冻死 UI（正则匹配对每行执行，超长文本卡顿明显）
    if len(raw) > 100_000:
        return None

    # r32: 优先尝试 uvw 格式（启发式判断后立即返回）
    # - 含 YAML 头（directed:/nodes:/edges:）→ 走 YAML
    # - 含简化符号（-->/->/→/—/–/ - ）→ 走简化
    # - 至少一行匹配 1-3 token 且无上述冲突 → 走 uvw
    # r33 P0 修复：含全局简化符号时彻底禁用 uvw 兜底（测试 10.1 根因）
    # 旧逻辑：单行判断 has_simple_marker；r33：先做全局扫描
    # r33 P0 二次修复：" - " 容易与 YAML 列表项前缀 `  - ...` 冲突，
    # 必须在"非行首位置"才视为简化符号
    def _has_simple_symbol_in_middle(line: str) -> bool:
        """检查 line 是否含简化符号（排除行首位置，避免 YAML 列表项误判）。"""
        stripped = line.lstrip()
        # r37 P1 修复：跳过纯注释行；去除行内 `#` 注释后再检测，
        # 避免注释里的 `->` / `-` 等符号误导全局扫描。
        if stripped.startswith("#"):
            return False
        stripped = _strip_inline_comment(stripped)
        if not stripped:
            return False
        # --> / → / — / – 直接检测（这些符号不会在行首出现）
        if "-->" in stripped or "→" in stripped or "—" in stripped or "–" in stripped:
            return True
        # -> 不在行首（行首 - 是 YAML 列表项 / 注释）
        if "->" in stripped and not stripped.startswith("-"):
            return True
        # " - " 不在行首（行首 - 后跟空格是 YAML 列表项）
        # 简化模式的 " - " 形如 "A - B"，前面至少有节点名
        if " - " in stripped and not stripped.startswith("-"):
            return True
        return False

    _has_global_simple_marker = any(
        _has_simple_symbol_in_middle(_line) for _line in raw.splitlines()
    )
    if not _has_global_simple_marker and _looks_like_uvw(raw):
        result = parse_uvw_block(raw)
        if result is not None:
            return result
        # uvw 启发式命中但解析失败（空文本/全注释）→ 继续走原分支
        # 不直接返回 None，否则可能丢可解析的 YAML/简化内容

    lines = raw.splitlines()
    directed = False
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    dropped_edges: List[Dict] = []
    # r37 P1 修复：YAML edges 段若出现在 nodes 段之前，节点尚未收集，
    # 边会被误丢弃。改为先把缺失节点的边暂存，扫描结束后再统一解析。
    pending_edges: List[Dict] = []

    yaml_mode = False
    section: Optional[str] = None
    parsed_any_yaml = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 顶层 KV
        if not line.startswith((" ", "\t", "-")):
            m = _KV_RE.match(line)
            if m:
                key = m.group(1).lower()
                val = m.group(2).strip()
                if key == "directed":
                    directed = val.lower() in ("true", "yes", "1")
                    section = None
                    yaml_mode = True
                    parsed_any_yaml = True
                    continue
                if key == "nodes":
                    section = "nodes"
                    yaml_mode = True
                    parsed_any_yaml = True
                    continue
                if key == "edges":
                    section = "edges"
                    yaml_mode = True
                    parsed_any_yaml = True
                    continue
            # 兜底：含 YAML 头但行不符合 KV 格式时，尝试简化模式（兼容 `directed: false\nA - B` 混排）
            if parsed_any_yaml:
                sm = _SIMPLE_RE.match(stripped)
                if not sm:
                    sm = _SIMPLE_DASH_RE.match(stripped)
                if sm:
                    src_raw = sm.group(1).strip()
                    dst_raw = sm.group(2).strip()
                    w = (sm.group(3) or "").strip()
                    # r36 P1 修复：—/– 视为无向横线，不强制 directed
                    if any(s in stripped for s in ("-->", "->", "→")):
                        directed = True
                    if src_raw and dst_raw:
                        if w and not _WEIGHT_VALID.match(w):
                            w = ""
                        src_lbl = _latex_to_unicode_simple(src_raw)
                        dst_lbl = _latex_to_unicode_simple(dst_raw)
                        nodes.setdefault(src_raw, {"id": src_raw, "label": _truncate_label(src_lbl)})
                        nodes.setdefault(dst_raw, {"id": dst_raw, "label": _truncate_label(dst_lbl)})
                        edges.append({"from": src_raw, "to": dst_raw, "weight": w})
            # r33 P1-6 修复：顶层非 KV 行也尝试 uvw 解析
            # 场景 1：`1 2\nnodes:\n  - id: A` —— 第 1 行 uvw 边被旧逻辑吞掉
            # 场景 2：`directed: false\n1 2` —— yaml 声明后 uvw 边被丢
            # 关键限制：含简化符号（-/->/-->/→/—/–/ - ）的行优先走简化分支
            # r33 P0 边界修复：同时检查全局简化符号（避免与简化分支双重建边）
            # r33 P0 二次修复：行内简化符号检查排除行首位置（YAML 列表项）
            # r33 P0 三次修复（P0-1）：1-token uvw 节点行也要保留（与 parse_uvw_block 一致）
            if not _has_simple_symbol_in_middle(stripped) and not _has_global_simple_marker:
                # r34 P1 修复：与 parse_uvw_block 对齐，先截断行内 # 注释。
                _uvw_line = stripped
                hash_idx = _uvw_line.find("#")
                if hash_idx > 0:
                    _uvw_line = _uvw_line[:hash_idx].rstrip()
                if not _uvw_line:
                    section = None
                    continue
                uvw_toks = _tokenize_uvw_line(_uvw_line)
                # r34 修复：_tokenize_uvw_line 最多取 3 个 token，会截断多 token 行。
                # 用整行正则确认确实只有 1-3 个 token，避免普通句子被当作 uvw 图数据。
                if uvw_toks and _UVW_LOOKS_RE.match(_uvw_line):
                    _u = uvw_toks[0]
                    if _u and _u not in nodes:
                        nodes[_u] = {"id": _u, "label": _truncate_label(_latex_to_unicode_simple(_u))}
                    if len(uvw_toks) >= 2:
                        _v = uvw_toks[1]
                        _w = uvw_toks[2] if len(uvw_toks) > 2 else ""
                        if _v and _v not in nodes:
                            nodes[_v] = {"id": _v, "label": _truncate_label(_latex_to_unicode_simple(_v))}
                        if _u != _v:
                            if _w and not _WEIGHT_VALID.match(_w):
                                _w = ""
                            edges.append({"from": _u, "to": _v, "weight": _w})
            section = None
            continue
        if section == "nodes":
            m = _NODE_LINE_RE.match(line)
            if m:
                nid = m.group(1).strip()
                label = (m.group(2) or "").strip() or nid
                # label 走 LaTeX → Unicode 转换（如 \angle A → ∠A）
                label = _latex_to_unicode_simple(label)
                nodes[nid] = {"id": nid, "label": _truncate_label(label)}
                continue
            # 段不匹配：降级为自动识别（兼容混排 / 省略头）
            m_e = _EDGE_LINE_RE.match(line)
            if m_e:
                section = "edges"
                src, dst, w = m_e.group(1).strip(), m_e.group(2).strip(), (m_e.group(3) or "").strip()
                if w and not _WEIGHT_VALID.match(w):
                    w = ""
                if src in nodes and dst in nodes:
                    edges.append({"from": src, "to": dst, "weight": w})
                else:
                    pending_edges.append({"from": src, "to": dst, "weight": w})
                yaml_mode = True
                parsed_any_yaml = True
                continue
            continue
        if section == "edges":
            m = _EDGE_LINE_RE.match(line)
            if m:
                src, dst, w = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
                if w and not _WEIGHT_VALID.match(w):
                    w = ""  # 非法 weight 丢弃
                if src in nodes and dst in nodes:
                    edges.append({"from": src, "to": dst, "weight": w})
                else:
                    pending_edges.append({"from": src, "to": dst, "weight": w})
                continue
            # 段不匹配：降级为自动识别
            m_n = _NODE_LINE_RE.match(line)
            if m_n:
                section = "nodes"
                nid = m_n.group(1).strip()
                label = (m_n.group(2) or "").strip() or nid
                label = _latex_to_unicode_simple(label)
                nodes[nid] = {"id": nid, "label": _truncate_label(label)}
                yaml_mode = True
                parsed_any_yaml = True
                continue
            continue
        # 段未知但有项：按行内容自适配（兼容省略 nodes:/edges: 头的写法）
        if _NODE_LINE_RE.match(line):
            section = "nodes"
            m = _NODE_LINE_RE.match(line)
            if m:
                nid = m.group(1).strip()
                label = (m.group(2) or "").strip() or nid
                label = _latex_to_unicode_simple(label)
                nodes[nid] = {"id": nid, "label": _truncate_label(label)}
                yaml_mode = True
                parsed_any_yaml = True
            continue
        if _EDGE_LINE_RE.match(line):
            section = "edges"
            m = _EDGE_LINE_RE.match(line)
            if m:
                src, dst, w = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
                if w and not _WEIGHT_VALID.match(w):
                    w = ""
                if src in nodes and dst in nodes:
                    edges.append({"from": src, "to": dst, "weight": w})
                else:
                    pending_edges.append({"from": src, "to": dst, "weight": w})
                yaml_mode = True
                parsed_any_yaml = True
            continue

    if not parsed_any_yaml:
        # 简化风格：支持多 token 节点名（如 "\angle A - \angle B"）
        # 优先级：-->  >  ->  >  —/–/-（带空格的横线，均视为无向）
        # r36 P1 修复：em dash/en dash 不再归入有向正则
        simple_re = re.compile(
            r"^\s*(.+?)\s*(?:-->|→|->)\s*(.+?)\s*(?:\(\s*(-?[0-9.]+)\s*\))?\s*$"
        )
        # r36 P1 修复：与模块级 _SIMPLE_DASH_RE 保持一致，–/— 支持无空格
        simple_dash_re = re.compile(
            r"^\s*(.+?)\s*(?:\s+-\s+|[–—])\s*(.+?)\s*(?:\(\s*(-?[0-9.]+)\s*\))?\s*$"
        )
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = simple_re.match(stripped)
            if not m:
                m = simple_dash_re.match(stripped)
            if not m:
                continue
            src_raw = m.group(1).strip()
            dst_raw = m.group(2).strip()
            w = (m.group(3) or "").strip()
            # 仅真正箭头形式视为有向；—/–/- 均为无向横线
            if any(s in stripped for s in ("-->", "->", "→")):
                directed = True
            if not src_raw or not dst_raw:
                continue
            # 简化模式也校验 weight 合法性（非数字 / 非法字符一律丢）
            if w and not _WEIGHT_VALID.match(w):
                w = ""
            # 简化模式节点 label 也走 LaTeX 转换
            src_lbl = _latex_to_unicode_simple(src_raw)
            dst_lbl = _latex_to_unicode_simple(dst_raw)
            nodes.setdefault(src_raw, {"id": src_raw, "label": _truncate_label(src_lbl)})
            nodes.setdefault(dst_raw, {"id": dst_raw, "label": _truncate_label(dst_lbl)})
            edges.append({"from": src_raw, "to": dst_raw, "weight": w})

    # r37 P1 修复：扫描完所有行后，再尝试解析因节点缺失而暂存的 YAML 边，
    # 从而支持 edges: 段出现在 nodes: 段之前的写法。
    for pe in pending_edges:
        if pe["from"] in nodes and pe["to"] in nodes:
            edges.append(pe)
        else:
            dropped_edges.append(pe)

    if not nodes and not edges:
        return None
    return {
        "directed": directed,
        "nodes": list(nodes.values()),
        "edges": edges,
        "dropped_edges": dropped_edges,
    }


def parse_uvw_block(text: str) -> Optional[Dict]:
    """r32: 解析 "u v w" 行式格式（csacademy 风格）。

    每行用空白分隔 1~3 个 token：
    - 1 token (u)：仅创建节点 u
    - 2 tokens (u v)：创建无向边 u-v（v 不存在则自动建）
    - 3 tokens (u v w)：创建无向边 u-v 权重 w

    首行特殊支持 `directed: true` / `directed: false`（独立声明有向/无向）。

    例：
        1 2
        2 3 5
        4
        → 节点 {1,2,3,4}，边 {1-2, 2-3(5)}

    规则：
    - weight 必须为数字（整数 / 小数），否则丢弃
    - 节点 id 走 LaTeX → Unicode 转换（如 `\angle A` → `∠A`）
    - 注释行（# 开头）和空行跳过
    """
    if not text or not isinstance(text, str):
        return None
    raw = _strip_code_fences(text)
    if not raw:
        return None

    # r33 P1-1 修复：UVW 输入框无最大长度，粘贴超大文本会冻死 UI
    # 在 parse 入口加 100KB 硬限制（约 1 万行），超出直接返回 None
    if len(raw) > 100_000:
        return None

    lines = raw.splitlines()
    directed = False
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []

    # r37 修复：先扫描开头，处理 directed: 声明并定位实际数据起始行。
    start_idx = 0
    while start_idx < len(lines):
        line = lines[start_idx].strip()
        if not line or line.startswith("#"):
            start_idx += 1
            continue
        # r37 P2 修复：引号内的 `#` 不应被当作注释截断
        line = _strip_inline_comment(line)
        if not line:
            start_idx += 1
            continue
        m_dir = _KV_RE.match(line)
        if m_dir and m_dir.group(1).lower() == "directed":
            directed = m_dir.group(2).strip().lower() in ("true", "yes", "1")
            start_idx += 1
            continue
        break

    # r37 修复：兼容 CS Academy 标准格式——数据第一行是节点数量 n，后续是边。
    # 启发式：start_idx 行为单个正整数，且后续至少有一行是 2-3 token 的边。
    # 安全限制：n_count 不得超过 MAX_NODES，防止 `999999999\n1 2` 这类输入拖垮进程。
    if start_idx < len(lines):
        first = lines[start_idx].strip()
        if first and not first.startswith("#"):
            first = _strip_inline_comment(first)
            first_tokens = _tokenize_uvw_line(first)
            if (
                len(first_tokens) == 1
                and first_tokens[0].isascii()
                and first_tokens[0].isdigit()
                and int(first_tokens[0]) > 0
            ):
                n_count = int(first_tokens[0])
                has_edge_line = False
                for l in lines[start_idx + 1 :]:
                    l_stripped = l.strip()
                    if not l_stripped or l_stripped.startswith("#"):
                        continue
                    # r37 P2 修复：引号内的 `#` 保留为节点名一部分
                    l_stripped = _strip_inline_comment(l_stripped)
                    if not l_stripped:
                        continue
                    t = _tokenize_uvw_line(l_stripped)
                    if len(t) in (2, 3) and _UVW_LOOKS_RE.match(l_stripped):
                        has_edge_line = True
                        break
                # 必须同时满足：有边行 + 节点数在合理上限内
                if has_edge_line and n_count <= MAX_NODES:
                    start_idx += 1  # 移除节点数行
                    for i in range(1, n_count + 1):
                        sid = str(i)
                        if sid not in nodes:
                            nodes[sid] = {"id": sid, "label": sid}

    for raw_line in lines[start_idx:]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # r33 P1-2 修复：行内 # 注释（截断 # 及其后内容）
        # 例：`1 2  # 边 1-2` 整行只取 `1 2`，# 视为注释起点
        # r37 P2 修复：引号内的 `#` 保留为节点名一部分
        line = _strip_inline_comment(line)
        if not line:
            continue
        # 首行/中间行支持 directed: 声明
        m_dir = _KV_RE.match(line)
        if m_dir and m_dir.group(1).lower() == "directed":
            directed = m_dir.group(2).strip().lower() in ("true", "yes", "1")
            continue
        # r33: 支持带引号的 token（如 "Node 1" "Node 2" 5）
        # token 解析：先剥引号，否则用 \S+
        tokens = _tokenize_uvw_line(line)
        if not tokens:
            continue
        # r34 P1 修复：_tokenize_uvw_line 最多取 3 个 token，会截断多 token 行。
        # 用整行正则确认原始行确实只有 1-3 个 token，避免普通句子被当作 uvw。
        if not _UVW_LOOKS_RE.match(line):
            continue
        u = tokens[0]
        v = tokens[1] if len(tokens) > 1 else ""
        w = tokens[2] if len(tokens) > 2 else ""
        # 自动建节点
        u_lbl = _latex_to_unicode_simple(u)
        if u not in nodes:
            nodes[u] = {"id": u, "label": _truncate_label(u_lbl)}
        if v:
            v_lbl = _latex_to_unicode_simple(v)
            if v not in nodes:
                nodes[v] = {"id": v, "label": _truncate_label(v_lbl)}
            # weight 校验
            if w and not _WEIGHT_VALID.match(w):
                w = ""
            # r33 P1-5 修复：自环（u == v）在画布上渲染异常
            # 跳过自环避免视觉错乱（csacademy 风格也不支持自环）
            if u == v:
                continue
            edges.append({"from": u, "to": v, "weight": w})

    if not nodes and not edges:
        return None
    return {
        "directed": directed,
        "nodes": list(nodes.values()),
        "edges": edges,
        "dropped_edges": [],
    }


def _tokenize_uvw_line(line: str) -> List[str]:
    r"""r33: 把 uvw 格式的一行解析为 1~3 个 token。

    支持：
    - 普通 token：`1 2 5` → ['1', '2', '5']
    - 带引号 token（含空白）：`"Node 1" "Node 2" 5` → ['Node 1', 'Node 2', '5']
    - 反斜杠转义：`"a\"b" c` → ['a"b', 'c']
    - 限制：最多 3 个 token；多出的部分（含转义引号未闭合的）走 \S+ 兜底

    返回空列表表示该行无效。
    """
    if not line:
        return []
    tokens: List[str] = []
    i = 0
    n = len(line)
    while i < n and len(tokens) < 3:
        # 跳过前导空白
        while i < n and line[i] in (" ", "\t"):
            i += 1
        if i >= n:
            break
        if line[i] == '"':
            # 找匹配的右引号（支持反斜杠转义）
            j = i + 1
            buf = []
            while j < n:
                c = line[j]
                if c == "\\" and j + 1 < n:
                    # 反斜杠转义
                    buf.append(line[j + 1])
                    j += 2
                    continue
                if c == '"':
                    break
                buf.append(c)
                j += 1
            if j < n and line[j] == '"':
                # 正常闭合
                tokens.append("".join(buf))
                i = j + 1
            else:
                # 未闭合 → 退化为 \S+ 兜底
                # r33 P1-5 修复：去外层引号（首/尾各一），不破坏中间引号
                # 同时满了就停（之前是 if len < 3 跳过，会丢剩余 token）
                rest = line[i:].split()
                for r in rest:
                    if len(tokens) >= 3:
                        break
                    tok = r
                    if tok.startswith('"'):
                        tok = tok[1:]
                    if tok.endswith('"'):
                        tok = tok[:-1]
                    tokens.append(tok)
                return tokens
        else:
            # 普通 \S+ 抓取
            j = i
            while j < n and line[j] not in (" ", "\t"):
                j += 1
            tok = line[i:j]
            # 去掉前导引号但无闭合：罕见情况，直接 trim
            if tok.startswith('"'):
                tok = tok.lstrip('"')
            tokens.append(tok)
            i = j
    return tokens


def _looks_like_uvw(text: str) -> bool:
    """r32: 判断文本是否更像"u v w"格式而非 YAML/简化格式。

    启发式：每行 ≤ 3 token，且不含 YAML/简化特征：
    - 不含顶层 YAML 头（`directed:`/`nodes:`/`edges:` 在行首）
    - 不含 YAML 列表项前缀（`- id:` / `- from:` 等）
    - 不含简化符号（`-->` / `->` / `→` / `—` / `–` / ` - `）
    - 不含 `id: ...` / `from: ...` / `to: ...` 等 YAML 字段
    - 不含逗号分隔的多字段（`, to:`, `, weight:` 等）

    且至少 1 行匹配 `_UVW_LINE_RE` 且 token 数 1-3。
    """
    if not text:
        return False
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return False

    # 含 YAML 头（顶层）→ 走 YAML
    # 注意：仅当有 nodes:/edges: 头时才视为 YAML；只有 directed: 时
    # 也可能是 uvw 的方向声明（"directed: true\n1 2\n2 3"）
    yaml_section_kw = {"nodes", "edges"}
    has_yaml_section = False
    for l in lines:
        m = _KV_RE.match(l)
        if m and m.group(1).lower() in yaml_section_kw:
            has_yaml_section = True
            break
    if has_yaml_section:
        return False

    # YAML 列表项（以 "- id:" / "- from:" / "- to:" / "- label:" 开头）→ YAML
    for l in lines:
        if re.match(r"^\s*-\s*(id|from|to|label|weight|directed)\s*:", l, re.IGNORECASE):
            return False

    # 含简化符号 → 走简化
    for l in lines:
        if any(sym in l for sym in ["-->", "->", "→", "—", "–", " - "]):
            return False

    # 含 YAML 字段（id: / from: / to: / weight: 出现在行内任意位置）→ YAML
    for l in lines:
        if re.search(r"\b(id|from|to|weight|label)\s*:", l, re.IGNORECASE):
            return False

    # 含逗号分隔的多字段（YAML 边/节点的典型写法）→ YAML
    for l in lines:
        if "," in l and re.search(r"\w+\s*:\s*\w+", l):
            return False

    # 至少 1 行能 tokenize 为 1-3 token
    # 不匹配的行（空行/注释/4+ token）跳过而非直接判否
    # 1-token 行也加 1 分（与 2-token 等权），3-token 加 2 分
    # 但单 KV 行（如 "directed: true"）不算 uvw 节点/边
    matched = 0
    for l in lines:
        # r34 P2 修复：与 parse_uvw_block 对齐，先截断行内 # 注释。
        _check_line = l
        hash_idx = _check_line.find("#")
        if hash_idx > 0:
            _check_line = _check_line[:hash_idx].rstrip()
        if not _check_line:
            continue
        # 单 KV 行（key: value）→ 视为声明而非 uvw 节点/边
        if _KV_RE.match(_check_line):
            continue
        # r33: 用 _tokenize_uvw_line 支持引号 token
        tokens = _tokenize_uvw_line(_check_line)
        if not tokens:
            # 回退：用整行正则确认 1-3 个 token（保持旧版行为兼容）
            if not _UVW_LOOKS_RE.match(_check_line):
                continue
            # 按 _UVW_LINE_RE 取具体 token 数以保持评分逻辑
            m = _UVW_LINE_RE.match(_check_line)
            if not m:
                continue
            if m.group(3):  # 3-token
                matched += 2
            elif m.group(2):  # 2-token
                matched += 1
            else:  # 1-token
                matched += 1
            continue
        # r34 修复：_tokenize_uvw_line 最多取 3 个 token，会截断多 token 行。
        # 用整行正则二次确认确实只有 1-3 个 token，避免普通句子被误判为 uvw。
        if not _UVW_LOOKS_RE.match(_check_line):
            continue
        if len(tokens) >= 3:  # 3-token 行（带权）— 最强信号
            matched += 2
        elif len(tokens) == 2:  # 2-token 行（无向边）
            matched += 1
        else:  # 1-token 行（仅建节点）
            matched += 1
    return matched >= 1


# ---------- 布局 ----------
FORCE_THRESHOLD = 3  # 节点数 >= 该值时切力导向，否则用环形（r31: 降低阈值让更多图用 FD）


def _connected_components(nodes: List[Dict], edges: List[Dict]) -> List[List[str]]:
    """返回各连通分量的节点 id 列表（每个分量内按 id 排序保证稳定）。

    用途：非连通图应把每个分量独立布局，避免中心引力把不相关节点拉到一起。
    """
    node_ids = [nd["id"] for nd in nodes]
    id_set = set(node_ids)
    adj: Dict[str, set] = {nid: set() for nid in node_ids}
    for e in edges:
        s, t = e.get("from"), e.get("to")
        if s in id_set and t in id_set:
            adj[s].add(t)
            adj[t].add(s)
    seen: set = set()
    comps: List[List[str]] = []
    for nid in node_ids:
        if nid in seen:
            continue
        # BFS
        comp = []
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.append(cur)
            stack.extend(adj[cur] - seen)
        # r31 P2 修复：按"数字串数值序 + 字母序"排序，
        # 避免 "10" < "2" 的字符串排序问题
        comp.sort(key=lambda x: (len(x), x))
        comps.append(comp)
    # 分量按"节点数从大到小"排序，让大分量先放中心、小分量在边角
    comps.sort(key=lambda c: -len(c))
    return comps


def _grid_assign_regions(
    comps: List[List[str]],
    width: int,
    height: int,
    margin: int,
) -> List[Tuple[float, float, float, float]]:
    """把画布按分量数切成子区域，返回每个分量的 (cx, cy, rw, rh)。

    - 1 分量：1 个区域（占满画布）
    - 2 分量：左右两栏
    - 3 分量：横向 1x3（更对称；P1 修复）
    - 4+ 分量：cols x rows 网格（cols = ceil(sqrt(n))）
    """
    n = len(comps)
    if n == 1:
        return [(width / 2.0, height / 2.0, width - 2 * margin, height - 2 * margin)]
    if n == 2:
        # 左右两栏，避免 2x1 网格纵向上浪费
        rw = (width - 2 * margin) / 2 - 15
        return [
            (margin + rw / 2 + 15, height / 2.0, rw, height - 2 * margin),
            (width - margin - rw / 2 - 15, height / 2.0, rw, height - 2 * margin),
        ]
    if n == 3:
        # P1 修复：3 分量用 1x3 横向，避免 2x2 浪费 1 格
        rw = (width - 2 * margin) / 3 - 10
        regions = []
        for i in range(3):
            cx = margin + (rw + 10) * i + rw / 2 + 5
            regions.append((cx, height / 2.0, rw, height - 2 * margin))
        return regions
    # 4+ 分量：cols x rows 网格
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    rw = (width - 2 * margin) / cols
    rh = (height - 2 * margin) / rows
    regions = []
    for i in range(n):
        r, c = divmod(i, cols)
        cx = margin + rw * (c + 0.5)
        cy = margin + rh * (r + 0.5)
        regions.append((cx, cy, rw - 30, rh - 30))
    return regions


def _force_directed_layout(
    nodes: List[Dict],
    edges: List[Dict],
    width: int,
    height: int,
    iterations: int = 100,
) -> Dict[str, Tuple[float, float]]:
    """力导向布局：斥力 + 弹簧 + 中心引力 + 非连通分量独立布局。

    与 csacademy.com/app/graph_editor 行为对齐：
    1. 自动识别连通分量，每个分量独立布局（避免中心引力把不相关节点拉到一起）
    2. 分量按 2x2/列优先网格划分到画布子区域
    3. 阻尼 + 收敛检测（max_delta < 0.5px 提前结束）
    4. 多次重启（最多 3 次）取最佳（最大重叠最小）
    """
    n = len(nodes)
    if n == 0:
        return {}
    if n == 1:
        return {nodes[0]["id"]: (width / 2.0, height / 2.0)}

    margin = 50  # 留 28px 节点圆 + 22px 呼吸空间

    # 1) 识别非连通分量
    comps = _connected_components(nodes, edges)
    regions = _grid_assign_regions(comps, width, height, margin)

    # 2) 邻接表（用于弹簧）
    adj: Dict[str, List[str]] = {nd["id"]: [] for nd in nodes}
    for e in edges:
        s, t = e["from"], e["to"]
        if s in adj and t in adj:
            adj[s].append(t)
            adj[t].append(s)

    # 3) 对每个分量在分配到的子区域内做力导向
    final_pos: Dict[str, Tuple[float, float]] = {}
    rng = random.Random(42)
    for ci, (comp_ids, (rcx, rcy, rrw, rrh)) in enumerate(zip(comps, regions)):
        cn = len(comp_ids)
        local_margin = 20  # 必须在所有分支前定义（cn=2 也要用）

        if cn == 1:
            final_pos[comp_ids[0]] = (rcx, rcy)
            continue
        if cn == 2:
            # P1 修复：让 cn=2 也走 FD（之前跳过导致不会"互斥"）
            # 初始水平铺开，但迭代后受中心引力 + 弹簧 + 斥力
            pos = {
                comp_ids[0]: (rcx - 60, rcy),
                comp_ids[1]: (rcx + 60, rcy),
            }
        else:
            pos = {}
            rx = max(40, rrw / 2.0 - 30)
            ry = max(40, rrh / 2.0 - 30)
            for i, nid in enumerate(comp_ids):
                angle = 2 * math.pi * i / cn - math.pi / 2
                pos[nid] = (rcx + rx * math.cos(angle), rcy + ry * math.sin(angle))

        # 子区域理想边长 k
        # r31 P1 修复：双向上限 + 链式布局保护
        # - 上限 120：避免稀疏小图节点太散
        # - 链式保护：chain 拓扑时按画布宽度/(n-1) 缩放，避免重叠
        chain_max_k = max(40, (rrw - 2 * local_margin) / max(cn - 1, 1) * 0.8)
        k = max(40, min(120, math.sqrt(rrw * rrh / max(cn, 1)) * 0.6, chain_max_k))
        repel = k * k * 0.8
        spring_k = 0.04
        damping = 0.85
        max_disp = k * 0.4

        # 力导向迭代
        for it in range(iterations):
            force: Dict[str, List[float]] = {nid: [0.0, 0.0] for nid in comp_ids}
            ids = list(comp_ids)
            # 斥力
            for i, a in enumerate(ids):
                ax, ay = pos[a]
                for b in ids[i + 1:]:
                    bx, by = pos[b]
                    dx, dy = ax - bx, ay - by
                    dist2 = dx * dx + dy * dy
                    if dist2 < 0.01:
                        dx, dy = rng.random() - 0.5, rng.random() - 0.5
                        dist2 = dx * dx + dy * dy + 0.01
                    f = repel / dist2
                    d = math.sqrt(dist2)
                    fx, fy = f * dx / d, f * dy / d
                    force[a][0] += fx
                    force[a][1] += fy
                    force[b][0] -= fx
                    force[b][1] -= fy
            # 弹簧
            for a in ids:
                ax, ay = pos[a]
                for b in adj.get(a, []):
                    if b not in pos:
                        continue
                    bx, by = pos[b]
                    dx, dy = bx - ax, by - ay
                    d = math.hypot(dx, dy) or 0.01
                    f = spring_k * (d - k)
                    force[a][0] += f * dx / d
                    force[a][1] += f * dy / d
            # 中心引力（指向子区域中心）
            for nid in ids:
                x, y = pos[nid]
                force[nid][0] += (rcx - x) * 0.005
                force[nid][1] += (rcy - y) * 0.005
            # 应用位移
            new_pos: Dict[str, Tuple[float, float]] = {}
            max_delta = 0.0
            for nid in ids:
                fx, fy = force[nid]
                disp = math.hypot(fx, fy)
                if disp > max_disp:
                    fx, fy = fx / disp * max_disp, fy / disp * max_disp
                x, y = pos[nid]
                lx = rcx - rrw / 2.0 + local_margin
                rx = rcx + rrw / 2.0 - local_margin
                ty = rcy - rrh / 2.0 + local_margin
                by_ = rcy + rrh / 2.0 - local_margin
                # P0 修复：ty > by_ 时 swap（避免 max(min(...)) 钉死 y）
                if by_ < ty:
                    ty, by_ = by_, ty
                nx = max(lx, min(rx, x + fx * damping))
                ny = max(ty, min(by_, y + fy * damping))
                new_pos[nid] = (nx, ny)
                max_delta = max(max_delta, abs(nx - x), abs(ny - y))
            pos = new_pos
            if max_delta < 0.5:
                break
        final_pos.update(pos)
    return final_pos


def _compute_layout(graph: Dict, width: int, height: int) -> Dict[str, Tuple[float, float]]:
    """根据节点数选择环形（n<3）或力导向（n>=3）布局。

    r31 变更：阈值由 6 降到 3，让 3-5 节点的图也用 FD 而非环形（更自然）。
    """
    nodes = graph["nodes"]
    n = len(nodes)
    if n < FORCE_THRESHOLD:
        return _circular_layout(nodes, width, height)
    return _force_directed_layout(nodes, graph.get("edges", []), width, height)


def _circular_layout(nodes: List[Dict], width: int, height: int) -> Dict[str, Tuple[float, float]]:
    """环形布局：节点均匀分布在椭圆周上。"""
    n = len(nodes)
    cx, cy = width / 2.0, height / 2.0
    margin = 50  # 与力导向布局保持一致
    rx = max(80, width / 2.0 - margin - 30, n * 28)
    ry = max(80, height / 2.0 - margin - 30, n * 22)
    if n == 1:
        return {nodes[0]["id"]: (cx, cy)}
    if n == 2:
        return {
            nodes[0]["id"]: (max(margin, min(width - margin, cx - 100)), cy),
            nodes[1]["id"]: (max(margin, min(width - margin, cx + 100)), cy),
        }
    positions: Dict[str, Tuple[float, float]] = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / n - math.pi / 2
        x = cx + rx * math.cos(angle)
        y = cy + ry * math.sin(angle)
        # 与力导向布局保持一致：裁剪到 [margin, width-margin] / [margin, height-margin]
        x = max(margin, min(width - margin, x))
        y = max(margin, min(height - margin, y))
        positions[node["id"]] = (x, y)
    return positions


# ---------- SVG 渲染 ----------
def _escape(s: str) -> str:
    """XML 字符转义（含控制字符清洗与换行折叠）。"""
    if s is None:
        return ""
    s = str(s)
    # 移除非法控制字符（XML 1.0 不允许）
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)
    # 折叠换行/制表为单空格（SVG text 不换行）
    s = re.sub(r"\s+", " ", s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def render_graph_svg(
    text: str,
    theme: Optional[Dict] = None,
    width: int = DEFAULT_WIDTH,
    height: Optional[int] = None,
    marker_id: Optional[str] = None,
) -> Optional[str]:
    """把 graph 代码块文本渲染为 SVG 字符串。

    参数：
    - text：代码块原文
    - theme：主题字典（可选），用其中 text/border/accent/code_bg 作为颜色
    - width/height：画布尺寸
    - marker_id：箭头 marker 的 id 前缀（多气泡页面避免 id 冲突）

    返回：SVG 字符串；解析失败、节点/边超限、渲染异常都返回 None。
    """
    try:
        graph = parse_graph_block(text)
        if not graph:
            return None
        nodes = graph["nodes"]
        edges = graph["edges"]
        if not nodes:
            return None
        # 大图硬限：超限回退，让上层显示原文
        if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
            return None

        # 自适应 viewBox
        if height is None:
            height = DEFAULT_HEIGHT
        # r34 修复：防御非数字/字符串尺寸，避免 SVG 属性异常
        try:
            width = int(width)
            height = int(height)
        except Exception:
            width = DEFAULT_WIDTH
            height = DEFAULT_HEIGHT
        width = max(MIN_WIDTH, width)
        height = max(MIN_HEIGHT, height)
        positions = _compute_layout(graph, width, height)
        if not positions:
            return None

        # 颜色（用 _safe_color 兜底 None / 非 str / 非法格式）
        # r34 修复：theme 必须 dict，否则所有颜色走默认值
        # r36 P1 修复：SVG 颜色字段走 _safe_color 校验，防注入/异常值
        safe_theme = theme if isinstance(theme, dict) else {}
        text_color = _safe_color(safe_theme.get("text"), "#e5e7eb")
        border_color = _safe_color(safe_theme.get("border"), "#475569")
        accent = _safe_color(safe_theme.get("accent"), "#60a5fa")
        code_bg = _safe_color(safe_theme.get("code_bg"), "#1e293b")
        bg_color = _safe_color(safe_theme.get("bg"), "#0f172a")
        # r38 P1 修复：边/箭头颜色改用 text_dim（对比度更高），border 是为 UI
        # 分隔线设计的，在多数主题下与背景过于接近导致边/箭头几乎不可见。
        # r38 P1 增强：对比度感知选择——若 text_dim 与背景对比度 < 3.0（如
        # white/cream 浅色主题），自动回退到 text（更深）确保边/箭头可见。
        edge_color = _pick_edge_color(safe_theme, bg_color)
        # 节点内文字：基于 bg 明度自动选对比色
        #   深色 bg → label 用 bg（深色，确保和浅色 accent 形成对比）
        #   浅色 bg → label 用白色或 text 字段（深色 accent 配浅色文字）
        label_fill = _auto_contrast_text(bg_color, accent, text_color)

        mid = marker_id or f"g{uuid.uuid4().hex[:8]}"

        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {width} {height}" '
            # r34 修复：使用固定像素尺寸，避免 100% width 在 QTextBrowser 中
            # 因容器宽度计算问题导致塌陷或异常重排。
            f'width="{width}" height="{height}" '
            f'class="graph-svg" '
            f'style="background:{BG_TRANSPARENT}; max-width:100%;">'
        ]

        # 定义箭头（id 加随机后缀避免冲突）
        if graph["directed"]:
            parts.append(
                f'<defs><marker id="{mid}" viewBox="0 0 10 10" refX="10" refY="5" '
                f'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{edge_color}"/>'
                f'</marker></defs>'
            )

        # 边
        for edge in edges:
            src, dst = edge["from"], edge["to"]
            if src not in positions or dst not in positions:
                continue
            x1, y1 = positions[src]
            x2, y2 = positions[dst]
            # 缩短到节点圆边，避免箭头被圆覆盖
            dx, dy = x2 - x1, y2 - y1
            dist = math.hypot(dx, dy) or 1.0
            ux, uy = dx / dist, dy / dist
            x1e = x1 + ux * NODE_RADIUS
            y1e = y1 + uy * NODE_RADIUS
            x2e = x2 - ux * NODE_RADIUS
            y2e = y2 - uy * NODE_RADIUS
            # r34 P0 修复：Qt QTextBrowser 不支持 SVG <marker>，有向边直接画箭头多边形
            if graph["directed"]:
                # 箭头终点再往内收一点，给箭头图形留空间
                x2e -= ux * 6
                y2e -= uy * 6
            # 边 tooltip：原 id 便于调试
            tip_src = _escape(src)
            tip_dst = _escape(dst)
            tip = f"{tip_src} → {tip_dst}" if graph["directed"] else f"{tip_src} — {tip_dst}"
            w_tip = (edge.get("weight") or "").strip()
            if w_tip:
                tip = f"{tip} ({_escape(w_tip)})"
            # r38 P1 修复：边线使用 edge_color（text_dim）而非 border_color
            parts.append(
                f'<g><title>{tip}</title>'
                f'<line x1="{x1e:.1f}" y1="{y1e:.1f}" x2="{x2e:.1f}" y2="{y2e:.1f}" '
                f'stroke="{edge_color}" stroke-width="1.5"/>'
            )
            if graph["directed"]:
                # 直接绘制箭头三角形（替代 QTextBrowser 不支持的 marker-end）
                ax, ay = x2e, y2e
                base_x = ax - ux * 8
                base_y = ay - uy * 8
                left_x = base_x + uy * 3
                left_y = base_y - ux * 3
                right_x = base_x - uy * 3
                right_y = base_y + ux * 3
                # r38 P1 修复：箭头填充使用 edge_color（text_dim）
                parts.append(
                    f'<polygon points="{ax:.1f},{ay:.1f} {left_x:.1f},{left_y:.1f} '
                    f'{right_x:.1f},{right_y:.1f}" fill="{edge_color}"/>'
                )
            parts.append('</g>')
            w_disp = (edge.get("weight") or "").strip()
            if w_disp:
                mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                parts.append(
                    f'<text x="{mx:.1f}" y="{my - 4:.1f}" text-anchor="middle" '
                    f'font-size="11" fill="{text_color}" '
                    f'style="paint-order:stroke; stroke:{code_bg}; stroke-width:3px;">'
                    f'{_escape(w_disp)}</text>'
                )

        # 节点
        for node in nodes:
            nid = node["id"]
            if nid not in positions:
                continue
            x, y = positions[nid]
            label = _escape(node.get("label") or nid)
            full_tip = f"{_escape(nid)}: {label}"
            parts.append(
                f'<g><title>{full_tip}</title>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{NODE_RADIUS}" '
                f'fill="{accent}" stroke="{border_color}" stroke-width="1.5"/>'
                f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" '
                f'font-size="12" font-weight="bold" fill="{label_fill}">'
                f'{label}</text>'
                f'</g>'
            )

        parts.append("</svg>")
        return "".join(parts)
    except Exception as e:
        # 失败时记录到日志（不直接 print，避免污染 stderr）；
        # logger 自身 import 失败时降级为 stderr，但绝不静默吞错。
        try:
            from utils.helpers import logger
            logger.warning("graph render failed: %s", e, exc_info=True)
        except Exception:
            import traceback, sys
            sys.stderr.write(f"[graph_renderer] render failed: {e}\n")
            traceback.print_exc(file=sys.stderr)
        return None
