"""OISystem 日志查看窗口。

显示当日结构化日志内容，含概览、事件流、危险操作、AI 指出的错误、作弊检测。
"""
import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QPushButton,
    QTabWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ui.frame_mixin import RoundedFrameMixin
from utils.helpers import (
    daily_log_path, get_today_log, format_time, now_cst, DAILY_LOGS_DIR
)


class LogView(QWidget, RoundedFrameMixin):
    """日志查看窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("日志")
        self.setWindowTitle("OISystem - 日志")
        self.resize(440, 460)
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        from ui.themes import ThemeManager
        self.setStyleSheet(ThemeManager().get_css())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("当日学习日志")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet("background: transparent;")
        layout.addWidget(title)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # 概览 Tab
        self.overview_tab = QTextEdit()
        self.overview_tab.setReadOnly(True)
        self.overview_tab.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.overview_tab, "概览")

        # 事件流 Tab
        self.events_tab = QTextEdit()
        self.events_tab.setReadOnly(True)
        self.events_tab.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.events_tab, "事件流")

        # 危险操作 Tab
        self.danger_tab = QTextEdit()
        self.danger_tab.setReadOnly(True)
        self.danger_tab.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.danger_tab, "危险操作")

        # AI 错误摘要 Tab
        self.ai_errors_tab = QTextEdit()
        self.ai_errors_tab.setReadOnly(True)
        self.ai_errors_tab.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.ai_errors_tab, "AI 指出的错误")

        # 作弊检测 Tab
        self.cheat_tab = QTextEdit()
        self.cheat_tab.setReadOnly(True)
        self.cheat_tab.setFont(QFont("Consolas", 10))
        self.tabs.addTab(self.cheat_tab, "作弊检测")

        # 底部按钮
        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        btn_row.addWidget(refresh_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

    def _refresh(self):
        data = get_today_log()
        # 概览
        ov = []
        ov.append(f"日期: {data.get('date', '?')}")
        ov.append(f"事件总数: {len(data.get('events', []))}")
        ov.append(f"专注会话数: {len(data.get('focus_sessions', []))}")
        ov.append(f"AI 指出错误数: {len(data.get('ai_errors_found', []))}")
        ov.append(f"对话摘要数: {len(data.get('dialog_summaries', []))}")
        ov.append(f"危险操作数: {len(data.get('danger_ops', []))}")
        ov.append(f"提醒次数: {data.get('reminders', 0)}")
        ov.append(f"网站拦截数: {len(data.get('site_blocks', []))}")
        ov.append("")
        ov.append("提交错误统计:")
        for et, n in data.get("submission_errors", {}).items():
            ov.append(f"  - {et}: {n}")
        self.overview_tab.setPlainText("\n".join(ov))

        # 事件流
        ev_lines = []
        for ev in data.get("events", []):
            ev_lines.append(f"[{ev.get('ts')}] {ev.get('type')}: {ev.get('detail')}")
        self.events_tab.setPlainText("\n".join(ev_lines))

        # 危险操作
        d_lines = []
        for d in data.get("danger_ops", []):
            d_lines.append(f"[{d.get('ts')}] {d.get('detail')}")
        self.danger_tab.setPlainText("\n".join(d_lines))

        # AI 错误
        a_lines = []
        for e in data.get("ai_errors_found", []):
            a_lines.append(f"- {e}")
        self.ai_errors_tab.setPlainText("\n".join(a_lines))

        # 作弊检测（代码写入速度异常追踪）
        try:
            from core.cheat_detector import CheatDetector
            cd = CheatDetector()
            summary = cd.get_summary()
            c_lines = [
                f"追踪题目数: {summary.get('total_problems', 0)}",
                f"标记作弊数: {summary.get('total_flagged', 0)}",
                f"累计代码量: {summary.get('total_bytes', 0)} 字节 / {summary.get('total_lines', 0)} 行",
                "",
                "被标记作弊的题目:",
            ]
            for f in cd.get_flagged_problems():
                st = f.get("stats", {})
                c_lines.append(
                    f"  - {f.get('problem_id')}: {st.get('total_bytes', 0)}B / "
                    f"{st.get('total_lines', 0)}行 (速度异常)"
                )
            if summary.get("total_flagged", 0) == 0:
                c_lines.append("  （无）")
            self.cheat_tab.setPlainText("\n".join(c_lines))
        except Exception as e:
            self.cheat_tab.setPlainText(f"作弊检测数据读取失败: {e}")
