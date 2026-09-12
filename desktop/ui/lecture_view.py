"""讲课面板（round 58）：从学习 Agent 拉试卷/题目，AI 教师逐步开讲。

设计：Design.md「桌面讲课：从学习 Agent 拉题讲解（round 58）」。
独立窗口（侧边栏「讲课」按钮唤起）；自建 TTSPlayer/Overlay 实例，
与课堂感知 coach 互不干扰；关闭时 shutdown。
"""
import os
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QLineEdit, QSplitter, QStatusBar
)

from ui.frame_mixin import RoundedFrameMixin
from config.settings import ConfigManager
from utils.helpers import logger


class _FetchWorker(QObject):
    """题目详情拉取（工作线程；fetch_question 同步网络）。"""

    plan_ready = Signal(list, str, dict)   # (fallback_steps, "", question)
    failed = Signal(str)
    done = Signal()

    def __init__(self, engine, question_id: str, request_id: int, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._question_id = question_id
        self._request_id = request_id

    def run(self):
        try:
            question = self._engine.fetch_question(self._question_id)
            if question is None:
                self.failed.emit("题目详情拉取失败（检查服务器地址/密码/网络）")
                return
            steps, summary = self._engine._finalize(question, None)
            self.plan_ready.emit(steps, summary, question)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {str(e)[:180]}")
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class _PlanSyncWorker(QObject):
    """AI 讲解计划生成（工作线程；真 AI 调用不阻塞 UI）。"""

    plan_ready = Signal(list, str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, engine, question: dict, request_id: int, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._question = question
        self._request_id = request_id

    def run(self):
        try:
            steps, summary = self._engine.build_plan_sync(self._question)
            self.plan_ready.emit(steps, summary)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {str(e)[:180]}")
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class LectureView(QWidget, RoundedFrameMixin):
    """讲课面板：试卷列表 → 题目列表 → 逐步讲解。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("讲课")
        self.setWindowTitle("OISystem - 讲课")
        self.resize(520, 620)
        self._engine = None
        self._paper_id = ""
        self._questions = []        # 当前试卷题目 [{id, number}]
        self._current_question = None
        self._steps = []
        self._fallback_steps = []   # F2：AI 计划失败时使用的兜底步骤（来自 _FetchWorker）
        self._paused = False
        self._request_seq = 0
        self._thread = None
        self._worker = None
        self._setup_ui()
        self._refresh_papers()

    # ---------- UI ----------

    def _setup_ui(self):
        from ui.themes import ThemeManager
        self.setStyleSheet(ThemeManager().get_css())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel("AI 教师讲题（服务器题库）")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet("background: transparent;")
        layout.addWidget(title)

        self.status = QLabel("就绪")
        self.status.setStyleSheet("color: #888;")
        layout.addWidget(self.status)

        layout.addWidget(QLabel("试卷"))
        row1 = QHBoxLayout()
        self.paper_list = QListWidget()
        self.paper_list.itemClicked.connect(self._on_paper_clicked)
        row1.addWidget(self.paper_list, 1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._refresh_papers)
        row1.addWidget(self.btn_refresh)
        layout.addLayout(row1, 2)

        layout.addWidget(QLabel("题目"))
        self.question_list = QListWidget()
        self.question_list.itemClicked.connect(self._on_question_clicked)
        layout.addWidget(self.question_list, 2)

        self.btn_lecture = QPushButton("生成本题讲解")
        self.btn_lecture.setObjectName("primary")
        self.btn_lecture.clicked.connect(self._start_lecture)
        layout.addWidget(self.btn_lecture)

        layout.addWidget(QLabel("讲解步骤"))
        self.step_list = QListWidget()
        layout.addWidget(self.step_list, 3)

        ctrl = QHBoxLayout()
        self.btn_prev = QPushButton("上一步")
        self.btn_prev.clicked.connect(self._prev_step)
        ctrl.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一步")
        self.btn_next.clicked.connect(self._next_step)
        ctrl.addWidget(self.btn_next)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.clicked.connect(self._toggle_pause)
        ctrl.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop)
        ctrl.addWidget(self.btn_stop)
        layout.addLayout(ctrl)

        self.btn_dismiss = QPushButton("清除屏幕板书")
        self.btn_dismiss.clicked.connect(self._clear_board)
        layout.addWidget(self.btn_dismiss)

        self.statusbar = QStatusBar()
        self.statusbar.showMessage("未在讲解")
        layout.addWidget(self.statusbar)

    # ---------- 数据拉取 ----------

    def _engine_ins(self):
        if self._engine is None:
            from core.lecture_engine import LectureEngine
            self._engine = LectureEngine()
            self._engine.papers_ready.connect(self._on_papers)
            self._engine.paper_ready.connect(self._on_paper)
            self._engine.error.connect(self._on_error)
        return self._engine

    def _refresh_papers(self):
        self.status.setText("正在从服务器拉取试卷列表...")
        self.btn_refresh.setEnabled(False)
        eng = self._engine_ins()
        eng.papers_ready.connect(self._papers_loaded, Qt.UniqueConnection)
        eng.fetch_papers()

    def _papers_loaded(self, papers):
        self.btn_refresh.setEnabled(True)
        self.status.setText(f"拉取到 {len(papers)} 份试卷")
        self.paper_list.clear()
        for p in papers:
            item = QListWidgetItem(f"{p['title']}（{p['question_count']}题）")
            item.setData(Qt.UserRole, p)
            self.paper_list.addItem(item)

    def _on_error(self, msg: str):
        self.btn_refresh.setEnabled(True)
        self.status.setText(msg)
        logger.warning(f"讲课面板错误: {msg}")

    def _on_paper_clicked(self, item):
        p = item.data(Qt.UserRole)
        if not p:
            return
        self._paper_id = p["id"]
        self.status.setText(f"正在加载试卷「{p['title']}」...")
        eng = self._engine_ins()
        eng.paper_ready.connect(self._paper_loaded, Qt.UniqueConnection)
        eng.fetch_paper(self._paper_id)

    def _paper_loaded(self, paper):
        self.status.setText(f"试卷「{paper['title']}」共 {len(paper['question_order'])} 题")
        self.question_list.clear()
        self._questions = paper["question_order"]
        for q in self._questions:
            item = QListWidgetItem(f"第 {q['number']} 题")
            item.setData(Qt.UserRole, q)
            self.question_list.addItem(item)

    def _on_question_clicked(self, item):
        q = item.data(Qt.UserRole)
        if q:
            self._current_question = q

    # ---------- 讲解 ----------

    def _start_lecture(self):
        if not self._current_question:
            self.status.setText("请先选择一道题目")
            return
        self._request_seq += 1
        seq = self._request_seq
        self.btn_lecture.setEnabled(False)
        self.status.setText("正在拉取题目详情...")
        eng = self._engine_ins()

        self._thread = QThread(self)
        worker = _FetchWorker(eng, self._current_question["id"], seq)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        # F1 修复：原为 `worker.done = self._thread.quit`（漏了 .connect）。
        # done 是 Signal()，而 worker.run() 的 finally 里必然 self.done.emit()：
        # 赋值把 Signal 换成了方法引用后，emit 会抛 AttributeError，且 quit 永远不会
        # 被作为槽调用 → QThread 永不退出，每次拉题泄漏一个线程。邻居 L236-239 均用 .connect。
        worker.done.connect(self._thread.quit)
        worker.failed.connect(self._on_fetch_failed)
        worker.failed.connect(self._thread.quit)
        worker.plan_ready.connect(lambda steps, summary, question, s=seq: self._on_question_fetched(steps, summary, question, s))
        worker.plan_ready.connect(self._thread.quit)
        self._worker = worker
        self._thread.start()

    def _on_fetch_failed(self, msg: str):
        self.btn_lecture.setEnabled(True)
        self.status.setText(msg)

    def _on_question_fetched(self, fallback_steps, summary, question, seq):
        """题目详情已拉到：触发 AI 讲解计划（工作线程，真 AI 调用）。

        F2 修复：`fallback_steps` 此前是**纯死参数**——信号里带着它、槽函数收下它，
        但函数体从不使用，AI 计划失败时只弹一句错误、用户无路可走。
        现在把它存下来，作为 `_on_plan_failed` 的降级方案，让这条声明过的兜底真正生效。
        """
        self._fallback_steps = list(fallback_steps or [])
        if seq != self._request_seq:
            return
        self.status.setText("AI 正在生成讲解计划...")
        self._thread = QThread(self)
        eng = self._engine_ins()
        # build_plan 是异步信号式：改用同步收口（在工作线程内调用 v4-pro）
        worker = _PlanSyncWorker(eng, question, seq)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.plan_ready.connect(lambda steps, summary, s=seq: self._on_plan(steps, summary, s))
        worker.plan_ready.connect(self._thread.quit)
        worker.failed.connect(self._on_plan_failed)
        worker.failed.connect(self._thread.quit)
        self._worker = worker
        self._thread.start()

    def _on_plan(self, steps, summary, seq):
        if seq != self._request_seq:
            return
        self.btn_lecture.setEnabled(True)
        self._steps = steps
        self.status.setText(f"讲解计划就绪（{len(steps)} 步）")
        self.step_list.clear()
        for i, s in enumerate(steps):
            self.step_list.addItem(QListWidgetItem(f"{i + 1}. {s['title']}"))
        eng = self._engine_ins()
        # F4 修复：**必须先连信号再起播**。原实现先 eng.start_lecture(...) 再 connect，
        # 而起播会立刻发出第 1 步的 step_started —— 此时还没有任何槽，信号被丢弃，
        # 于是状态栏与步骤列表**永远漏掉第 1 步**（高亮停在第 2 步起）。
        eng.step_started.connect(self._on_step_started, Qt.UniqueConnection)
        eng.lecture_finished.connect(self._on_finished, Qt.UniqueConnection)
        # F5 修复：订阅引擎的 error 信号。原实现从未订阅任何错误通道，TTS 报 error 或
        # "无文本"导致自动连播停下来时，界面**没有任何提示**，用户只看到"卡在第 N 步"。
        # （注意：引擎本身没有 stateChanged；那是 TTS player 的信号，不要连错对象。）
        try:
            eng.error.connect(self._on_engine_error, Qt.UniqueConnection)
        except Exception:
            pass
        eng.start_lecture(steps, summary)
        self.btn_pause.setText("暂停")
        self._paused = False

    def _on_engine_error(self, msg):
        """把引擎/TTS 的错误如实反映到界面（F5）。"""
        try:
            self.status.setText(f"讲解异常：{msg}")
            self.statusbar.showMessage(msg)
        except Exception:
            pass

    def _on_plan_failed(self, msg):
        # F2 修复：AI 计划失败时**先用兜底步骤**（题面+标准答案直读），让讲解仍能进行；
        # 原实现只把错误写进状态栏，用户拿着一个"计划失败"的界面无路可走。
        self.btn_lecture.setEnabled(True)
        if self._fallback_steps:
            self.status.setText(f"AI 计划失败，已改用题面直读兜底：{msg}")
            self._on_plan(self._fallback_steps, "", self._request_seq)
            return
        self.status.setText(f"讲解计划失败: {msg}")

    def _on_step_started(self, idx, title, speech):
        self.statusbar.showMessage(f"第 {idx + 1}/{len(self._steps)} 步：{title}")
        for i in range(self.step_list.count()):
            self.step_list.item(i).setSelected(i == idx)

    def _on_finished(self):
        self.statusbar.showMessage("讲解结束")
        self.btn_pause.setText("暂停")

    def _prev_step(self):
        if self._engine:
            # F3 修复：原实现**无条件** pause()，只在真要回退的分支里 resume()。
            # 在第 1 步按下「上一步」时 _step_idx == 0 → 走不进 if → **引擎永久停在 paused**，
            # 讲题静默卡死且界面无任何提示（用户只会觉得"点了没反应，然后就死了"）。
            # 改为：无事可做就直接返回，不改变播放状态。
            if self._engine._step_idx > 0:
                self._engine.pause()
                self._engine._step_idx -= 2   # _advance 会 +1
                self._engine.resume()

    def _next_step(self):
        if self._engine and self._steps:
            self._engine.pause()
            self._engine.resume()

    def _toggle_pause(self):
        if not self._engine:
            return
        self._paused = not self._paused
        if self._paused:
            self._engine.pause()
            self.btn_pause.setText("继续")
        else:
            self._engine.resume()
            self.btn_pause.setText("暂停")

    def _stop(self):
        if self._engine:
            self._engine.stop()
        self.statusbar.showMessage("已停止")

    def _clear_board(self):
        if self._engine:
            self._engine._clear_board()

    def closeEvent(self, event):
        if self._engine:
            self._engine.shutdown()
            self._engine = None
        super().closeEvent(event)
