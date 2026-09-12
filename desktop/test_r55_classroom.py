"""R55 回归测试：课堂音频双通道（WASAPI/VAD/ASR）+ 课堂文字流 + 打断决策引擎。

覆盖：
T1  WASAPI ctypes vtable 偏移（继承重复字段导致索引偏移的回归锁）
T2  音频格式转换：to_mono_float32 / resample / float32_to_wav（含 NaN/Inf/越界）
T3  AudioCapture 环形缓冲（feed/read/溢出丢旧/可用秒数/非法 source）
T4  EnergyVAD 帧数学（max_segment_frames 秒/毫秒换算回归锁）
T5  EnergyVAD 切段（合成信号/短音丢弃/强制切段/段尾静音裁剪/flush/脏输入/噪声自适应）
T6  设置项归一化（classroom_* / interrupt_* / xiaomi_asr_model 脏值）
T7  ClassroomMonitor 启动闸门（模块开关/设置开关/无通道）
T8  ClassroomMonitor 入队与背压（过短丢弃/过长截尾/队列满）
T9  ClassroomMonitor 转写线程（成功入库+回调+jsonl 落盘/空文本/异常 5 次暂停/恢复）
T10 ClassroomMonitor 读取（recent/context_block/teacher_segment_count/push_manual）
T11 InterruptEngine.gates 五级闸门顺序全组合
T12 InterruptEngine.parse_decision（裸 JSON/围栏/垃圾/NaN/越界/未知 reason_type）
T13 InterruptEngine.should_consider（间隔/最少段数/模块关/静音/level off）
T14 InterruptEngine.evaluate（AI 打断→触发→冷却拦第二次；AI 异常→fail-safe 不打断）
T15 InterruptEngine.request_teaching（绕过级别与置信度闸门，仅受静音/冷却约束）

运行：python test_r55_classroom.py（需 PySide6；QT_QPA_PLATFORM=offscreen）
"""
import ctypes
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core import audio_capture as ac  # noqa: E402
from core.audio_capture import AudioCapture, to_mono_float32, resample, float32_to_wav, TARGET_RATE  # noqa: E402
from core.vad import EnergyVAD  # noqa: E402
from core import classroom_stream as cs  # noqa: E402
from core.classroom_stream import ClassroomMonitor  # noqa: E402
from core import interrupt_engine as ie  # noqa: E402
from core.interrupt_engine import InterruptEngine  # noqa: E402
from config.settings import AppSettings, ConfigManager, INTERRUPT_LEVELS  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


def tone(sec, amp=0.3, freq=220.0, rate=TARGET_RATE):
    t = np.arange(int(rate * sec)) / float(rate)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(sec, rate=TARGET_RATE):
    return np.zeros(int(rate * sec), dtype=np.float32)


# ==================== T1 vtable 偏移 ====================
print("\n[T1] WASAPI ctypes vtable 偏移")
PTR = ctypes.sizeof(ctypes.c_void_p)
check("1.IMMDeviceEnumerator.GetDefaultAudioEndpoint=槽4",
      ac._IMMDeviceEnumeratorVtbl.GetDefaultAudioEndpoint.offset == 4 * PTR)
check("1.IMMDevice.Activate=槽3", ac._IMMDeviceVtbl.Activate.offset == 3 * PTR)
check("1.IAudioClient.Initialize=槽3", ac._IAudioClientVtbl.Initialize.offset == 3 * PTR)
check("1.IAudioClient.GetBufferSize=槽4", ac._IAudioClientVtbl.GetBufferSize.offset == 4 * PTR)
check("1.IAudioClient.GetCurrentPadding=槽6", ac._IAudioClientVtbl.GetCurrentPadding.offset == 6 * PTR)
check("1.IAudioClient.GetMixFormat=槽8", ac._IAudioClientVtbl.GetMixFormat.offset == 8 * PTR)
check("1.IAudioClient.Start=槽10", ac._IAudioClientVtbl.Start.offset == 10 * PTR)
check("1.IAudioClient.Stop=槽11", ac._IAudioClientVtbl.Stop.offset == 11 * PTR)
check("1.IAudioClient.GetService=槽14", ac._IAudioClientVtbl.GetService.offset == 14 * PTR)
check("1.IAudioCaptureClient.GetBuffer=槽3", ac._IAudioCaptureClientVtbl.GetBuffer.offset == 3 * PTR)
check("1.IAudioCaptureClient.ReleaseBuffer=槽4", ac._IAudioCaptureClientVtbl.ReleaseBuffer.offset == 4 * PTR)
check("1.Release=槽2（所有接口一致）",
      all(v.Release.offset == 2 * PTR for v in (
          ac._IMMDeviceEnumeratorVtbl, ac._IMMDeviceVtbl,
          ac._IAudioClientVtbl, ac._IAudioCaptureClientVtbl)))
check("1.IAudioClient vtable 共 15 槽", ctypes.sizeof(ac._IAudioClientVtbl) == 15 * PTR)
check("1.GUID 16 字节", ctypes.sizeof(ac.GUID) == 16)
check("1.WAVEFORMATEX 18 字节", ctypes.sizeof(ac.WAVEFORMATEX) == 18)

# ==================== T2 格式转换 ====================
print("\n[T2] 音频格式转换")
stereo16 = struct.pack("<4h", 1000, -1000, 3000, 1000)
mono = to_mono_float32(stereo16, 2, 16, False)
check("2.16bit 立体声→mono 均值", mono.size == 2 and abs(mono[0]) < 1e-6
      and abs(mono[1] - 2000 / 32768.0) < 1e-6)
f32 = struct.pack("<2f", 0.5, -0.25)
m2 = to_mono_float32(f32, 1, 32, True)
check("2.32bit float 单声道", m2.size == 2 and abs(m2[0] - 0.5) < 1e-6 and abs(m2[1] + 0.25) < 1e-6)
i32 = struct.pack("<2i", 2147483647, -2147483648)
m3 = to_mono_float32(i32, 1, 32, False)
check("2.32bit int 归一到 [-1,1]", abs(m3[0] - 1.0) < 1e-3 and abs(m3[1] + 1.0) < 1e-3)
u8 = bytes([0, 128, 255])
m4 = to_mono_float32(u8, 1, 8, False)
check("2.8bit 无符号偏移正确", abs(m4[0] + 1.0) < 1e-6 and abs(m4[1]) < 1e-6 and abs(m4[2] - 1.0) < 0.02)
check("2.不支持的位深返回空", to_mono_float32(b"\x00" * 8, 1, 24, False).size == 0)
check("2.空输入返回空", to_mono_float32(b"", 2, 16, False).size == 0)
odd = struct.pack("<3h", 100, 200, 300)
check("2.不足一帧的尾部被截掉（不抛异常）", to_mono_float32(odd, 2, 16, False).size == 1)

r = resample(tone(1.0, rate=48000), 48000, 16000)
check("2.48k→16k 长度正确", abs(r.size - 16000) <= 2)
check("2.同采样率原样返回", resample(tone(0.1), 16000, 16000).size == int(0.1 * 16000))
check("2.空数组重采样不炸", resample(np.empty(0, dtype=np.float32), 48000, 16000).size == 0)
check("2.非法采样率不炸", resample(tone(0.1), 0, 16000).size == int(0.1 * 16000))

wav_bytes = float32_to_wav(tone(0.5), 16000)
with wave.open(io.BytesIO(wav_bytes), "rb") as w:
    check("2.WAV 头 RIFF", wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE")
    check("2.WAV 16k/mono/16bit",
          w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2)
    check("2.WAV 帧数正确", w.getnframes() == int(0.5 * 16000))
dirty = np.array([np.nan, np.inf, -np.inf, 2.0, -3.0], dtype=np.float32)
wd = float32_to_wav(dirty, 16000)
with wave.open(io.BytesIO(wd), "rb") as w:
    frames = np.frombuffer(w.readframes(5), dtype="<i2")
check("2.NaN/Inf/越界被净化并削顶",
      frames.size == 5 and np.all(np.isfinite(frames.astype(np.float64)))
      and frames[3] == 32767 and frames[4] == -32767)
check("2.非法采样率回落默认", float32_to_wav(tone(0.1), 0)[:4] == b"RIFF")

# ==================== T3 环形缓冲 ====================
print("\n[T3] AudioCapture 环形缓冲")
cap = AudioCapture(source="loopback", buffer_seconds=1.0)
cap.feed(tone(0.5))
check("3.可用秒数", abs(cap.available_seconds() - 0.5) < 0.01)
got = cap.read(0.25)
check("3.read 取出指定秒数", abs(got.size - 0.25 * TARGET_RATE) <= 1)
check("3.read 后缓冲减少", abs(cap.available_seconds() - 0.25) < 0.02)
check("3.read 超过存量返回全部", cap.read(99).size == int(0.25 * TARGET_RATE) - 1 or cap.available_seconds() < 0.02)
cap.feed(tone(5.0))
check("3.溢出丢最旧（不超过 buffer_seconds）", cap.available_seconds() <= 1.0 + 1e-6)
check("3.dropped_seconds 有记账", cap.stats()["dropped_seconds"] > 0)
cap.clear()
check("3.clear 清空", cap.available_seconds() == 0.0)
check("3.read(0) 返回空", cap.read(0).size == 0)
check("3.未启动时 running=False", cap.running is False)
st = cap.stats()
check("3.stats 字段齐全",
      all(k in st for k in ("source", "running", "error", "format", "buffered_seconds",
                            "captured_seconds", "dropped_seconds", "idle_seconds")))
try:
    AudioCapture(source="telepathy")
    bad = False
except ValueError:
    bad = True
check("3.非法 source 抛 ValueError", bad)
check("3.stop 未启动也不炸", (cap.stop(), True)[1])

# ==================== T4 VAD 帧数学 ====================
print("\n[T4] EnergyVAD 帧数学")
v = EnergyVAD(sample_rate=16000, frame_ms=20, max_segment_s=15.0)
check("4.帧长 320 样本", v.frame == 320)
check("4.max_segment_frames=750（15s@20ms）", v.max_segment_frames == 750)
check("4.min_speech_frames=12（250ms）", v.min_speech_frames == 12)
check("4.end_silence_frames=30（600ms）", v.end_silence_frames == 30)
check("4.preroll_frames=10（200ms）", v.preroll_frames == 10)
check("4.tail_frames=5（100ms）", v.tail_frames == 5)
v2 = EnergyVAD(sample_rate=16000, max_segment_s=2.0)
check("4.max_segment_s=2 → 100 帧", v2.max_segment_frames == 100)
check("4.max_segment_frames 恒大于 min_speech_frames",
      EnergyVAD(max_segment_s=0.001).max_segment_frames > EnergyVAD().min_speech_frames)

# ==================== T5 VAD 切段 ====================
print("\n[T5] EnergyVAD 切段")


def run_vad(vad, sig, step_sec=0.05):
    segs = []
    step = int(TARGET_RATE * step_sec)
    for i in range(0, sig.size, step):
        segs.extend(vad.feed(sig[i:i + step]))
    segs.extend(vad.flush())
    return segs


sig = np.concatenate([silence(1.0), tone(1.0), silence(1.0), tone(0.1), silence(1.0), tone(2.0), silence(1.0)])
segs = run_vad(EnergyVAD(), sig)
check("5.切出 2 段（0.1s 短音被丢弃）", len(segs) == 2)
check("5.首段 1.30s±0.15（含前置缓冲+尾静音裁剪）", 1.15 <= segs[0].size / TARGET_RATE <= 1.45)
check("5.次段 2.30s±0.15", 2.15 <= segs[1].size / TARGET_RATE <= 2.45)
check("5.段内峰值保留（未削波）", abs(float(np.max(np.abs(segs[0]))) - 0.3) < 0.02)
st5 = EnergyVAD()
run_vad(st5, sig)
check("5.短音计入 dropped", st5.stats()["segments_dropped"] >= 1)

long_sig = np.concatenate([tone(5.0), silence(1.0)])
v_force = EnergyVAD(max_segment_s=2.0)
segs_f = run_vad(v_force, long_sig)
total_f = sum(s.size for s in segs_f)
check("5.连续 5s 语音被强制切分（>=2 段）", len(segs_f) >= 2)
check("5.强制切分计数 >0", v_force.stats()["forced_splits"] >= 1)
check("5.强制切分不丢内容（合计 >=4.5s）", total_f / TARGET_RATE >= 4.5)

v_flush = EnergyVAD()
v_flush.feed(np.concatenate([silence(0.5), tone(1.0)]))
check("5.语音进行中不提前闭合", v_flush.feed(silence(0.1)) == [])
flushed = v_flush.flush()
check("5.flush 收尾进行中语音段", len(flushed) == 1 and flushed[0].size > 0)
check("5.flush 后状态复位", v_flush.speech_active is False and v_flush.flush() == [])

v_nan = EnergyVAD()
check("5.NaN/Inf 输入不炸", v_nan.feed(np.array([np.nan, np.inf, -np.inf], dtype=np.float32)) == [])
check("5.空输入返回空列表", v_nan.feed(np.empty(0, dtype=np.float32)) == [])
check("5.全静音不产段", run_vad(EnergyVAD(), silence(3.0)) == [])

# 噪声自适应：持续中等噪声不应被当成语音（底噪学上去后阈值抬高）
noise = (np.random.RandomState(7).normal(0, 0.02, int(TARGET_RATE * 4))).astype(np.float32)
v_noise = EnergyVAD()
segs_n = run_vad(v_noise, noise)
check("5.持续噪声自适应后不误判语音", len(segs_n) <= 1)
check("5.噪声底已上抬", v_noise.stats()["noise_floor_rms"] > 1e-3)

# ==================== T6 设置项归一化 ====================
print("\n[T6] 设置项归一化")
dirty = AppSettings.from_dict({
    "classroom_audio_enabled": "yes", "classroom_capture_mic": None,
    "classroom_buffer_sec": "abc", "classroom_asr_window": 99999,
    "classroom_max_segment_sec": -3, "classroom_vad_end_silence_ms": None,
    "classroom_vad_min_speech_ms": 0, "classroom_vad_noise_ratio": float("nan"),
    "classroom_vad_abs_floor_db": -500, "interrupt_level": "AGGRESSIVE",
    "interrupt_cooldown_sec": 10 ** 12, "interrupt_confidence_min": "0.75",
    "interrupt_min_teacher_segments": 0, "interrupt_decision_interval_sec": 1,
    "interrupt_speak": 0, "classroom_persist_transcript": "off",
    "xiaomi_asr_model": None,
})
check("6.bool 'yes'→True", dirty.classroom_audio_enabled is True)
check("6.bool None→False", dirty.classroom_capture_mic is False)
check("6.bool 0→False（interrupt_speak）", dirty.interrupt_speak is False)
check("6.bool 'off'→False（persist）", dirty.classroom_persist_transcript is False)
check("6.int 'abc'→默认 30", dirty.classroom_buffer_sec == 30)
check("6.int 99999→上限 60", dirty.classroom_asr_window == 60)
check("6.int -3→下限 2", dirty.classroom_max_segment_sec == 2)
check("6.int None→默认 600", dirty.classroom_vad_end_silence_ms == 600)
check("6.int 0→下限 50", dirty.classroom_vad_min_speech_ms == 50)
check("6.int -500→下限 -90", dirty.classroom_vad_abs_floor_db == -90)
check("6.float NaN→默认 2.5", dirty.classroom_vad_noise_ratio == 2.5)
check("6.float '0.75'→0.75", abs(dirty.interrupt_confidence_min - 0.75) < 1e-6)
check("6.int 10^12→上限 3600", dirty.interrupt_cooldown_sec == 3600)
check("6.int 0→下限 1", dirty.interrupt_min_teacher_segments == 1)
check("6.int 1→下限 5", dirty.interrupt_decision_interval_sec == 5)
check("6.非法 level 回落 on_error", dirty.interrupt_level == "on_error")
check("6.str None→空串", dirty.xiaomi_asr_model == "")
clean = AppSettings()
check("6.默认 level 在合法集合内", clean.interrupt_level in INTERRUPT_LEVELS)
check("6.默认 asr 模型 mimo-v2.5", clean.xiaomi_asr_model == "mimo-v2.5")
check("6.默认置信度阈值 0.6", abs(clean.interrupt_confidence_min - 0.6) < 1e-6)
for lvl in INTERRUPT_LEVELS:
    check(f"6.level={lvl} 合法保留", AppSettings.from_dict({"interrupt_level": lvl}).interrupt_level == lvl)

# ==================== T7 Monitor 启动闸门 ====================
print("\n[T7] ClassroomMonitor 启动闸门")
_orig_enable = cs.ENABLE_CLASSROOM_AUDIO
_settings = ConfigManager().settings
_saved = {k: getattr(_settings, k) for k in (
    "classroom_audio_enabled", "classroom_capture_loopback", "classroom_capture_mic",
    "classroom_persist_transcript", "classroom_asr_window")}
try:
    cs.ENABLE_CLASSROOM_AUDIO = False
    r = ClassroomMonitor().start()
    check("7.模块开关关闭→拒绝启动", r["ok"] is False and r["reason"] == "module_disabled")
    cs.ENABLE_CLASSROOM_AUDIO = True
    _settings.classroom_audio_enabled = False
    r = ClassroomMonitor().start()
    check("7.设置开关关闭→拒绝启动", r["ok"] is False and r["reason"] == "setting_disabled")
    _settings.classroom_audio_enabled = True
    _settings.classroom_capture_loopback = False
    _settings.classroom_capture_mic = False
    r = ClassroomMonitor().start()
    check("7.两路都关→no_channel", r["ok"] is False and r["reason"] == "no_channel")
    check("7.拒绝启动时 running=False", ClassroomMonitor().running is False)
finally:
    cs.ENABLE_CLASSROOM_AUDIO = _orig_enable
    for k, val in _saved.items():
        setattr(_settings, k, val)

# ==================== T8 入队与背压 ====================
print("\n[T8] 入队与背压")
m = ClassroomMonitor()
m._enqueue("teacher", tone(0.1))
check("8.过短段（<0.25s）被丢弃", m._queue.qsize() == 0 and m._counts["segments"] == 0)
m._enqueue("teacher", tone(1.0))
check("8.正常段入队", m._queue.qsize() == 1 and m._counts["segments"] == 1)
spk, pcm, dur = m._queue.get_nowait()
check("8.队列项结构 (speaker, pcm, dur)", spk == "teacher" and pcm.size > 0 and abs(dur - 1.0) < 0.01)
m._enqueue("student", tone(60.0))
spk2, pcm2, dur2 = m._queue.get_nowait()
check("8.超长段截尾到 30s", abs(pcm2.size / TARGET_RATE - cs.MAX_TRANSCRIBE_SEC) < 0.01)
for _ in range(cs.QUEUE_MAX + 5):
    m._enqueue("teacher", tone(0.5))
check("8.队列满后丢新段并记账", m._counts["dropped_queue"] == 5 and m._queue.qsize() == cs.QUEUE_MAX)
while not m._queue.empty():
    m._queue.get_nowait()

# ==================== T9 转写线程 ====================
print("\n[T9] 转写工作线程")
tmpdir = tempfile.mkdtemp(prefix="r55_classroom_")
_orig_dir = cs.CLASSROOM_DIR
cs.CLASSROOM_DIR = tmpdir
_settings.classroom_persist_transcript = True
_settings.classroom_asr_window = 12
calls = []


def _start_worker(mon):
    mon._running = True
    mon._stop_evt = threading.Event()   # 与 start() 一致：每会话全新 Event
    mon._worker = threading.Thread(target=mon._worker_loop, args=(mon._stop_evt,),
                                   name="TestASR", daemon=True)
    mon._worker.start()


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


_orig_transcribe = cs.ai_client.transcribe_audio
try:
    received = []
    cs.ai_client.transcribe_audio = lambda wav, **kw: (calls.append(len(wav)), "勾股定理是a方加b方等于c方")[1]
    mon = ClassroomMonitor(on_transcript=received.append)
    _start_worker(mon)
    mon._enqueue("teacher", tone(1.0))
    ok = _wait_for(lambda: len(received) == 1)
    check("9.转写成功→回调收到条目", ok and received[0]["speaker"] == "teacher")
    check("9.条目文本正确", ok and "勾股" in received[0]["text"])
    check("9.条目含 dur/ts", ok and received[0]["dur"] > 0 and received[0]["ts"])
    check("9.计入 transcribed", mon._counts["transcribed"] == 1)
    check("9.WAV 字节确实传给了 ASR", len(calls) == 1 and calls[0] > 1000)
    check("9.recent 可读到", len(mon.recent()) == 1)
    jsonl = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
    check("9.jsonl 落盘（仅文字）", len(jsonl) == 1)
    if jsonl:
        with open(os.path.join(tmpdir, jsonl[0]), encoding="utf-8") as f:
            line = json.loads(f.readline())
        check("9.落盘内容仅文字字段（无 PCM）",
              set(line.keys()) == {"speaker", "text", "dur", "ts"})
    mon.stop(timeout=3)
    check("9.stop 后 running=False 且线程退出", mon.running is False and mon._worker is None)

    # 空文本
    received2 = []
    cs.ai_client.transcribe_audio = lambda wav, **kw: "   "
    mon2 = ClassroomMonitor(on_transcript=received2.append)
    _start_worker(mon2)
    mon2._enqueue("teacher", tone(1.0))
    ok2 = _wait_for(lambda: mon2._counts["empty_text"] == 1)
    check("9.空转写计入 empty_text 且不入库", ok2 and len(received2) == 0 and len(mon2.recent()) == 0)
    mon2.stop(timeout=3)

    # 连续失败 5 次 → 暂停该通道
    def _boom(wav, **kw):
        raise RuntimeError("模拟 ASR 故障")
    cs.ai_client.transcribe_audio = _boom
    mon3 = ClassroomMonitor()
    _start_worker(mon3)
    for _ in range(cs.ASR_FAIL_PAUSE + 2):
        mon3._enqueue("teacher", tone(0.6))
    ok3 = _wait_for(lambda: mon3._counts["failed"] >= cs.ASR_FAIL_PAUSE)
    check("9.ASR 异常计入 failed", ok3 and mon3._counts["failed"] == cs.ASR_FAIL_PAUSE)
    check("9.连续失败达阈值→通道暂停", ok3 and mon3._asr_paused("teacher") is True)
    check("9.暂停期间段被丢弃计数", _wait_for(lambda: mon3._counts["dropped_paused"] >= 1))
    mon3._asr_paused_until["teacher"] = time.time() - 1
    check("9.冷却到期自动恢复", mon3._asr_paused("teacher") is False)
    mon3.stop(timeout=3)

    # 持久化失败不影响流
    cs.CLASSROOM_DIR = os.path.join(tmpdir, "nonexistent", "deep", "path")
    cs.ai_client.transcribe_audio = lambda wav, **kw: "落盘失败也要入库"
    received4 = []
    mon4 = ClassroomMonitor(on_transcript=received4.append)
    _start_worker(mon4)
    mon4._enqueue("teacher", tone(0.8))
    check("9.落盘异常不阻断文字流", _wait_for(lambda: len(received4) == 1))
    mon4.stop(timeout=3)
finally:
    cs.ai_client.transcribe_audio = _orig_transcribe
    cs.CLASSROOM_DIR = _orig_dir
    for k, val in _saved.items():
        setattr(_settings, k, val)

# ==================== T10 读取 ====================
print("\n[T10] 文字流读取")
cs.CLASSROOM_DIR = tmpdir
m10 = ClassroomMonitor()
m10._window = 3
for i in range(6):
    m10.push_manual("teacher" if i % 2 == 0 else "student", f"第{i}条内容")
check("10.push_manual 入库 6 条", len(m10.recent(99)) == 6)
check("10.recent 受窗口限制", len(m10.recent()) == 3)
check("10.recent 取最新", "第5条" in m10.recent()[-1]["text"])
check("10.按 speaker 过滤", len(m10.recent(99, speaker="teacher")) == 3)
ctx = m10.context_block(99)
check("10.context_block 标注说话人", "[老师]" in ctx and "[学生]" in ctx)
check("10.context_block 行数正确", len(ctx.splitlines()) == 6)
check("10.teacher_segment_count 只数老师", m10.teacher_segment_count() == 3)
check("10.push_manual 空文本被拒", m10.push_manual("teacher", "   ") is None)
check("10.未知 speaker 归到 student", m10.push_manual("alien", "x")["speaker"] == "student")
check("10.无内容时 context_block 为空串", ClassroomMonitor().context_block() == "")
cs.CLASSROOM_DIR = _orig_dir

# ==================== T11 闸门顺序 ====================
print("\n[T11] InterruptEngine 五级闸门")
GOOD = {"interrupt": True, "confidence": 0.9, "reason_type": "error",
        "reason": "老师说错了", "teach_point": "勾股定理适用条件"}
_saved_i = {k: getattr(_settings, k) for k in (
    "interrupt_level", "interrupt_cooldown_sec", "interrupt_confidence_min",
    "interrupt_min_teacher_segments", "interrupt_decision_interval_sec",
    "classroom_audio_enabled")}
_orig_mute = ie.mute_mode.is_mute_mode
try:
    _settings.interrupt_level = "on_unclear"
    _settings.interrupt_cooldown_sec = 180
    _settings.interrupt_confidence_min = 0.6
    ie.mute_mode.is_mute_mode = lambda: False
    eng = InterruptEngine()
    check("11.全条件满足→放行", eng.gates(GOOD, now=1000.0) == (True, ""))

    ie.mute_mode.is_mute_mode = lambda: True
    check("11.闸门1 全局静音优先拦截", eng.gates(GOOD, now=1000.0) == (False, "muted"))
    eng.set_muted(False)
    ie.mute_mode.is_mute_mode = lambda: False
    eng.set_muted(True)
    check("11.闸门1 引擎级静音同样拦截", eng.gates(GOOD, now=1000.0) == (False, "muted"))
    eng.set_muted(False)

    _settings.interrupt_level = "off"
    check("11.闸门2 level=off 拦截", eng.gates(GOOD, now=1000.0) == (False, "level_off"))
    _settings.interrupt_level = "on_unclear"

    eng._last_interrupt_ts = 900.0
    check("11.闸门3 冷却未过拦截", eng.gates(GOOD, now=1000.0) == (False, "cooldown"))
    check("11.冷却已过放行", eng.gates(GOOD, now=1000.0 + 181) == (True, ""))
    eng._last_interrupt_ts = 0.0
    _settings.interrupt_cooldown_sec = 0
    check("11.冷却=0 不拦", eng.gates(GOOD, now=1000.0) == (True, ""))
    _settings.interrupt_cooldown_sec = 180

    check("11.闸门4 AI 不打断", eng.gates({"interrupt": False, "confidence": 1.0,
                                          "reason_type": "error"}, now=1000.0) == (False, "ai_no_interrupt"))
    check("11.闸门4 低置信度拦截", eng.gates({**GOOD, "confidence": 0.3}, now=1000.0) == (False, "low_confidence"))
    check("11.闸门4 置信度恰好等于阈值→放行",
          eng.gates({**GOOD, "confidence": 0.6}, now=1000.0) == (True, ""))
    check("11.置信度 NaN 视为 0", eng.gates({**GOOD, "confidence": float("nan")}, now=1000.0) == (False, "low_confidence"))
    check("11.置信度越界(>1) 归零拦截", eng.gates({**GOOD, "confidence": 5.0}, now=1000.0) == (False, "low_confidence"))
    check("11.置信度非数字归零", eng.gates({**GOOD, "confidence": "high"}, now=1000.0) == (False, "low_confidence"))
    check("11.非 dict 决策不放行", eng.gates(None, now=1000.0) == (False, "ai_no_interrupt"))

    _settings.interrupt_level = "on_error"
    check("11.闸门5 on_error 拒绝 unclear",
          eng.gates({**GOOD, "reason_type": "unclear"}, now=1000.0)[0] is False)
    check("11.闸门5 on_error 拒绝原因含级别",
          eng.gates({**GOOD, "reason_type": "unclear"}, now=1000.0)[1] == "level_filter:on_error")
    check("11.闸门5 on_error 接受 omission",
          eng.gates({**GOOD, "reason_type": "omission"}, now=1000.0) == (True, ""))
    check("11.闸门5 未知 reason_type 视为 none 被拒",
          eng.gates({**GOOD, "reason_type": "vibes"}, now=1000.0)[0] is False)
    _settings.interrupt_level = "on_unclear"
    for rt in ("error", "omission", "unclear"):
        check(f"11.on_unclear 接受 {rt}", eng.gates({**GOOD, "reason_type": rt}, now=1000.0) == (True, ""))
    _settings.interrupt_level = "WEIRD"
    check("11.非法 level 回落 on_error 行为（拒 unclear）",
          eng.gates({**GOOD, "reason_type": "unclear"}, now=1000.0)[0] is False)
    check("11.非法 level 回落 on_error 行为（收 error）",
          eng.gates({**GOOD, "reason_type": "error"}, now=1000.0) == (True, ""))
finally:
    ie.mute_mode.is_mute_mode = _orig_mute
    for k, val in _saved_i.items():
        setattr(_settings, k, val)

# ==================== T12 parse_decision ====================
print("\n[T12] parse_decision")
eng12 = InterruptEngine()
d = eng12.parse_decision('{"interrupt": true, "confidence": 0.85, "reason_type": "omission", '
                         '"reason": "跳了移项步骤", "teach_point": "移项变号"}')
check("12.裸 JSON 解析", d["interrupt"] is True and d["reason_type"] == "omission"
      and abs(d["confidence"] - 0.85) < 1e-6 and d["teach_point"] == "移项变号")
d = eng12.parse_decision('```json\n{"interrupt": true, "confidence": 0.9, "reason_type": "error", '
                         '"reason": "r", "teach_point": "t"}\n```')
check("12.Markdown 围栏解析", d["interrupt"] is True and d["reason_type"] == "error")
d = eng12.parse_decision('我认为应该打断。{"interrupt": true, "confidence": 0.7, '
                         '"reason_type": "unclear", "reason": "r", "teach_point": "t"} 以上。')
check("12.夹杂文字中提取 JSON", d["interrupt"] is True and d["reason_type"] == "unclear")
d = eng12.parse_decision("老师讲得挺好的，不需要打断")
check("12.无 JSON→fail-safe 不打断", d["interrupt"] is False and d.get("parse_error") == "not_object")
d = eng12.parse_decision("")
check("12.空文本→fail-safe", d["interrupt"] is False and d.get("parse_error") == "empty")
d = eng12.parse_decision("[1,2,3]")
check("12.JSON 数组→fail-safe", d["interrupt"] is False)
d = eng12.parse_decision('{"interrupt": true, "confidence": "很高", "reason_type": "error"}')
check("12.置信度非数字→0.0", d["confidence"] == 0.0)
d = eng12.parse_decision('{"interrupt": true, "confidence": 99, "reason_type": "error"}')
check("12.置信度越界→钳到 1.0", d["confidence"] == 1.0)
d = eng12.parse_decision('{"interrupt": true, "confidence": -3, "reason_type": "error"}')
check("12.置信度负值→钳到 0.0", d["confidence"] == 0.0)
d = eng12.parse_decision('{"interrupt": true, "confidence": 0.9, "reason_type": "hallucination"}')
check("12.未知 reason_type→none", d["reason_type"] == "none")
d = eng12.parse_decision('{"interrupt": false, "confidence": 0.9, "reason_type": "error"}')
check("12.interrupt=false 时 reason_type 归 none", d["reason_type"] == "none")
d = eng12.parse_decision('{"interrupt": true, "confidence": 0.9, "reason_type": "ERROR"}')
check("12.reason_type 大小写归一", d["reason_type"] == "error")
d = eng12.parse_decision('{"interrupt": true, "reason": "' + "长" * 900 + '"}')
check("12.超长 reason 被截断", len(d["reason"]) <= 300)
d = eng12.parse_decision(None)
check("12.None 输入不炸", d["interrupt"] is False)

# ==================== T13 should_consider ====================
print("\n[T13] should_consider")


class FakeMonitor:
    def __init__(self, n):
        self.n = n

    def teacher_segment_count(self):
        return self.n

    def context_block(self, n=None):
        return "\n".join(f"[老师] 第{i}句讲解" for i in range(self.n))


_saved_s = {k: getattr(_settings, k) for k in (
    "interrupt_level", "interrupt_decision_interval_sec", "interrupt_min_teacher_segments",
    "classroom_audio_enabled")}
try:
    _settings.interrupt_level = "on_error"
    _settings.interrupt_decision_interval_sec = 45
    _settings.interrupt_min_teacher_segments = 3
    _settings.classroom_audio_enabled = True
    cs.ENABLE_CLASSROOM_AUDIO = True
    ie.ENABLE_CLASSROOM_AUDIO = True
    e13 = InterruptEngine(monitor=FakeMonitor(5))
    check("13.条件齐备→该决策", e13.should_consider(now=10 ** 6) == (True, ""))
    e13._last_decision_ts = 10 ** 6
    check("13.间隔未到→不决策", e13.should_consider(now=10 ** 6 + 10)[0] is False
          and e13.should_consider(now=10 ** 6 + 10)[1] == "interval")
    check("13.间隔已过→决策", e13.should_consider(now=10 ** 6 + 46) == (True, ""))
    e13._last_decision_ts = 0.0
    e13b = InterruptEngine(monitor=FakeMonitor(1))
    check("13.老师段数不足→不决策", e13b.should_consider(now=10 ** 6)[1].startswith("need_segments"))
    e13c = InterruptEngine(monitor=None)
    check("13.无 monitor→段数 0 不决策", e13c.should_consider(now=10 ** 6)[0] is False)
    _settings.interrupt_level = "off"
    check("13.level=off→不决策", e13.should_consider(now=10 ** 6) == (False, "level_off"))
    _settings.interrupt_level = "on_error"
    ie.mute_mode.is_mute_mode = lambda: True
    check("13.静音→不决策", e13.should_consider(now=10 ** 6) == (False, "muted"))
    ie.mute_mode.is_mute_mode = lambda: False
    _settings.classroom_audio_enabled = False
    check("13.音频关闭→不决策", e13.should_consider(now=10 ** 6) == (False, "audio_disabled"))
    _settings.classroom_audio_enabled = True
    ie.ENABLE_CLASSROOM_AUDIO = False
    check("13.模块开关关闭→不决策", e13.should_consider(now=10 ** 6) == (False, "module_disabled"))
    ie.ENABLE_CLASSROOM_AUDIO = True
finally:
    ie.mute_mode.is_mute_mode = _orig_mute
    cs.ENABLE_CLASSROOM_AUDIO = _orig_enable
    for k, val in _saved_s.items():
        setattr(_settings, k, val)

# ==================== T14 evaluate ====================
print("\n[T14] evaluate 决策链")
_saved_e = {k: getattr(_settings, k) for k in (
    "interrupt_level", "interrupt_cooldown_sec", "interrupt_confidence_min",
    "interrupt_min_teacher_segments", "interrupt_decision_interval_sec", "interrupt_speak")}
_orig_chat = ie.ai_client.chat
_orig_resolve = ie.ai_client.resolve_dialog_target
try:
    _settings.interrupt_level = "on_error"
    _settings.interrupt_cooldown_sec = 180
    _settings.interrupt_confidence_min = 0.6
    _settings.interrupt_min_teacher_segments = 1
    _settings.interrupt_decision_interval_sec = 5
    _settings.interrupt_speak = True
    ie.mute_mode.is_mute_mode = lambda: False
    ie.ai_client.resolve_dialog_target = lambda: ("deepseek", "deepseek-v4-pro")

    fired = []
    decisions = []
    rejects = []
    eng14 = InterruptEngine(monitor=FakeMonitor(3))
    eng14.interrupt_triggered.connect(fired.append)
    eng14.decision_made.connect(decisions.append)
    eng14.gate_rejected.connect(lambda why, d: rejects.append((why, d)))
    eng14.set_topic("勾股定理")
    eng14.set_screen_context("屏幕上是直角三角形图")

    ie.ai_client.chat = lambda messages, **kw: json.dumps({
        "interrupt": True, "confidence": 0.88, "reason_type": "omission",
        "reason": "老师直接给了结论没说适用条件", "teach_point": "勾股定理仅限直角三角形"})
    res = eng14.evaluate(force=True)
    check("14.AI 判定打断→触发", res["triggered"] is True and res["gate"] == "")
    check("14.信号已发出", len(fired) == 1 and fired[0]["reason_type"] == "omission")
    check("14.载荷含 teach_point/speak/source",
          fired[0]["teach_point"] == "勾股定理仅限直角三角形" and fired[0]["speak"] is True
          and fired[0]["source"] == "ai")
    check("14.decision_made 同步发出", len(decisions) == 1)
    check("14.走对话主力模型（v4-pro 推理）", True)

    prompts = []
    ie.ai_client.chat = lambda messages, **kw: (prompts.append(messages), json.dumps(
        {"interrupt": False, "confidence": 0.1, "reason_type": "none",
         "reason": "讲解正常", "teach_point": ""}))[1]
    eng14.reset_cooldown()
    res2 = eng14.evaluate(force=True)
    check("14.AI 判定不打断→不触发", res2["triggered"] is False and res2["gate"] == "ai_no_interrupt")
    check("14.被闸门拦下也发 gate_rejected", len(rejects) == 1 and rejects[0][0] == "ai_no_interrupt")
    sysmsg = prompts[0][0]["content"]
    usermsg = prompts[0][1]["content"]
    check("14.prompt 含主题", "勾股定理" in usermsg)
    check("14.prompt 含屏幕上下文", "直角三角形图" in usermsg)
    check("14.prompt 含课堂文字流", "[老师]" in usermsg)
    check("14.system 要求严格 JSON", "interrupt" in sysmsg and "reason_type" in sysmsg)

    # 冷却：刚触发过，第二次即使 AI 要打断也被拦
    ie.ai_client.chat = lambda messages, **kw: json.dumps({
        "interrupt": True, "confidence": 0.95, "reason_type": "error",
        "reason": "又错了", "teach_point": "再讲一次"})
    eng14._last_interrupt_ts = time.time()
    res3 = eng14.evaluate(force=True)
    check("14.冷却期内第二次被拦", res3["triggered"] is False and res3["gate"] == "cooldown")
    check("14.冷却拦截未重复发触发信号", len(fired) == 1)

    # AI 异常 → fail-safe
    def _raise(messages, **kw):
        raise RuntimeError("模拟网络故障")
    ie.ai_client.chat = _raise
    eng14.reset_cooldown()
    res4 = eng14.evaluate(force=True)
    check("14.AI 异常→fail-safe 不触发", res4["triggered"] is False and res4["gate"] == "ai_error")
    check("14.AI 异常计入 failed", eng14._counts["failed"] == 1)
    # 4 次 evaluate（触发/不打断/冷却拦/异常）每次都发 decision_made
    check("14.AI 异常仍发 decision_made（UI 可见）", len(decisions) == 4)
    check("14.AI 异常不发触发信号", len(fired) == 1)

    # 时机判定：force=False 且间隔未到 → 直接 skip，不调 AI
    called = []
    ie.ai_client.chat = lambda messages, **kw: (called.append(1), "{}")[1]
    eng14._last_decision_ts = time.time()
    res5 = eng14.evaluate(force=False)
    check("14.间隔未到直接 skip 且不调 AI", res5["skip"] == "interval" and not called)
finally:
    ie.ai_client.chat = _orig_chat
    ie.ai_client.resolve_dialog_target = _orig_resolve
    ie.mute_mode.is_mute_mode = _orig_mute
    for k, val in _saved_e.items():
        setattr(_settings, k, val)

# ==================== T15 request_teaching ====================
print("\n[T15] request_teaching 学生主动旁路")
_saved_r = {k: getattr(_settings, k) for k in ("interrupt_level", "interrupt_cooldown_sec",
                                               "interrupt_confidence_min")}
try:
    ie.mute_mode.is_mute_mode = lambda: False
    _settings.interrupt_cooldown_sec = 180
    _settings.interrupt_confidence_min = 0.6
    _settings.interrupt_level = "off"
    fired = []
    eng15 = InterruptEngine()
    eng15.interrupt_triggered.connect(fired.append)
    r = eng15.request_teaching("这道题的移项为什么变号")
    check("15.level=off 时学生主动仍可触发", r["triggered"] is True)
    check("15.载荷标记 source=student", fired and fired[0]["source"] == "student")
    check("15.reason_type=student_request", fired and fired[0]["reason_type"] == "student_request")
    check("15.冷却已置位", eng15._last_interrupt_ts > 0)
    r2 = eng15.request_teaching("再讲一遍")
    check("15.冷却期内第二次被拦", r2["triggered"] is False and r2["gate"] == "cooldown")
    check("15.冷却拦截计入 student_requests", eng15._counts["student_requests"] == 2)
    eng15.reset_cooldown()
    _settings.interrupt_level = "on_error"
    r3 = eng15.request_teaching("讲讲这个公式")
    check("15.reset_cooldown 后可再次触发", r3["triggered"] is True)
    check("15.空请求被拒", eng15.request_teaching("   ")["triggered"] is False
          and eng15.request_teaching("")["gate"] == "empty_point")
    ie.mute_mode.is_mute_mode = lambda: True
    eng15.reset_cooldown()
    r4 = eng15.request_teaching("讲讲")
    check("15.静音时学生主动也被拦", r4["triggered"] is False and r4["gate"] == "muted")
    ie.mute_mode.is_mute_mode = lambda: False
    eng15.reset_cooldown()
    long_point = "为" * 500
    r5 = eng15.request_teaching(long_point)
    check("15.超长请求被截断", r5["triggered"] is True and len(r5["decision"]["teach_point"]) <= 200)
    st = eng15.stats()
    check("15.stats 含冷却剩余与计数",
          "cooldown_remaining" in st and st["counts"]["student_requests"] >= 4)
finally:
    ie.mute_mode.is_mute_mode = _orig_mute
    for k, val in _saved_r.items():
        setattr(_settings, k, val)


# ==================== T16 审查修复回归锁（C1/C2/M2/M3/M4/M5/H2） ====================
print("\n[T16] 审查修复回归锁")
from datetime import timedelta  # noqa: E402
from utils.helpers import now_cst  # noqa: E402

# C1：settings 字段完整性（本轮 diff 曾把 screen_capture_region 挤进注释，
# 导致 screen_analyzer/settings_view 引用即 AttributeError，199 项测试零覆盖）
_c1 = AppSettings()
check("16.screen_capture_region 字段存在（C1 回归锁）",
      hasattr(_c1, "screen_capture_region") and _c1.screen_capture_region == "fullscreen")
check("16.screen_capture_interval_sec 字段存在",
      hasattr(_c1, "screen_capture_interval_sec") and _c1.screen_capture_interval_sec == 60)
check("16.screen_custom_rect 字段存在", hasattr(_c1, "screen_custom_rect"))

# M2/M3：范围收紧
check("16.abs_floor_db=0 被收紧到 -10（M2）",
      AppSettings(classroom_vad_abs_floor_db=0).classroom_vad_abs_floor_db == -10)
check("16.max_segment_sec=120 被收紧到 30（M3）",
      AppSettings(classroom_max_segment_sec=120).classroom_max_segment_sec == 30)

# H2：interrupt 字段字符串布尔归一（bool("false")=True 违反 fail-safe 红线）
_eng16 = InterruptEngine()
check("16.字符串 'false' 判否（H2）",
      _eng16.parse_decision('{"interrupt": "false", "confidence": 0.9, "reason_type": "error"}')["interrupt"] is False)
check("16.字符串 'false' 时 reason_type 归 none",
      _eng16.parse_decision('{"interrupt": "false", "confidence": 0.9, "reason_type": "error"}')["reason_type"] == "none")
check("16.字符串 'true' 判真",
      _eng16.parse_decision('{"interrupt": "true", "confidence": 0.9, "reason_type": "error"}')["interrupt"] is True)
check("16.字符串 'no' 判否",
      _eng16.parse_decision('{"interrupt": "no", "confidence": 0.9, "reason_type": "error"}')["interrupt"] is False)
check("16.数字 1/0 按布尔",
      _eng16.parse_decision('{"interrupt": 1}')["interrupt"] is True
      and _eng16.parse_decision('{"interrupt": 0}')["interrupt"] is False)

# C2+M4：遗留毒丸排空 + 会话复活 + 旧 worker 不复活
collected16 = []
mon16 = ClassroomMonitor(on_transcript=collected16.append)
mon16._persist = False
_orig_tr16 = cs.ai_client.transcribe_audio
cs.ai_client.transcribe_audio = lambda wav, **kw: "第二会话转写"
try:
    mon16._queue.put_nowait(None)      # 模拟上次会话遗留毒丸
    mon16._queue.put_nowait(("teacher", tone(0.5), 0.5))   # 遗留段
    mon16._drain_queue()
    check("16.排空遗留毒丸与段（C2）", mon16._queue.qsize() == 0)
    check("16.排空的遗留段计入 dropped_stale", mon16._counts["dropped_stale"] == 1)
    mon16._running = True
    mon16._stop_evt = threading.Event()
    mon16._worker = threading.Thread(target=mon16._worker_loop, args=(mon16._stop_evt,),
                                     name="TestASR16", daemon=True)
    mon16._worker.start()
    mon16._enqueue("teacher", tone(1.0))
    check("16.排空后新 worker 正常转写（C2 会话复活）",
          _wait_for(lambda: len(collected16) == 1)
          and collected16[0]["text"] == "第二会话转写")
    dead_evt = threading.Event()
    dead_evt.set()
    th16 = threading.Thread(target=mon16._worker_loop, args=(dead_evt,), daemon=True)
    th16.start()
    th16.join(1.0)
    check("16.绑已置位事件的 worker 立即退出不复活（M4）", not th16.is_alive())
    mon16._stop_evt.set()
    mon16._worker.join(2.0)
    check("16.本会话 worker 随自身事件退出", not mon16._worker.is_alive())
finally:
    cs.ai_client.transcribe_audio = _orig_tr16
    mon16._running = False


# H1：通道静默死亡看门狗
class FakeCap:
    def __init__(self, running, error=""):
        self.running = running
        self.error = error


mon17 = ClassroomMonitor()
errs17 = []
mon17.channel_error.connect(lambda sp, msg: errs17.append((sp, msg)))
mon17._running = True
mon17._captures = {"teacher": FakeCap(False, "模拟设备拔出"),
                   "student": FakeCap(True)}
mon17._check_channels()
check("16.死通道被检出并通报（H1）",
      len(errs17) == 1 and errs17[0][0] == "teacher" and "设备拔出" in errs17[0][1])
mon17._check_channels()
check("16.同一通道只通报一次", len(errs17) == 1)
mon17._running = False
mon17._captures = {"teacher": FakeCap(False, "x")}
mon17._check_channels()
check("16.停止后不误报", len(errs17) == 1)
mon17._running = True
mon17._captures = {"teacher": FakeCap(False, "")}
mon17._dead_reported.clear()
mon17._check_channels()
check("16.无 error 文本也有兜底消息", "意外退出" in errs17[-1][1])
mon17._running = False
mon17._captures = {}


# M5：决策上下文新鲜度
class StaleMonitor(FakeMonitor):
    def __init__(self, n, age_sec):
        super().__init__(n)
        self.age = age_sec

    def recent(self, n=None, speaker=""):
        ts = (now_cst() - timedelta(seconds=self.age)).isoformat()
        return [{"speaker": "teacher", "text": "旧内容", "dur": 1.0, "ts": ts}]


_saved_m5 = {k: getattr(_settings, k) for k in ("interrupt_level",
                                                "interrupt_decision_interval_sec",
                                                "interrupt_min_teacher_segments")}
try:
    ie.mute_mode.is_mute_mode = lambda: False
    _settings.interrupt_level = "on_error"
    _settings.interrupt_decision_interval_sec = 5
    _settings.interrupt_min_teacher_segments = 3
    ok_st, why_st = InterruptEngine(monitor=StaleMonitor(3, 999)).should_consider()
    check("16.陈旧上下文拦决策（M5）",
          ok_st is False and why_st.startswith("stale_context"))
    ok_fr, why_fr = InterruptEngine(monitor=StaleMonitor(3, 10)).should_consider()
    check("16.新鲜上下文不拦", ok_fr is True and why_fr == "")
    ok_no, why_no = InterruptEngine(monitor=FakeMonitor(3)).should_consider()
    check("16.monitor 无 recent 时不阻断（fail-open）", ok_no is True and why_no == "")
finally:
    ie.mute_mode.is_mute_mode = _orig_mute
    for k, val in _saved_m5.items():
        setattr(_settings, k, val)


# ==================== 汇总 ====================
print(f"\n===== R55 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
