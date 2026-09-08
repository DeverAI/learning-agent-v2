"""
结构梳理图服务：分层树状布局 + SVG 渲染
AI 输出图结构数据（nodes + edges），本服务负责自动排版布局和 SVG 生成。
"""
import re
import math
import copy
import uuid
import json
import html as html_mod
from logger import get_logger

logger = get_logger()

# ===== 布局参数 =====
NODE_H = 46           # 节点最小高度（像素）
NODE_RX = 2           # 参考图接近直角，仅保留轻微圆角
NODE_PAD_X = 20       # 节点内水平 padding
NODE_PAD_Y = 14       # 节点内垂直 padding
NODE_MIN_W = 92       # 节点最小宽度
LINE_H = 21           # 多行文字行高
MAX_TEXT_W = 280      # 参考图使用较宽的公式文本块
H_GAP = 52            # 同层节点水平间距
V_GAP = 76            # 为正交汇合线留出呼吸空间
PADDING = 42          # 画布边距
ARROW_SIZE = 8        # 箭头大小


def _measure_text_width(text):
    """估算文字宽度（匹配 15px 题图字体，给公式字符留足横向空间）"""
    w = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            w += 11
            i += 2
            while i < len(text) and text[i].isalpha():
                i += 1
        elif ch == '$':
            i += 1
        elif '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
            w += 16
            i += 1
        else:
            w += 9
            i += 1
    return max(w, 30)


def _wrap_text(text):
    """按最大文字宽度自动换行，支持显式 \\n，返回 (lines, width)"""
    if not text:
        return [''], NODE_MIN_W

    def _wrap_segment(seg):
        seg_lines = []
        current = ''
        cur_w = 0
        for ch in seg:
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
                cw = 16
            else:
                cw = 9
            if cur_w + cw > MAX_TEXT_W and current:
                seg_lines.append(current)
                current = ch
                cur_w = cw
            else:
                current += ch
                cur_w += cw
        if current:
            seg_lines.append(current)
        return seg_lines or ['']

    # 先按显式换行符分段，再对每段做宽度折行
    lines = []
    for segment in str(text).split('\n'):
        lines.extend(_wrap_segment(segment))
    if not lines:
        lines = ['']
    width = max(min(max(_measure_text_width(l) for l in lines), MAX_TEXT_W) + NODE_PAD_X * 2, NODE_MIN_W)
    return lines, width


def _node_size(text):
    """返回节点 (width, height, lines)"""
    lines, w = _wrap_text(text)
    h = max(NODE_H, len(lines) * LINE_H + NODE_PAD_Y)
    return w, h, lines


# 常见 LaTeX → Unicode 映射表（缺失项保留原命令，不替换为空）
_LATEX_MAP = {
    r'\triangle': '△', r'\angle': '∠', r'\pi': 'π', r'\alpha': 'α',
    r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ', r'\theta': 'θ',
    r'\lambda': 'λ', r'\mu': 'μ', r'\sigma': 'σ', r'\omega': 'ω',
    r'\times': '×', r'\div': '÷', r'\pm': '±', r'\mp': '∓',
    r'\leq': '≤', r'\geq': '≥', r'\neq': '≠', r'\approx': '≈',
    r'\infty': '∞', r'\sqrt': '√', r'\circ': '∘', r'\cdot': '·',
    r'\parallel': '∥', r'\perp': '⊥', r'\therefore': '∴',
    r'\because': '∵', r'\rightarrow': '→', r'\leftarrow': '←',
    r'\Rightarrow': '⇒', r'\Leftarrow': '⇐', r'\sum': '∑',
    r'\prod': '∏', r'\int': '∫', r'\partial': '∂', r'\nabla': '∇',
    r'\forall': '∀', r'\exists': '∃', r'\in': '∈', r'\notin': '∉',
    r'\subset': '⊂', r'\supset': '⊃', r'\cup': '∪', r'\cap': '∩',
    r'\emptyset': '∅', r'\ldots': '…', r'\cdots': '⋯',
    r'\degree': '°', r'\prime': '′', r'\Delta': '△', r'\Omega': 'Ω',
    r'\equiv': '≡', r'\sim': '∼', r'\cong': '≅',
    r'\cdotp': '·', r'\bullet': '•', r'\to': '→', r'\gets': '←',
    r'\implies': '⇒', r'\iff': '⇔', r'\land': '∧', r'\lor': '∨',
    r'\neg': '¬',
}


_SAFE_COLORS = {"red","green","blue","yellow","orange","purple","cyan","magenta","lime","pink","teal","lavender","brown","beige","maroon","mint","olive","coral","navy","grey","gray","black","white","gold","silver"}


def _sanitize_color(raw_color: str) -> str:
    """过滤 SVG color 属性，只允许 #RGB、#RRGGBB 或有限安全颜色名。"""
    if not raw_color:
        return ""
    c = str(raw_color)[:20].strip().lower()
    if re.fullmatch(r'#([0-9a-f]{3}|[0-9a-f]{6})', c):
        return c
    if c in _SAFE_COLORS:
        return c
    return ""


def _latex_to_unicode(text):
    """将 LaTeX 表达式转换为 Unicode 字符，便于 SVG 直接显示。未映射命令保留原样。"""
    # 先去掉 $ 包裹
    text = text.replace('$', '')
    # 按长度降序替换（长的先替换，避免 \Rightarrow 被 \Right 截断）
    for latex, uni in sorted(_LATEX_MAP.items(), key=lambda x: -len(x[0])):
        text = text.replace(latex, uni)
    # 清理单目符号后的多余空格，使 "∠ BAD" 变成 "∠BAD"
    text = re.sub(r'(∠|△|√)\s+', r'\1', text)
    # 清理单目符号后紧跟的 { }，如 √{2} -> √2；支持一层嵌套花括号
    text = re.sub(r'(∠|△|√)\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', r'\1\2', text)
    # 角度上标规范化：90^{∘} / 90^∘ / 90^{o} / 90^o -> 90°
    text = text.replace('^{∘}', '°').replace('^∘', '°').replace('^{o}', '°').replace('^o', '°')
    # 未映射的 \xxx 命令保留原样，不再粗暴去掉反斜杠
    return text


# 安全限制：防止超大图导致服务资源耗尽
MAX_STRUCTURE_NODES = 200
MAX_STRUCTURE_EDGES = 500


def _validate_structure_graph(sg):
    """
    校验并修复 AI 输出的 structure_graph 数据。
    返回 (nodes, edges) 或 (None, None) 表示数据无效。
    """
    if not sg:
        return None, None
    # P2#12: AI 偶尔把图数据序列化成字符串，尝试解析一次
    if isinstance(sg, str):
        try:
            sg = json.loads(sg)
        except Exception:
            return None, None
    if not isinstance(sg, dict):
        return None, None
    nodes = sg.get("nodes", [])
    edges = sg.get("edges", [])
    if not isinstance(edges, list):
        edges = []
    if not nodes or not isinstance(nodes, list):
        return None, None

    # P1#6: 限制节点/边数量，防止 DoS
    if len(nodes) > MAX_STRUCTURE_NODES:
        logger.warning("Structure graph has too many nodes: %d > %d", len(nodes), MAX_STRUCTURE_NODES)
        return None, None
    if len(edges) > MAX_STRUCTURE_EDGES:
        logger.warning("Structure graph has too many edges: %d > %d", len(edges), MAX_STRUCTURE_EDGES)
        return None, None

    # 校验节点
    valid_nodes = []
    seen_ids = set()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        # P2#9: 强制 id 为非负整数
        try:
            nid = int(nid)
        except (TypeError, ValueError):
            continue
        if nid < 0 or nid in seen_ids:
            continue
        seen_ids.add(nid)

        # P0#1: 过滤 color，只允许 #RGB、#RRGGBB 或有限安全颜色名
        clean_color = _sanitize_color(node.get("color", ""))

        # P1#5: LaTeX → Unicode 转换
        raw_text = str(node.get("text") or "")[:200]
        clean_text = _latex_to_unicode(raw_text)

        # level 防御式处理
        try:
            clean_level = max(0, int(node.get("level", 0)))
        except (TypeError, ValueError):
            clean_level = 0

        # 节点类型：condition/key/conclusion，缺省由后续启发式推断
        raw_type = str(node.get("type", "")).strip().lower()
        clean_type = raw_type if raw_type in {"condition", "key", "conclusion"} else ""

        valid_nodes.append({
            "id": nid,
            "level": clean_level,
            "text": clean_text,
            "color": clean_color,
            "type": clean_type,
        })

    if not valid_nodes:
        return None, None

    # P1#8: 限制 level 上限，防止 AI/用户写入超大 level 导致 viewBox 异常
    max_allowed_level = max(len(valid_nodes), 5)
    for node in valid_nodes:
        node["level"] = min(node["level"], max_allowed_level)

    # 校验边：只保留两端节点都存在的边，过滤自环
    valid_ids = {n["id"] for n in valid_nodes}
    valid_edges = []
    seen_edges = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        f, t = edge.get("from"), edge.get("to")
        try:
            f, t = int(f), int(t)
        except (TypeError, ValueError):
            continue
        if f not in valid_ids or t not in valid_ids:
            continue
        if f == t:  # P2#8: 过滤自环边
            continue
        key = (f, t)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        valid_edges.append({"from": f, "to": t})

    return valid_nodes, valid_edges


def _sanitize_structure_graph(sg):
    """统一校验清洗 structure_graph，返回 {nodes, edges, is_user_edited?} 或 None。
    显式传入空图（{} 或 nodes 为空数组）视为有效清空请求。"""
    if not sg:
        return {"nodes": [], "edges": []}
    raw = sg
    # 字符串形式的空图需要先解析，才能按 dict 判断清空意图
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    nodes, edges = _validate_structure_graph(raw)
    if nodes is None:
        # 允许显式清空图（nodes 为空数组或缺失）
        if isinstance(raw, dict) and not raw.get("nodes"):
            nodes, edges = [], []
        else:
            return None
    result = {"nodes": nodes, "edges": edges}
    # 保留用户编辑标记，使 OCR 重新处理等场景能识别出手动编辑版本
    if isinstance(raw, dict) and raw.get("is_user_edited"):
        result["is_user_edited"] = True
    return result


# 题目对比模式默认配色（与 ai_service 中 color_table 浅色/深色保持一致）
_COMPARISON_DEFAULT_COLORS = [
    ("#3b82f6", "#60a5fa"),   # 蓝
    ("#ef4444", "#f87171"),   # 红
    ("#10b981", "#34d399"),   # 绿
    ("#f59e0b", "#fbbf24"),   # 黄
    ("#8b5cf6", "#a78bfa"),   # 紫
    ("#ec4899", "#f472b6"),   # 粉
    ("#06b6d4", "#22d3ee"),   # 青
]

_ALLOWED_REGION_KEYS = {"id", "node_ids", "paragraph_range", "color", "color_dark", "purpose"}


def _sanitize_comparison_regions(regions: dict) -> dict | None:
    """校验清洗题目对比模式分区数据。
    输入: {"regions": [...]} 或已被序列化的字符串。
    返回: {"regions": [...]} 或 None（数据无效时）。
    校验规则:
      - 最多保留 7 个 region
      - color/color_dark 只允许 HEX 或安全颜色名
      - node_ids 为正整数数组（>0），去重并保持顺序
      - paragraph_range 为长度 2 的整数数组 [start, end)
      - purpose 为字符串，可为空
    """
    if not regions:
        return {"regions": []}
    raw = regions
    # AI 偶尔把数据序列化成字符串，尝试解析一次
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    region_list = raw.get("regions", [])
    if not isinstance(region_list, list):
        return None

    valid_regions = []
    seen_ids = set()
    for i, r in enumerate(region_list[:7]):  # 限制最多 7 个 region
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id", f"r{i+1}"))[:32].strip()
        if not rid or rid in seen_ids:
            rid = f"r{i+1}"
        seen_ids.add(rid)

        # 节点 ID 从 0 开始，单个 region 最多保留 100 个
        raw_node_ids = r.get("node_ids", [])
        clean_node_ids = []
        if isinstance(raw_node_ids, list):
            for n in raw_node_ids[:200]:
                try:
                    nid = int(n)
                except (TypeError, ValueError):
                    continue
                if nid >= 0:
                    clean_node_ids.append(nid)
        # 去重并保持顺序
        seen_nodes = set()
        unique_node_ids = []
        for nid in clean_node_ids:
            if nid not in seen_nodes:
                seen_nodes.add(nid)
                unique_node_ids.append(nid)
        unique_node_ids = unique_node_ids[:100]
        if not unique_node_ids:
            continue

        # paragraph_range 必须为长度 2 的整数数组 [start, end) 且 start < end
        raw_range = r.get("paragraph_range", [0, 1])
        clean_range = None
        if isinstance(raw_range, (list, tuple)) and len(raw_range) >= 2:
            try:
                start = int(raw_range[0])
                end = int(raw_range[1])
                if start < end:
                    clean_range = [start, end]
            except (TypeError, ValueError, IndexError):
                pass
        if clean_range is None:
            continue

        # color 校验 HEX 或安全色，缺失或无效时使用默认配色
        clean_color = _sanitize_color(r.get("color", ""))
        clean_color_dark = _sanitize_color(r.get("color_dark", ""))
        if not clean_color:
            clean_color, default_dark = _COMPARISON_DEFAULT_COLORS[i % len(_COMPARISON_DEFAULT_COLORS)]
            if not clean_color_dark:
                clean_color_dark = default_dark
        if not clean_color_dark:
            clean_color_dark = clean_color

        purpose = html_mod.escape(str(r.get("purpose", "")).strip(), quote=True)
        clean_rid = html_mod.escape(rid, quote=True)

        region = {
            "id": clean_rid,
            "node_ids": unique_node_ids,
            "paragraph_range": clean_range,
            "color": clean_color,
            "color_dark": clean_color_dark,
            "purpose": purpose,
        }
        # 键名白名单：仅保留已知字段，防止 AI/用户注入额外字段影响前端渲染
        valid_regions.append({k: v for k, v in region.items() if k in _ALLOWED_REGION_KEYS})

    if not valid_regions:
        # 允许显式清空（regions 为空数组）
        if isinstance(raw, dict) and isinstance(raw.get("regions"), list) and not raw.get("regions"):
            result = {"regions": []}
        else:
            return None
    else:
        result = {"regions": valid_regions}

    # 保留用户编辑标记，与 _sanitize_structure_graph 语义保持一致，便于后续开放编辑时识别手动版本
    if isinstance(raw, dict) and raw.get("is_user_edited"):
        result["is_user_edited"] = True
    return result


def _shift_subtree(nid, shift, positions, children, visited):
    """迭代平移指定节点及其所有后代；visited 由调用方维护，防止同轮重复平移"""
    stack = [nid]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        if cur in positions:
            positions[cur][0] += shift
        # 将子节点入栈；倒序以保持与递归 DFS 相近的处理顺序
        for c in reversed(children.get(cur, [])):
            if c not in visited:
                stack.append(c)


def _resolve_overlaps(positions, node_widths, levels, children):
    """按层级自左向右消除节点重叠，并将位移传播给后代子树；以收敛为终止条件"""
    max_rounds = max(50, len(positions) * 2)
    for round_idx in range(max_rounds):
        moved = False
        # 每轮外层循环使用一个 visited 集合，避免同一节点通过多条祖先路径被重复平移
        visited = set()
        for lv in sorted(levels.keys()):
            ids = levels[lv]
            if len(ids) < 2:
                continue
            ids_sorted = sorted(ids, key=lambda nid: positions[nid][0])
            for i in range(1, len(ids_sorted)):
                prev = ids_sorted[i - 1]
                cur = ids_sorted[i]
                min_dist = (node_widths[prev] + node_widths[cur]) / 2 + H_GAP
                actual = positions[cur][0] - positions[prev][0]
                if actual < min_dist:
                    shift = min_dist - actual
                    _shift_subtree(cur, shift, positions, children, visited)
                    moved = True
        if not moved:
            break
    else:
        logger.warning("_resolve_overlaps did not converge after %d rounds (nodes=%d)", max_rounds, len(positions))


def compute_layout(nodes, edges):
    """
    分层树状布局（类似 Sugiyama）：自动计算每个节点的 (x, y) 坐标。
    策略：
    1. 校验并规范 level，保证所有边都从低 level 指向高 level；
    2. 按 level 分层，同层节点按父节点平均 x 坐标排序，减少边交叉；
    3. 同层按文字宽度自适应水平排列，并自动消除重叠；
    4. 支持节点多行文字，层间距根据节点高度自适应。
    """
    if not nodes:
        return {}, {}, {}, [], []

    # 深拷贝节点和边，避免修改调用方传入的对象
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy([e for e in edges if isinstance(e, dict)])

    node_map = {n["id"]: n for n in nodes}
    node_sizes = {nid: _node_size(n.get("text", "")) for nid, n in node_map.items()}
    node_widths = {nid: s[0] for nid, s in node_sizes.items()}
    node_heights = {nid: s[1] for nid, s in node_sizes.items()}

    # 规范 level：确保边 from.level < to.level
    # 先检测并打破环，避免循环边导致 level 无限提升
    def _break_cycles(nodes, edges):
        valid_ids = {n["id"] for n in nodes}
        adj = {nid: [] for nid in valid_ids}
        edge_idx = {nid: [] for nid in valid_ids}
        for i, edge in enumerate(edges):
            f, t = edge.get("from"), edge.get("to")
            if f in valid_ids and t in valid_ids and f != t:
                adj[f].append(t)
                edge_idx[f].append(i)
        # 迭代 DFS 找环并移除环上一条边，避免递归过深
        removed = set()
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in valid_ids}
        for start in valid_ids:
            if color.get(start, WHITE) != WHITE:
                continue
            stack = [(start, 0)]  # (node, next neighbor index to explore)
            color[start] = GRAY
            while stack:
                nid, next_idx = stack[-1]
                neighbors = adj.get(nid, [])
                if next_idx < len(neighbors):
                    stack[-1] = (nid, next_idx + 1)
                    nb = neighbors[next_idx]
                    nb_color = color.get(nb, WHITE)
                    if nb_color == GRAY:
                        # 发现环，移除当前边
                        removed.add(edge_idx[nid][next_idx])
                    elif nb_color == WHITE:
                        color[nb] = GRAY
                        stack.append((nb, 0))
                else:
                    color[nid] = BLACK
                    stack.pop()
        return [edge for i, edge in enumerate(edges) if i not in removed]

    edges = _break_cycles(nodes, edges)

    changed = True
    max_iter = len(nodes) * 2
    iteration = 0
    max_level_cap = len(nodes) + 5  # 限制最大 level，防止反向边导致 viewBox 异常拉伸
    while changed and iteration < max_iter:
        changed = False
        iteration += 1
        for edge in edges:
            f, t = edge.get("from"), edge.get("to")
            if f not in node_map or t not in node_map:
                continue
            fl = node_map[f].get("level", 0)
            tl = node_map[t].get("level", 0)
            if tl <= fl:
                new_level = min(fl + 1, max_level_cap)
                if new_level != tl:
                    node_map[t]["level"] = new_level
                    changed = True

    # 保留所有连接低 level 到高 level 的边（含跨层边）。参考图中存在跨层推理关系
    # （如 level1 的 "AB=AB'" 直接推出 level4 的 "菱形 ABEB'"），应完整保留。
    edges = [
        e for e in edges
        if node_map[e["to"]].get("level", 0) > node_map[e["from"]].get("level", 0)
    ]

    # 按 level 分组
    levels = {}
    for nid, node in node_map.items():
        lv = node.get("level", 0)
        levels.setdefault(lv, []).append(nid)
    sorted_levels = sorted(levels.keys())

    # 构建父子关系（所有边）
    parents = {nid: [] for nid in node_map}
    children = {nid: [] for nid in node_map}
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f in node_map and t in node_map:
            children[f].append(t)
            parents[t].append(f)

    # 计算每层的 y 坐标（按该层最高节点自适应）
    level_height = {lv: max(node_heights[nid] for nid in levels[lv]) for lv in sorted_levels}
    level_y = {}
    y = 0
    for lv in sorted_levels:
        level_y[lv] = y
        y += level_height[lv] + V_GAP

    # 计算每层节点的初始 x 坐标
    positions = {}
    for lv in sorted_levels:
        ids = levels[lv]
        if lv == sorted_levels[0]:
            # 首层按 id 稳定排序
            ids_sorted = sorted(ids)
        else:
            # 按父节点平均 x 排序，无父节点排最后
            def _sort_key(nid):
                ps = parents[nid]
                placed = [p for p in ps if p in positions]
                if placed:
                    avg_x = sum(positions[p][0] for p in placed) / len(placed)
                    return (avg_x, nid)
                return (float('inf'), nid)
            ids_sorted = sorted(ids, key=_sort_key)

        total_w = sum(node_widths[nid] for nid in ids_sorted) + (len(ids_sorted) - 1) * H_GAP
        start_x = -total_w / 2
        cur_x = start_x
        target_y = level_y[lv] + level_height[lv] / 2
        for nid in ids_sorted:
            nw = node_widths[nid]
            positions[nid] = [cur_x + nw / 2, target_y]
            cur_x += nw + H_GAP

    # 消除同层节点重叠（位移会传播给后代）
    _resolve_overlaps(positions, node_widths, levels, children)

    # 返回处理后的 nodes（深拷贝且 level 已规范化），便于调用方与渲染视图保持一致
    return positions, node_widths, node_heights, edges, nodes


def render_svg(nodes, edges, positions, node_widths, node_heights=None, dark_mode=False, marker_id=None):
    """
    根据节点、边、布局位置和节点尺寸生成 SVG 字符串。
    支持多行文字节点与圆角矩形。
    marker_id 用于唯一标识箭头 marker，防止同一页面多个 SVG 冲突；
    若传 None，则内部自动生成唯一 ID。
    """
    # 过滤 marker_id 中的非法字符，防止 SVG 属性注入；未提供或过滤后为空则自动生成
    marker_id = re.sub(r'[^a-zA-Z0-9_-]', '', str(marker_id or ''))
    if not marker_id:
        marker_id = f"sg-arrow-{uuid.uuid4().hex[:8]}"

    if not nodes:
        return ""
    if not positions or not all(n["id"] in positions for n in nodes):
        return ""
    if node_heights is None:
        node_heights = {n["id"]: NODE_H for n in nodes}

    # 计算 viewBox
    min_x = float('inf')
    max_x = float('-inf')
    min_y = float('inf')
    max_y = float('-inf')
    for nid, (x, y) in positions.items():
        nw = node_widths.get(nid, NODE_MIN_W)
        nh = node_heights.get(nid, NODE_H)
        min_x = min(min_x, x - nw / 2)
        max_x = max(max_x, x + nw / 2)
        min_y = min(min_y, y - nh / 2)
        max_y = max(max_y, y + nh / 2)

    # 跨层边走节点整体边界外的独立车道；车道本身也必须计入 viewBox。
    node_min_x, node_max_x = min_x, max_x
    graph_center_x = (node_min_x + node_max_x) / 2
    node_map = {n["id"]: n for n in nodes}
    cross_routes = {}
    left_lane = 0
    right_lane = 0
    cross_edges = sorted(
        [e for e in edges if e.get("from") in positions and e.get("to") in positions and
         node_map.get(e.get("to"), {}).get("level", 0) - node_map.get(e.get("from"), {}).get("level", 0) > 1],
        key=lambda e: (node_map[e["from"]].get("level", 0), node_map[e["to"]].get("level", 0), e["from"], e["to"]),
    )
    for edge in cross_edges:
        f, t = edge["from"], edge["to"]
        midpoint = (positions[f][0] + positions[t][0]) / 2
        if midpoint < graph_center_x:
            route_x = node_min_x - H_GAP * 0.65 - left_lane * 14
            left_lane += 1
        else:
            route_x = node_max_x + H_GAP * 0.65 + right_lane * 14
            right_lane += 1
        cross_routes[(f, t)] = route_x
        min_x = min(min_x, route_x)
        max_x = max(max_x, route_x)

    # 左侧额外留出“分析”标题区，避免标题压到第一列节点。
    min_x -= PADDING + 72
    max_x += PADDING
    min_y -= PADDING
    max_y += PADDING
    vw = max_x - min_x
    vh = max_y - min_y

    # 所有业务色由页面全局主题变量决定；SVG 只表达语义层次。
    bg_color = "var(--paper-bg)"
    edge_color = "var(--text)"
    node_text = "var(--text)"
    condition_bg = "var(--sg-condition-bg)"
    condition_border = "var(--sg-condition-border)"
    key_bg = "var(--sg-key-bg)"
    key_border = "var(--sg-key-border)"
    conclusion_bg = "var(--sg-conclusion-bg)"
    conclusion_border = "var(--sg-conclusion-border)"
    default_bg = condition_bg
    default_border = condition_border

    # 计算入度/出度，用于启发式着色
    in_degree = {n["id"]: 0 for n in nodes}
    out_degree = {n["id"]: 0 for n in nodes}
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f in in_degree and t in out_degree:
            out_degree[f] += 1
            in_degree[t] += 1

    svg_parts = []
    svg_parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x} {min_y} {vw} {vh}" '
        f'width="{vw}" height="{vh}" '
        f'style="background:{bg_color};border-radius:8px;display:block">'
    )

    # 箭头 marker（使用唯一 ID，避免同一页面多个 SVG 冲突）
    svg_parts.append('<defs>')
    svg_parts.append(
        f'<marker id="{marker_id}" viewBox="0 0 10 10" refX="9" refY="5" '
        f'markerWidth="{ARROW_SIZE}" markerHeight="{ARROW_SIZE}" orient="auto-start-reverse">'
        f'<path d="M 1 1 L 9 5 L 1 9" fill="none" stroke="{edge_color}" stroke-width="1.6" '
        f'stroke-linecap="square" stroke-linejoin="miter"/>'
        f'</marker>'
    )
    svg_parts.append('</defs>')

    # 标题“分析”
    title_color = "var(--accent)"
    svg_parts.append(
        f'<text x="{min_x + 18}" y="{min_y + 31}" '
        f'font-size="23" font-weight="600" fill="{title_color}" '
        f'font-family="STKaiti, KaiTi, Kaiti SC, sans-serif">分析</text>'
    )

    # 确定首层和末层 level
    levels_present = {node.get("level", 0) for node in nodes}
    min_lv = min(levels_present)
    max_lv = max(levels_present)

    # 计算每层上下边界，用于同层过渡边共享 horizontal-y
    level_node_ids = {}
    for node in nodes:
        lv = node.get("level", 0)
        level_node_ids.setdefault(lv, []).append(node["id"])
    sorted_levels = sorted(level_node_ids.keys())
    level_top = {}
    level_bottom = {}
    for lv in sorted_levels:
        ids = level_node_ids[lv]
        level_top[lv] = min(positions[nid][1] - node_heights.get(nid, NODE_H) / 2 for nid in ids)
        level_bottom[lv] = max(positions[nid][1] + node_heights.get(nid, NODE_H) / 2 for nid in ids)

    # 为每一对 (from_level, to_level) 计算共享 routing_y，使多父节点汇入同一子节点时线条对齐；支持跨层边
    routing_y = {}
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f not in positions or t not in positions or f not in node_map or t not in node_map:
            continue
        fl = node_map[f].get("level", 0)
        tl = node_map[t].get("level", 0)
        if fl >= tl:
            continue
        key = (fl, tl)
        if key not in routing_y:
            routing_y[key] = (level_bottom[fl] + level_top[tl]) / 2

    # 节点延后统一输出，使连线始终位于色块下方，避免线条穿过文字。
    node_parts = []
    for node in nodes:
        nid = node["id"]
        if nid not in positions:
            continue
        x, y = positions[nid]
        nw = node_widths.get(nid, NODE_MIN_W)
        nh = node_heights.get(nid, NODE_H)
        rx = x - nw / 2
        ry = y - nh / 2
        lv = node.get("level", 0)
        ntype = node.get("type", "")

        # 自定义颜色（接口）优先；在 render_svg 中再次清洗，防止独立调用时注入
        semantic_type = ntype
        if ntype:
            # AI 显式提供 type 时优先按语义着色
            if ntype == "condition":
                bg = condition_bg
                border = condition_border
            elif ntype == "key":
                bg = key_bg
                border = key_border
            elif ntype == "conclusion":
                bg = conclusion_bg
                border = conclusion_border
            else:
                bg = default_bg
                border = default_border
        else:
            # 无 type 时按层级/出入度启发式推断
            if lv == min_lv:
                semantic_type = "condition"
                bg = condition_bg
                border = condition_border
            elif out_degree.get(nid, 0) == 0:
                semantic_type = "conclusion"
                bg = conclusion_bg
                border = conclusion_border
            elif in_degree.get(nid, 0) > 1:
                semantic_type = "key"
                bg = key_bg
                border = key_border
            else:
                semantic_type = "condition"
                bg = default_bg
                border = default_border

        # 节点矩形（带 data-node-id 供前端对比模式匹配）
        node_parts.append(
            f'<rect x="{rx}" y="{ry}" width="{nw}" height="{nh}" '
            f'rx="{NODE_RX}" fill="{bg}" stroke="{border}" stroke-width="0.8" '
            f'vector-effect="non-scaling-stroke" data-node-id="{nid}" '
            f'data-node-type="{semantic_type}" class="sg-node sg-node-{semantic_type}"/>'
        )

        # 文字（支持多行居中，使用数学斜体风格）
        raw_text = node.get("text", "")
        lines, _ = _wrap_text(raw_text)
        font_size = 15
        start_dy = -(len(lines) - 1) * LINE_H / 2
        for i, line in enumerate(lines):
            line_text = html_mod.escape(line)
            node_parts.append(
                f'<text x="{x}" y="{y + start_dy + i * LINE_H}" text-anchor="middle" '
                f'dominant-baseline="middle" font-size="{font_size}" fill="{node_text}" '
                f'font-family="Times New Roman, STIX Two Math, STKaiti, KaiTi, Kaiti SC, serif" '
                f'font-style="italic" pointer-events="none">{line_text}</text>'
            )

    # 绘制边（正交折线 + 开口箭头），随后再覆盖节点色块。
    for edge in edges:
        f, t = edge.get("from"), edge.get("to")
        if f not in positions or t not in positions:
            continue
        fx, fy = positions[f]
        tx, ty = positions[t]
        fh = node_heights.get(f, NODE_H)
        th = node_heights.get(t, NODE_H)
        # 从父节点底部中心到子节点顶部中心
        sx, sy = fx, fy + fh / 2
        ex, ey = tx, ty - th / 2

        fl = node_map.get(f, {}).get("level", 0)
        tl = node_map.get(t, {}).get("level", 0)
        level_gap = tl - fl

        if level_gap == 1:
            # 相邻 level：共享 horizontal-y，直线汇入
            ry = routing_y.get((fl, tl), (sy + ey) / 2)
            path_d = f"M {sx} {sy} L {sx} {ry} L {ex} {ry} L {ex} {ey}"
        else:
            # 跨层边：沿节点整体边界外的独立车道下行，避免穿过中间节点。
            gap_below_source = level_bottom[fl] + V_GAP / 4
            gap_above_target = level_top[tl] - V_GAP / 4
            lane_x = cross_routes.get((f, t), node_max_x + H_GAP * 0.65)
            path_d = f"M {sx} {sy} L {sx} {gap_below_source} L {lane_x} {gap_below_source} L {lane_x} {gap_above_target} L {ex} {gap_above_target} L {ex} {ey}"

        svg_parts.append(
            f'<path d="{path_d}" fill="none" stroke="{edge_color}" stroke-width="1.8" '
            f'stroke-linecap="square" stroke-linejoin="miter" vector-effect="non-scaling-stroke" '
            f'marker-end="url(#{marker_id})"/>'
        )

    svg_parts.extend(node_parts)
    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def process_structure_graph(structure_graph, dark_mode=False, marker_id=None):
    """
    完整处理流程：校验 → 布局计算 → SVG 渲染。
    输入: structure_graph = {nodes: [...], edges: [...]}
    输出: {nodes, edges, positions, svg} 或 None
    """
    nodes, edges = _validate_structure_graph(structure_graph)
    if not nodes:
        return None

    positions, node_widths, node_heights, edges, nodes = compute_layout(nodes, edges)
    if marker_id is None:
        marker_id = f"sg-arrow-{uuid.uuid4().hex[:8]}"
    svg = render_svg(nodes, edges, positions, node_widths, node_heights, dark_mode, marker_id=marker_id)

    return {
        "nodes": nodes,
        "edges": edges,
        "positions": {str(k): v for k, v in positions.items()},
        "svg": svg,
    }
