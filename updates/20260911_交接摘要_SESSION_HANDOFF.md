# 会话交接摘要（SESSION HANDOFF）— 2026-09-11

> 用途：本文件是**轮换（rotate）交接的预写版**。用户在 App UI 触发 rotate 后，运行时会让当前会话写一份同结构摘要；本文件让那份摘要更快更准。
> 本会话（`mvs_fbacd16f90344301959ccaca1571554a`）已极长，建议轮换后从本文件接手。

## Goal

对一个自建 AI 学习系统（`C:/Users/david/Documents/all_projects/学习Agent_new`：FastAPI+SQLite 后端 / Android WebView 壳 / 独立 PyQt 桌面端 `desktop/`）做**逐轮深度检修**：看代码 → 发现问题 → 验证 → 修复 → 部署 → 记录。用户明确授权：**确定的 BUG 直接修不要问、长任务自行推进、能修就搞**。

## Progress（本会话完成的批次）

| 批次 | 内容 |
|------|------|
| R4 | 三路代码审计 + 16 项修复（含 **pytest 全量崩溃**——6 个 `test_*.py` 里 2 个带模块级 `sys.exit` → 用 `conftest.collect_ignore` 修复；回归基线 **137 → 160 passed**） |
| R5 / R5b | 全项目文档线索穷尽挖掘（**49 条未做/待做**）+ 4 项补实现（打包下载 / `_find_matches` 前置过滤 / 上传分块读 / 编辑器触屏兜底）+ **文档漂移修正 3 处** |
| R6 / R6 / R7 | 三端**讲题 Agent** 审计（约 45 项假实现）+ 讲题链路 7 处修复 + **停止向长期记忆写入伪造值**（通过率恒 100%、时长=次数×2、语音特征编造） |
| R8 / R9 | 后端 8 + 前端 4 + 桌面端 7 + 文档 3 处修复 |
| **R10** | **黑板子系统审计 + 8 问判定**：三层断点致桌面端「视觉选位」从未运行；`/api/notes` 漏鉴权头致课堂笔记从未同步；另修好**我自己引入的 2 个 bug** |
| **R11** | **Agent 工具注册表落地 + 相关内容接入**：新增 `services/agent_core.py` + `services/agent_tools.py`；`routers/sessions.py` **1113 -> 360 行**；修 `edit_paper`/`delete_paper` **死分支**；修 `delete_paper` 确认闸建在 **LLM 输出**上的安全漏洞；新增 `AgentTask` 表；新增 4 个内容工具（含**可自证**的联网检索）；前端 4 类卡片 |
| **R12** | **下载入口刷新**：APK 重出为 **v1.4 / versionCode 5**（含 TTS 桥）；桌面端 zip 重出；下载页的体积/日期改为**读取磁盘真实值**（原先页面写 v1.3/09-10，磁盘上其实是 09-05 的旧包）。公开下载核验：APK 远程 SHA256 与本地构建**逐字节一致** |
| **R13** | **锁拆分** `services/session_locks.py`：`fg_lock` / `file_lock` / `bg_lock`；删除/改名不再等待在跑的回合（立刻 409）；`_save(guard_deleted=True)` 阻止"删了又回来" |
| **R14** | **后台推理端 + 备课**：`services/background_agent.py`（协作式取消 + **25 秒兜底硬杀** + 续跑 + 重启对账）、`services/lesson_service.py` + `Lesson` 表（可编辑课稿 + **切片读取契约**）、`prepare_lesson` 前台秒回、前端任务卡片（取消/接着做）。详见 `updates/20260911_R12R14_下载入口与双端推理落地.md` |

## Modified Files（本轮 R10）

| 文件 | 改动 | 状态 |
|------|------|------|
| `backend/services/focus_service.py` | 修正自查发现的 `NameError`（会把"截断"变成整条丢失）；`ops[:8]` 静默丢弃加日志 | **已部署** |
| `backend/static/js/focus.js` | 保存快照静默失败补 else toast；新增 `syncBoardNav()` 翻页边界置灰 | **已部署** |
| `desktop/core/placement.py` | 修三层断点（`from core import ai_client` + 同步 `vision_chat`）、清 `or True` 与 `rounds >= 0` 恒真条件 | 本机（不进服务器） |
| `desktop/core/screen_paint.py` | `_Qt.SmoothTransformation` → `Qt.SmoothTransformation`（`_Qt` 从未定义） | 本机 |
| `desktop/core/lecture_engine.py` | `stop()` 顺序：先 `_clear_board()` 再 `hide_overlay()`（原顺序被 `_repaint` 自恢复显示抵消） | 本机 |
| `desktop/core/classroom_sync.py` | POST `/api/notes` 补 `X-Auth-Token`（原漏带 → 恒定 401 → 笔记从未同步） | 本机 |

## Key Decisions

1. **运维**：**永不用 `nssm restart` / `nssm stop`**（STOP 卡在 `SERVICE_STOP_PENDING`，实测两次打停服务）→ 改为**强杀 app 进程 + nssm 自动拉起**（`AppExit` 为空 = 默认 Restart，实测通过）。
2. **脚本编码**：`.py/.js/.html/.md` 一律**无 BOM**；**`.ps1` 含中文必须带 UTF-8 BOM**（PS 5.1 会按 GBK 读无 BOM 脚本）。
3. **判定纪律**：子 Agent 结论**只当线索**，关键判词必须本人独立复核（本轮复核出 **2 处我自己的错**）。
4. **"不是 bug 就不动"**：`F13` 海马体端点无 UI（属待做功能，不删可用代码）、`F16` 防御性死分支（保留）、`open_page` 全屏白板（**用户实测后主动否决**，不擅自接回）。

## 用户本轮的三处重要纠正（覆盖我先前的判定）

| 主题 | 我原先的判定 | 用户纠正 |
|------|--------------|----------|
| **临时上课** | "不能——需要'课'的实体与生命周期" | **"临时上课本质就是 AI 讲题的加强版"**。既然都是 LLM，不必担心"能不能讲知识点"——**它是 `lecture`（讲题）链路的强化，不是新建一套课程体系**。→ 实现成本远低于我先前的判断 |
| **双端实时** | "不能——指桌面端↔Web端两个客户端" | **理解错了**。"双端"指**前模型 / 后模型**：**前模型负责实时响应（老师角色），后模型并发跑后续推理与准备工作**。→ 这正是 `Agent双端架构设计.md` 里的架构（前台交互 / 后台推理 + 前台可打断后台） |
| **相关内容接入** | "部分" | **"必须有，毋庸置疑"**。→ 从"待定"升级为**必做项** |

## Open Questions（待用户拍板，**不要用提问工具，列在这里即可**）

> **2026-09-11 R11 更新**：下面 12 问里，**1–4、6–8、11 已由用户回答并锁定**（见 `updates/20260911_新功能需求定稿.md`）；
> **11 已落地实现**（R11 的 4 个内容工具）；**9、10 方向已定**（前模型拿着后模型产出讲解，同时安排后续内容让后端推理；
> 用户不管理后端模型，但能让前端模型"不用思考某个方向"）；**12 已由用户解释**（Web 端因为不可能在边看视频时讲课，天然就是黑板白板
> —— 我的解读是 Web 端不必新增 `fill`/`mask`，桌面端 `paint_rect` 继续服务"浮在视频上"场景；**这条是我的解读，理解偏差请纠正**）。
> **唯一仍未回答的是第 5 问：「提前录好」的"录"指什么。**

关于 **① 备课** 与 **② 提前录好** 这两个新功能：

1. **备课的产物形态**：是一份可编辑的"课稿"（含板书 ops + 讲解词），还是只存"选题范围 + 要点"由 AI 现场发挥？
2. **备课的颗粒度**：一次备"一节课"（含多题/多段），还是备"一个知识点"？
3. **备课与讲题的关系**：备课产物是给 `lecture`（讲题）当输入，还是给 `focus`（专注模式）当输入，还是两者都能吃？
4. **备课时能否人工编辑**：是否需要"AI 生成草稿 → 人改 → 定稿"的三段式，还是全自动？
5. **提前录好的"录"指什么**：是**预生成完整音频**（省实时 TTS 的等待），还是**预生成讲解词**（播放时才合成），还是**录屏/录播**（含板书动画）？
6. **"提前"的时机与触发**：只在用户显式点"开始备课"时做，还是允许定时/空闲自动？（此前 R5 已确认**提前备课是手动触发**，此处需确认是否沿用）
7. **存储与生命周期**：录好的内容存哪（会话 JSON？独立课件库？服务器文件？），多久过期、能否复用/跨端共享？
8. **临时上课与备课是否共用同一产物**：即"临时上课"是否就是"用一份备课产物直接开讲"？
9. **双端实时架构的落点**：前模型/后模型这套是先在**桌面端**落地，还是先在后端 `/focus`+`/lecture` 落地？（涉及 `Agent双端架构设计.md` 里那套工具注册表与打断协议）
10. **双端实时的并发上限与打断语义**：后台并发上限仍按已确认的 **5**？前台自主判断打断（已确认）是否直接沿用？
11. **相关内容接入的范围**：要接哪些源——题库 / 笔记 / 知识点树 / 海马体记忆 / 联网检索？以及入口放在哪（板书里插入、还是对话框选、还是自动召回）？
12. **Web 端要不要也支持"填充黑/白板"**：桌面端已具备（`paint_rect` 任意色 + 覆盖窗置顶）；Web 端目前**无任何填充 op**，`clear` 只是清空条目。要加就得新增 `fill`/`mask` op + 前端渲染 + 提示词白名单。

## Next Steps

> **2026-09-11 R14 更新**：R11 完成工具注册表与内容接入，**R12 完成下载入口刷新**，
> **R13 完成拆锁**，**R14 完成后台推理端 + 备课 + 任务卡片**。
> 回归基线 **181 passed**，FreqErr **156 条**。
> **下一步从这个顺序接手**：**P4 打断判定（cancel/keep/observe） -> 课稿页面 -> P8 临时上课 -> P6b/P6c 归并**。
> 唯一仍需用户回答的是「提前录好」的"录"指什么（Open Questions 第 5 问）。

1. **用户按 App UI 的 rotate 按钮**完成上下文轮换（无工具命令可代劳），新会话从本文件接手。
2. 已完成的顺序：工具注册表（R11）-> 内容接入（R11）-> 下载入口（R12）-> 拆锁（R13）-> 后台推理端（R14）-> 备课（R14）。
3. 长期待办（需外部条件，无法在本机推进）：浏览器/真机冒烟（含 **Android WebView 是否提供 `webkitSpeechRecognition`** 这个决定性未知）、`desktop/` 重新出包（zip 已重出但**未在 Windows 实跑**）、APK 重出（**已于 R12 完成**）。
4. 已知未做的小项：桌面端 `set_click_through` / `open_page|close_page` 无生产接线（其中 `open_page` 被用户否决）、`ClassroomSync._lock` 定义后未 acquire、`test_r60b_board.py` 断言与 `ref_ok` 拆分后不一致（该脚本已被 `collect_ignore` 排除）。

## R12–R14 改动文件（下轮须知）

| 文件 | 改动 |
|------|------|
| **新增** `backend/services/session_locks.py` | 三把锁：`fg_lock` / `file_lock` / `bg_lock` + `fg_busy` + 弱引用回收 |
| **新增** `backend/services/background_agent.py` | 任务生命周期、`Checkpoint`、`TaskCancelled`、兜底硬杀、重启对账、并发上限 5 |
| **新增** `backend/services/lesson_service.py` | 课稿：`create_lesson` / `generate_lesson` / **`slice_lesson`（唯一读取契约）** / `update_section` / `list_lessons` |
| **新增** `backend/routers/agent_tasks.py` | `GET /api/agent-tasks`、`GET /{id}`、`POST /{id}/cancel`、`POST /{id}/resume` |
| **新增** `backend/routers/lessons.py` | 课稿 CRUD + `GET /{id}?section_index&offset&max_chars`（切片） + `PATCH /{id}/sections/{index}` |
| **新增** `backend/test_r12_locks.py` / `test_r13_background.py` | 11 + 10 条行为断言（锁语义 / 取消 / 兜底 / 续跑 / 重启对账） |
| `backend/services/agent_tools.py` | +4 个备课工具（总数 27） |
| `backend/models/models.py` | 新增 `Lesson` 表 |
| `backend/routers/sessions.py` | 接入拆锁；`_save(guard_deleted=True)`；`SessionDeletedError` -> 409 |
| `backend/main.py` | 注册两个路由；启动对账；`/download` 读取磁盘真实体积/日期 |
| `backend/templates/agent.html` | 后台任务卡片（进度/步骤/取消/接着做）+ 课稿列表卡片 |
| `backend/templates/download.html` | 体积/日期改为渲染真实值；去掉「待重打包」字样 |
| `android/app/build.gradle.kts` | versionCode 4 -> 5、versionName 1.3 -> 1.4 |
| `FreqErr.md` | 151 -> **156 条** |

## 关键环境信息（新会话必读）

- **服务器**：`ssh mindog`（8.138.12.209 / Administrator，密码经 `~/.ssh/_askpass.cmd`），服务 nssm `LearningAgent`（8000），代码在 `C:\all_projects\learningAgent\backend\`
- **部署脚本**：`backups/deploy_r5_20260911/_deploy_r{5,5b,6,7,8,9,10}.ps1`（本地编排）+ `_remote_deploy.ps1`（远端强杀+拉起+核验）+ `_remote_verify.ps1`（只核验）
- **探针**：`backups/deploy_r5_20260911/_probe_*.py`、`_patch_*.py`（全部可重跑）
- **回归基线**：`python -m pytest backend` → **160 passed**
- **FreqErr**：**144 条**（本轮新增 `[宽 except 吞掉 NameError]`、`[async 未 await + 跨端导错包]`、`[隐藏后立刻重绘会自恢复显示]`、`[跨端客户端漏带鉴权头]`）
- **备份**：`backups/audit_r4_20260911/`、`backups/audit_r6_20260911/`（369 文件 / 11.29 MB）
- **本轮记录**：`updates/20260911_黑板子系统审计与8问判定_R10.md`、`updates/20260911_讲题链路审计与修复_R6R7.md`、`updates/20260911_文档线索实现状态对照.md`、`updates/20260911_deep_audit_r4_工作记录.md`
