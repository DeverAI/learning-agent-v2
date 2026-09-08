# 本轮已完成任务（2026-09-07，功能级检查轮：逐模块读码/验证/修复/记录）

> 触发：用户要求"按功能检查，不是纯冒烟——看代码、想到问题、验证、修复、记录，中间及时备份与文档更新"。
> 本轮方法：按功能链路逐个读核心代码 → 提出疑点 → 写探针/读调用链验证 → 修复 → 登记本文；大范围深查由两个子 Agent 并行（F2-F4 / F5-F9），其发现由主会话逐条取证后修复。
> 备份：`backups/func_check_20260907/`（10 文件，本轮全部改动对象的修复前副本）。上一轮 done.md 已归档 `updates/done_20260906_核验补课轮.md`。

## 1. 路由对账（前后端契约）

自建探针递归展开 FastAPI 0.141 的 `_IncludedRouter`（惰性复合路由，顶层只有 52 个对象）：后端 **212 条 API 路由** vs 前端 96 处引用 → **74 条精确匹配，0 条真实 404 断口**。其余 22 条"疑似 MISSING"均为 JS 拼接截断（`'/api/notes/' + id`）或 `_rollback/` 备份目录误报；96 条"后端零引用"均为带参详情/操作端点（拼接调用静态不可配对）。探针教训：改探针时误删 `sys.path.insert`，加载了错误 main——两个"灵异结果"折腾三轮，根因是探针自身。

## 2. 修复清单（10 项，全部探针或测试验证）

| # | 功能 | 问题 | 修复 | 验证 |
|---|------|------|------|------|
| 1 | F1 多图角色分类 | 视觉链 ZhipuAI→Kimi：违 MiMo-first 定规，Kimi 无视觉必 400 | 分类链改 MiMo→ZhipuAI，删 Kimi；`xiaomi_chat` 增加 `thinking_disabled` 参数 | 160 测试全绿 |
| 2 | F1 统一视觉入口 | `_call_vision_model_async` ZhipuAI 优先 MiMo 次之 | 调整为 MiMo→ZhipuAI→Kimi（Fact.md 2026-09-06 定规） | 测试更新为新契约断言 |
| 3 | F8 检查点幂等 | **探针坐实**：重复提交未被拒，新教学段被误挂 checkpoint_response，total_checkpoints 虚增污染掌握度统计 | 前后端：CheckpointRequest/submit 增加 `segment_index`（前端 currentSegment.index），已响应段重复提交 400 拒绝 | 探针：重复提交被拒、正常推进计数 1→2 |
| 4 | F4 整卷批改漏题记 0 分 | AI 分段漏题 → unanswered 强制 0 分计入且 failed_count=0 → `paper.user_score` 被低分覆盖 | 缺题标记 `unmapped`（score=None 不计分），complete 需 failed=0 且 unmapped=0 | 逻辑核验 |
| 5 | F4 批改失败残留 | 冻结题缺失/超容量/无法分割三处 raise 不清理临时 Question+两份图片，重试翻倍 | 三处 raise 前统一 `_cleanup_failed_query()` | 代码路径核验 |
| 6 | F3 组卷覆盖校验 | `_has_exact_question_coverage` 要求严格同序，但 prompt 只约束"保留一次"——AI 按题型重排必两次重试全挂 | 改集合相等+无重复（题序合法性由 data-question-id 支撑） | 语义核验 |
| 7 | F3 saved_config 合并 | 默认值永不为空（paper_type="custom" 等），配置三项被静默丢弃 | 合并判定改 `req.model_dump(exclude_unset=True)` | 语义核验 |
| 8 | F5-F9 kimi_ocr | 主链 GLM 优先 + Kimi 视觉兜底必 400 白烧 2 次 | MiMo 主力 → GLM 回退，删 Kimi 分支 | 代码核验 |
| 9 | F5-F9 reference SVG | 同上（GLM→Kimi） | GLM→MiMo，删 Kimi | 代码核验 |
| 10 | F5-F9 笔记 OCR/表情分析 | 笔记 OCR 无 MiMo（只配小米 key 必 400）；表情分析 GLM 主力+Kimi 兜底 | 笔记 OCR MiMo 优先；表情分析 MiMo→GLM 删 Kimi | 代码核验 |

## 3. 记档未修（下轮候选）

- **组卷 generate 同步挂起 20-30s 无落盘无找回**（已立案给方案：异步四件套，待用户拍板——改响应契约涉前端 4 模板，APK 不受影响已核实）
- **Agent 会话 save_image 死路**（ChatMsg 50KB 装不下图片 base64，需产品决策：前端直传 ocr/upload）
- M：focus 教学段 AI 失败吞成伪段落、会话消息失败不落盘、Agent 改笔记绕过图谱同步、拆题失败静默单题、banks 读改写竞态、worksheet modify 不保留题集、L1-L6 若干
- 文档裁决：Design 12.7"AI 自主编辑海马体"与 5.1 权限边界不冲突（编辑权=业务决策权，写入仍走服务层），待措辞调和

## 4. 验证（用户要求：手动运行时验证直到没有问题）

- **B 档（临时 SQLite + 真实 service 函数）11/11**：F4-H1 漏题 unmapped 不计分且 user_score 不被覆盖；F4-H3 失败清理计数不增；F3-H2 重排通过/重复拒绝/缺题拒绝；F3-H4 saved 的 worksheet 类型+模板生效、显式字段覆盖
- **A 档（子进程 uvicorn + HTTP，隔离库）8/8**：health/daily-quote 200；focus/start 真 AI 首段 200 且带 index；checkpoint 200 → 重复提交 **400 拒绝**；total_checkpoints 保持 1（F8-2 HTTP 层闭环）
- **C 档（真 AI 网络调用）2/2**：多图角色分类真 MiMo 返回合法 roles（question/extra 判定正确）；kimi_ocr 真调用日志 "OCR succeeded with Xiaomi mimo-v2.5"，逐字正确（3+5 / 12÷4）
- `pytest` **160 passed**（含更新后的视觉链契约断言）；compileall 通过
- F8-2 service 层探针：重复提交拒绝 ✓、正常推进计数 1→2 ✓
- 路由对账探针：212 条路由 0 断口

## 5. 备份与文档

- `backups/func_check_20260907/`：10 文件（全部改动对象修复前副本）
- FreqErr.md 新增：mock 与现实脱节、夹具时间戳、stream 泄漏三条（见该文件）
- 深查报告全文：两个子 Agent（F2-F4 / F5-F9）已完成并逐条取证

## 6. 部署（2026-09-07 05:00，本轮收尾）

- 服务器：12 个改动文件（6 services + 2 routers + 3 前端 JS + 1 模板）经 scp（mindog 通道）推送到 `C:\all_projects\learningAgent\backend\`，`_la_restart.ps1`（云助手 RunCommand）重启，**health HTTP 200**
- zhongkao-widget：修 build.py GBK 崩溃残留 + config 外置改造（frozen 时 ROOT_DIR=exe 目录、首启从包内释放 config）→ PyInstaller onedir 打包（153MB/zip 66MB）→ 上传 `C:\all_projects\zhongkao-widget\`：`countdown.exe`(7MB) + `_internal/` + **外置 `config/`（app/calendar/quotes/schedule 四 json，升级 exe 不丢配置）**；exe 本机烟测存活、config 释放验证通过
- 踩坑记录：服务器 cmd 默认 shell 中文路径/PowerShell 管道损坏 → 远程命令用 cmd 语法 + 英文文件名（countdown.exe）；zip UTF-8 文件名经 bsdtar 在 GBK 会话解出乱码 → 英文重命名修复
- 验证矩阵（手动运行时）：B 档 11/11、A 档 8/8、C 档 2/2、pytest 160/160

## 7. round 60：讲课 API 化 + 整卷连讲 + Agent 试卷读写（2026-09-08）

- **讲课 API**：`services/lecture_service.py` + `routers/lecture.py`——GET papers/questions 清单、POST plan/{qid}（单题）、POST plan-paper（**整卷连讲，跨题摘要压缩传递**：只带上题收尾句，长卷不超限）。v4-pro 生成，失败降级两步直读。
- **Web 讲课页**：`/lecture`（templates/lecture.html + static/js/lecture.js）——试卷/题目双入口选择、逐步步骤列表高亮、浏览器 SpeechSynthesis 朗读、整卷连讲。**安卓 WebView 直接打开即为手机讲课入口**。
- **Agent 试卷读写意图**：sessions.py 新增 `edit_paper`（title/subject/grade）与 `delete_paper`（pending_confirmation 确认闸；题目保留仅解绑）。
- **部署**：6 文件 scp → nssm restart → health/lecture/plan 端点全部线上 200（真 AI 5 步计划）。
- 期间修复：服务器 8000 被孤儿 python 进程占位（SSH 会话起的 0.0.0.0 绑定进程静默死亡所致）→ 清孤儿 + nssm 服务化（LearningAgent，SYSTEM 常驻+自动拉起+日志滚动）根治。
- 验证：test_r60 15/15；线上端点 200 ×4；真 AI 5 步计划（含九宫格 hint）。
