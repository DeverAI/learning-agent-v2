# -*- coding: utf-8 -*-
"""课堂感知端到端实机演示：TTS 放音 -> WASAPI loopback 抓取 -> VAD 切段 -> MiMo 转写。

运行：python demo_classroom.py
流程：
  1. 用 MiMo TTS 合成一段"老师讲课"音频（写入 %TEMP%\\la_probe，不落项目目录）；
  2. 启动 ClassroomMonitor（仅 loopback 通道，避免扬声器声音被 mic 二次录入）；
  3. 等 1s 让 VAD 校准噪声底，winsound 异步播放讲课音频；
  4. 收集 transcript_ready（纯 Python 回调，无需 Qt 事件循环）；
  5. 验证关键词命中，打印 PASS/FAIL，自动退出。
"""
import os
import sys
import time
import winsound

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from core import ai_client  # noqa: E402
from core.classroom_stream import ClassroomMonitor  # noqa: E402
from config.settings import ConfigManager  # noqa: E402

TEMP_DIR = os.path.join(os.environ.get("TEMP", "."), "la_probe")
WAV_PATH = os.path.join(TEMP_DIR, "classroom_demo.wav")

TEACHER_TEXT = (
    "同学们，今天我们讲勾股定理。"
    "直角三角形两条直角边的平方和，等于斜边的平方。"
    "注意，这个定理只适用于直角三角形。"
)
KEYWORDS = ["勾股定理", "直角", "平方", "斜边"]


def main():
    app = QCoreApplication(sys.argv)  # noqa: F841  信号系统需要 QCoreApplication
    os.makedirs(TEMP_DIR, exist_ok=True)

    # ---- 1. TTS 合成老师讲课音频 ----
    print("[1] MiMo TTS 合成讲课音频 ...")
    wav_bytes = ai_client.tts_speech(TEACHER_TEXT, style_instruction="像老师讲课一样，语速平稳")
    with open(WAV_PATH, "wb") as f:
        f.write(wav_bytes)
    print(f"    WAV 已写入 {WAV_PATH}（{len(wav_bytes)/1024:.0f} KB，仅演示用临时文件）")

    # ---- 2. 启动监听（仅 loopback） ----
    s = ConfigManager().settings
    s.classroom_capture_mic = False   # 内存内临时关闭，不保存设置文件
    collected = []
    mon = ClassroomMonitor(on_transcript=lambda item: collected.append(item))
    print("[2] 启动 ClassroomMonitor（loopback）...")
    res = mon.start()
    print(f"    start -> ok={res.get('ok')} started={res.get('started')}")
    if not res.get("ok"):
        print(f"[FAIL] 监听启动失败: {res}")
        return 1

    try:
        # ---- 3. 噪声底校准后播放 ----
        time.sleep(1.0)
        print("[3] winsound 异步播放讲课音频 ...")
        winsound.PlaySound(WAV_PATH, winsound.SND_ASYNC | winsound.SND_FILENAME)

        # ---- 4. 等待转写（播放时长 + 段尾静音 + ASR 往返，上限 45s） ----
        print("[4] 等待 VAD 切段 + MiMo 转写 ...")
        deadline = time.time() + 45.0
        stable_since = None
        while time.time() < deadline:
            app.processEvents()
            if collected:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since > 6.0:
                    break   # 6s 没有新转写，认为已收完
            time.sleep(0.2)

        # ---- 5. 验证 ----
        print("[5] 转写结果：")
        joined = ""
        for item in collected:
            print(f"    [{item.get('speaker')}] ({item.get('dur', 0):.1f}s) {item.get('text')}")
            joined += str(item.get("text", ""))
        hits = [k for k in KEYWORDS if k in joined]
        stats = mon.stats()
        print(f"    stats: segments={stats['counts']['segments']} "
              f"transcribed={stats['counts']['transcribed']} failed={stats['counts']['failed']}")
        print(f"    关键词命中 {len(hits)}/{len(KEYWORDS)}: {hits}")
        if len(hits) >= 3 and stats["counts"]["failed"] == 0:
            print("[PASS] 端到端闭环验证通过：放音 -> loopback -> VAD -> 转写 -> 文字流")
            return 0
        print("[FAIL] 关键词命中不足或存在转写失败")
        return 1
    finally:
        mon.stop()
        winsound.PlaySound(None, 0)
        print("demo finished, monitor stopped")


if __name__ == "__main__":
    sys.exit(main())
