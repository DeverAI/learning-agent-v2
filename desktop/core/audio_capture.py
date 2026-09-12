"""WASAPI 音频采集（纯 ctypes COM 直调，零新依赖）。

设计：Design.md「课堂感知音频通道（round 55）」、Techniques.md「1. 纯 ctypes WASAPI 采集」。

支持两种源，共用同一套实现：
- source="loopback"：系统音频回环（网课老师声音）。取 eRender 默认端点 +
  AUDCLNT_STREAMFLAGS_LOOPBACK。无播放时不产生数据包（属正常，不伪造静音）。
- source="mic"：麦克风（学生提问）。取 eCapture 默认端点。

输出统一归一为 mono float32 @ 16kHz，压入有界环形缓冲；采集线程内可选同步跑 VAD
（on_segment 回调），闭合语音段交给上层转写。

红线：原始 PCM 只存在于内存环形缓冲与回调段对象内，本模块不落盘、不上传。
"""
import ctypes
import io
import struct
import threading
import time
import uuid
import wave
from collections import deque

import numpy as np

from utils.helpers import logger, append_err_record

# ========== 常量 ==========

TARGET_RATE = 16000          # 归一采样率（ASR 用 16k mono 足够且省流量）
HRESULT = ctypes.c_long      # 有符号 32 位，失败码为负
DWORD = ctypes.c_uint32
UINT32 = ctypes.c_uint32
UINT64 = ctypes.c_uint64
BYTE = ctypes.c_uint8
REFERENCE_TIME = ctypes.c_longlong   # 100ns 单位

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x8
RPC_E_CHANGED_MODE = 0x80010106
S_OK = 0
S_FALSE = 1   # COM 已在本线程初始化过：仍算初始化成功，必须配对 CoUninitialize

E_RENDER = 0
E_CAPTURE = 1
E_MULTIMEDIA = 1

# GUID
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"

KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = uuid.UUID("{00000003-0000-0010-8000-00aa00389b71}").bytes_le
KSDATAFORMAT_SUBTYPE_PCM = uuid.UUID("{00000001-0000-0010-8000-00aa00389b71}").bytes_le

WAVEFORMATEX_SIZE = 18
WAVEFORMATEXTENSIBLE_SIZE = 40


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
        return g


class WAVEFORMATEX(ctypes.Structure):
    # C 头文件里 WAVEFORMATEX 是 #pragma pack(1) 的 18 字节结构；
    # ctypes 默认对齐会补到 20，虽当前只用作指针类型（解析走 string_at），
    # 仍按 ABI 精确声明，防御未来实例化时 sizeof 不符。
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", UINT32),
        ("nAvgBytesPerSec", UINT32),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


# ========== COM vtable 声明 ==========
# 只给实际调用的槽位声明原型；其余槽位用 c_void_p 占位以保证偏移正确。
# IUnknown: 0 QueryInterface / 1 AddRef / 2 Release
#
# 注意：ctypes Structure 继承会把父类字段自动排在子类字段之前，
# 子类 _fields_ 只能写「新增槽位」，重复父类字段会让 vtable 索引整体偏移
# （实测偏移 3 → GetDefaultAudioEndpoint 调到第 7 槽，返回 E_POINTER 0x80004003）。


class _IUnknownHead(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p,
                                              ctypes.POINTER(GUID),
                                              ctypes.POINTER(ctypes.c_void_p))),
        ("AddRef", ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)),
        ("Release", ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)),
    ]


class _IMMDeviceEnumeratorVtbl(_IUnknownHead):
    _fields_ = [
        ("EnumAudioEndpoints", ctypes.c_void_p),                       # 3
        ("GetDefaultAudioEndpoint", ctypes.WINFUNCTYPE(                # 4
            HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p))),
        ("GetDevice", ctypes.c_void_p),                                # 5
    ]


class IMMDeviceEnumerator(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IMMDeviceEnumeratorVtbl))]


class _IMMDeviceVtbl(_IUnknownHead):
    _fields_ = [
        ("Activate", ctypes.WINFUNCTYPE(                               # 3
            HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), DWORD,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))),
        ("OpenPropertyStore", ctypes.c_void_p),                        # 4
        ("GetId", ctypes.c_void_p),                                    # 5
        ("GetState", ctypes.c_void_p),                                 # 6
    ]


class IMMDevice(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IMMDeviceVtbl))]


class _IAudioClientVtbl(_IUnknownHead):
    _fields_ = [
        ("Initialize", ctypes.WINFUNCTYPE(                             # 3
            HRESULT, ctypes.c_void_p, ctypes.c_int, DWORD,
            REFERENCE_TIME, REFERENCE_TIME,
            ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p)),
        ("GetBufferSize", ctypes.WINFUNCTYPE(                          # 4
            HRESULT, ctypes.c_void_p, ctypes.POINTER(UINT32))),
        ("GetStreamLatency", ctypes.c_void_p),                         # 5
        ("GetCurrentPadding", ctypes.WINFUNCTYPE(                      # 6
            HRESULT, ctypes.c_void_p, ctypes.POINTER(UINT32))),
        ("IsFormatSupported", ctypes.c_void_p),                        # 7
        ("GetMixFormat", ctypes.WINFUNCTYPE(                           # 8
            HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(WAVEFORMATEX)))),
        ("GetDevicePeriod", ctypes.c_void_p),                          # 9
        ("Start", ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)),       # 10
        ("Stop", ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)),        # 11
        ("Reset", ctypes.c_void_p),                                    # 12
        ("SetEventHandle", ctypes.c_void_p),                           # 13
        ("GetService", ctypes.WINFUNCTYPE(                             # 14
            HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p))),
    ]


class IAudioClient(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IAudioClientVtbl))]


class _IAudioCaptureClientVtbl(_IUnknownHead):
    _fields_ = [
        ("GetBuffer", ctypes.WINFUNCTYPE(                              # 3
            HRESULT, ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(BYTE)),
            ctypes.POINTER(UINT32), ctypes.POINTER(DWORD),
            ctypes.POINTER(UINT64), ctypes.POINTER(UINT64))),
        ("ReleaseBuffer", ctypes.WINFUNCTYPE(                          # 4
            HRESULT, ctypes.c_void_p, UINT32)),
        ("GetNextPacketSize", ctypes.c_void_p),                        # 5
    ]


class IAudioCaptureClient(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IAudioCaptureClientVtbl))]


def _hr(hr: int, what: str):
    """HRESULT 判定：负值为失败，抛出带十六进制码的异常便于定位。"""
    if hr < 0:
        raise OSError(f"{what} 失败: 0x{hr & 0xFFFFFFFF:08X}")


def _release(obj):
    """安全 Release（obj 为 ctypes 结构体实例或 None）。"""
    if obj is None:
        return
    try:
        obj.lpVtbl.contents.Release(ctypes.addressof(obj))
    except Exception:
        pass


# ========== 音频工具 ==========

def resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性插值重采样（float32 mono）。块内插值，块边界有极小相位误差，
    对 ASR 无影响（块长 >= 50ms）。"""
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.size == 0 or src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return pcm
    n_out = int(round(pcm.size * dst_rate / src_rate))
    if n_out <= 0:
        return pcm[:0]
    x_old = np.linspace(0.0, 1.0, pcm.size, dtype=np.float64)
    x_new = np.linspace(0.0, 1.0, n_out, dtype=np.float64)
    return np.interp(x_new, x_old, pcm.astype(np.float64)).astype(np.float32)


def to_mono_float32(raw: bytes, channels: int, bits: int, is_float: bool) -> np.ndarray:
    """WASAPI 原始包 → mono float32 [-1,1]。格式不支持时返回空数组。"""
    if not raw:
        return np.empty(0, dtype=np.float32)
    channels = max(1, int(channels))
    if is_float and bits == 32:
        arr = np.frombuffer(raw, dtype="<f4")
    elif not is_float and bits == 16:
        arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif not is_float and bits == 32:
        arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif not is_float and bits == 8:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return np.empty(0, dtype=np.float32)
    # 截掉不足一帧的尾部，保证 reshape 不抛异常
    usable = (arr.size // channels) * channels
    if usable <= 0:
        return np.empty(0, dtype=np.float32)
    arr = arr[:usable]
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(arr, dtype=np.float32)


def float32_to_wav(pcm, rate: int = TARGET_RATE) -> bytes:
    """mono float32 → 16bit PCM WAV 字节（ASR 上传格式）。"""
    arr = np.asarray(pcm, dtype=np.float32)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    arr = np.clip(np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
    data = (arr * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate) if rate and rate > 0 else TARGET_RATE)
        w.writeframes(data)
    return buf.getvalue()


def _parse_mix_format(fmt_ptr) -> dict:
    """解析 GetMixFormat 返回的 WAVEFORMATEX(/EXTENSIBLE)。"""
    head = ctypes.string_at(fmt_ptr, WAVEFORMATEX_SIZE)
    tag, ch, rate, _avg, _align, bits, _cb = struct.unpack_from("<HHIIHHH", head, 0)
    is_float = False
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE：SubFormat 在偏移 24
        ext = ctypes.string_at(fmt_ptr, WAVEFORMATEXTENSIBLE_SIZE)
        sub = ext[24:40]
        is_float = (sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)
    elif tag == 0x0003:  # WAVE_FORMAT_IEEE_FLOAT
        is_float = True
    elif tag != 0x0001:  # 非 PCM
        raise OSError(f"不支持的混音格式 wFormatTag=0x{tag:04X}")
    if rate <= 0 or ch <= 0 or bits not in (8, 16, 32):
        raise OSError(f"混音格式参数异常 rate={rate} ch={ch} bits={bits}")
    return {"tag": tag, "channels": int(ch), "rate": int(rate),
            "bits": int(bits), "is_float": bool(is_float)}


# ========== 采集器 ==========

class AudioCapture:
    """单路 WASAPI 采集器（线程安全；一个实例对应一个音频源）。"""

    def __init__(self, source: str = "loopback", buffer_seconds: float = 30.0,
                 block_ms: int = 50, on_segment=None, label: str = ""):
        src = str(source or "loopback").strip().lower()
        if src not in ("loopback", "mic"):
            raise ValueError(f"source 只支持 loopback/mic，收到: {source!r}")
        self.source = src
        self.label = str(label or src)
        self.block_ms = max(5, min(500, int(block_ms or 50)))
        self.buffer_seconds = max(1.0, float(buffer_seconds or 30.0))
        self.on_segment = on_segment      # 可选 VAD 回调：fn(pcm_float32_16k)
        self.error = ""                   # 启动失败原因（空=正常）
        self.format_info = {}             # 混音格式（启动成功后填充）

        self._max_samples = int(TARGET_RATE * self.buffer_seconds)
        self._chunks = deque()
        self._samples = 0
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread = None
        self._dropped_seconds = 0.0
        self._captured_seconds = 0.0
        self._idle_seconds = 0.0          # loopback 空闲期注入的合成静音秒数（非设备数据）

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive())

    def start(self) -> bool:
        """启动采集线程。失败不抛异常：置 self.error 并记 Err.md，返回 False。"""
        if self.running:
            return True
        self.error = ""
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"AudioCapture-{self.label}", daemon=True)
        self._thread.start()
        # 等待设备初始化结果（最多 3s），让调用方能立刻拿到成败
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if self.error or self.format_info:
                break
            if not self._thread.is_alive():
                break
            time.sleep(0.02)
        return not self.error and bool(self.format_info)

    def stop(self, timeout: float = 2.0):
        """停止采集并等待线程退出（COM 资源在线程内释放）。"""
        self._stop_evt.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=max(0.1, float(timeout)))
        self._thread = None

    # ---------- 缓冲读写 ----------

    def feed(self, pcm):
        """写入归一后的 PCM（采集线程内部使用；测试可直接注入合成信号）。"""
        arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return
        with self._lock:
            self._chunks.append(arr)
            self._samples += arr.size
            self._captured_seconds += arr.size / float(TARGET_RATE)
            while self._samples > self._max_samples and self._chunks:
                if len(self._chunks) > 1:
                    old = self._chunks.popleft()
                    self._samples -= old.size
                    self._dropped_seconds += old.size / float(TARGET_RATE)
                else:
                    # 单块自身超限（公开 feed 注入大块时会发生；实际采集为
                    # block_ms 小块不会走到这）：切掉块头部，保留最近
                    # _max_samples，守住"有界环形缓冲=最近 N 秒"的契约
                    only = self._chunks[0]
                    excess = self._samples - self._max_samples
                    self._chunks[0] = only[excess:]
                    self._samples -= excess
                    self._dropped_seconds += excess / float(TARGET_RATE)

    def read(self, seconds: float):
        """取出并移除最近 seconds 秒 PCM（不足则返回全部）。"""
        want = max(0, int(float(seconds) * TARGET_RATE))
        if want <= 0:
            return np.empty(0, dtype=np.float32)
        out = []
        with self._lock:
            while self._chunks and want > 0:
                chunk = self._chunks[0]
                if chunk.size <= want:
                    self._chunks.popleft()
                    want -= chunk.size
                    out.append(chunk)
                else:
                    out.append(chunk[:want])
                    self._chunks[0] = chunk[want:]
                    want = 0
            self._samples = sum(c.size for c in self._chunks)
        if not out:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32)

    def available_seconds(self) -> float:
        with self._lock:
            return self._samples / float(TARGET_RATE)

    def stats(self) -> dict:
        with self._lock:
            return {
                "source": self.source,
                "label": self.label,
                "running": self.running,
                "error": self.error,
                "format": dict(self.format_info),
                "buffered_seconds": round(self._samples / float(TARGET_RATE), 2),
                "captured_seconds": round(self._captured_seconds, 2),
                "dropped_seconds": round(self._dropped_seconds, 2),
                "idle_seconds": round(self._idle_seconds, 2),
            }

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._samples = 0

    # ---------- 采集线程 ----------

    def _capture_loop(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, DWORD]
        ole32.CoInitializeEx.restype = HRESULT
        ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, DWORD,
                                           ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = HRESULT
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE)
        com_inited = hr in (S_OK, S_FALSE)
        if hr != S_OK and (hr & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
            self._fail(f"CoInitializeEx 0x{hr & 0xFFFFFFFF:08X}")
            return

        enumerator = None
        device = None
        client = None
        capture_client = None
        fmt_ptr = None
        started = False
        try:
            p_enum = ctypes.c_void_p()
            _hr(ole32.CoCreateInstance(
                ctypes.byref(GUID.from_str(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                ctypes.byref(GUID.from_str(IID_IMMDeviceEnumerator)), ctypes.byref(p_enum)),
                "CoCreateInstance(MMDeviceEnumerator)")
            enumerator = ctypes.cast(p_enum, ctypes.POINTER(IMMDeviceEnumerator)).contents

            flow = E_RENDER if self.source == "loopback" else E_CAPTURE
            p_dev = ctypes.c_void_p()
            _hr(enumerator.lpVtbl.contents.GetDefaultAudioEndpoint(
                ctypes.addressof(enumerator), flow, E_MULTIMEDIA, ctypes.byref(p_dev)),
                f"GetDefaultAudioEndpoint({'eRender' if flow == E_RENDER else 'eCapture'})")
            device = ctypes.cast(p_dev, ctypes.POINTER(IMMDevice)).contents

            p_client = ctypes.c_void_p()
            _hr(device.lpVtbl.contents.Activate(
                ctypes.addressof(device), ctypes.byref(GUID.from_str(IID_IAudioClient)),
                CLSCTX_ALL, None, ctypes.byref(p_client)), "IAudioClient Activate")
            client = ctypes.cast(p_client, ctypes.POINTER(IAudioClient)).contents

            p_fmt = ctypes.POINTER(WAVEFORMATEX)()
            _hr(client.lpVtbl.contents.GetMixFormat(
                ctypes.addressof(client), ctypes.byref(p_fmt)), "GetMixFormat")
            fmt_ptr = ctypes.cast(p_fmt, ctypes.c_void_p)
            info = _parse_mix_format(fmt_ptr)
            self.format_info = info

            flags = AUDCLNT_STREAMFLAGS_LOOPBACK if self.source == "loopback" else 0
            _hr(client.lpVtbl.contents.Initialize(
                ctypes.addressof(client), AUDCLNT_SHAREMODE_SHARED, flags,
                REFERENCE_TIME(2_000_000), REFERENCE_TIME(0), p_fmt, None),
                "IAudioClient Initialize")

            p_cap = ctypes.c_void_p()
            _hr(client.lpVtbl.contents.GetService(
                ctypes.addressof(client), ctypes.byref(GUID.from_str(IID_IAudioCaptureClient)),
                ctypes.byref(p_cap)), "GetService(IAudioCaptureClient)")
            capture_client = ctypes.cast(p_cap, ctypes.POINTER(IAudioCaptureClient)).contents

            _hr(client.lpVtbl.contents.Start(ctypes.addressof(client)), "IAudioClient Start")
            started = True

            block_sleep = self.block_ms / 1000.0
            # 连续无数据超过该时长才注入空闲静音：活跃放音期"消耗快于生产"的
            # 瞬时 padding==0 不注入，避免静音帧穿插进真实语音、拉长 VAD 时间轴
            # （实测不設门槛时段长虚增 ~70%：2.8s 语音被算成 5.18s）。
            # 真实停流后的收段延迟 = 该门槛 + end_silence ≈ 0.85s，可接受。
            idle_inject_after = 0.25
            idle_streak = 0.0
            ch, rate, bits, is_float = (info["channels"], info["rate"],
                                        info["bits"], info["is_float"])
            while not self._stop_evt.is_set():
                padding = UINT32(0)
                hr_pad = client.lpVtbl.contents.GetCurrentPadding(
                    ctypes.addressof(client), ctypes.byref(padding))
                if hr_pad < 0:
                    self._fail(f"GetCurrentPadding 0x{hr_pad & 0xFFFFFFFF:08X}")
                    break
                if padding.value == 0:
                    # WASAPI loopback 在系统无放音（render 流空闲）时停产数据包：
                    # 持续空闲时向 VAD 路径补偿注入合成静音，按实时速率推进
                    # "段尾静音收段"逻辑，否则空闲前最后一个语音段永远卡在
                    # 开启态、内容滞留不转写。只走 on_segment（VAD），不进环形
                    # 缓冲——captured_seconds 只记真实设备数据。
                    time.sleep(block_sleep)
                    idle_streak += block_sleep
                    if idle_streak >= idle_inject_after and self.on_segment is not None:
                        self._idle_seconds += idle_streak
                        n_silence = int(TARGET_RATE * idle_streak)
                        idle_streak = 0.0
                        try:
                            self.on_segment(np.zeros(n_silence, dtype=np.float32))
                        except Exception as e:
                            logger.warning(
                                f"[{self.label}] 空闲静音注入回调异常: {str(e)[:150]}")
                    continue
                idle_streak = 0.0
                p_data = ctypes.POINTER(BYTE)()
                n_frames = UINT32(0)
                pkt_flags = DWORD(0)
                hr_buf = capture_client.lpVtbl.contents.GetBuffer(
                    ctypes.addressof(capture_client), ctypes.byref(p_data),
                    ctypes.byref(n_frames), ctypes.byref(pkt_flags), None, None)
                if hr_buf < 0:
                    self._fail(f"GetBuffer 0x{hr_buf & 0xFFFFFFFF:08X}")
                    break
                frames = int(n_frames.value)
                try:
                    if frames > 0:
                        if pkt_flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                            pcm = np.zeros(frames, dtype=np.float32)
                        else:
                            nbytes = frames * ch * (bits // 8)
                            raw = ctypes.string_at(p_data, nbytes)
                            pcm = to_mono_float32(raw, ch, bits, is_float)
                        if pcm.size:
                            pcm = resample(pcm, rate, TARGET_RATE)
                            self.feed(pcm)
                            if self.on_segment is not None:
                                try:
                                    self.on_segment(pcm)
                                except Exception as e:
                                    logger.warning(f"[{self.label}] on_segment 回调异常: {str(e)[:150]}")
                finally:
                    # 处理包时无论是否抛异常都必须 ReleaseBuffer，
                    # 否则该采集客户端的后续 GetBuffer 全部失败（L1）
                    _hr(capture_client.lpVtbl.contents.ReleaseBuffer(
                        ctypes.addressof(capture_client), UINT32(frames)), "ReleaseBuffer")
        except Exception as e:
            self._fail(f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            if started and client is not None:
                try:
                    client.lpVtbl.contents.Stop(ctypes.addressof(client))
                except Exception:
                    pass
            _release(capture_client)
            _release(client)
            _release(device)
            _release(enumerator)
            if fmt_ptr:
                try:
                    ole32.CoTaskMemFree(fmt_ptr)
                except Exception:
                    pass
            if com_inited:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

    def _fail(self, msg: str):
        self.error = f"[{self.label}] WASAPI 采集失败: {msg}"
        logger.error(self.error)
        try:
            append_err_record("core/audio_capture.py", "WASAPI 采集失败", self.error)
        except Exception:
            pass
