# Design.md - OISystem 设计文档

## 产品定位 v3：网课辅助 AI 教师（2026-09-06，文化课方向主定位）

本副本（学习搭子桌面版）的核心形态：**上网课时的随身 AI 教师**。与信奥原版（focus_tools_v2，防偷懒管控）不同，本副本的价值主张是"陪学 + 补位"：

### 三端生态中的位置
- 手机端（学习 Agent APK/PWA）：随时答疑、拍搜批——随身轻用。
- Web 端（学习 Agent）：完整服务与数据中枢（题库/组卷/笔记/知识库管理）。
- **桌面端（本副本）**：网课辅助 AI 教师——感知课堂 → 补位教学 → 成果回流。
- 互通：桌面端把 AI 记的笔记、板书、发现的错题同步到服务器（学习 Agent），进 Web 端知识库/题库。

### 新子系统 1：课堂感知（双通道）
- **音频通道**：系统音频捕获（WASAPI loopback）→ VAD 切分 → 语音转文字（首选 MiMo V2.5 全模态音频输入，待验证；备选本地 ASR）。产出滚动"课堂讲解文字流"。
- **视觉通道**：复用 OISystem 截屏基建 → 视觉模型（MiMo V2.5 / GLM）分析当前课件/讲义/PPT 内容。
- 两通道合并为"课堂上下文"，是打断决策的依据。

### 新子系统 2：打断决策引擎
- **分级开关**（用户权限，最高优先级）：
  - `off`：从不主动打断，仅回答学生主动提问；
  - `on_error`：仅当检测到老师讲错、讲漏关键步骤时打断；
  - `on_unclear`：发现讲解不清楚/不明不白/跳步即打断（最激进）。
- 全局静音热键（复用 OISystem 静音模式基建）：一键让 AI 闭嘴到再次按键。
- **冷却机制**：两次主动打断之间有最小间隔（可配置），防狂打断毁体验。
- 决策输入：课堂讲解文字流（近 N 段）+ 最新截屏分析 + 学生当前学习主题；决策输出 `{interrupt, reason, teach_point}` 由 AI 结构化给出，低置信度不打断。

### 新子系统 3：屏幕绘制原语引擎（替代"黑板"）
不做传统黑板控件。AI 获得一组**屏幕绘制指令原语**，黑板/标记/全屏板书全是原语组合，从根本上消解"黑板该长什么样"的设计问题：
- `paint_rect(x, y, w, h, color)`：用白色/黑色涂抹屏幕上任意矩形区域（如盖住网课老师写得潦草的板书区域，AI 在上面重写）；
- `write_text(x, y, text, size, color)`：在屏幕任意位置以任意大小、颜色打字；
- `open_page()` / `close_page()`：展开独立全屏白板页（深度讲解时切换），讲完关闭恢复网课画面；
- `clear()`：清除全部 AI 绘制。
- 实现形态：透明置顶覆盖窗（WA_TranslucentBackground + WindowStaysOnTopHint），可选鼠标穿透；全局热键一键隐藏/清除全部绘制（学生随时恢复原始屏幕）。
- AI 输出结构化绘制指令（JSON ops，风格与网页端黑板的 board.ops 一致），由执行器渲染；所有绘制内容可存档回传服务器。

### 新子系统 4：成果互通
- 服务器地址 + 访问密码配置（与 APK 同机制）。
- 上传内容：AI 记的课堂笔记（讲解文字流 + AI 总结）、板书存档、学生提问与 AI 解答中发现的错题。
- 走学习 Agent HTTP API（/api/notes 等），离线排队、联网重放（复用网页端离线队列设计思想）。

### 风险与验证项
- ~~MiMo V2.5 音频输入：token plan 网关是否接受 audio input 未验证，为最高风险项~~ → **2026-09-06 已验证通过**：`mimo-v2.5` 接受 OpenAI 风格 `input_audio`（`{"type":"input_audio","input_audio":{"data":<b64>,"format":"wav"}}`），TTS 合成的"一加一等于二"WAV 转写正确。本地 ASR（faster-whisper）备选方案**作废**，不引入重型依赖。
- ~~音频捕获依赖：WASAPI loopback 需新库（pyaudiowpatch 或 soundcard）~~ → **本轮推翻该预判**：改为**纯 ctypes 直调 WASAPI COM**（零新依赖）。理由：Fact.md"不引入新的重型第三方依赖"优先；pywin32/pynput 已在 requirements 内，捕获只需 IMMDeviceEnumerator + IAudioClient + IAudioCaptureClient 三个 COM 接口，ctypes vtable 直调可控且可当场实机验证。代价是 ~300 行 COM 互操作代码，用单元测试 + 实机放音验证兜住。
- 主动打断的用户体验风险：由分级开关 + 冷却 + 全局静音三重护栏控制。

## 课堂 AI 教师联动层与设置接线（2026-09-07 round 56）

round 55 交付了感知与决策引擎层（只发信号、不出声不绘制）。本轮补上"最后一公里"：把 `interrupt_triggered` 信号接到真实的教学动作（开口说话 + 屏幕板书），并让全部新设置项可从设置中心配置。

### 1. ClassroomCoach 联动编排器（core/classroom_coach.py）
- **输入**：订阅 `InterruptEngine.interrupt_triggered(dict)`——AI 主动打断（source=ai）与学生主动请求教学（source=student）走同一条联动链（学生点名请求同样值得"开口+板书"）。
- **讲解词生成**：打断信号只带 30 字内 `teach_point` 要点，直接朗读体验生硬。Coach 用对话主力模型（`resolve_dialog_target()` → v4-pro，遵循模型分工规则）基于 teach_point + reason + 课堂上下文快速生成 40~80 字讲解词（一次调用，max_tokens 收紧）；**fail-safe**：生成失败/空文本 → 直接朗读 teach_point 原文，绝不因讲解词失败放弃教学。
- **朗读**：复用 round 53 `TTSPlayer`（合成+播放线程、防重入、Markdown 剥壳全部现成），受 `interrupt_speak` 设置开关控制。
- **板书**：复用 round 54 `ScreenPaintOverlay`：`open_page` 全屏白板 + 标题（"AI 教师补讲：{teach_point}"）+ 讲解词正文 + `clear` 自动恢复。板书展示 `COACH_BOARD_SEC=30` 秒后自动清除（常量起步，后续按用户反馈再加设置项）。讲解词生成期间先上白板标题（先看见后听见），TTS 完成后正文补齐——两路独立、互不阻塞。
- **自说回环防护（关键设计）**：TTS 播放的声音会被 loopback 抓进 teacher 通道 → 转写进文字流 → AI 又对自己的话做决策（回环）。方案：Coach 朗读前调用 `ClassroomMonitor.suppress_teacher(seconds)`，窗口内 teacher 通道 PCM 在 VAD 入口直接丢弃（估算时长 = 字数/4 + 2s 余量）；窗口结束 VAD 状态重置。真实老师声音在窗口内被丢弃是可接受损失（AI 打断时老师通常停顿，且 round 55 已有"超长段截尾"同类取舍）。

### 2. 启动接线（main.py）
- 启动时若 `ENABLE_CLASSROOM_AUDIO`（源码开关）且 `classroom_audio_enabled`（设置开关）都开 → 创建 ClassroomMonitor + InterruptEngine + ScreenPaintOverlay + TTSPlayer + ClassroomCoach 并 `monitor.start()`；toast 提示"课堂 AI 教师已开启"。
- 任一环节初始化失败（设备被占/无麦克风等）**不阻断应用启动**：记 Err + toast 警告，降级为无课堂感知。
- `app.aboutToQuit` 统一清理：coach → engine → monitor.stop()（沿用 round 55 的 stop 时序）。
- **生效规则**：`classroom_audio_enabled` / 通道开关变更需重启应用生效（提示文案写明）；`interrupt_level/cooldown/confidence/speak` 等参数引擎每轮实时读设置，改完即生效。

### 3. 设置中心（ui/settings_view.py 新增 ClassroomTab「课堂感知」）
- 核心控件（6 个，轻量化不做全量铺开）：课堂音频总开关、采集系统音频（老师）、采集麦克风（学生）、打断分级（off/on_error/on_unclear 三选）、冷却秒数、置信度下限、朗读开关。
- 低频微调项（VAD 阈值/窗口大小/段上限等）留在 config.json 手改，不进 UI。
- 课堂感知开关旁标注"改后需重启应用生效"；其余参数"保存即生效"。

### 4. 本轮不做（记 Future 候选）
成果互通（学习 Agent API 上传）、课堂视觉通道、看门狗自动重建通道、板书时长设置项、完整讲解词流式生成。

## 成果互通：课堂笔记同步到学习 Agent（2026-09-07 round 57）

三端生态的最后一环：桌面端上课产生的**课堂文字流 + AI 补讲记录**同步到学习 Agent 服务器（http://8.138.12.209:8000），手机端（APK/PWA）的「笔记」页直接可见——手机端无需任何改动即吃到桌面端能力。

### 1. 同步内容（core/classroom_sync.py）
- **数据源**：monitor 的转写滚动窗（recent）+ coach 的补讲日志（teach_point/讲解词/ts）。
- **形态**：组装成一篇 Markdown 笔记 `POST /api/notes`：
  - 标题 `课堂笔记 M月D日 HH:MM`；subject=近期转写判定的学科（判不了留空）；
  - 正文：课堂时间线（`[老师]/[学生]` 逐条）+ `> 🤖 AI 补讲`块（要点+讲解词）；
  - knowledge_tags：从本次课堂 reason_type 提炼（error→概念纠错 等），可空。
- **增量水位**：`data/classroom/sync_state.json` 记 `last_synced_ts`；每次只上传水位之后的条目，上传成功才推进——服务端无去重，客户端保证幂等。
- **触发**：定时（`classroom_sync_interval_min`，默认 30min，QTimer 主线程触发、上传在工作线程）+ 手动触发接口。monitor 未运行时跳过。
- **失败处理**：网络失败只记 Err 不推进水位（下次自然重试）；转写 jsonl 本就按天落盘，断电也不丢数据。

### 2. 隐私与边界
- 上传的是**转写文字与补讲记录**（round 55 红线内允许的内容），原始音频绝不离开本机。
- 同步开关默认**关**（`classroom_sync_enabled=False`）：涉及把数据发到服务器，由用户显式开启。
- 服务器地址可配置（`sync_server_url`），指向用户自己的学习 Agent 实例。

### 3. 手机端（不改代码）
- APK/PWA 的笔记页渲染服务器笔记，桌面同步上去的课堂笔记自动出现；后续手机端可选做"课堂笔记"专属筛选（按 knowledge_tags），本轮不做。

## 课堂感知音频通道与打断决策引擎（2026-09-06 round 55）

上一轮已完成"屏幕绘制原语引擎"原型（`core/screen_paint.py` + `demo_paint.py`，未编号）。本轮落地新子系统 1 的音频通道与新子系统 2 的决策内核，形态为**可独立运行的引擎层 + 控制台验证 demo**；UI 面板、TTS/绘制联动、成果互通留后续轮次。

### 1. 音频捕获（core/audio_capture.py，零新依赖）
- **双通道双实例**：`source="loopback"`（系统音频=网课老师声音）与 `source="mic"`（学生提问）。同一 `AudioCapture` 类，仅设备端点与 STREAMFLAGS 不同（loopback 加 `AUDCLNT_STREAMFLAGS_LOOPBACK`，取 eRender 默认端点）。
- 采集线程按 50ms 粒度 `GetCurrentPadding → GetBuffer → ReleaseBuffer`，即时转 **mono float32** 并重采样到 **16kHz**（numpy 线性插值），压入**有界环形缓冲**（默认 30s，超限丢最旧）。
- loopback 在无播放时不产生数据包（WASAPI render 流空闲行为）。**实现后修订（实机验证推翻原预判）**：若完全不处理，空闲前最后一个语音段会永远卡在"开启态"，内容滞留不转写。最终方案：连续空闲 ≥250ms 才按实时速率向 VAD 路径补偿注入合成静音（推动段尾收段逻辑）；瞬时 padding==0 不注入，避免静音帧穿插真实语音拉长时间轴（实测无门槛时段长虚增 ~70%）。注入只走 VAD，不进环形缓冲——`captured_seconds` 只记真实设备数据。
- 设备不可用/COM 失败**不抛到 UI**：置 `error` 状态 + 记 Err.log，调用方按"该通道不可用"降级（只走另一通道或纯视觉通道）。

### 2. VAD（core/vad.py，能量法，零依赖）
- 不用 silero/webrtcvad：前者要下模型权重（网络依赖），后者要新装 C 扩展。numpy 已在，能量法足够切分课堂语音段。
- 帧级 RMS + **自适应噪声底**（静音帧滑动均值，语音阈值取 `max(绝对下限, 噪声底×倍数)`），20ms 帧；语音段需 ≥250ms 才成立（防咳嗽/敲击误触发），段尾静音 ≥600ms 收段，单段硬上限 15s 强制切分（防老师一口气讲到底导致转写超长）。
- **前置缓冲 200ms**：语音起点前的音频补进段首，避免吞掉第一个字。

### 3. 语音转写（core/ai_client.py 新增 transcribe_audio）
- 契约按已验证探针：`model=xiaomi_asr_model`（新设置项，默认 `mimo-v2.5`；**不复用 `xiaomi_model=mimo-v2.5-pro`**——验证时用的是非 pro 名，pro 是否接受音频输入未验证，故单列设置项，实测后可改）+ `input_audio` + `max_tokens` + `reasoning_effort="none"`（MiMo 思考会吃掉转写 token，本项目已实测的既有结论）。
- PCM → WAV 由 `audio_capture.float32_to_wav()` 生成（16k/mono/16bit），转写完立即丢弃原始 PCM。
- 转写失败（超时/空文本/非 JSON）：记 Err.log，该段标记失败但**不打断流**；连续失败 N 次自动暂停该通道 ASR（防课堂全程刷错误）。

### 4. 课堂文字流（core/classroom_stream.py）
- `ClassroomMonitor` 持有两路 capture + 两路 VAD + **单一转写工作线程**（`queue.Queue`，requests 同步调用不入 Qt 主线程），产出带说话人标签的转写条目 `{speaker: teacher|student, text, dur, ts}`。
- 滚动窗口（默认近 12 条）供决策取上下文；按天落盘 `data/classroom/YYYYMMDD.jsonl`（仅文字）。
- **隐私护栏（硬约束）**：原始 PCM 只在内存环形缓冲与 VAD 段内存在，**绝不落盘、绝不上传**；互通只传转写文字与 AI 总结。

### 5. 打断决策引擎（core/interrupt_engine.py）
- 决策输入：课堂文字流近 N 段 + 最新截屏分析结论（可选，无则跳过）+ 学生当前学习主题；输出结构化 `{interrupt, confidence, reason_type, reason, teach_point}`（JSON，低置信度不打断）。**模型分工修正**：判断"老师讲错/跳步/讲不清"属**推理任务**，走对话主力（`resolve_dialog_target()` → deepseek-v4-pro），不走 flash 轻任务通道——遵循用户全局规则"需要思考推理的一律 v4-pro，轻任务才走 MiMo"。
- **闸门顺序**（任一不过即不打断，顺序固定且可单测）：全局静音 → `interrupt_level=off` → 冷却未到 → 置信度低于阈值 → 级别过滤（`on_error` 只接受 `reason_type=error/omission`，`on_unclear` 接受全部）。
- 触发时只发信号 `interrupt_triggered(dict)`，**不直接朗读/绘制**——出声与屏幕绘制由上层（后续轮次）订阅信号联动，保持引擎无 UI 依赖、可测试。
- 学生主动要求教学（`request_teaching(point)`）**绕过决策闸门**（仅受静音与冷却约束）：用户明确点名的教学请求不该被"AI 觉得没必要"否决。
- 新增设置项：`interrupt_level`（off/on_error/on_unclear，默认 `on_error`——保守起步）、`interrupt_cooldown_sec`（默认 180）、`interrupt_confidence_min`（默认 0.6）、`classroom_audio_enabled`、`classroom_asr_window`、`classroom_max_segment_sec`。

### 6. 模块开关
- `ENABLE_CLASSROOM_AUDIO`（源码级总开关，config.py 风格常量）：关闭时 monitor 不启动、demo 直接提示、UI 入口后续轮次一并隐藏。运行期细粒度开关走上述设置项。

### 7. round 55 实现后的设计补强（两轮子 Agent 故障检测产出）
- **通道看门狗**：设备拔出/默认设备切换会让 WASAPI 采集线程静默死亡且 UI 无从知晓。转写 worker 在队列空闲时低频巡检各通道存活，死通道经 `channel_error(speaker, message)` 信号通报（每通道只报一次），自动重建设计为后续轮次。
- **会话生命周期**：stop→start 反复开关是常规操作。每会话使用全新 `threading.Event`（旧 worker 绑旧事件，处理完当前段即死，杜绝"僵尸线程复活"）；新会话启动前排空上会话遗留队列（陈旧毒丸会让新 worker 一口即毙、转写全灭），被丢弃段计入 `dropped_stale` 保持计数闭合。
- **决策新鲜度闸门**：`should_consider` 新增第 6 判——最新一条老师转写距今超过 5 分钟（课间休息上限）即拒绝决策，防止通道死亡/课程结束后拿陈旧文字流反复调 AI、冷却一过就对旧内容误触发打断。新鲜度无法确认时 fail-open（不阻断）。
- **fail-safe 红线强化**：AI 决策的 `interrupt` 字段显式归一（字符串 "false"/"no" 等一律判否），堵死 `bool("false")=True` 的隐性违例。
- **ASR 语义分级**：空 content（静音段，正常）返回空串；响应结构异常（系统性故障）抛错计入 failed，触发连续失败暂停保护——两者不得折叠，否则坏响应让转写流无声变空。
- **设置项范围收紧**：`classroom_vad_abs_floor_db` 上界 0→-10（0 会让 VAD 阈值恒满幅、一切语音切不出段）；`classroom_max_segment_sec` 上界 120→30（与转写截尾上限对齐，避免合法配置静默丢前段内容）。


## 核心设计决策

### 1. AI对话系统
- **Flash模型**: 用于快速判断用户意图（闲聊/解题）的轻量级模型
- **对话主力模型**: 支持任意 OpenAI 兼容格式的模型和自定义 API URL
- **配置要求**: 默认限制应足够宽松，支持用户自定义各种模型参数

### 2. 专注模式
- **卡住触发阈值**: 明确为"同一题目连续失败次数"
- **退出方式**: 急事退出 / 正常退出（直接结束专注）/ 关机退出。已移除"做题退出"（答题）机制，正常退出不再要求解题

### 3. UI/UX 设计
- **主题系统**: 所有 UI 组件必须正确应用主题颜色
- **窗口管理**: 设置窗口关闭时需先关闭所有旧窗口
- **布局适配**: 各页面输入框应有合理的最小尺寸，避免挤压

### 4. 网络与错误处理
- **网络错误**: ZZOI 题库访问应有合理的重试和降级机制
- **错误记录**: 所有运行时错误必须被 Err.md 记录并修复

### 5. API 角色路由（role 路由）
- **角色字段**: `kimi_role`（默认 knowledge）/ `glm_role`（默认 vision）/ `deepseek_role`（默认 dialog）决定各 provider 服务哪种任务
- **解析规则**: 视觉任务 → "vision" 角色 provider；主对话 → "dialog" 角色；flash/快速判定（闲聊判定/领域判定/摘要/上下文评分/元监督）→ "knowledge" 角色
- **回退**: knowledge/dialog 的默认 provider 未启用或无 key 时回退 deepseek；非 deepseek provider 时模型名改用该 provider 自身模型，避免发错模型名

## 本轮优化目标（2026-07-29 round 24）
1. **UI 全面优化**：以设置中心为重点，修正 Tab 内布局挤压、输入框不拉伸、长表单无滚动、APITab 内容溢出等布局问题，确保 720p~4K 屏幕均可正常使用。
2. **专注模式时间健壮性**：倒计时改用单调时钟（monotonic）计算，消除系统时间被修改/休眠恢复后导致的剩余时间跳变或无法自然结束等异常。
3. **自动截屏发送逻辑修复**：
   - 截图分析按钮在分析期间正确进入禁用态，避免重复触发；
   - 截图分析结果自动作为上下文进入 AI 对话；
   - 专注模式下检测到卡住时，自动将当前屏幕分析结果作为上下文发送给 AI，而非仅发送纯文本。

## 本轮新增：学习文化课模式（2026-07-29 round 25）
- **核心思路**：在 `focus_mode` 字段上做开关（oi / study），专注模式的倒计时、退出、信号、AI 对话等基础设施不变，仅切换 prompt、退出策略、网站白名单三处。
- **模式选择器**：`FocusView` 顶部新增 `QComboBox`（信息学奥赛 / 学习文化课），专注进行中禁用切换（避免运行中状态混乱）。
- **Prompt 切换**：
  - `core/ai_dialog.py` 新增 `SYSTEM_PROMPT_STUDY`（通用学习引导：禁代码输出、引导思路、走神识别、网课视频识别、错误指出到知识点/概念/公式）；
  - `_DialogWorker` 接收 `mode` 参数，`AIDialog.send()` 创建 worker 时传入 `mode=self.cfg.settings.focus_mode`；
  - `core/ai_supervisor.py` 新增 `SUPERVISOR_PROMPT_STUDY` 与 `_build_supervisor_prompt(mode)`，元监督规则按模式分支。
- **退出策略**：
  - OI 模式：保持原"做题退出"逻辑；
  - 学习模式：默认直接放行（语义同自然完成），可选开启 `study_require_problem` 强制做题；
  - `FocusView._normal_exit_clicked` 中按 `_mode` / `_require` 分支。
  - **（注：本段的"做题退出"与 `study_require_problem` 已在 r45 全量检修中彻底移除，正常退出改为直接结束专注，此段仅作历史记录保留）**
- **网站白名单**：
  - `AppSettings` 新增 `study_site_whitelist`（默认：腾讯课堂 / 慕课 / 学堂在线 / 学习强国 / ClassIn / B 站学习区 / Coursera / edX / Khan Academy / Google Docs / WPS 等），与 OI 白名单独立；
  - `SiteTab` 新增独立 UI 分组（添加 / 删除 / 持久化）；
  - 网站管控实际接入留待后续 round（当前字段已可读可写，site_guard 模块尚未引入）。
- **ZZOI Tab 占位**：重命名为 "ZZOI/任务源"，顶部加占位说明（"暂保留 OI 字段，学习模式暂不生效，后续可扩展为通用学习任务来源"）。
- **文档合并**：外层 `focus_tools_v2/` 下的 Design.md / Techniques.md / done.md / Err.md / FreqErr.md / todo.md 已覆盖为占位说明，原始内容归档到 `OISystem/dev_history/*_legacy_20260720.md`，后续仅维护 `OISystem/` 内层文档。

## 本轮新增：Markdown 展示器与公式渲染（2026-07-29 round 26）
- **核心思路**：在已有轻量 Markdown 渲染器 `ui/md_renderer.py` 中追加公式解析，不引入 KaTeX/MathJax 等重型依赖；利用 QTextBrowser 的 HTML 能力直接显示 Unicode 公式。
- **公式语法**：支持行内公式 `$...$` 与块级公式 `$$...$$`，在代码块、行内代码内保护原样不转换。
- **LaTeX → Unicode**：维护常见数学符号映射表（希腊字母、运算符、几何符号、关系符、箭头、集合逻辑等），将命令替换为可直接渲染的 Unicode 字符；未映射命令保留原样，避免信息丢失。
- **降级保护**：复杂或未识别公式保留原始 LaTeX 源码，用户可点击"源码"查看；`_latex_to_unicode` 对输入做截断与异常捕获，防止异常输入导致渲染崩溃。
- **样式集成**：`.math` 与 `.math-block` CSS 类跟随主题，在 `DialogView` 的深色/浅色主题下均保持可读。

## 本轮新增：AI 自动画图（2026-07-29 round 28）
- **核心思路**：让 OI 模式 AI 在解释具体图结构（树/图/DAG/网络流/状态机）时，输出 `graph` 代码块，系统自动渲染为 SVG 并嵌入消息气泡。
- **格式（两套）**：
  - YAML 风格（推荐）：`directed / nodes: - id, label / edges: - from, to, weight`
  - 简化风格：每行 `A - B (5)` / `A -> B` / `A --> B` / `A → B` / `A — B` / `A – B`
- **渲染**：`ui/graph_renderer.py`（无第三方依赖）→ 环形布局 → SVG 字符串 → `ui/md_renderer.py` 嵌入 HTML → `DialogView.MessageBubble` 显示。
- **防护**：
  - 节点上限 30 / 边上限 100（硬限 + prompt 软限 12）
  - label 截断 12 字符
  - 主题字段 None 防御（`_color` 兜底）
  - marker id 唯一性（`garr{i}` / uuid）
  - 控制字符 / 换行 / 单引号 XML 转义
  - 失败静默回退到原文 `<pre class="graph-fallback">`，不显示错误条
- **主题同步**：`MessageBubble._has_graph` 标志位，主题切换时仅含图气泡重渲染。
- **Prompt 教学**：`core/ai_dialog.py:SYSTEM_PROMPT` 第 9 条明确格式选择、节点数限制、禁止每条都画图。

## 本轮新增：图论编辑器 uvw 格式输入（2026-07-30 round 32）
- **核心思路**：用户反馈 CS Academy Graph Editor 风格是"左侧输入框 + 实时预览"，本轮把这种"u v w"行式格式作为 graph 代码块的第三种格式加入解析与编辑器 UI。
- **uvw 格式定义**（csacademy 风格）：
  - `1 token`：仅创建节点 `u`
  - `2 tokens (u v)`：创建无向边 `u-v`，`v` 不存在则自动建节点
  - `3 tokens (u v w)`：创建无向边 `u-v` 权重 `w`
  - 第 1 行支持 `directed: true/false` 独立声明
  - 注释行（`#` 开头）与空行跳过
- **优先级**：`parse_graph_block` 内先走 `_looks_like_uvw()` 启发式判断，命中则直接调用 `parse_uvw_block`；否则原 YAML → 简化分支保持不变。
- **编辑器 UI**：
  - 顶部新增"UVW 输入"折叠面板（左侧），多行 `QPlainTextEdit`；
  - 200ms debounce 实时调用 `parse_uvw_block` 解析 → 反馈到 `GraphScene` 自动布局；
  - 预览失败显示原 parse error；成功后状态栏显示"已加载 N 节点 M 边"。
- **降级**：UVW 解析失败（既无节点也无边）回退到原 parse_graph_block YAML/简化分支，保证旧文本不破。
- **prompt 教学**：`SYSTEM_PROMPT` 提示 AI 优先使用 uvw 格式（最简、最稳）。

## 本轮新增：AI对话卡死 + 文化课跳转 + 画图前端修复（2026-07-31 round 34）
- **AI 对话卡死**：继续加固 `DialogView` 虚拟滚动，避免消息增多后 UI 冻结（加载更多状态同步、滚动阈值保护、异常路径 UI 恢复）。
- **文化课问题误判**：`ai_dialog.py` 的闲聊检测增加"学习/文化课"关键词白名单（卖炭翁、古诗、文言文、课文等），即使 Flash 模型误判闲聊也要关键词兜底放行；同时 banner 提示用户切换到"学习文化课"模式。
- **画图前端**：加固 `md_renderer.py` graph 代码块渲染流程与 `graph_renderer.py` SVG 输出，确保 SVG 正确嵌入、主题字段缺失时安全降级、SVG 尺寸不会撑破气泡。

## 本轮新增：回归测试修复与健壮性迭代（2026-07-31 round 35）
- **核心思路**：响应"检查是否完善，继续更新迭代"，先跑全量回归测试发现两处测试/接口不一致，再修复并补强。
- **graph 代码块围栏兼容**：`parse_graph_block` / `parse_uvw_block` 统一去除外层 ```` ```graph ```` / ```` ``` ```` 围栏，保证 Markdown 原文和 UVW 输入框粘贴 fences 都能正确解析。
- **回归测试同步**：`test_r32_regression.py` 同步到当前 polygon 箭头实现；新增 `test_r35_regression.py` 覆盖 fences 兼容性与 polygon 箭头回归。
- **子AGENT查错**：对 graph_renderer / graph_editor / md_renderer / dialog_view / ai_dialog 做健壮性审查；审查发现的疑似问题经复核均已由前期 round 修复或不构成真实缺陷，本轮未引入新 P0/P1 问题。

## 当前状态：全量检修后的端口与健壮性基准（2026-08-20 round 50）
- **配置自愈**：`AppSettings.__post_init__` 对 bool/int/str/list/dict 字段统一归一化；int 字段按业务范围夹取；列表元素同样归一化（矩形转 int、收件人转 str、网站/收藏只留 str）；config.json/secrets.json 顶层非对象按空配置自愈。
- **网络任务线程化**：主对话、截图分析/关联页面、对话摘要/存档、ZZOI 手动检查/登录、30 分钟自动 ZZOI 检查全部在线程执行；自动 ZZOI 结果经主线程 QObject 桥应用锁定；所有线程引用带代次校验，旧线程 finished 不会清空新线程引用。
- **请求代次**：截图分析与关联页面结果携带 request_id，DialogView 只接受自己发起的代次；窗口关闭重开后，旧 worker 迟到结果不会泄漏进新窗口。
- **状态机**：ZZOI 锁定期间 FocusView 正常/急事/关机退出按钮全部隐藏，`request_emergency_shutdown` 在锁定下被拦截；侧边栏退出按钮捕获 FocusLockedError 后不再 quit 绕过锁定；ZZOI 抓取网络失败 `fetch_ok=False`，不触发零提交误锁。
- **虚拟滚动**：`_render_start`（更早未渲染数）与 `_user_scrolled_up`（用户是否离开底部）解耦；用户停在底部时新消息持续渲染，上滚时只追加数据并显示"跳到底部"。
- **死配置/死端口清零**：`selected_svgs` 按语义映射到侧边栏按钮；`note_ms_base_url` 真实消费；watchdog/note.ms/风险阈值/冷却分钟等均有设置中心入口；ContextManager 裁剪/引用/拉黑信号与 ScreenAnalyzer 失败信号全部接入 UI。
- **图输入硬限**：graph 渲染与图论编辑器 UVW/邻接表导入统一 100KB + 20 节点 / 100 边上限，超限快速拒绝且保留原画布。
- **自定义主题**：`ThemeManager.current_theme` 统一补齐全部标准主题键，最小合法主题（仅 4 个必填键）也不会 KeyError。
- **嵌套 JSON 解析**：`screen_analyzer.py:_parse_ai_json` 使用花括号深度计数的 `_extract_json_object` 替代贪婪/非贪婪正则，正确处理嵌套 JSON 对象（如 AI 返回含嵌套 detail 的屏幕分析结果）。
- **主题数据安全**：`ThemeTab.collect()` 仅在自定义主题时携带 `theme_custom_data`，避免内置主题保存时擦除已有自定义主题数据。
- **SVG 选择器生命周期**：`_open_svg_picker` 在 shiboken6 不可用时使用 `try/except RuntimeError` 兜底检查 C++ 对象存活性，防止悬挂引用导致崩溃。
- **画像时间戳保护**：`ProfileTab.collect()` 仅在文本实际变更时更新 `user_profile_updated_at`，保留"AI 最后更新时间"语义。

## 后续待办（Future）
- **自由画图**：在图编辑器基础上扩展手绘板/自由画布。
- **AI 教学图比例优化**：统计用户图渲染成功率，调整 prompt 引导 AI 写更稳定的简化格式。
- **AI 检测器 mode 分支**：`ai_dialog` 中的 `_is_chat` / `_has_code_output` / `_is_new_algo_idea` 在学习模式下需要更细致判断（如"输出答案"≠"输出代码"）。
- **全局热键唤醒专注模式**：当前仅托盘双击展开侧边栏，尚无全局热键直接启动/唤醒专注。
- **通用学习任务源**：ZZOI Tab 在学习文化课模式下暂不消费 OI 字段，后续可扩展为 MOOC/ClassIn 等通用学习任务来源。
- **托盘消息通知**：`TrayController.show_message` 暂无调用方，可接入 ZZOI 锁定/截图失败等系统通知。
- **代码卫生**：清理散落的 `*_backup.py` 与少量未使用 import（不影响运行）。

## 用户偏好约束
- 支持任意 OpenAI 兼容 API
- UI 布局自适应不同屏幕尺寸
- 不引入新的重型第三方依赖

## 做题退出 v2（2026-08-24 round 52 新增需求）
- **背景**：用户反馈旧"做题退出"由 AI 自己出题，题目诡异且无法提交；要求改为**真实可提交的题目**（最近开放的比赛 / 作业 / 题库），系统挑一道让用户做掉，AC 之后才允许正常退出。
- **核心设计**：
  1. **题目源只来自 ZZOI（Hydro OJ）真实数据**：降级链为「进行中比赛 → 作业 → 题库首页 → 已结束/时间未知比赛（最后手段，UI 带"可能无法提交"警示）」；任何来源失败自动降级，全部失败则面板提示并允许重试/换一题，**绝不使用 AI 生成题目**。
  2. **选题规则**：从题目池排除当日已 AC 的题后随机挑一道；全部做过时允许重复（不锁死用户）；记录分配时间与引擎权威副本。
  3. **退出放行条件**：该 pid 当日出现 AC 提交即放行（复用当日提交抓取，翻页聚合、日期归一比对）；记 `focus_problem_solved_exit` 日志。
  4. **状态机**：`request_normal_exit()` 在 OI 模式且 `problem_exit_enabled` 且 ZZOI 可用时进入 pending 态（专注倒计时继续走）；自然结束优先级高于做题退出；急事退出/关机退出是始终可用的逃生通道；ZZOI 锁定语义不变。study 模式保持直接结束。
  5. **ZZOI 锁定解除通路（历史缺口补齐）**：daily_check 检出当日有提交 → 记录器收集请求 → 主线程桥应用 `force_release_if_locked("zzoi")`（仅 zzoi 锁定态生效，普通专注不受影响）。
  6. **降级保护**：ZZOI 未配置或开关关闭时退化为直接结束（与 r45 后行为一致）；网络失败不锁死用户——面板提供重试/换一题。
- **新增配置**：`problem_exit_enabled: bool = True`（设置中心「专注模式」Tab 可改）。

## 全量检修（2026-08-20 round 52）
- **目标**：对整个项目进行全面检查，确保前后端配合良好，配置与代码一致，接口调用正确
- **检查范围**：
  1. 配置与代码配合：检查所有配置项是否被正确消费，是否有"有口没码"或"有码没口"
  2. 前后端接口：检查信号/槽连接是否正确，参数传递是否一致
  3. 线程安全：检查跨线程调用是否安全，是否有竞争条件
  4. 生命周期：检查Qt对象生命周期管理，信号断开是否完整
  5. 错误处理：检查异常路径是否完整，UI状态是否正确恢复
  6. 资源管理：检查QTimer、QThread等资源是否正确释放
- **验收标准**：最后一轮除非是0问题或者问题可忽略否则继续检查，防止引入新问题

## 小米 MiMo Token Plan 讲题接入 + TTS（2026-08-25 round 53）

### API 角色路由扩展
- provider 由三套扩为四套：kimi / glm / deepseek / **xiaomi**；新增 `xiaomi_role`
  字段参与 knowledge/vision/dialog 路由；vision 回退链 glm→kimi→xiaomi。
- 小米 Token Plan 专用 URL `https://token-plan-cn.xiaomimimo.com/v1` 写入默认配置；
  V2 系列模型已下线，全部使用 V2.5 模型名。

### MiMo 深度思考适配（实测结论驱动）
- MiMo 为深度思考模型：思维链会耗尽小 max_tokens 使 content 为空。逐一实测四种
  关闭思考的参数，只有 `reasoning_effort: "none"` 生效——chat()/vision_chat() 在
  max_tokens≤64（判定类任务）时自动附带。
- 统一 `_extract_reply_content`：content 为空一律抛 AICallError（上层有关键词兜底），
  空回复不再静默产生空气泡；响应体异常信息截断防日志膨胀。

### TTS 设计决策
- 复用 chat/completions 端点：待合成文本放 assistant 消息、user 消息承载风格指令
  （不会被朗读）；`audio.format="wav"` + 预置音色。
- 播放采用 winsound.SND_MEMORY 同步播放置于 QThread 内，规避 SND_ASYNC 下缓冲区
  生命周期陷阱；停止经独立线程 SND_PURGE + 请求代次失效双保险。
- 朗读文本先剥离 Markdown（代码块/链接/公式定界符/表格分隔行），只读正文。

### 安全基线（讲题 Agent & API）
- 密钥仅存 data/secrets.json（非 git 目录）；无密钥路径进日志、无 TLS 降级调用。
- 小米深度推理超时独立放宽至 180s；TTS 输入截断 ≤2000 字符控制额度消耗。

## 桌面讲课：从学习 Agent 拉题讲解（2026-09-07 round 58）

定位：三端生态闭环的最后一块——服务器（Web/手机录入的题目与试卷）→ 桌面端 AI 教师**主动开讲**。此前桌面端只有课堂感知（round 55-57），讲课能力依赖网课内容；本轮让桌面端可以脱离网课，直接对题库里的任何一道题开讲。

### 1. 数据通道（复用 round 57 的服务器连接配置）
- `GET {sync_server_url}/api/papers?limit=50`（试卷列表）、`GET /api/papers/{id}`（详情含 question_order）、`GET /api/questions/{id}`（题目详情：ocr_text/standard_answer/score_points_html/question_type）。
- 鉴权：学习 Agent password_guard 接受 `X-Auth-Token` 头 → 新设置项 `sync_server_password`（与 ClassroomTab 的服务器地址配套；留空则不发头）。
- 拉取在工作线程（requests 同步），不阻塞 UI。

### 2. 讲解引擎（core/lecture_engine.py）
- `LectureEngine(QObject)`：依赖注入（TTSPlayer/ScreenPaintOverlay 由 view 创建传入）。
- **讲解计划**：每题一次 v4-pro 调用（`resolve_dialog_target`），输入题面+标准答案+得分点，输出 JSON `{steps:[{title, speech}], summary}`，3~6 步教学式（题意分析→解题思路→逐步解法→答案与得分点→易错提醒）。fail-safe：AI 失败/解析失败 → 降级两步（读题面、读标准答案），绝不沉默。
- **播放**：plan_ready 后逐步自动播放——每步 = 卡片板书（ScreenPaintOverlay，复用 coach 卡片样式：标题=步骤名，正文=speech 摘要）+ TTS 朗读；TTS `stateChanged→idle` 驱动下一步（自动连播）；`pause/next/prev/stop` 手动控制。
- 信号：papers_ready / paper_ready / plan_ready / step_started(idx,title,speech) / lecture_finished / error。

### 3. UI（ui/lecture_view.py，侧边栏新按钮「讲课」）
- 仿 LogView 的独立窗口（RoundedFrameMixin）：试卷列表 → 点选加载题目列表（题号+题面摘要）→ 选中题目「生成本题讲解」→ 步骤列表 + 播放控制（上一题/下一步/暂停/停止）。
- 每步当前项高亮；生成期间按钮禁用+状态文本。
- 讲课用独立的 TTSPlayer 与 ScreenPaintOverlay 实例（与课堂感知的 coach 互不干扰），view 关闭时 shutdown。

### 4. 接线
- `icons.py` SIDEBAR_BUTTONS 增加 ("lecture","讲课",SVG_LECTURE)；`sidebar_v2._on_button` 增加 lecture 分支（_toggle_view 标准扩展）。
- `config/settings.py` 新增 `sync_server_password`。

### 5. 本轮不做
试卷整卷连讲（先单题，整卷连讲按用户反馈再加）、讲解计划缓存、题目图片拉取展示（文字讲解先行）。

## 黑板智能空间管理：放置代理 + 视觉自检回路（2026-09-07 round 59）

背景：卡片坐标语义 bug 修复后，多个卡片/区域并存时仍可能重叠。用户要求：
已存在内容的矩形框位置要持续跟踪并告知 AI；新内容与旧内容位置重合时反馈
AI 保持/擦除/删除部分内容，尽可能不重叠（除非故意）；**写什么由推理者
（v4-pro）决定并叮嘱大致区域；全模态模型（MiMo 视觉）选具体位置、看截图、
调整到相对满意，限制修改次数**。

### 1. 布局状态跟踪（core/screen_paint.py）
- `get_occupied_regions()`：从当前 ops 提取已占用包围盒（比例坐标）——rect 直接取；
  text 按估算（宽=len×size/屏宽、高=size×1.6/屏高）。
- `grab_b64(max_width=960)`：widget 自渲染 → JPEG base64（供视觉模型看图）。

### 2. 放置代理与回路（core/placement.py）
- `rects_overlap_ratio(a, b)`：IoU 纯函数（可单测）。
- `PlacementAgent.place(content, hint, occupied) -> {action, rect, reason}`：
  全模态调用（MiMo 多模态：截图 + 已占区域清单 + 新内容 + 九宫格叮嘱），
  输出 action=place(带 rect)/keep/erase(region)。
- **自检回路（限次）**：渲染 → grab → 重叠检测（与已占区域 IoU>0.15 即重叠）
  → 重叠则带着"哪里重叠"反馈给放置代理重试，**最多 2 轮**；超限保持现状
  并记 warning（诚实不假装）。
- 分工边界：v4-pro 只决定"写什么+叮嘱大概区域（九宫格 hint）"；位置微调、
  避让、看图调整全在放置代理（MiMo）。

### 3. 集成（lecture_engine）
- 讲解计划的 steps 每步带 `placement_hint`（v4-pro 输出，九宫格：top_left/
  top_center/.../bottom_right）。
- 每步渲染前：place() 决策 → keep 时沿用原位 / erase 时先清区域 / place 时
  用返回 rect 渲染 → 自检回路 ≤2 轮。

### 4. nssm（服务器恢复，本轮末尾）
- 学习 Agent 用 nssm 注册为 Windows 服务（SYSTEM 常驻+自动拉起），替代
  SSH/云助手会话启动（0.0.0.0 绑定在 SSH 会话静默死亡的规避）。
