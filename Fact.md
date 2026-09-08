# 用户偏好约束记录

## 强制约束
| 约束 | 说明 | 违反记录 |
|------|------|----------|
| **禁止使用 EMOJI** | 任何输出、按钮文字、提示信息中不得出现 emoji | 多次违反（🔒🔓⬆⬇↩✕⇌💾📂等），已全面清除 |

## 模型偏好
- **主力模型**：ZhipuAI GLM 系列（用户明确要求"全面转向GLM"）
- GLM-4-Flash 免费文本模型，GLM-4V-Flash 免费视觉模型
- Kimi 的 temperature 固定为 1（不可修改），曾因设置 temperature=0.7/0.3 导致多次 400 错误
- Kimi 稳定性不足，仅作备用或搜索功能
- **DeepSeek 模型名（2026-09-06 用户纠正）**：`deepseek-chat`/`deepseek-reasoner` 已于 2026-07-24 被官方下线禁用，项目内一律使用 `deepseek-v4-pro`（推理主力，"需要思考推理的直接用 V4 Pro 不含糊"）与 `deepseek-v4-flash`（仅作轻量任务兜底）。轻量调用需显式 `"thinking": {"type": "disabled"}`（v4 默认开思考）。
- **轻量任务分工（2026-09-06 用户定规）**：意图分类/标题/摘要等轻量任务优先走小米 MiMo V2.5（token plan 免费），统一入口 `ai_service.light_task_chat`（MiMo 未配置或失败时兜底 flash）。
- **图像识别分工**：一律走小米 MiMo V2.5 全模态（用户明确指示）；DeepSeek V4 无图像输入（官方另有 deepseek-v4-flash-vision-exp 实验模型，暂不引入）。
- **语音生成**：走 MiMo TTS（`mimo-v2.5-tts`）。
- **小米 MiMo（2026-08-25 用户批准接入）**：使用 DSH harness 凭据中的 `XIAOMI_TOKEN_PLAN_CN_API_KEY`（token plan CN 网关）接入：①专注模式 AI 配音（`mimo-v2.5-tts`）；②视觉备用链路与轻量文本任务（`mimo-v2.5`）。未选用 mimo-v2.5-pro 推理模型。
- DeepSeek Key 以 DSH harness 凭据文件为准，只写本项目 `backend/settings.json`；共享的 `Documents\api.txt` 不改动。

## 设计偏好
- 化学实验图纯平面，不引入 z 轴
- 锁定是装置组合级别的（一键锁全部或锁选中几个）
- 高亮是添加字符标注和阴影
- 按钮文字纯中文，不加图标装饰
- 桌面与移动端使用同一套自动适配界面；桌面仍保留完整功能和信息密度，不制作独立移动版
- 除黑白基础色和实验图米白画布外，页面、动态 HTML 与 SVG 的业务颜色必须跟随全局主题变量
- 人工维护文档只保留根目录一套（README + 设计/技术/约束文档）；同一信息不得同时堆在设计、技术、完成和开发日志中

## 本轮强制约束（2026-07-29）
- **禁止命令行文件操作**：本轮实现过程中不得使用 cmd/shell 的 Copy/Del/Force Copy 等命令操作项目文件；所有文件读写必须使用内置工具（Read/Write/Edit/DeleteFile）。
- **备份统一在最后，由用户手动执行**：6 项功能全部实现、文档统一更新、测试审查完成后，由 Agent 生成备份命令，用户手动执行；Agent 自己不执行命令、也不用 Write 工具重新生成备份文件。
- **文档先行**：动手前先更新 Design.md / Techniques.md，实现过程中及时补全文档。

## 本轮强制约束（2026-08-25 更新验收）
- **禁止命令行文件操作**：本轮实现过程中不得使用 cmd/shell 的 Copy/Del/Force Copy 等命令操作项目文件；所有文件读写必须使用内置工具（Read/Write/Edit 等）。
- **备份统一在最后，由用户手动执行**：全部实现、文档统一更新、测试审查完成后，由 Agent 生成备份命令，用户手动执行；Agent 自己不执行备份命令、也不用 Write 工具重新生成备份文件。
- **文档先行**：动手前先更新 Design.md / Techniques.md，实现过程中及时补全文档。
- **允许真实 API 冒烟**（2026-08-25 用户授权）："想用几次用几次……别玩ddos我都OK"——允许对 DeepSeek/小米发起合理次数的真实小请求验证 Key 与链路。
- **文档单套制收敛（2026-08-25 用户决策）**：删除对不存在文件的死引用，不重建 `dev_log/HISTORY.md`、`updates/INDEX.md`、`AGENT.txt`、`tools/`；`AGENT.txt` 本质是 Agent 系统指令而非项目文档。当前项目文档只保留根目录一份：`README.md`、`Design.md`、`Techniques.md`、`Fact.md`、`FUTURE.md`、`FreqErr.md`、`Err.log`、`done.md`（仅当轮结果）。旧工作流程中 todo.md/done.md 归档 updates 的环节按本决策取消。

## 冲突记录
- [2026-07-26] [冲突记录] 备份机制要求全量复制主功能文件 vs 用户“少输出”偏好及单次输出长度限制 → 处理方案：保留 `backups/semantic_rules_20260726.py`；当时的增量改动已归纳进当时的技术文档（其中提及的 `dev_log/HISTORY.md` 在本副本中从未存在，2026-08-25 起历史文档体系按用户决策取消），不再保留重复日期文档。
- [2026-07-29] [冲突记录] 项目常规流程要求"阶段性功能完成后立即备份" vs 用户要求"备份只在最后统一做、过程中禁止命令行 Copy/Del" → 处理方案：按用户要求，本轮仅在 Design.md/Techniques.md 统一更新、全部功能实现并审查测试通过后做一次完整备份；过程中严格使用内置工具，避免覆盖/误删。
- [2026-08-08] [冲突记录] 旧 `AGENT.txt` 要求把同轮内容同时复制到 `done.md`、`updates/` 和按日期 `dev_log/`，与用户要求合并冗余文档冲突 → 处理方案：用户当前指令优先；`done.md` 仅保留当前轮，长期历史只进 `dev_log/HISTORY.md`，`updates/INDEX.md` 仅作索引。
- [2026-08-25] [冲突记录] 工作流程要求维护 dev_log/版本更新文档+updates 归档 vs 本工作区这些目录根本不存在且用户明确"删掉这些，没用的" → 处理方案：按用户决策删除 README/Design/Techniques/Fact 中所有指向 `tools/`、`dev_log/`、`updates/`、`AGENT.txt` 的引用，不重建历史文档；当轮结果只写 `done.md`。
- [2026-08-25] [冲突记录] config.py 让共享 api.txt 覆盖项目 DeepSeek/Kimi Key vs 用户"API Key 直接找 DSH harness 配置、改本项目即可" → 处理方案：api.txt 降级为空值兜底来源，项目 settings.json 显式值优先；共享 api.txt 文件内容不动，不影响其他项目。
- [2026-09-06] [用户纪律·重申] 用户批评"你又又又忘了核验代码"——每轮（含桌面版等新产物）结束前必须执行子 Agent 故障检测（阶段 2 逐任务 + 阶段 3 全局），跳过的轮次用户会点名补课。处理方案：本轮已补做核验；后续所有轮次 todo 列表最后两项固定为"子 Agent 核验 + 检查点备份"。
- [2026-09-06] [冲突记录] 核验轮按通用工作流重建主仓 `updates/`、`dev_log/` 目录并归档 done.md 副本 vs Fact.md [2026-08-25] 用户决策"这些目录没用的，删掉，当轮结果只写 done.md" → 处理方案：立即停止该实践；误建目录与归档副本不自行删除（删除须用户执行），保留原地由用户处置；本轮结果只以根 done.md 为准。
