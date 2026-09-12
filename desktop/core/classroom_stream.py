"""课堂文字流编排（网课老师声音 + 学生提问 → 转写文字流）。

设计：Design.md「4. 课堂文字流（round55）」、Techniques.md「4. 线程与信号模型」。

数据流：
    AudioCapture(loopback/mic) ──采集线程──> EnergyVAD.feed() ──闭合段──> queue.Queue
                                                                            │
                                                            单一转写工作线程（串行）
                                                                            │
                                        deque 滚动窗口 + jsonl 落盘（仅文字） + Qt 信号

红线（隐私）：原始 PCM 只存在于采集缓冲与队列中的段对象内，**绝不落盘、绝不上传**；
落盘内容仅转写文字（data/classroom/YYYYMMDD.jsonl）。

线程纪律（沿用 r52 教训）：Qt 信号只从工作线程 emit（AutoConnection 自动排队到主线程），
上层订阅必须连绑定方法，禁止无 receiver 的 lambda。
"""
import json
import os
import queue
import threading
import time
from collections import deque

import numpy as np

from PySide6.QtCore import QObject, Signal

from config.settings import ConfigManager, ENABLE_CLASSROOM_AUDIO
from core.audio_capture import AudioCapture, float32_to_wav, TARGET_RATE
from core.vad import EnergyVAD
from core import ai_client
from utils.helpers import logger, DATA_DIR, append_err_record, now_cst

CLASSROOM_DIR = os.path.join(DATA_DIR, "classroom")

# 内存滚动窗口上限（条）：长时间挂机不涨内存
TRANSCRIPT_MAX = 400
# 待转写队列上限：满了丢新段（背压），不让内存无界增长
QUEUE_MAX = 64
# 单通道 ASR 连续失败上限：达到后暂停该通道转写，冷却后自动恢复
ASR_FAIL_PAUSE = 5
ASR_PAUSE_SEC = 120.0
# 最短有效语音段（秒）：低于此长度不值得打一次 ASR
MIN_SEGMENT_SEC = 0.25
# 最长送转写段（秒）：超过则只取尾部，防止超长段撑爆 token
MAX_TRANSCRIBE_SEC = 30.0


class ClassroomMonitor(QObject):
    """课堂音频双通道监听器：产出带说话人标签的转写文字流。"""

    transcript_ready = Signal(dict)   # {speaker, text, dur, ts}
    channel_error = Signal(str, str)  # (speaker, message)——首参统一为 speaker
                                      # （teacher/student），启动失败与看门狗一致
    state_changed = Signal(dict)      # stats 快照

    def __init__(self, on_transcript=None, parent=None):
        super().__init__(parent)
        self._on_transcript = on_transcript      # 可选纯 Python 回调（无 Qt 事件循环时也能用）
        self._captures = {}                      # speaker -> AudioCapture
        self._vads = {}                          # speaker -> EnergyVAD
        self._queue = queue.Queue(maxsize=QUEUE_MAX)
        self._worker = None
        self._stop_evt = threading.Event()
        self._transcripts = deque(maxlen=TRANSCRIPT_MAX)
        self._lock = threading.Lock()
        self._asr_fail = {}
        self._asr_paused_until = {}
        self._dead_reported = set()               # 已通报过死亡的通道（防重复告警）
        self._teacher_suppressed_until = 0.0      # 自说回环防护窗口（TTS 播放期丢弃 teacher 段）
        self._vad_reset_needed = False            # 防护窗过期后待执行的 VAD 重置（采集线程执行）
        self._running = False
        self._start_result = {}
        self._counts = {"segments": 0, "transcribed": 0, "failed": 0,
                        "dropped_queue": 0, "dropped_paused": 0, "empty_text": 0,
                        "dropped_stale": 0}
        self._persist = True
        self._window = 12

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> dict:
        """启动双通道采集 + 转写线程。返回各通道启动结果；失败不抛异常。"""
        if self._running:
            return dict(self._start_result)
        if not ENABLE_CLASSROOM_AUDIO:
            self._start_result = {"ok": False, "reason": "module_disabled",
                                  "detail": "ENABLE_CLASSROOM_AUDIO=False（源码级总开关关闭）"}
            return dict(self._start_result)
        s = ConfigManager().settings
        if not s.classroom_audio_enabled:
            self._start_result = {"ok": False, "reason": "setting_disabled",
                                  "detail": "classroom_audio_enabled=False（设置中心已关闭）"}
            return dict(self._start_result)

        self._persist = bool(s.classroom_persist_transcript)
        self._window = max(1, int(s.classroom_asr_window))
        vad_kwargs = dict(
            sample_rate=TARGET_RATE,
            end_silence_ms=s.classroom_vad_end_silence_ms,
            min_speech_ms=s.classroom_vad_min_speech_ms,
            max_segment_s=float(s.classroom_max_segment_sec),
            noise_ratio=float(s.classroom_vad_noise_ratio),
            abs_floor_db=float(s.classroom_vad_abs_floor_db),
        )

        channels = []
        if s.classroom_capture_loopback:
            channels.append(("teacher", "loopback", "网课老师"))
        if s.classroom_capture_mic:
            channels.append(("student", "mic", "学生麦克风"))
        if not channels:
            self._start_result = {"ok": False, "reason": "no_channel",
                                  "detail": "loopback 与 mic 两路都被关闭"}
            return dict(self._start_result)

        results = {}
        self._dead_reported = set()
        for speaker, source, label in channels:
            try:
                vad = EnergyVAD(**vad_kwargs)
                cap = AudioCapture(
                    source=source,
                    buffer_seconds=float(s.classroom_buffer_sec),
                    block_ms=40,
                    on_segment=lambda pcm, sp=speaker: self._on_audio(sp, pcm),
                    label=label,
                )
                ok = cap.start()
                self._captures[speaker] = cap
                self._vads[speaker] = vad
                results[speaker] = {"ok": bool(ok), "source": source,
                                    "error": cap.error, "format": dict(cap.format_info)}
                if not ok:
                    logger.warning(f"课堂音频通道 {speaker}({source}) 启动失败: {cap.error}")
                    self._dead_reported.add(speaker)   # 启动即失败已通报，看门狗不再重复
                    try:
                        self.channel_error.emit(speaker, cap.error)
                    except RuntimeError:
                        pass
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:180]}"
                logger.error(f"课堂音频通道 {speaker} 初始化异常: {msg}")
                self._dead_reported.add(speaker)
                results[speaker] = {"ok": False, "source": source, "error": msg, "format": {}}
                try:
                    append_err_record("core/classroom_stream.py", f"通道 {speaker} 初始化异常", msg)
                except Exception:
                    pass

        started = [k for k, v in results.items() if v.get("ok")]
        self._running = bool(started)
        if self._running:
            # 新会话：排空上次遗留队列 + 换全新 Event（C2/M4 修复）。
            # 陈旧毒丸若不清掉，新 worker 第一口就吃到、立即退出，
            # 之后 running=True 但永远零转写（关/开课堂音频一次即触发）；
            # 旧 worker 若因 join 超时还活着，它绑的是旧（已置位）Event，
            # 处理完当前段自然退出，不会与新 worker 并发消费。
            self._drain_queue()
            self._stop_evt = threading.Event()
            self._worker = threading.Thread(
                target=self._worker_loop, args=(self._stop_evt,),
                name="ClassroomASR", daemon=True)
            self._worker.start()
        self._start_result = {"ok": self._running, "channels": results,
                              "started": started,
                              "reason": "" if self._running else "all_channels_failed"}
        try:
            self.state_changed.emit(self.stats())
        except RuntimeError:
            pass
        return dict(self._start_result)

    def stop(self, timeout: float = 3.0):
        """停止采集与转写线程：停采集 → VAD flush → 毒丸 → join → 最后置事件。

        事件最后置位，让 worker 按设计把 flush 出的尾段转写完（兑现"救最后
        一句"承诺）；join 超时也置位，保证旧 worker 处理完当前段即退出，
        下次 start() 换全新 Event 后旧线程不会复活（M4）。
        """
        self._running = False
        for cap in list(self._captures.values()):
            try:
                cap.stop(timeout=max(0.5, timeout / 2))
            except Exception as e:
                logger.warning(f"停止采集异常: {str(e)[:150]}")
        # VAD 收尾：把进行中的段冲刷出来（可能还能救回最后一句）——趁 worker 还在入队
        for speaker, vad in list(self._vads.items()):
            try:
                for seg in vad.flush():
                    self._enqueue(speaker, seg)
            except Exception:
                pass
        try:
            self._queue.put_nowait(None)   # 毒丸：worker 排空队列后自然退出
        except queue.Full:
            pass
        w = self._worker
        if w and w.is_alive():
            w.join(timeout=max(0.5, float(timeout)))
        self._stop_evt.set()
        self._worker = None
        self._captures.clear()
        self._vads.clear()
        try:
            self.state_changed.emit(self.stats())
        except RuntimeError:
            pass

    # ---------- 音频入口（采集线程内调用） ----------

    def _on_audio(self, speaker: str, pcm):
        if speaker == "teacher":
            if self._teacher_suppressed():
                return
            if self._vad_reset_needed:
                # 窗口刚过期：在本采集线程内重置 VAD（与 feed 串行，无竞态）
                self._vad_reset_needed = False
                vad = self._vads.get("teacher")
                if vad is not None:
                    try:
                        vad.reset()
                    except Exception:
                        pass
        vad = self._vads.get(speaker)
        if vad is None:
            return
        try:
            for seg in vad.feed(pcm):
                self._enqueue(speaker, seg)
        except Exception as e:
            logger.error(f"[{speaker}] VAD 处理异常: {str(e)[:180]}")
            try:
                append_err_record("core/classroom_stream.py", f"{speaker} VAD 异常", str(e)[:300])
            except Exception:
                pass

    def suppress_teacher(self, seconds: float):
        """自说回环防护窗口（round56）：AI TTS 朗读的声音会被 loopback 抓进
        teacher 通道 → 转写进文字流 → 决策引擎又对自己的话做判断。窗口内
        teacher 通道 PCM 在 VAD 入口直接丢弃（真实老师声音的损失可接受——
        AI 打断时老师通常停顿）。窗口过期后由采集线程重置 VAD 状态，
        防止窗口前的残帧混入恢复后的音频。主线程写 / 采集线程读，持锁防丢改（审查 M3）。"""
        sec = max(1.0, float(seconds or 0))
        with self._lock:
            until = time.time() + sec
            if until > self._teacher_suppressed_until:
                self._teacher_suppressed_until = until
        logger.info(f"teacher 通道自说防护窗口 {sec:.1f}s")

    def _teacher_suppressed(self) -> bool:
        """仅在采集线程调用（_on_audio 入口）。过期时置 _vad_reset_needed 标志，
        由本线程在 feed 前执行 VAD reset——VAD 内部无锁，reset 与 feed 必须
        同线程串行，不能在持 monitor 锁时跨线程 reset。"""
        with self._lock:
            until = self._teacher_suppressed_until
            if not until:
                return False
            if time.time() < until:
                return True
            self._teacher_suppressed_until = 0.0
            self._vad_reset_needed = True
            return False

    def _enqueue(self, speaker: str, seg):
        arr = np.asarray(seg, dtype=np.float32).reshape(-1)
        dur = arr.size / float(TARGET_RATE)
        if dur < MIN_SEGMENT_SEC:
            return
        if dur > MAX_TRANSCRIBE_SEC:      # 只保留尾部，避免超长段撑爆 token
            arr = arr[-int(MAX_TRANSCRIBE_SEC * TARGET_RATE):]
            dur = MAX_TRANSCRIBE_SEC
        with self._lock:
            self._counts["segments"] += 1
        try:
            self._queue.put_nowait((speaker, arr, dur))
        except queue.Full:
            with self._lock:
                self._counts["dropped_queue"] += 1
            logger.warning("课堂转写队列已满，丢弃最新语音段（背压保护）")

    # ---------- 转写工作线程 ----------

    def _worker_loop(self, stop_evt):
        while not stop_evt.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                self._check_channels()
                continue
            except Exception as e:
                logger.error(f"转写队列读取异常: {str(e)[:180]}")
                continue
            if item is None:
                break
            speaker, pcm, dur = item
            if self._asr_paused(speaker):
                with self._lock:
                    self._counts["dropped_paused"] += 1
                continue
            try:
                wav = float32_to_wav(pcm, TARGET_RATE)
                text = ai_client.transcribe_audio(wav)
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:180]}"
                with self._lock:
                    self._counts["failed"] += 1
                    self._asr_fail[speaker] = self._asr_fail.get(speaker, 0) + 1
                    if self._asr_fail[speaker] >= ASR_FAIL_PAUSE:
                        self._asr_paused_until[speaker] = time.time() + ASR_PAUSE_SEC
                        self._asr_fail[speaker] = 0
                        logger.error(f"[{speaker}] ASR 连续失败 {ASR_FAIL_PAUSE} 次，"
                                     f"暂停 {int(ASR_PAUSE_SEC)}s：{msg}")
                continue
            with self._lock:
                self._asr_fail[speaker] = 0
            clean = str(text or "").strip()
            if not clean:
                with self._lock:
                    self._counts["empty_text"] += 1
                continue
            entry = {"speaker": speaker, "text": clean[:2000],
                     "dur": round(float(dur), 2), "ts": now_cst().isoformat()}
            with self._lock:
                self._transcripts.append(entry)
                self._counts["transcribed"] += 1
            if self._persist:
                self._persist_entry(entry)
            try:
                self.transcript_ready.emit(dict(entry))
            except RuntimeError:
                pass            # QObject 已销毁
            if self._on_transcript is not None:
                try:
                    self._on_transcript(dict(entry))
                except Exception as e:
                    logger.warning(f"on_transcript 回调异常: {str(e)[:150]}")

    def _asr_paused(self, speaker: str) -> bool:
        with self._lock:
            until = self._asr_paused_until.get(speaker, 0)
            if until and time.time() >= until:
                self._asr_paused_until.pop(speaker, None)
                return False
            return bool(until)

    def _drain_queue(self):
        """排空上一会话遗留的段与毒丸（新 worker 启动前调用）。
        被丢弃的段已计入 segments，这里对应记 dropped_stale，
        保证 stats 各出口计数之和与 segments 对得上。"""
        drained = 0
        stale_segs = 0
        while True:
            try:
                item = self._queue.get_nowait()
                drained += 1
                if item is not None:
                    stale_segs += 1
            except queue.Empty:
                break
        if stale_segs:
            with self._lock:
                self._counts["dropped_stale"] += stale_segs
        if drained:
            logger.info(f"新会话开始：排空上次遗留的 {drained} 个队列项"
                        f"（含 {stale_segs} 个未转写段）")

    def _check_channels(self):
        """通道静默死亡看门狗（H1）：设备拔出/默认设备切换会让 WASAPI 返回
        错误、采集线程自行终止，但 UI 无从知晓。worker 空闲时低频巡检，
        每通道只通报一次。自动重建留给后续轮次（先保证可见性）。"""
        if not self._running:
            return
        for speaker, cap in list(self._captures.items()):
            if cap.running or speaker in self._dead_reported:
                continue
            self._dead_reported.add(speaker)
            msg = cap.error or "采集线程意外退出"
            logger.error(f"课堂音频通道 {speaker} 已死亡: {msg}")
            try:
                self.channel_error.emit(speaker, msg)
            except RuntimeError:
                pass

    def _persist_entry(self, entry: dict):
        """转写文字按天落盘（jsonl）。失败只记日志，不影响流。
        调用方为 worker 线程与 push_manual（主线程），append 模式单行
        小写在实践中原子；不持锁做文件 I/O，避免慢盘阻塞采集回调链
        上的 _enqueue（L4）。"""
        try:
            os.makedirs(CLASSROOM_DIR, exist_ok=True)
            path = os.path.join(CLASSROOM_DIR, f"{now_cst().strftime('%Y%m%d')}.jsonl")
            line = json.dumps(entry, ensure_ascii=False)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"课堂转写落盘失败: {str(e)[:180]}")

    # ---------- 读取 ----------

    def recent(self, n: int = None, speaker: str = "") -> list:
        """最近 n 条转写（默认取决策窗口大小）。speaker 非空时只取该通道。"""
        want = self._window if n is None else max(1, int(n))
        with self._lock:
            items = list(self._transcripts)
        if speaker:
            items = [x for x in items if x.get("speaker") == speaker]
        return items[-want:]

    def context_block(self, n: int = None) -> str:
        """拼出供打断决策/教学使用的课堂上下文文本块。"""
        items = self.recent(n)
        if not items:
            return ""
        lines = []
        for it in items:
            who = "老师" if it.get("speaker") == "teacher" else "学生"
            lines.append(f"[{who}] {it.get('text', '')}")
        return "\n".join(lines)

    def teacher_segment_count(self) -> int:
        with self._lock:
            return sum(1 for x in self._transcripts if x.get("speaker") == "teacher")

    def push_manual(self, speaker: str, text: str):
        """手工注入一条文字流（截屏分析结论/学生打字提问等非音频来源）。"""
        clean = str(text or "").strip()
        if not clean:
            return None
        sp = "teacher" if str(speaker).strip().lower() in ("teacher", "screen") else "student"
        entry = {"speaker": sp, "text": clean[:2000], "dur": 0.0,
                 "ts": now_cst().isoformat(), "manual": True}
        with self._lock:
            self._transcripts.append(entry)
        if self._persist:
            self._persist_entry(entry)
        try:
            self.transcript_ready.emit(dict(entry))
        except RuntimeError:
            pass
        return entry

    def stats(self) -> dict:
        with self._lock:
            counts = dict(self._counts)
            paused = {k: round(v - time.time(), 1) for k, v in self._asr_paused_until.items()}
            n_transcripts = len(self._transcripts)
        caps = {k: v.stats() for k, v in list(self._captures.items())}
        vads = {k: v.stats() for k, v in list(self._vads.items())}
        return {
            "running": self._running,
            "transcripts": n_transcripts,
            "counts": counts,
            "asr_paused_remaining": paused,
            "teacher_suppressed_remaining": round(
                max(0.0, self._teacher_suppressed_until - time.time()), 1),
            "captures": caps,
            "vads": vads,
            "queue_size": self._queue.qsize(),
        }
