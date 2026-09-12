# 分支说明

- **来源**：本仓库于 2026-09-06 由 `all_projects/focus_tools_v2`（OISystem，信奥/OI 方向）整仓拷贝而来，原版保留不动。
- **方向差异**：本副本定位为**文化课学习**方向的桌面工具（学习 Agent 生态）。信奥专属能力（ZZOI 提交检测、做题退出、排行榜锁定等）在本副本中长期保留但不再演进；新功能优先落在本副本。
- **已继承的现成能力**（R53 轮实现，无需重做）：
  - TTS 朗读正文清洗（`core/tts_player.py` `strip_markdown_for_speech`：代码块/链接/粗斜体/表格/公式定界符剥离，已接在朗读链上）；
  - MiMo V2.5 讲题对话 + MiMo TTS 双音色体系；TTS 状态机（防重入/stop 失效/无 Key 跳过）。
- **下一步待设计**：讲解词与板书分离的双通道形态（AI 讲解词进 TTS 朗读、板书独立成黑板展示，对齐网页端 `学习Agent_new` `/focus` 黑板的 content + board.ops 架构）——桌面 UI 方案待与用户细化后启动。
- 原版仓库：`focus_tools_v2`（OISystem）；姊妹项目：`学习Agent_new`（网页端）。
