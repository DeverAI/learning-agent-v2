"""OISystem 讲题朗读播放器（小米 MiMo-V2.5-TTS）。

职责：
- 把 AI 讲题回复合成为语音并播放（QThread 异步，不阻塞 UI）
- 剥离 Markdown/代码块/graph 块，只朗读正文
- 防重复提交；可随时停止当前合成与播放

播放实现说明：
- 使用 Windows 内置 winsound.PlaySound(SND_MEMORY) 同步播放，放在工作线程中，
  避免 SND_MEMORY|SND_ASYNC 组合下缓冲区生命周期的已知陷阱；
  停止播放通过另一线程发送 PlaySound(None, SND_PURGE) 实现。
"""
import threading
import re

from PySide6.QtCore import QObject, Signal, QThread

from config.settings import ConfigManager
from utils.helpers import logger
from utils.exceptions import AICallError


def strip_markdown_for_speech(text: str) -> str:
    """把 Markdown 讲题文本清洗成适合朗读的纯文本。

    - 移除 ``` 代码块 / graph 块 / mermaid 块整体（代码不适合朗读）
    - 移除行内代码、图片、链接 URL（保留链接文字）
    - 移除标题符号、粗斜体标记、引用符、分隔线等装饰符号
    """
    if not isinstance(text, str):
        return ""
    t = text
    # 围栏代码块（含 graph/mermaid）整体移除，替换为一句提示
    t = re.sub(r"```[a-zA-Z]*\s*[\s\S]*?```", "，（代码块略），", t)
    # 图片 ![alt](url) → alt
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 链接 [text](url) → text
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # 行内代码
    t = re.sub(r"`([^`]*)`", r"\1", t)
    # 标题符号 / 引用符 / 分隔线 / 列表符号
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^>\s?", "", t)
    t = re.sub(r"(?m)^[-*+]\s+", "", t)
    t = re.sub(r"(?m)^\d+\.\s+", lambda m: m.group(0), t)  # 有序列表保留数字
    t = re.sub(r"(?m)^(-{3,}|={3,}|_{3,})\s*$", "", t)
    # 粗体/斜体/删除线标记
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"_([^_]+)_", r"\1", t)
    t = re.sub(r"~~([^~]+)~~", r"\1", t)
    # 表格分隔行 |---|---| 与多余竖线简化
    t = re.sub(r"(?m)^\s*\|?[\s:|-]+\|?\s*$", "", t)
    t = t.replace("|", "，")
    # 数学公式定界符（保留内容，MiMo TTS 对 $ 符号会读出来）
    t = t.replace("$", "").replace("\\(", "").replace("\\)", "")
    t = t.replace("\\[", "").replace("\\]", "")
    # 多余空白归一
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


class _TTSWorker(QObject):
    """TTS 合成工作线程：调用 ai_client.tts_speech。"""

    ready = Signal(bytes, int)   # (wav_bytes, request_id)
    failed = Signal(str, int)    # (reason, request_id)
    done = Signal()

    def __init__(self, speech_text: str, style: str, request_id: int = 0, parent=None):
        super().__init__(parent)
        self._speech_text = speech_text
        self._style = style
        self._request_id = request_id

    def run(self):
        try:
            from core.ai_client import tts_speech
            wav = tts_speech(self._speech_text, style_instruction=self._style)
            self.ready.emit(wav, self._request_id)
        except AICallError as e:
            self.failed.emit(str(e), self._request_id)
        except Exception as e:
            self.failed.emit(f"异常: {e}", self._request_id)
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class _PlayWorker(QObject):
    """同步播放工作线程：winsound.PlaySound(SND_MEMORY) 在线程内阻塞播放。"""

    finished = Signal(int)

    def __init__(self, wav_bytes: bytes, request_id: int = 0, parent=None):
        super().__init__(parent)
        self._wav = wav_bytes
        self._request_id = request_id

    def run(self):
        try:
            import winsound
            winsound.PlaySound(self._wav, winsound.SND_MEMORY)
        except Exception as e:
            logger.warning(f"TTS 播放失败: {e}")
        finally:
            try:
                self.finished.emit(self._request_id)
            except RuntimeError:
                pass


class TTSPlayer(QObject):
    """讲题朗读控制器（每个 DialogView 持有一个实例）。

    speak(text):
      1) 剥离 Markdown 得到纯正文（空正文直接跳过）
      2) QThread 中调用小米 TTS 合成 WAV
      3) 再起线程同步播放；期间再次调用 speak 会先停止当前任务
    stop(): 中止合成等待与音频播放。
    """

    stateChanged = Signal(str)   # idle / synthesizing / playing / error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._request_seq = 0
        self._active_request = -1
        self._tts_thread = None
        self._play_thread = None
        self._lock = threading.Lock()

    # ---------- 内部 ----------
    def _cleanup_thread(self, thread_attr):
        t = getattr(self, thread_attr, None)
        if t is not None:
            try:
                t.quit()
                t.wait(1500)
            except RuntimeError:
                pass
            setattr(self, thread_attr, None)

    def _stop_playback_only(self):
        try:
            import winsound
            # 在独立线程发 PURGE，避免阻塞主线程
            threading.Thread(
                target=lambda: _safe_purge(winsound), daemon=True
            ).start()
        except Exception:
            pass

    # ---------- 公共 ----------
    def stop(self):
        """停止当前合成等待与音频播放。"""
        with self._lock:
            self._active_request = -1  # 使迟到结果失效
        self._stop_playback_only()
        self.stateChanged.emit("idle")

    def is_busy(self) -> bool:
        return (self._tts_thread is not None and self._tts_thread.isRunning()) or \
               (self._play_thread is not None and self._play_thread.isRunning())

    def speak(self, markdown_text: str, style_instruction: str = ""):
        """合成并朗读一段 AI 回复（Markdown 自动剥壳）。"""
        speech = strip_markdown_for_speech(markdown_text)
        if not speech:
            logger.debug("TTS 跳过：清洗后无可朗读文本")
            return
        try:
            s = ConfigManager().settings
            enabled = getattr(s, "xiaomi_enabled", False)
            api_key = getattr(s, "xiaomi_api_key", "")
            if not enabled or not api_key:
                self.stateChanged.emit("error")
                logger.warning("TTS 跳过：小米 provider 未启用或未配置密钥")
                return
        except Exception as e:
            logger.warning(f"TTS 配置读取失败: {e}")
            return

        with self._lock:
            self._request_seq += 1
            req_id = self._request_seq
            prev_active = self._active_request
            self._active_request = req_id

        # 打断上一次合成/播放：先广播 idle 让旧气泡按钮复位，
        # 避免自动朗读打断手动朗读时旧按钮卡在"合成中/停止"
        if prev_active != -1:
            self.stateChanged.emit("idle")
        self._stop_playback_only()
        self._cleanup_thread("_tts_thread")
        self._cleanup_thread("_play_thread")

        self.stateChanged.emit("synthesizing")

        self._tts_thread = QThread(self)
        worker = _TTSWorker(speech[:2000], style_instruction, req_id)
        worker.moveToThread(self._tts_thread)
        self._tts_thread.started.connect(worker.run)
        worker.done.connect(self._tts_thread.quit)

        # 必须连绑定方法（r52 整训 + r56 实机复现）：闭包无 receiver 是 direct
        # connection，槽会在合成子线程执行，_on_synthesized 里 QThread(self)
        # 变成跨线程创建 children（QObject: Cannot create children...）。
        worker.ready.connect(self._on_synthesized)
        worker.failed.connect(self._on_tts_failed)
        self._tts_worker = worker  # 防 GC
        self._tts_thread.start()

    def _on_tts_failed(self, reason: str, rid: int):
        """合成失败（主线程槽）。"""
        if rid != self._active_request:
            return
        logger.warning(f"TTS 合成失败: {reason}")
        self.stateChanged.emit("error")

    def _on_synthesized(self, wav: bytes, rid: int):
        """合成完成（主线程槽）：起播放线程。"""
        if rid != self._active_request or not wav:
            return
        self.stateChanged.emit("playing")
        self._cleanup_thread("_play_thread")
        self._play_thread = QThread(self)
        player = _PlayWorker(wav, rid)
        player.moveToThread(self._play_thread)
        self._play_thread.started.connect(player.run)
        player.finished.connect(self._play_thread.quit)
        player.finished.connect(self._on_play_finished)   # 绑定方法，queued 回主线程
        self._play_worker = player  # 防 GC
        self._play_thread.start()

    def _on_play_finished(self, rid: int):
        """播放完成（主线程槽）。"""
        if rid == self._active_request:
            self._active_request = -1
            self.stateChanged.emit("idle")

    def shutdown(self):
        """窗口关闭时调用：停止一切并回收线程。"""
        self.stop()
        self._cleanup_thread("_tts_thread")
        self._cleanup_thread("_play_thread")


def _safe_purge(winsound_module):
    try:
        winsound_module.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass
