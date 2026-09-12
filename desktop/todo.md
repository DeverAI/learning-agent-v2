# todo.md — 本轮任务（round 58，2026-09-07：桌面讲课——拉取服务器题库开讲）

> 上一轮（round 57 成果互通）结果见 `updates/done_20260907_round57.md`。
> 设计依据：Design.md「桌面讲课：从学习 Agent 拉题讲解（round 58）」。

| # | 任务 | 预估实现 | 状态 |
|---|------|----------|:----:|
| 1 | 设置项 `sync_server_password` | settings.py 字段 + 归一化 | ⬜ |
| 2 | `core/lecture_engine.py` 讲解引擎 | 数据拉取（X-Auth-Token）+ v4-pro 讲解计划（fail-safe 降级）+ 逐步播放状态机 | ⬜ |
| 3 | `ui/lecture_view.py` 讲课面板 | 试卷→题目→生成讲解→步骤播放控制；独立 TTSPlayer/Overlay | ⬜ |
| 4 | sidebar 接线 | icons.py 按钮 + sidebar_v2 分支 | ⬜ |
| 5 | `test_r58_lecture.py` 离线回归 | mock 拉取/AI/TTS：计划降级/播放推进/幂等/视图往返 | ⬜ |
| 6 | 实机验证 | 真服务器拉卷 + 真 AI 讲解 + TTS 出声 | ⬜ |
| 7 | 子 Agent 审查 + 修复 + 归档 | 刁难视角 + 修复复核 + done/updates/dev_log/备份 | ⬜ |
