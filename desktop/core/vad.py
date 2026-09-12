"""能量法 VAD（语音活动检测）——课堂音频切段，零新依赖。

设计：Design.md「2. VAD（能量法）」、Techniques.md「2. VAD 参数」。

不用 silero-vad（需下载模型权重，引入网络依赖）也不用 webrtcvad（需新装 C 扩展）：
课堂场景只需把连续音频切成"一句话一段"喂给 ASR，能量法 + 自适应噪声底足够。

用法：
    vad = EnergyVAD()
    for seg in vad.feed(pcm_16k_mono_float32):   # 返回已闭合的语音段
        transcribe(seg)
    for seg in vad.flush():                      # 停止时收尾未闭合段
        transcribe(seg)
"""
import numpy as np

DEFAULT_RATE = 16000
_EPS = 1e-9


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (float(db) / 20.0))


class EnergyVAD:
    """自适应噪声底的能量 VAD。所有阈值可配（走设置项），线程内同步调用。"""

    def __init__(self, sample_rate: int = DEFAULT_RATE, frame_ms: int = 20,
                 onset_frames: int = 3, min_speech_ms: int = 250,
                 end_silence_ms: int = 600, max_segment_s: float = 15.0,
                 preroll_ms: int = 200, noise_ratio: float = 2.5,
                 abs_floor_db: float = -50.0, noise_adapt: float = 0.05,
                 tail_ms: int = 100):
        self.rate = max(1000, int(sample_rate or DEFAULT_RATE))
        self.frame = max(1, int(self.rate * max(5, min(200, int(frame_ms or 20))) / 1000))
        self.onset_frames = max(1, int(onset_frames or 3))
        self.min_speech_frames = max(1, int(self.rate * max(0, int(min_speech_ms or 250)) / 1000 / self.frame))
        self.end_silence_frames = max(1, int(self.rate * max(50, int(end_silence_ms or 600)) / 1000 / self.frame))
        # 注意：max_segment_s 单位是「秒」，不能套用上面 ms→帧 的 /1000 公式
        # （实测套用后算得 0 帧，被兜底成 min_speech_frames+1，导致每 0.26s 强制切段）
        self.max_segment_frames = max(
            self.min_speech_frames + 1,
            int(self.rate * max(1.0, float(max_segment_s or 15.0)) / self.frame),
        )
        self.preroll_frames = max(0, int(self.rate * max(0, int(preroll_ms or 200)) / 1000 / self.frame))
        self.tail_frames = max(0, int(self.rate * max(0, int(tail_ms or 100)) / 1000 / self.frame))
        self.noise_ratio = max(1.05, float(noise_ratio or 2.5))
        self.abs_floor = _db_to_linear(abs_floor_db if abs_floor_db is not None else -50.0)
        self.noise_adapt = min(0.5, max(0.001, float(noise_adapt or 0.05)))

        self._pending = np.empty(0, dtype=np.float32)
        self._preroll = []          # 最近若干帧（onset 时补进段首，防吞第一个字）
        self._noise = self.abs_floor
        self._in_speech = False
        self._run_frames = []       # 当前段帧数据
        self._speech_frames = 0     # 当前段内语音帧计数
        self._silence_run = 0       # 当前段内连续静音帧
        self._onset_run = 0         # 非语音态下的连续语音帧
        self._segments_out = 0
        self._segments_dropped = 0
        self._forced_splits = 0

    # ---------- 对外 ----------

    @property
    def speech_active(self) -> bool:
        return self._in_speech

    def reset(self):
        self._pending = np.empty(0, dtype=np.float32)
        self._preroll = []
        self._in_speech = False
        self._run_frames = []
        self._speech_frames = 0
        self._silence_run = 0
        self._onset_run = 0

    def stats(self) -> dict:
        return {
            "rate": self.rate, "frame_samples": self.frame,
            "noise_floor_rms": round(float(self._noise), 6),
            "threshold_rms": round(self._threshold(), 6),
            "in_speech": self._in_speech,
            "segments_out": self._segments_out,
            "segments_dropped": self._segments_dropped,
            "forced_splits": self._forced_splits,
        }

    def feed(self, pcm):
        """喂入归一 PCM（16k mono float32），返回本次闭合的语音段列表。"""
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return []
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0).astype(np.float32)
        self._pending = np.concatenate([self._pending, arr]) if self._pending.size else arr
        closed = []
        while self._pending.size >= self.frame:
            frame = self._pending[:self.frame]
            self._pending = self._pending[self.frame:]
            seg = self._process_frame(frame)
            if seg is not None:
                closed.append(seg)
        return closed

    def flush(self):
        """收尾：把未满一帧的残余与进行中的段强制闭合（停止采集时调用）。"""
        out = []
        if self._in_speech and self._run_frames:
            seg = self._close_segment()
            if seg is not None:
                out.append(seg)
        self.reset()
        return out

    # ---------- 内部 ----------

    def _threshold(self) -> float:
        return max(self.abs_floor, float(self._noise) * self.noise_ratio)

    def _process_frame(self, frame: np.ndarray):
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) if frame.size else 0.0
        thr = self._threshold()
        is_speech = rms > thr

        if not self._in_speech:
            # 前置缓冲收录所有帧（含 onset 计数中的语音帧），
            # 否则触发 onset 前的 1-2 帧语音音频会丢失（吞掉起始音）
            self._preroll.append(frame)
            cap = self.preroll_frames + self.onset_frames
            if cap > 0 and len(self._preroll) > cap:
                self._preroll = self._preroll[-cap:]
            if is_speech:
                self._onset_run += 1
                if self._onset_run >= self.onset_frames:
                    self._in_speech = True
                    self._onset_run = 0
                    self._silence_run = 0
                    self._speech_frames = self.onset_frames
                    self._run_frames = list(self._preroll)
                    self._preroll = []
                    return None
            else:
                self._onset_run = 0
                # 噪声底只在非语音时自适应，避免把讲课声学进底噪
                self._noise = (1.0 - self.noise_adapt) * self._noise + self.noise_adapt * rms
            return None

        # 语音态
        self._run_frames.append(frame)
        if is_speech:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        if self._silence_run >= self.end_silence_frames:
            return self._close_segment(trim_tail=True)
        if len(self._run_frames) >= self.max_segment_frames:
            # 强制切段但不退出语音态：老师一口气讲到底时继续开新段，内容不丢
            self._forced_splits += 1
            seg = self._close_segment(trim_tail=False)
            self._in_speech = True
            self._silence_run = 0
            return seg
        return None

    def _close_segment(self, trim_tail: bool = True):
        frames = self._run_frames
        # 先取快照再复位：_silence_run 若在裁剪前被清零，段尾静音永远裁不掉
        # （实测每段多留 end_silence_ms 的静音，1s 语音被算成 1.82s）
        silence_run = self._silence_run
        speech_frames = self._speech_frames
        self._run_frames = []
        self._in_speech = False
        self._onset_run = 0
        self._speech_frames = 0
        self._silence_run = 0
        if not frames or speech_frames < self.min_speech_frames:
            self._segments_dropped += 1
            return None
        if trim_tail and self.tail_frames < len(frames):
            keep = max(1, len(frames) - max(0, silence_run) + self.tail_frames)
            frames = frames[:min(keep, len(frames))]
        pcm = np.concatenate(frames).astype(np.float32)
        self._segments_out += 1
        return pcm
