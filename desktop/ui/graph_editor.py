"""OISystem 轻量图论示意图编辑器。

基于 QGraphicsScene/QGraphicsView，支持：
- 节点增删、拖拽、标签编辑
- 边增删、权重编辑、有向/无向切换
- 自动力导向布局
- 导出 PNG / 文本邻接表
- 跟随当前主题

参考交互：CS Academy Graph Editor（简化为本地轻量版）。
"""
import math
from typing import List, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QInputDialog, QMessageBox, QButtonGroup,
    QGraphicsScene, QGraphicsView, QGraphicsEllipseItem,
    QGraphicsSimpleTextItem, QGraphicsItem, QGraphicsLineItem,
    QPlainTextEdit, QSplitter,
)
from PySide6.QtCore import Qt, QPointF, QRectF, QLineF, QTimer, Signal
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QMouseEvent,
    QWheelEvent, QKeyEvent, QTransform, QPixmap,
)

from ui.frame_mixin import RoundedFrameMixin
from ui.themes import ThemeManager
from ui.graph_renderer import (
    parse_uvw_block, parse_graph_block, _compute_layout, _pick_edge_color,
    MAX_NODES, MAX_EDGES,
)
from utils.helpers import logger

NODE_RADIUS = 24
NODE_DIAMETER = NODE_RADIUS * 2
ARROW_SIZE = 10


# ---------- 节点 ----------
class NodeItem(QGraphicsEllipseItem):
    """图节点：圆形、可拖拽、可编辑标签。"""

    def __init__(self, node_id: str, label: str = "", x: float = 0, y: float = 0,
                 radius: int = NODE_RADIUS, parent=None):
        super().__init__(-radius, -radius, NODE_DIAMETER, NODE_DIAMETER, parent)
        self.node_id = node_id
        self._label = label or node_id
        self._radius = radius
        self.setPos(x, y)
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setZValue(2)

        self._text = QGraphicsSimpleTextItem(self)
        self._text.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        self._text.setAcceptedMouseButtons(Qt.NoButton)
        self._update_text()
        # r37 修复：力导向模式下被用户拖拽过的节点应固定（参考 CS Academy）
        self._pinned = False
        self._drag_start_pos: Optional[QPointF] = None

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, value: str):
        self._label = value or self.node_id
        self._update_text()

    def center(self) -> QPointF:
        return self.sceneBoundingRect().center()

    def _update_text(self):
        self._text.setText(self._label)
        br = self._text.boundingRect()
        self._text.setPos(-br.width() / 2, -br.height() / 2)

    def mousePressEvent(self, event: QMouseEvent):
        # r37 修复：力导向模式下开始拖拽时临时取消固定，允许本次拖动
        if self.scene() and getattr(self.scene(), "_mode", "") == "force":
            self._pinned = False
            self._drag_start_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        super().mouseReleaseEvent(event)
        # r37 修复：力导向模式下真正发生拖拽（位移超过阈值）后才固定节点（参考 CS Academy）
        if self.scene() and getattr(self.scene(), "_mode", "") == "force":
            start = self._drag_start_pos
            if start is not None:
                dist = math.hypot(self.x() - start.x(), self.y() - start.y())
                if dist >= 3.0:  # 3px 阈值，避免纯单击也固定
                    self._pinned = True
            self._drag_start_pos = None

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and self.scene():
            # 限制在场景范围内（粗略）
            rect = self.scene().sceneRect()
            if isinstance(value, QPointF):
                x = max(rect.left() + self._radius,
                        min(rect.right() - self._radius, value.x()))
                y = max(rect.top() + self._radius,
                        min(rect.bottom() - self._radius, value.y()))
                value = QPointF(x, y)
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self._edges():
                edge.update_position()
        return super().itemChange(change, value)

    def _edges(self):
        if self.scene() is None:
            return []
        return [item for item in self.scene().items()
                if isinstance(item, EdgeItem) and (item.source is self or item.target is self)]

    def paint(self, painter: QPainter, option, widget=None):
        t = ThemeManager().current_theme
        painter.setRenderHint(QPainter.Antialiasing)
        r = self._radius
        accent = QColor(t.get("accent", "#3b82f6"))
        surface = QColor(t.get("surface", "#1e293b"))
        text = QColor(t.get("text", "#e2e8f0"))

        painter.setPen(QPen(accent, 2))
        painter.setBrush(QBrush(surface))
        painter.drawEllipse(-r, -r, r * 2, r * 2)

        # 选中高亮
        if self.isSelected():
            painter.setPen(QPen(accent, 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(-r - 2, -r - 2, r * 2 + 4, r * 2 + 4)

        self._text.setBrush(QBrush(text))


# ---------- 边 ----------
class EdgeItem(QGraphicsItem):
    """图边：直线、可选箭头、可编辑权重。"""

    def __init__(self, source: NodeItem, target: NodeItem,
                 weight: str = "", directed: bool = False, parent=None):
        super().__init__(parent)
        self.source = source
        self.target = target
        self.weight = weight
        self.directed = directed
        self.setZValue(1)
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.update_position()

    def boundingRect(self) -> QRectF:
        # r39 P0 修复：source/target 可能已被 removeItem 但 Python 对象仍存在
        try:
            p1 = self.source.center()
            p2 = self.target.center()
        except RuntimeError:
            return QRectF(0, 0, 0, 0)
        x1, y1, x2, y2 = p1.x(), p1.y(), p2.x(), p2.y()
        pad = ARROW_SIZE + 6
        return QRectF(min(x1, x2) - pad, min(y1, y2) - pad,
                      abs(x2 - x1) + pad * 2, abs(y2 - y1) + pad * 2)

    def update_position(self):
        try:
            self.prepareGeometryChange()
            self.update()
        except RuntimeError:
            # 节点已销毁
            pass

    def paint(self, painter: QPainter, option, widget=None):
        # r39 P0 修复：节点销毁后不绘制，避免 RuntimeError
        try:
            t = ThemeManager().current_theme
            painter.setRenderHint(QPainter.Antialiasing)
            # r38 P1：与 renderer 保持一致，使用对比度感知的边色
            bg = t.get("bg", "#0f172a")
            color = QColor(_pick_edge_color(t, bg))
            pen = QPen(color, 2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            p1 = self.source.center()
            p2 = self.target.center()
        except RuntimeError:
            return
        painter.drawLine(p1, p2)

        # 箭头
        if self.directed:
            angle = math.atan2(p2.y() - p1.y(), p2.x() - p1.x())
            rad = NODE_RADIUS
            ax = p1.x() + math.cos(angle) * (self._distance(p1, p2) - rad)
            ay = p1.y() + math.sin(angle) * (self._distance(p1, p2) - rad)
            self._draw_arrow(painter, ax, ay, angle, color)

        # 权重文本
        if self.weight:
            mid = QPointF((p1.x() + p2.x()) / 2, (p1.y() + p2.y()) / 2)
            painter.setPen(QColor(t.get("text", "#e2e8f0")))
            painter.setFont(QFont("Microsoft YaHei", 8))
            rect = QRectF(mid.x() - 30, mid.y() - 10, 60, 20)
            painter.drawText(rect, Qt.AlignCenter, self.weight)

    @staticmethod
    def _distance(a: QPointF, b: QPointF) -> float:
        return math.hypot(a.x() - b.x(), a.y() - b.y())

    def _draw_arrow(self, painter: QPainter, x: float, y: float,
                    angle: float, color: QColor):
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        points = [
            QPointF(x, y),
            QPointF(x - ARROW_SIZE * math.cos(angle - math.pi / 6),
                    y - ARROW_SIZE * math.sin(angle - math.pi / 6)),
            QPointF(x - ARROW_SIZE * math.cos(angle + math.pi / 6),
                    y - ARROW_SIZE * math.sin(angle + math.pi / 6)),
        ]
        painter.drawPolygon(points)


# ---------- 临时边（连线拖拽预览） ----------
class TempEdgeItem(QGraphicsLineItem):
    """Draw 模式下从节点拖到鼠标的预览线。"""

    def __init__(self, source: NodeItem, parent=None):
        super().__init__(parent)
        self.source = source
        self.setZValue(0)
        # 预览线颜色跟随主题（对比度感知），替代硬编码 #94a3b8
        try:
            t = ThemeManager().current_theme
            bg = t.get("bg", "#0f172a")
            color = QColor(_pick_edge_color(t, bg))
        except Exception:
            color = QColor("#94a3b8")
        self.setPen(QPen(color, 2, Qt.DashLine))
        self.update_end(source.center())

    def update_end(self, end: QPointF):
        self.setLine(QLineF(self.source.center(), end))


# ---------- 场景 ----------
class GraphScene(QGraphicsScene):
    """图编辑场景：管理节点/边，处理工具模式交互。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSceneRect(-400, -300, 800, 600)
        self._nodes: List[NodeItem] = []
        self._edges: List[EdgeItem] = []
        self._mode = "draw"          # draw / edit / delete / force
        self._directed = False
        self._temp_edge: Optional[TempEdgeItem] = None
        self._edge_start: Optional[NodeItem] = None
        self._id_counter = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._force_step)
        self._timer.setInterval(16)
        self._force_running = False

    # ---- 属性 ----
    def set_mode(self, mode: str):
        self._mode = mode.lower()
        if self._mode == "force":
            # r37 修复：重新启用 force 时清除旧固定态，允许整体重新布局
            for node in self._nodes:
                node._pinned = False
            self.start_force()
        else:
            self.stop_force()
        self._clear_temp_edge()

    def set_directed(self, directed: bool):
        self._directed = directed
        for edge in self._edges:
            edge.directed = directed
            edge.update()

    def directed(self) -> bool:
        return self._directed

    # ---- 节点/边 CRUD ----
    def add_node(self, x: float = None, y: float = None, label: str = "") -> NodeItem:
        self._id_counter += 1
        node_id = str(self._id_counter)
        if x is None:
            x = self._id_counter * 15 % self.sceneRect().width() - 400
        if y is None:
            y = self._id_counter * 23 % self.sceneRect().height() - 300
        node = NodeItem(node_id, label or node_id, x, y)
        self.addItem(node)
        self._nodes.append(node)
        return node

    def add_edge(self, source: NodeItem, target: NodeItem,
                 weight: str = "") -> EdgeItem:
        # 避免重复边（同方向）
        for e in self._edges:
            if e.source is source and e.target is target:
                return e
            if not self._directed and e.source is target and e.target is source:
                return e
        edge = EdgeItem(source, target, weight, self._directed)
        self.addItem(edge)
        self._edges.append(edge)
        return edge

    def remove_node(self, node: NodeItem):
        edges_to_remove = [e for e in self._edges
                           if e.source is node or e.target is node]
        for e in edges_to_remove:
            self.remove_edge(e)
        if node in self._nodes:
            self._nodes.remove(node)
        self.removeItem(node)

    def remove_edge(self, edge: EdgeItem):
        if edge in self._edges:
            self._edges.remove(edge)
        self.removeItem(edge)

    def clear_graph(self):
        for edge in list(self._edges):
            self.remove_edge(edge)
        for node in list(self._nodes):
            self.remove_node(node)
        self._id_counter = 0

    # ---- 鼠标交互 ----
    def mousePressEvent(self, event: QMouseEvent):
        pos = event.scenePos()
        item = self.itemAt(pos, QTransform())

        if self._mode == "draw":
            if isinstance(item, NodeItem):
                self._edge_start = item
                self._temp_edge = TempEdgeItem(item)
                self.addItem(self._temp_edge)
            else:
                # 空白处添加节点
                self.add_node(pos.x(), pos.y())
            return

        if self._mode == "edit":
            if isinstance(item, NodeItem):
                self._edit_node(item)
            elif isinstance(item, EdgeItem):
                self._edit_edge(item)
            return

        if self._mode == "delete":
            if isinstance(item, NodeItem):
                self.remove_node(item)
            elif isinstance(item, EdgeItem):
                self.remove_edge(item)
            return

        # force 模式允许默认拖拽
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._temp_edge is not None:
            self._temp_edge.update_end(event.scenePos())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._temp_edge is not None:
            end_item = self.itemAt(event.scenePos(), QTransform())
            if isinstance(end_item, NodeItem) and end_item is not self._edge_start:
                self.add_edge(self._edge_start, end_item)
            self._clear_temp_edge()
            return
        super().mouseReleaseEvent(event)

    def _clear_temp_edge(self):
        if self._temp_edge is not None:
            self.removeItem(self._temp_edge)
            self._temp_edge = None
        self._edge_start = None

    def _edit_node(self, node: NodeItem):
        text, ok = QInputDialog.getText(None, "编辑节点", "节点标签：",
                                        text=node.label)
        if ok:
            node.label = text.strip()

    def _edit_edge(self, edge: EdgeItem):
        text, ok = QInputDialog.getText(None, "编辑边权", "边权重：",
                                        text=edge.weight)
        if ok:
            edge.weight = text.strip()
            edge.update()

    # ---- 力导向布局 ----
    def start_force(self):
        if not self._force_running:
            self._timer.start()
            self._force_running = True

    def stop_force(self):
        if self._force_running:
            self._timer.stop()
            self._force_running = False

    def _force_step(self):
        # r39 P1 修复：force timer 与 UVW debounce 存在竞争窗口，_nodes 可能在迭代中被清空
        if len(self._nodes) < 2:
            return
        # 快照当前节点/边列表，避免迭代中被修改
        snapshot_nodes = list(self._nodes)
        snapshot_edges = list(self._edges)
        if len(snapshot_nodes) < 2:
            return
        repel = 5000.0
        spring = 0.003
        spring_len = 120.0
        center_force = 0.005
        damping = 0.85
        max_disp = 30.0

        rect = self.sceneRect()
        cx, cy = rect.center().x(), rect.center().y()

        velocities = {n: QPointF(0, 0) for n in snapshot_nodes}

        # 斥力
        for i, a in enumerate(snapshot_nodes):
            for b in snapshot_nodes[i + 1:]:
                dx = a.x() - b.x()
                dy = a.y() - b.y()
                dist = math.hypot(dx, dy) or 0.1
                fx = repel * dx / (dist ** 3)
                fy = repel * dy / (dist ** 3)
                velocities[a] += QPointF(fx, fy)
                velocities[b] += QPointF(-fx, -fy)

        # 引力（边）
        for edge in snapshot_edges:
            a, b = edge.source, edge.target
            if a not in velocities or b not in velocities:
                continue
            dx = b.x() - a.x()
            dy = b.y() - a.y()
            dist = math.hypot(dx, dy) or 0.1
            fx = spring * (dist - spring_len) * dx / dist
            fy = spring * (dist - spring_len) * dy / dist
            velocities[a] += QPointF(fx, fy)
            velocities[b] += QPointF(-fx, -fy)

        # 中心引力
        for node in snapshot_nodes:
            dx = cx - node.x()
            dy = cy - node.y()
            velocities[node] += QPointF(dx * center_force, dy * center_force)

        # 应用速度
        for node in snapshot_nodes:
            # r37 修复：用户拖拽过的节点固定，不再被力导向拉动（参考 CS Academy）
            if getattr(node, "_pinned", False):
                continue
            vel = velocities[node] * damping
            disp = math.hypot(vel.x(), vel.y())
            if disp > max_disp:
                vel *= max_disp / disp
            new_pos = node.pos() + vel
            # 边界限制
            r = node._radius
            new_pos.setX(max(rect.left() + r, min(rect.right() - r, new_pos.x())))
            new_pos.setY(max(rect.top() + r, min(rect.bottom() - r, new_pos.y())))
            node.setPos(new_pos)

    # ---- 导入/导出 ----
    def to_adjacency(self) -> str:
        lines = []
        for node in self._nodes:
            parts = [node.label]
            for edge in self._edges:
                if edge.source is node:
                    target_label = edge.target.label
                    w = f"({edge.weight})" if edge.weight else ""
                    parts.append(f"{target_label}{w}")
            lines.append(": ".join(parts))
        return "\n".join(lines)

    def from_adjacency(self, text: str) -> bool:
        """从邻接表文本重建图；返回是否成功。

        round48：与 graph 渲染路径对齐 100KB / MAX_NODES / MAX_EDGES 三重限制，
        先解析后清空画布（解析失败/超限不破坏现有图），避免超大粘贴冻结 UI。
        """
        if not text or not text.strip():
            return False
        if len(text) > 100_000:
            return False

        names: set = set()
        edges: list = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                names.add(line)
                continue
            head, rest = line.split(":", 1)
            head = head.strip()
            names.add(head)
            for token in rest.split():
                token = token.strip()
                if not token:
                    continue
                # 形如 B(5)；节点标签本身可能含括号，故从最后一个 '(' 分割
                if "(" in token and token.endswith(")"):
                    target, weight = token.rsplit("(", 1)
                    target = target.strip()
                    weight = weight[:-1].strip()
                else:
                    target = token
                    weight = ""
                if not target:
                    continue
                names.add(target)
                edges.append((head, target, weight))
                if len(edges) > MAX_EDGES:
                    return False

        names.discard("")
        if not names or len(names) > MAX_NODES:
            return False

        # 全部校验通过后才修改画布
        self.clear_graph()
        name_to_node = {}
        angle_step = 2 * math.pi / max(1, len(names))
        for i, name in enumerate(sorted(names)):
            angle = i * angle_step
            x = math.cos(angle) * 150
            y = math.sin(angle) * 150
            node = self.add_node(x, y, name)
            name_to_node[name] = node

        for src, dst, weight in edges:
            s = name_to_node.get(src)
            d = name_to_node.get(dst)
            if s and d:
                self.add_edge(s, d, weight)
        return True

    def from_uvw(self, text: str) -> bool:
        """r32+r33: 解析 uvw 文本（csacademy 风格）后重建图。

        返回 True 表示成功（含空文本清空），False 表示解析失败。
        成功后用 _compute_layout（r31 力导向）自动布局节点位置，
        与用户原诉求"你写出来，节点自动在图的区域内互相排斥"对齐。

        关键不变量：
        - r32 修复：解析失败不破坏现有画布（不调 clear_graph）
        - r32 修复：set_directed 在 add_edge 之前（避免有向双向边被去重）
        - r32 修复：信号回环（setPlainText → textChanged → debounce）用 blockSignals 保护
        - **r33 修复（P0-1）**：原子替换 — 先在临时列表中建好所有 NodeItem/EdgeItem
          数据，全部成功后才统一 clear_graph + addItem；中途任何异常都 return False
          且**不修改现有画布**。原"clear_graph 后异常 → 清空画布丢原图"问题修复。
        - **r33 修复（P1-2）**：force 模式下 from_uvw 后临时 stop_force，避免
          持续运行的力导向 timer 把刚布局好的节点位置洗乱。
        """
        # 空文本：清空（视为"重置"语义）
        if not text or not text.strip():
            self.clear_graph()
            return True
        # r33 P1-6 修复：force 模式下无论成功失败都临时停 force
        # 原因：force timer 持续 16ms 重算节点位置；解析失败时原图节点若仍在 force 中会持续被打散
        if self._mode == "force" and self._force_running:
            self.stop_force()
        try:
            data = parse_uvw_block(text)
            # r33 P0-2 修复：uvw 解析失败时回退到 parse_graph_block
            # 原因：状态栏提示"含 →/->/--> 的可能是简化格式；含 nodes:/edges: 的可能是 YAML"
            # 但代码不自动回退会让用户按提示改格式仍失败，UX 自相矛盾
            if data is None:
                data = parse_graph_block(text)
        except Exception as e:
            # 解析异常：不破坏现有图
            try:
                logger.warning(f"parse_uvw_block 异常: {e}", exc_info=True)
            except Exception:
                print(f"parse_uvw_block 异常: {e}")
            return False
        if data is None:
            return False  # 解析失败，保留原图

        # round48 P1：解析成功但图过大的输入同样拒绝（与 render_graph_svg/from_adjacency
        # 对齐），否则 100KB UVW 可构造上万节点/边，QGraphicsScene 主线程冻结百秒。
        try:
            n_nodes = len(data.get("nodes") or [])
            n_edges = len(data.get("edges") or [])
        except Exception:
            return False
        if n_nodes > MAX_NODES or n_edges > MAX_EDGES:
            return False

        # r33 P0-1 修复：原子替换 —— 在临时列表里建好新图，全部成功后再统一应用
        # 关键：建图期间不调 clear_graph、不调 addItem（除创建 NodeItem 本身外）
        # 这样中途任何异常，调用方 return False 后原图仍完好
        staged_nodes: list = []  # 临时 NodeItem 列表（已 addItem，但暂不连到 scene._nodes）
        staged_edges: list = []  # 临时 EdgeItem 列表
        temp_dirty = True  # 标记是否需要清理已加入 scene 的临时对象
        try:
            # 1) 先设 directed（影响 add_edge 去重逻辑）
            directed = bool(data.get("directed", False))

            # 2) 建节点（add_node 内部已 addItem，先收集到 staged_nodes）
            name_to_node = {}
            for nd in data["nodes"]:
                node = NodeItem(nd["id"], nd["label"], 0, 0)
                self.addItem(node)  # 必须 addItem 才能在 layout 阶段拿到 scene context
                staged_nodes.append(node)
                name_to_node[nd["id"]] = node

            # 3) 建边（此时 _directed 仍是原值 —— 我们要在 staged 阶段保持原图不变）
            #    ⚠ 关键：临时建边时不走 add_edge（会修改 self._edges 列表），
            #    直接构造 EdgeItem 并 addItem，收集到 staged_edges
            #    r33 修复：手动去重（保留 add_edge 内部的无向去重语义）
            seen_edges: set = set()
            for e in data["edges"]:
                s = name_to_node.get(e["from"])
                d = name_to_node.get(e["to"])
                if not (s and d):
                    continue
                # 去重 key：(s.node_id, d.node_id) 或 (d.node_id, s.node_id) 无向
                key = (s.node_id, d.node_id)
                if not directed:
                    key = tuple(sorted(key))
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edge = EdgeItem(s, d, e["weight"], directed)
                self.addItem(edge)
                staged_edges.append(edge)

            # 4) 自动布局（仅设置临时节点的 pos）
            # r36 P1 修复：_compute_layout 返回以 (0,0) 为左上角的相对坐标，
            # 必须加上 sceneRect 的左上角偏移，否则节点会偏向画布右下象限。
            rect = self.sceneRect()
            pos = _compute_layout(data, int(rect.width()), int(rect.height()))
            offset_x = rect.left()
            offset_y = rect.top()
            for nid, (x, y) in pos.items():
                node = name_to_node.get(nid)
                if node is not None:
                    node.setPos(offset_x + x, offset_y + y)

            # 5) 全部成功 → 提交：先清空原图，再把 staged 加入主列表
            temp_dirty = False  # 标记已提交，不再清理
            self.clear_graph()
            self._directed = directed
            self._nodes.extend(staged_nodes)
            self._edges.extend(staged_edges)
            # r37 P1 修复：同步 _id_counter，避免用户随后添加节点时产生重复 ID
            # r39 P0 修复：str.isdigit() 对 Unicode 数字（'²' '１'）返回 True，
            # 但 int() 抛 ValueError。改用 str.isascii() + str.isdigit() 双重校验。
            numeric_ids = []
            for n in self._nodes:
                nid = n.node_id
                if isinstance(nid, str) and nid.isascii() and nid.isdigit():
                    try:
                        numeric_ids.append(int(nid))
                    except ValueError:
                        pass
            if numeric_ids:
                self._id_counter = max(numeric_ids)

            # r33 P1-2 修复：force 模式重建后临时 stop_force
            # 原因：力导向 timer 每 16ms 重算位置，会把刚布局好的节点洗乱
            if self._mode == "force" and self._force_running:
                self.stop_force()
            return True
        except Exception as e:
            # 异常兜底：清理可能已加入 scene 的临时对象；不动原图
            try:
                logger.warning(f"from_uvw 重建失败: {e}", exc_info=True)
            except Exception:
                print(f"from_uvw 重建失败: {e}")
            if temp_dirty:
                # 清理临时对象（不调 clear_graph，因为它会清掉原图）
                for edge in staged_edges:
                    try:
                        self.removeItem(edge)
                    except Exception:
                        pass
                for node in staged_nodes:
                    try:
                        self.removeItem(node)
                    except Exception:
                        pass
            return False

    def export_png(self, path: str) -> bool:
        painter = None
        try:
            pixmap = QPixmap(self.sceneRect().size().toSize())
            pixmap.fill(QColor(ThemeManager().current_theme.get("bg", "#000000")))
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            self.render(painter)
            return pixmap.save(path)
        except Exception as e:
            logger.warning(f"导出图 PNG 失败: {e}")
            return False
        finally:
            if painter is not None and painter.isActive():
                painter.end()


# ---------- 视图 ----------
class GraphView(QGraphicsView):
    """图编辑视图：支持缩放、平移。"""

    def __init__(self, scene: GraphScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._scale_factor = 1.0
        self._update_bg()

    def _update_bg(self):
        t = ThemeManager().current_theme
        self.setBackgroundBrush(QBrush(QColor(t.get("bg", "#000000"))))

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = 1.1 if delta > 0 else 0.9
        new_factor = self._scale_factor * factor
        if new_factor < 0.1 or new_factor > 10.0:
            event.accept()
            return
        self._scale_factor = new_factor
        self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_bg()


# ---------- 主窗口 ----------
class GraphEditor(QWidget, RoundedFrameMixin):
    """图论编辑器独立窗口。"""

    graph_text_ready = Signal(str)  # 用于把图文本插入对话

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("图论编辑器")
        self.setWindowTitle("OISystem - 图论编辑器")
        self.resize(900, 650)
        # r32: UVW 实时预览 debounce 定时器
        self._uvw_debounce = QTimer(self)
        self._uvw_debounce.setSingleShot(True)
        self._uvw_debounce.setInterval(200)
        self._uvw_debounce.timeout.connect(self._apply_uvw_preview)
        self._setup_ui()
        self._apply_theme()
        # 默认放在主屏中央（避免 Win 下默认 (0,0) 出现"在北面看不见"）
        # 若用户后续拖动，Qt 会记下新位置；此处仅设首次出现的兜底
        self._center_on_primary_screen()

    def _center_on_primary_screen(self):
        """把窗口居中到主屏幕可视区域内。"""
        try:
            from PySide6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            avail = screen.availableGeometry()
            x = avail.x() + max(0, (avail.width() - self.width()) // 2)
            y = avail.y() + max(0, (avail.height() - self.height()) // 2)
            self.move(x, y)
        except Exception as e:
            # 居中失败时记录，但不阻断 UI
            try:
                from utils.helpers import logger
                logger.debug(f"GraphEditor 居中失败: {e}")
            except Exception:
                pass

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 工具栏
        toolbar = QHBoxLayout()

        self.mode_group = QButtonGroup(self)
        for mode, label in [("draw", "绘制"), ("edit", "编辑"),
                            ("delete", "删除"), ("force", "力导向")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setProperty("mode", mode)
            btn.setObjectName("tool" if mode != "draw" else "tool_checked")
            self.mode_group.addButton(btn)
            btn.clicked.connect(lambda _=False, m=mode: self._set_mode(m))
            toolbar.addWidget(btn)
            if mode == "draw":
                btn.setChecked(True)

        toolbar.addSpacing(16)

        self.directed_btn = QPushButton("无向")
        self.directed_btn.setCheckable(True)
        self.directed_btn.clicked.connect(self._toggle_directed)
        toolbar.addWidget(self.directed_btn)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self._clear)
        toolbar.addWidget(self.clear_btn)

        self.import_btn = QPushButton("导入")
        self.import_btn.clicked.connect(self._import_text)
        toolbar.addWidget(self.import_btn)

        self.export_png_btn = QPushButton("导出 PNG")
        self.export_png_btn.clicked.connect(self._export_png)
        toolbar.addWidget(self.export_png_btn)

        self.export_text_btn = QPushButton("导出文本")
        self.export_text_btn.clicked.connect(self._export_text)
        toolbar.addWidget(self.export_text_btn)

        self.copy_btn = QPushButton("复制到输入")
        self.copy_btn.clicked.connect(self._copy_to_input)
        toolbar.addWidget(self.copy_btn)

        self.insert_btn = QPushButton("插入对话")
        self.insert_btn.setObjectName("primary")
        self.insert_btn.clicked.connect(self._insert_to_dialog)
        toolbar.addWidget(self.insert_btn)

        toolbar.addSpacing(16)

        # r32: UVW 面板开关
        # P1 修复：初始文字 "▾" 与 checked 状态（展开）一致
        self.uvw_toggle_btn = QPushButton("UVW 输入 ▾")
        self.uvw_toggle_btn.setCheckable(True)
        self.uvw_toggle_btn.setChecked(True)
        self.uvw_toggle_btn.clicked.connect(self._toggle_uvw_panel)
        toolbar.addWidget(self.uvw_toggle_btn)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        # r32: 主区域 — QSplitter 左侧 UVW 输入框 / 右侧画布
        self.splitter = QSplitter(Qt.Horizontal, self)

        # 左侧 UVW 面板
        self.uvw_panel = QWidget(self)
        uvw_layout = QVBoxLayout(self.uvw_panel)
        uvw_layout.setContentsMargins(6, 6, 6, 6)
        uvw_layout.setSpacing(6)

        hint = QLabel(
            "每行：u v w 格式（csacademy 风格）\n"
            "1 token: 仅建节点  2 token: 无向边  3 token: 带权边\n"
            "第 1 行可写 directed: true/false"
        )
        hint.setObjectName("uvw_hint")
        hint.setWordWrap(True)
        uvw_layout.addWidget(hint)

        self.uvw_edit = QPlainTextEdit(self)
        self.uvw_edit.setPlaceholderText(
            "1 2\n2 3 5\n4\n# 注释行以 # 开头\ndirected: true"
        )
        self.uvw_edit.setMinimumWidth(180)
        self.uvw_edit.textChanged.connect(self._on_uvw_text_changed)
        uvw_layout.addWidget(self.uvw_edit, 1)

        # UVW 辅助按钮行
        uvw_btn_row = QHBoxLayout()
        self.uvw_clear_btn = QPushButton("清空输入")
        self.uvw_clear_btn.clicked.connect(self._clear_uvw_input)
        uvw_btn_row.addWidget(self.uvw_clear_btn)
        self.uvw_example_btn = QPushButton("示例")
        self.uvw_example_btn.clicked.connect(self._load_uvw_example)
        uvw_btn_row.addWidget(self.uvw_example_btn)
        self.uvw_copy_btn = QPushButton("复制当前图")
        self.uvw_copy_btn.clicked.connect(self._copy_current_to_uvw)
        uvw_btn_row.addWidget(self.uvw_copy_btn)
        uvw_layout.addLayout(uvw_btn_row)

        self.splitter.addWidget(self.uvw_panel)

        # 右侧画布
        self.scene = GraphScene(self)
        self.view = GraphView(self.scene, self)
        self.splitter.addWidget(self.view)

        # 初始比例：左 ~28% / 右 ~72%
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([260, 640])
        layout.addWidget(self.splitter, 1)

        # 状态栏
        self.status = QLabel(
            "模式：绘制 | 在空白处点击添加节点，拖动节点之间连线 | "
            "左侧输入 uvw 文本实时预览"
        )
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def _apply_theme(self):
        self.setStyleSheet(ThemeManager().get_css())
        self.view._update_bg()

    # r32: UVW 实时预览相关方法
    def _on_uvw_text_changed(self):
        """文本变化时启动 200ms debounce 定时器。"""
        # 每次输入都重置定时器；停止输入 200ms 后才真正解析
        self._uvw_debounce.start()

    def _apply_uvw_preview(self):
        """r33: 实际把 uvw 文本应用到画布（带具体错误提示）。"""
        try:
            text = self.uvw_edit.toPlainText()
        except Exception:
            # widget 已销毁（窗口关闭时序）
            return
        # r33 P1-4 修复：长度超限给明确提示（不依赖 from_uvw 的 None 反馈）
        if text and len(text) > 100_000:
            try:
                self.status.setText(
                    "UVW 解析失败：输入文本超过 100KB 上限（可能误粘贴大文件）"
                )
            except Exception:
                pass
            return
        ok = self.scene.from_uvw(text)
        if ok:
            # 成功（含空输入清空）
            try:
                if text.strip():
                    # r37 修复：同步 directed 按钮状态，避免 from_uvw 后 UI 与场景不一致
                    self.set_directed(self.scene._directed)
                    n_nodes = len(self.scene._nodes)
                    n_edges = len(self.scene._edges)
                    # r33 P1-3 修复：状态栏显示有向/无向信息
                    direction = "有向" if self.scene._directed else "无向"
                    base_msg = f"UVW 预览：已加载 {n_nodes} 节点 / {n_edges} 边（{direction}）"
                    # r33 P1-2 修复：force 模式下重建后提示用户 force 已停
                    if self.scene._mode == "force":
                        base_msg += "（force 已停止，请重新点 force 按钮）"
                    self.status.setText(base_msg)
                else:
                    self.status.setText("UVW 输入已清空")
            except Exception:
                pass
        else:
            # r33 P1-4 修复：失败时尝试回退 YAML/简化并给更具体提示
            try:
                self.status.setText(
                    "UVW 解析失败：检查格式（每行 1-3 token，# 开头为注释；"
                    "含 →/->/--> 的可能是简化格式；含 nodes:/edges: 的可能是 YAML）"
                )
            except Exception:
                pass

    def _toggle_uvw_panel(self):
        """展开/折叠左侧 UVW 输入面板。"""
        checked = self.uvw_toggle_btn.isChecked()
        if checked:
            self.uvw_panel.show()
            self.uvw_toggle_btn.setText("UVW 输入 ▾")
        else:
            self.uvw_panel.hide()
            self.uvw_toggle_btn.setText("UVW 输入 ▸")

    def _clear_uvw_input(self):
        """r33: 清空 UVW 输入框（带画布保护）。

        P1-3 修复：如果画布上已有内容（节点/边），清空输入框前先确认，
        避免"清空草稿区"误把画布也清了。空画布直接清。
        """
        has_canvas = bool(self.scene._nodes) or bool(self.scene._edges)
        if has_canvas:
            reply = QMessageBox.question(
                self, "清空 UVW 输入",
                "清空输入框也会清空画布上的节点和边。是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        # P2 修复：用 blockSignals 避免触发 textChanged → debounce 闭环节外生枝
        self.uvw_edit.blockSignals(True)
        try:
            self.uvw_edit.clear()
        finally:
            self.uvw_edit.blockSignals(False)
        # 立即清空画布（不等待 debounce）
        self._uvw_debounce.stop()
        self.scene.clear_graph()
        self.status.setText("UVW 输入已清空，画布已重置")

    def _load_uvw_example(self):
        """加载示例 uvw 文本。"""
        example = (
            "# 示例：有向图，5 节点，6 边\n"
            "directed: true\n"
            "1 2 3\n"
            "2 3 5\n"
            "3 4 2\n"
            "4 5 7\n"
            "5 1 4\n"
            "2 4 6"
        )
        # P1 修复：阻塞信号避免回环（虽然示例本就要预览）
        self.uvw_edit.blockSignals(True)
        try:
            self.uvw_edit.setPlainText(example)
        finally:
            self.uvw_edit.blockSignals(False)
        # 主动触发一次预览
        self._uvw_debounce.stop()
        self._apply_uvw_preview()

    def _copy_current_to_uvw(self):
        """r33: 把当前画布的图转成 uvw 文本写入输入框。

        P0-3 修复：有向模式下 uvw 本身不编码方向（csacademy 风格），
        我们仍输出 `directed: true` 标志，并在状态栏显式提示"方向信息
        仅在重新加载时通过有向模式保留，但单条边在 uvw 语法中无方向标记"。

        P0-4 修复：节点 label 含空白/特殊字符时，用 `"..."` 引号包裹 token，
        内部双引号转义为 `\"`，反斜杠转义为 `\\`，与 `_tokenize_uvw_line` 对称。
        """
        def _quote(tok: str) -> str:
            """含空白或特殊字符的 token 用引号包裹。"""
            if not tok:
                return '""'
            # r33 P2-1 修复：触发引号包裹的字符（含换行符，避免回读拆成多行）
            needs_quote = any(c in tok for c in (' ', '\t', '\n', '\r', '"', '\\'))
            if not needs_quote:
                return tok
            # 转义反斜杠和双引号
            esc = tok.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{esc}"'

        lines: List[str] = []
        # 边优先（带权重）
        for e in self.scene._edges:
            w = e.weight or ""
            src = _quote(e.source.label)
            dst = _quote(e.target.label)
            if w:
                lines.append(f"{src} {dst} {_quote(w)}")
            else:
                lines.append(f"{src} {dst}")
        # 孤立节点
        for n in self.scene._nodes:
            incident = [e for e in self.scene._edges
                        if e.source is n or e.target is n]
            if not incident:
                lines.append(_quote(n.label))
        # directed 标记
        if self.scene._directed:
            lines.insert(0, "directed: true")
        text = "\n".join(lines)
        # P1 修复：先停 debounce 并阻塞信号，避免 200ms 后回环重绘
        self._uvw_debounce.stop()
        self.uvw_edit.blockSignals(True)
        try:
            self.uvw_edit.setPlainText(text)
        finally:
            self.uvw_edit.blockSignals(False)
        # r33 P0-3：有向图给明确提示（uvw 不编码单边方向）
        if self.scene._directed:
            self.status.setText(
                "已将当前图转换为 uvw 文本（含 directed: true；"
                "注意：uvw 语法本身不区分单边方向，回读后所有边按无向处理）"
            )
        else:
            self.status.setText("已将当前图转换为 uvw 文本")

    def _set_mode(self, mode: str):
        self.scene.set_mode(mode)
        hints = {
            "draw": "模式：绘制 | 点击空白添加节点，拖动节点之间连线",
            "edit": "模式：编辑 | 点击节点或边编辑标签/权重",
            "delete": "模式：删除 | 点击节点或边删除",
            "force": "模式：力导向 | 自动布局，仍可拖拽节点",
        }
        self.status.setText(hints.get(mode, ""))

    def _toggle_directed(self):
        directed = self.directed_btn.isChecked()
        self.scene.set_directed(directed)
        self.directed_btn.setText("有向" if directed else "无向")

    def set_directed(self, directed: bool):
        """同步场景方向性与 UI 按钮状态（供 from_uvw 等外部调用）。"""
        self.directed_btn.setChecked(directed)
        self.directed_btn.setText("有向" if directed else "无向")
        self.scene.set_directed(directed)

    def _clear(self):
        reply = QMessageBox.question(
            self, "清空画布", "确定清空所有节点和边吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.scene.clear_graph()

    def _import_text(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "导入邻接表", "每行格式：节点: 邻居(权重) 邻居(权重)..."
        )
        if ok:
            if self.scene.from_adjacency(text):
                self.status.setText("邻接表导入成功")
            else:
                self.status.setText(
                    "邻接表导入失败：输入为空、超过 100KB，"
                    f"或节点/边数超过上限（{MAX_NODES} 节点 / {MAX_EDGES} 边）"
                )

    def _export_png(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PNG", "graph.png", "PNG 图片 (*.png)"
        )
        if path:
            if self.scene.export_png(path):
                self.status.setText(f"已导出 PNG: {path}")
            else:
                self.status.setText("导出 PNG 失败")

    def _export_text(self):
        text = self.scene.to_adjacency()
        # 简单弹窗展示
        from PySide6.QtWidgets import QDialog, QTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("邻接表")
        dlg.resize(400, 300)
        l = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setPlainText(text)
        l.addWidget(te)
        dlg.exec()

    def _copy_to_input(self):
        text = self.scene.to_adjacency()
        if text:
            clipboard = self.clipboard()
            if clipboard:
                clipboard.setText(text)
                self.status.setText("邻接表已复制到剪贴板")

    def _insert_to_dialog(self):
        text = self.scene.to_adjacency()
        if text:
            self.graph_text_ready.emit(text)
            self.status.setText("邻接表已插入对话输入区")

    def clipboard(self):
        try:
            from PySide6.QtWidgets import QApplication
            return QApplication.clipboard()
        except Exception:
            return None

    def get_graph_text(self) -> str:
        """供外部读取当前图的文本表示。"""
        return self.scene.to_adjacency()

    def closeEvent(self, event):
        # r32 P1 修复：停止 UVW debounce 定时器，避免窗口关闭后
        # 残留的 200ms 回调访问半销毁的 widget
        try:
            # r33 P2-11 修复：先 disconnect 再 stop，
            # 防止事件队列中已入队的 timeout 事件被处理（断开后 _apply_uvw_preview 不会执行）
            try:
                self._uvw_debounce.timeout.disconnect(self._apply_uvw_preview)
            except (TypeError, RuntimeError):
                # 已断开或方法未连接
                pass
            self._uvw_debounce.stop()
        except Exception:
            pass
        # r33 P1-4 修复：显式 disconnect force timer 的 timeout 信号
        # scene.stop_force() 只调 _timer.stop()，但不 disconnect timeout
        # 万一事件队列中已入队 timeout 事件未被 stop 取消（极端时序），
        # _force_step 会访问半销毁的 _nodes
        try:
            scene_timer = getattr(self.scene, "_timer", None)
            if scene_timer is not None:
                try:
                    scene_timer.timeout.disconnect(self.scene._force_step)
                except (TypeError, RuntimeError):
                    pass
        except Exception:
            pass
        self.scene.stop_force()
        super().closeEvent(event)
