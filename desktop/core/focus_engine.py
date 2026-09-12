"""OISystem 专注模式引擎。

职责：
- 30 分钟倒计时（可配置）
- 急事退出：直接退出但记日志
- 正常退出：直接结束专注
- 自动分析触发：按频率调用 screen_analyzer，连续 N 次无进展触发 AI 分析
- 网站管控切到严格模式
- ZZOI 零提交/末尾自动锁定
"""
import time
import inspect
from datetime import datetime, timedelta
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from config.settings import ConfigManager
from utils.helpers import logger, log_event, now_cst, format_time


class FocusEngine(QObject):
    """专注模式引擎（单例，由主进程持有）。"""

    focus_started = Signal(int)            # 总秒数
    focus_tick = Signal(int)               # 剩余秒数
    focus_ended = Signal()                 # 专注终止（任何原因）
    focus_emergency_exited = Signal(str)   # 原因
    focus_auto_analyze_triggered = Signal(dict)  # 自动分析结果
    focus_stuck_ai_triggered = Signal(str, dict) # 卡住触发AI分析（题目pid/描述, 屏幕结果）
    focus_locked_for_zzoi = Signal(str)    # 因 ZZOI 零提交锁定

    # 做题退出 v2 信号（UI 发起抓取，引擎只管状态）
    focus_problem_exit_started = Signal()      # 进入"做题退出"pending 态
    focus_problem_assigned = Signal(dict)      # 已分配真实题目 {"pid","title","url",...}
    focus_problem_status = Signal(str)         # 分配过程状态文本（抓取中/失败原因等）
    focus_problem_solved = Signal(dict)        # 检测到 AC，退出放行

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self._active = False
        self._end_time: Optional[datetime] = None
        self._timer: Optional[QTimer] = None
        self._auto_analyze_timer: Optional[QTimer] = None
        self._stuck_count = 0
        self._last_progress_snapshot = None
        self._lock_reason = ""             # "focus" / "zzoi_no_submit" / "zzoi_rank_tail"
        self._on_screen_analyze: Optional[Callable] = None
        self._on_ai_stuck_analyze: Optional[Callable] = None
        # 做题退出 v2 状态
        self._problem_exit_pending = False   # 正常退出等待做题 AC
        self._assigned_problem: Optional[dict] = None  # 已分配的真实题目
        # 单调时钟：防止系统时间被修改或休眠恢复导致倒计时错乱
        self._start_monotonic: Optional[float] = None
        self._total_seconds: int = 0

    # ---------- 注入回调（由 main 在阶段4/5/9 接入） ----------
    def set_screen_analyze_callback(self, cb: Callable):
        self._on_screen_analyze = cb

    def set_ai_stuck_callback(self, cb: Callable):
        self._on_ai_stuck_analyze = cb

    # ---------- 状态 ----------
    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def lock_reason(self) -> str:
        return self._lock_reason

    @property
    def problem_exit_pending(self) -> bool:
        """做题退出等待中（正常退出已请求，等待完成分配的题目）。"""
        return self._problem_exit_pending

    @property
    def assigned_problem(self) -> Optional[dict]:
        """当前分配的做题退出题目（未分配为 None）。"""
        return self._assigned_problem

    def remaining_seconds(self) -> int:
        """基于单调时钟计算剩余秒数，避免系统时间跳变导致错乱。"""
        if not self._active or self._start_monotonic is None:
            return 0
        elapsed = int(time.monotonic() - self._start_monotonic)
        return max(0, self._total_seconds - elapsed)

    # ---------- 启动/退出 ----------
    def start(self, duration_minutes: Optional[int] = None, lock_reason: str = "focus"):
        if self._active:
            logger.warning("专注模式已在进行中")
            return
        cfg = self.cfg.settings
        # round48：外部传入/脏配置统一归一化，避免 None/str 引发定时器与比较异常
        lock_reason = lock_reason or "focus"
        if duration_minutes is None:
            try:
                minutes = int(cfg.focus_duration_minutes)
            except (TypeError, ValueError):
                minutes = 30
        else:
            try:
                minutes = int(duration_minutes)
            except (TypeError, ValueError):
                minutes = 30
        minutes = max(1, min(365 * 24 * 60, minutes))
        self._active = True
        self._lock_reason = lock_reason
        self._stuck_count = 0
        self._last_progress_snapshot = None
        self._start_monotonic = time.monotonic()

        # ZZOI 锁定不设自然结束（设极大值），仅靠 force_release 解除
        if lock_reason.startswith("zzoi"):
            minutes = 365 * 24 * 60  # 名义一年
            self._end_time = now_cst() + timedelta(days=365)
        else:
            self._end_time = now_cst() + timedelta(minutes=minutes)

        self._total_seconds = minutes * 60
        self.focus_started.emit(self._total_seconds)
        log_event("focus_start", {
            "duration_minutes": minutes,
            "lock_reason": lock_reason,
            "start_time": format_time(now_cst()),
            "expected_end": format_time(self._end_time),
        })
        logger.info(f"专注模式启动 {minutes} 分钟, reason={lock_reason}")

        # 倒计时定时器（每秒 tick，仅用于刷新 UI）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # r49 P2：ZZOI 锁定为 365 天名义时长，无需每秒刷新倒计时，节省 CPU
        if not lock_reason.startswith("zzoi"):
            self._timer.start(1000)

        # 自动分析定时器：优先用 screen_capture_interval_sec（用户可改的"屏幕检测频率"）
        # 其次用 focus_auto_analyze_interval_sec（兼容字段）
        # r39 P1 修复：两个间隔都可能为 None 或缺失，getattr 兜底避免 AttributeError，
        # 若均为 None 用 60 秒默认值，避免 max(30, None) 抛 TypeError
        cap_interval = getattr(cfg, "screen_capture_interval_sec", None)
        analyze_interval = getattr(cfg, "focus_auto_analyze_interval_sec", None)
        # round48：字符串等脏配置统一 int() 归一化并夹取合理范围
        try:
            raw_interval = int(cap_interval or analyze_interval or 60)
        except (TypeError, ValueError):
            raw_interval = 60
        interval_sec = max(30, min(86400, raw_interval))
        interval_ms = interval_sec * 1000
        self._auto_analyze_timer = QTimer(self)
        self._auto_analyze_timer.timeout.connect(self._auto_analyze)
        self._auto_analyze_timer.start(interval_ms)

    def _tick(self):
        rem = self.remaining_seconds()
        self.focus_tick.emit(rem)
        if rem <= 0:
            # ZZOI 锁定不允许自然结束
            if self._lock_reason.startswith("zzoi"):
                logger.info("ZZOI 锁定中，自然结束被禁止")
                return
            self._normal_complete()

    def emergency_exit(self, reason: str = "急事"):
        """急事退出：允许退出但记入日志。ZZOI 锁定下禁止急事退出。"""
        if not self._active:
            return
        if self._lock_reason.startswith("zzoi"):
            logger.warning(f"ZZOI 锁定中，禁止急事退出 (reason={reason})")
            log_event("focus_emergency_exit_blocked", {
                "reason": reason,
                "lock_reason": self._lock_reason,
                "ts": format_time(now_cst()),
            })
            return
        rem = self.remaining_seconds()
        self.focus_emergency_exited.emit(reason)
        log_event("focus_emergency_exit", {
            "reason": reason,
            "remaining_seconds": rem,
            "lock_reason": self._lock_reason,
            "ts": format_time(now_cst()),
        })
        logger.warning(f"专注模式急事退出: {reason} (剩余 {rem}s)")
        self._finish_session("focus_emergency_exit")

    def request_normal_exit(self):
        """正常退出（做题退出 v2）。

        - OI 模式 + problem_exit_enabled + ZZOI 已配置：进入 pending 态，
          由 FocusView 从真实题目源分配题目，AC 后 confirm_problem_solved() 放行；
        - 其余情况（study 模式 / 开关关闭 / ZZOI 未配置）：直接结束专注；
        - ZZOI 锁定下禁止退出。
        """
        if not self._active:
            return
        if self._lock_reason.startswith("zzoi"):
            logger.warning("ZZOI 锁定中，禁止正常退出")
            log_event("focus_normal_exit_blocked", {
                "lock_reason": self._lock_reason,
                "ts": format_time(now_cst()),
            })
            return
        if self._problem_exit_pending:
            logger.debug("做题退出已在等待中，忽略重复请求")
            return
        s = self.cfg.settings
        mode = getattr(s, "focus_mode", "oi") or "oi"
        try:
            enabled = bool(getattr(s, "problem_exit_enabled", True))
        except Exception:
            enabled = True
        zzoi_ready = bool(getattr(s, "zzoi_uid", "") and getattr(s, "zzoi_base_url", ""))
        use_problem_exit = (mode == "oi" and enabled and zzoi_ready)

        if not use_problem_exit:
            reason = ("study_mode" if mode != "oi"
                      else "" if enabled and zzoi_ready else "disabled_or_unconfigured")
            log_event("focus_normal_exit", {
                "lock_reason": self._lock_reason,
                "problem_exit_skipped": reason,
                "ts": format_time(now_cst()),
            })
            logger.info(f"专注模式正常退出（跳过做题校验: {reason or 'ok'}）")
            self._finish_session("focus_normal_exit")
            return

        # 进入做题退出 pending 态；倒计时继续走，自然结束优先级更高
        self._problem_exit_pending = True
        log_event("focus_problem_exit_started", {
            "lock_reason": self._lock_reason,
            "ts": format_time(now_cst()),
        })
        logger.info("正常退出进入做题流程，等待分配题目")
        self.focus_problem_exit_started.emit()

    def assign_problem(self, problem: Optional[dict]):
        """UI 抓取到真实目标后回填引擎状态。problem 为 None 表示池为空。

        实测适配：目标可以是作业/比赛整体（无单一 pid），也可以是
        题库单题（有 pid）。引擎按原样保存权威副本，FocusView 重开时恢复。
        """
        if not self._problem_exit_pending:
            return
        if not isinstance(problem, dict) or \
                not (problem.get("pid") or problem.get("key")):
            self._assigned_problem = None
            self.focus_problem_status.emit("未获取到可用题目")
            return
        self._assigned_problem = {
            "kind": str(problem.get("kind") or ""),
            "key": str(problem.get("key") or problem.get("pid") or ""),
            "pid": str(problem.get("pid") or ""),
            "title": str(problem.get("title") or "未命名目标"),
            "url": str(problem.get("url") or ""),
            "count": int(problem.get("count") or 0),
            "source": str(problem.get("source") or ""),
            "contest_title": str(problem.get("contest_title") or ""),
            "source_note": str(problem.get("source_note") or ""),
        }
        self.focus_problem_assigned.emit(dict(self._assigned_problem))

    def confirm_problem_solved(self, problem: dict = None):
        """检测到所分配题目的 AC 提交，放行正常退出。幂等防竞争。

        子AGENT审查修复：先收尾会话（停定时器/置 inactive）再发 solved 信号，
        避免 UI 模态弹窗阻塞在信号发射中途导致倒计时/自动分析继续空转。
        """
        if not self._active or not self._problem_exit_pending:
            return
        info = problem if isinstance(problem, dict) else {}
        pid = str(info.get("pid", ""))
        title = str(info.get("title", ""))
        log_event("focus_problem_solved_exit", {
            "lock_reason": self._lock_reason,
            "pid": pid,
            "title": title[:60],
            "ts": format_time(now_cst()),
        })
        logger.info(f"做题退出完成 AC={pid} {title}")
        self._finish_session("focus_problem_solved_exit")
        self.focus_problem_solved.emit(info)

    def cancel_problem_exit(self):
        """取消做题退出，回到普通专注状态（不结束会话）。"""
        if not self._problem_exit_pending:
            return
        self._problem_exit_pending = False
        self._assigned_problem = None
        log_event("focus_problem_exit_cancelled", {
            "lock_reason": self._lock_reason,
            "ts": format_time(now_cst()),
        })
        logger.info("做题退出已取消，继续专注")

    def _finish_session(self, log_type: str):
        """统一会话收尾：清 pending/锁 → 停定时器 → inactive → focus_ended。"""
        self._problem_exit_pending = False
        self._assigned_problem = None
        self._stop_timers()
        self._active = False
        self._lock_reason = ""
        self.focus_ended.emit()

    def _normal_complete(self):
        """倒计时自然结束，直接放行（优先级高于做题退出 pending）。幂等防竞争。"""
        if not self._active:
            return
        if self._problem_exit_pending:
            logger.info("倒计时自然结束，做题退出等待自动解除")
        log_event("focus_normal_complete", {
            "lock_reason": self._lock_reason,
            "ts": format_time(now_cst()),
        })
        logger.info("专注模式自然完成")
        self._finish_session("focus_normal_complete")

    def _stop_timers(self):
        for t in (self._timer, self._auto_analyze_timer):
            if t is not None:
                try:
                    t.stop()
                    t.deleteLater()
                except Exception:
                    pass
        self._timer = None
        self._auto_analyze_timer = None
        self._start_monotonic = None
        self._total_seconds = 0

    # ---------- 自动分析 ----------
    def _auto_analyze(self):
        """触发截图分析（异步，真实结果通过 _on_screen_result 回流）。"""
        if not self._active:
            return
        if self._on_screen_analyze is None:
            logger.debug("屏幕分析回调未注入")
            return
        try:
            self._on_screen_analyze()  # 异步触发
        except Exception as e:
            logger.warning(f"屏幕分析触发失败: {e}")

    def _on_screen_result(self, result: dict = None):
        """接收异步屏幕分析的**真实**结果，做进展判定和卡住检测。"""
        if not self._active:
            return
        if not isinstance(result, dict):
            logger.debug(f"屏幕分析结果类型异常: {type(result).__name__}")
            return
        result = result or {}
        self.focus_auto_analyze_triggered.emit(result)
        log_event("focus_auto_analyze", {
            "activity": result.get("activity"),
            "efficiency": result.get("efficiency"),
            "code_seen": result.get("code_seen"),
            "ts": format_time(now_cst()),
        })
        # 跳过占位结果
        activity = result.get("activity", "")
        if not isinstance(activity, str):
            activity = "" if activity is None else str(activity)
        if activity in ("分析中（异步）", "上次分析未完成", "截图失败", "API 失败", "解析失败"):
            return

        snapshot = (
            activity,
            result.get("code_seen", False),
            result.get("progress_delta", ""),
        )
        if self._last_progress_snapshot == snapshot:
            self._stuck_count += 1
        else:
            self._stuck_count = 0
            self._last_progress_snapshot = snapshot

        # r39 P1 修复：threshold 可能为 None（配置缺失）或 0（配置非法），
        # 用 getattr 兜底默认值 3，并确保 >= 1 避免 0 时每次都触发 AI 卡住检测
        try:
            threshold = int(getattr(self.cfg.settings, "focus_stuck_threshold", 3) or 3)
        except (TypeError, ValueError):
            threshold = 3
        threshold = max(1, min(100, threshold))
        if self._stuck_count >= threshold:
            pid_desc = result.get("current_problem", "未知题目")
            if not isinstance(pid_desc, str):
                pid_desc = "" if pid_desc is None else str(pid_desc)
            if not pid_desc:
                pid_desc = "未知题目"
            logger.warning(f"连续 {self._stuck_count} 次无进展，触发 AI 分析")
            self.focus_stuck_ai_triggered.emit(pid_desc, result)
            log_event("focus_stuck_ai_triggered", {
                "stuck_count": self._stuck_count,
                "problem": pid_desc,
                "screen_result": result,
                "ts": format_time(now_cst()),
            })
            if self._on_ai_stuck_analyze is not None:
                try:
                    # 优先通过签名判断参数数量，避免 TypeError 吞掉真实异常
                    sig = inspect.signature(self._on_ai_stuck_analyze)
                    params = list(sig.parameters.values())
                    positional = [
                        p for p in params
                        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    ]
                    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
                    if has_var_pos or len(positional) >= 2:
                        self._on_ai_stuck_analyze(pid_desc, result)
                    else:
                        self._on_ai_stuck_analyze(pid_desc)
                    # r38 P2 修复：仅在回调成功（未抛异常）时清零 stuck_count，
                    # 避免回调失败时卡住检测被重置导致用户无法再次获得帮助。
                    self._stuck_count = 0
                except Exception as e:
                    logger.error(f"AI 卡住分析失败: {e}")
                    # 回调失败：折半而非清零，下次更快重新触发
                    self._stuck_count = max(0, threshold // 2)
            else:
                self._stuck_count = 0

    # ---------- ZZOI 锁定 ----------
    def lock_for_zzoi(self, reason: str = "zzoi_no_submit"):
        """因 ZZOI 零提交/排行榜末尾锁定专注模式。
        若当前已在专注模式则忽略；否则名义一年，直到提交成功后 force_release 解除。
        """
        if self._active:
            logger.info(f"已在专注模式，ZZOI 锁定请求忽略 ({reason})")
            return
        log_event("focus_locked_for_zzoi", {
            "reason": reason,
            "ts": format_time(now_cst()),
        })
        # 先启动锁定，再发射信号，确保 _on_zzoi_locked 中隐藏按钮的状态不会被 focus_started 覆盖
        self.start(duration_minutes=480, lock_reason=reason)
        self.focus_locked_for_zzoi.emit(reason)

    def force_release(self):
        """强制释放（ZZOI 提交成功后调用）。"""
        if not self._active:
            return
        log_event("focus_force_released", {
            "lock_reason": self._lock_reason,
            "ts": format_time(now_cst()),
        })
        logger.info("专注模式被强制释放")
        self._finish_session("focus_force_released")

    def force_release_if_locked(self, reason_prefix: str = "zzoi"):
        """仅当当前处于指定前缀的锁定态时才强制释放。

        子AGENT终审 C-2：force_release 长期无生产调用方，ZZOI 锁定后唯一
        出路是重启进程。现由每日检查在检测到当日有提交时调用本方法；
        普通专注（lock_reason 不匹配）不受影响，防止误结束正常会话。
        """
        if not self._active:
            return
        if not (self._lock_reason or "").startswith(reason_prefix):
            logger.debug(f"非 {reason_prefix} 锁定，忽略强制释放请求")
            return
        self.force_release()
