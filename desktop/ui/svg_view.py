"""OISystem SVG 选择器。

展示所有可用 SVG（内建 + 文件），按类别分组。
用户可勾选喜欢的 SVG，勾选结果持久化到 config。
"""
import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QGridLayout, QCheckBox, QGroupBox, QMessageBox,
    QApplication
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QPixmap, QPainter

from ui.frame_mixin import RoundedFrameMixin
from ui.icons import get_all_svgs_grouped, render_svg
from config.settings import ConfigManager
from utils.helpers import logger


# 每行显示的图标数
_COLS = 6


class SvgCard(QWidget):
    """单个 SVG 卡片：缩略图 + 名称 + 勾选框。"""

    def __init__(self, item: dict, checked: bool = False, parent=None):
        super().__init__(parent)
        self.item = item
        self.setFixedSize(130, 150)
        self.setStyleSheet("""
            SvgCard {
                background: #111c30; border: 1px solid #2b3a52;
                border-radius: 8px;
            }
            SvgCard:hover { border-color: #3b82f6; background: #162240; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignCenter)

        # 缩略图
        self.preview = QLabel()
        self.preview.setFixedSize(64, 64)
        self.preview.setAlignment(Qt.AlignCenter)
        pm = self._render_thumbnail()
        self.preview.setPixmap(pm)
        layout.addWidget(self.preview, 0, Qt.AlignCenter)

        # 名称
        name = item.get("label") or item["name"]
        name_label = QLabel(name[:10])
        name_label.setFont(QFont("Microsoft YaHei", 8))
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setStyleSheet("color: #cbd5e1; background: transparent;")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        # 勾选框
        self.checkbox = QCheckBox("喜欢")
        self.checkbox.setChecked(checked)
        self.checkbox.setStyleSheet("""
            QCheckBox {
                color: #94a3b8; font-size: 8pt; spacing: 3px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 1px solid #475569; background: transparent;
            }
            QCheckBox::indicator:checked {
                background: #2563eb; border-color: #3b82f6;
            }
        """)
        layout.addWidget(self.checkbox, 0, Qt.AlignCenter)

    def _render_thumbnail(self) -> QPixmap:
        svg = self.item["svg"]
        try:
            # 文件 SVG 把 #000000 替换为 #e2e8f0 以便在暗色背景可见
            colored = svg.replace('#000000', '#e2e8f0')
            return render_svg(colored, size=48, color="#e2e8f0")
        except Exception:
            pm = QPixmap(QSize(48, 48))
            pm.fill(Qt.transparent)
            return pm


class SvgGroupSection(QWidget):
    """一个类别分组。"""

    def __init__(self, category: str, items: list, selected_names: set, parent=None):
        super().__init__(parent)
        self.cards = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(6)

        # 分组标题
        title = QLabel(f"  {category}  ({len(items)})")
        title.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        title.setStyleSheet("color: #fbbf24; background: #0d1526; "
                            "padding: 6px 12px; border-radius: 6px;")
        layout.addWidget(title)

        # 图标网格
        grid = QGridLayout()
        grid.setSpacing(8)
        for idx, item in enumerate(items):
            card = SvgCard(item, checked=item["name"] in selected_names)
            self.cards.append(card)
            grid.addWidget(card, idx // _COLS, idx % _COLS)
        layout.addLayout(grid)

    def get_selected(self) -> list:
        return [c.item["name"] for c in self.cards if c.checkbox.isChecked()]


class SvgPickerView(QWidget, RoundedFrameMixin):
    """SVG 选择器主窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("SVG 选择器")
        self.setWindowTitle("OISystem - SVG 选择器")
        self.resize(880, 640)
        # 让窗口可缩小
        self._set_min_size(640, 400)
        self._sections = []
        self._setup_ui()
        self._load_groups()

        # 恢复已保存的选中状态
        self._restore_selection()

    def _setup_ui(self):
        from ui.themes import ThemeManager
        self.setStyleSheet(ThemeManager().get_css())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        # 顶部：标题 + 计数 + 操作按钮
        top_row = QHBoxLayout()
        title = QLabel("SVG 图标选择器")
        title.setFont(QFont("Microsoft YaHei", 13, QFont.Bold))
        title.setStyleSheet("background: transparent;")
        top_row.addWidget(title)

        self.count_label = QLabel("已选: 0")
        self.count_label.setFont(QFont("Microsoft YaHei", 10))
        self.count_label.setStyleSheet("background: transparent;")
        top_row.addWidget(self.count_label)

        top_row.addStretch(1)

        select_all_btn = QPushButton("全选")
        select_all_btn.clicked.connect(self._select_all)
        top_row.addWidget(select_all_btn)

        deselect_all_btn = QPushButton("取消全选")
        deselect_all_btn.clicked.connect(self._deselect_all)
        top_row.addWidget(deselect_all_btn)

        save_btn = QPushButton("保存选择")
        save_btn.setObjectName("success")
        save_btn.clicked.connect(self._save)
        top_row.addWidget(save_btn)

        layout.addLayout(top_row)

        # 提示文字
        hint = QLabel(
            "勾选你喜欢的 SVG 图标，点击保存后持久保留。按类别分组展示。\n"
            "已勾选的图标会按语义应用到侧边栏对应按钮："
            "AI聊天→对话、图表→日志、设置类→设置、铃铛→静音、指南→专注、"
            "标记正确→图论、用户→ZZOI、删除→退出。"
        )
        hint.setObjectName("tip")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 滚动区域（内容容器）
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(4, 4, 4, 4)
        self.content_layout.setSpacing(12)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll, 1)

    def _load_groups(self):
        """加载所有 SVG 并分组显示。"""
        # 清空之前的 section
        for section in self._sections:
            self.content_layout.removeWidget(section)
            section.deleteLater()
        self._sections.clear()

        groups = get_all_svgs_grouped()
        # 优先展示内建类别
        category_order = ["内建", "锁", "提醒/状态", "计时", "操作",
                          "导航", "工具", "消息", "上传", "图表",
                          "卡片", "创意", "用户", "标签", "其他"]
        for cat in category_order:
            if cat in groups:
                items = groups[cat]
                section = SvgGroupSection(cat, items, set())
                self._sections.append(section)
                # 插入到 stretch 前
                self.content_layout.insertWidget(
                    self.content_layout.count() - 1, section
                )
        # 绑定单个勾选到计数刷新，保证计数实时跟随
        for section in self._sections:
            for card in section.cards:
                card.checkbox.toggled.connect(self._update_count)
        self._update_count()

    def _restore_selection(self):
        """从配置恢复已选中的 SVG 名称。"""
        cfg = ConfigManager().settings
        # round48：脏配置（非 list/含非字符串）安全化为名称集合
        if isinstance(cfg.selected_svgs, list):
            selected = {str(x) for x in cfg.selected_svgs}
        else:
            selected = set()
        if not selected:
            return
        for section in self._sections:
            for card in section.cards:
                if card.item["name"] in selected:
                    card.checkbox.setChecked(True)
        self._update_count()

    def _all_cards(self) -> list:
        """展开所有分组的卡片。"""
        cards = []
        for section in self._sections:
            cards.extend(section.cards)
        return cards

    def _select_all(self):
        """全选所有 SVG。"""
        for card in self._all_cards():
            card.checkbox.setChecked(True)
        self._update_count()

    def _deselect_all(self):
        """取消全选。"""
        for card in self._all_cards():
            card.checkbox.setChecked(False)
        self._update_count()

    def _update_count(self):
        """刷新已选计数标签。"""
        n = sum(1 for c in self._all_cards() if c.checkbox.isChecked())
        self.count_label.setText(f"已选: {n}")

    def _save(self):
        """把当前勾选持久化到 config.selected_svgs。"""
        selected = [c.item["name"] for c in self._all_cards() if c.checkbox.isChecked()]
        try:
            ConfigManager().update(selected_svgs=selected)
            self._update_count()
            QMessageBox.information(self, "已保存", f"已保存 {len(selected)} 个 SVG 选择。")
        except Exception as e:
            logger.warning(f"保存 SVG 选择失败: {e}")
            QMessageBox.warning(self, "保存失败", str(e))
