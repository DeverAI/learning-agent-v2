# Techniques.md - 技术方案与选型

## 实测适配（2026-08-25：zzoi.ac.cn 真实站点联调）

用户指出真实域名为 `zzoi.ac.cn`（Hydro OJ，阿里云），并提供了受限学生账号用于只读实测。
原实现基于猜测的 DOM（表格/uid 登录字段），真实站点全部对不上，逐项修正：

| 事项 | 原实现 | 实测真相 → 修正 |
|---|---|---|
| 配置域名 | zzoi.com.cn（公网不存在） | zzoi.ac.cn → config.json/site_whitelist 已改 |
| 登录字段 | POST uid/password | POST uname/password/tfa/authnChallenge/login_submit |
| 登录校验 | /user/{uid} 用配置值即可 | 用户页仅收数字 uid；用户名登录时回退"站点根是否重定向到 /login"判定 |
| 作业列表 | 表格行解析（0 行） | UiContextNew.docs(docType=30)：_id/title/pids/beginAt/endAt/assign |
| 比赛列表 | 表格行解析（0 行） | media 分块正则（contest__title 链接+supplementary 状态文本） |
| 比赛题目 | 页面链接解析（无链接） | UiContextNew.tdoc.pids；链接解析保留为通用兜底 |
| 提交记录 | /record?uid=（403） | /record?uidOrName={uid}；col--status 文本 + data-timestamp(epoch) |
| 题目列 | 解析 pid 列 | 学生角色显示 *（权限隐藏）→ pid 置空 |
| AC 检测 | 当日 AC pid 集合比对 | 双通道：①pid={展示ID} 服务端过滤（精确）②status=1 流中 ts≥分配时刻的增量 AC |

**做题退出 v2 目标制改造**：
- pick_problem 返回"目标"（作业《X》共N题 / 比赛 / 题库单题），不再假装能定位单题；
- FocusView 新增可选「展示ID」输入框：填写→按该题过滤检测；留空→增量新 AC 检测
  （自动轮询仅在填了展示ID时运行，避免在别处 AC 被误放行）；
- 引擎 assign_problem 接受无 pid 的目标字典（key/title/count/url）。
- 已知限制：学生号无法从站点数据建立内部id↔展示ID映射（题目页/记录详情均 403，
  作业题目列表经 WebSocket 动态加载），故展示ID需用户从作业页抄写，属一次性小成本。

**验证**：真实站点端到端 6/6（登录/目标池/pick/pid通道阴阳例/new_ac通道/daily_check 冒烟）；
test_r52_overhaul 64/64（夹具改为实测 DOM 结构 + 十六进制 rid + 防跨午夜时间戳）；
test_r48_overhaul 151/151。

## 本轮实现方案（2026-08-24 round 52：做题退出 v2 + 全量检修）

### 0. 基础设施恢复（工作区曾为不完整副本）
- 恢复 `config/settings.py`、`watchdog.py`、`data/`（config/secrets/daily/dialogs）、`.pytest_cache/backups` 内 r48 快照；字段级校验（全码消费点 vs AppSettings 定义）零缺失。
- 重建 `requirements.txt`（README 清单 + 补 pywin32，激活 site_guard/screen_analyzer 的 win32 路径）；重建占位版 `svg_mapping.json`（12 个语义图标，currentColor，可被原文件覆盖）。
- 从备份快照恢复 `test_r48_overhaul.py`(609 行) 与 `audit_static.py` 到项目根，对 live 代码跑通 151/151 基线。

### 1. 真实题目源（core/oj_tracker.py）
- `fetch_contest_problems(contest_id)`：GET `{base}/{domain}/contest/{id}`，通道1 解析 `/p/<pid>`、`/showProblem/<pid>` 链接，通道2 无链接时表格行兜底（题号须含数字且长度≥3，过滤序号垃圾）。
- `fetch_problem_pool()`：四级降级「进行中比赛(严格解析 end_time) → 作业 → 题库首页(补登录) → 已结束/时间未知比赛」，每级最多尝试 5 来源；已结束比赛附 `source_note` 警示。
- `fetch_today_submissions()`：时间列按日期正则扫描单元格（末列可能是语言列），分组捕获补零 ISO 归一比对；翻页最多 3 页空页即停；**all-or-nothing 成功语义**防部分失败误判零提交。
- `pick_problem(exclude_solved=True)`：排除当日 AC 后 random.choice；返回含 pid/title/url/source/source_note。

### 2. 引擎状态机（core/focus_engine.py）
- 新信号：`focus_problem_exit_started()/focus_problem_assigned(dict)/focus_problem_status(str)/focus_problem_solved(dict)`。
- `request_normal_exit()` 分支：study / 开关关闭 / ZZOI 未配置 → 直接结束（日志注明跳过原因）；否则置 pending 态发 started（网络抓取全部由 UI 线程完成，引擎不做 IO）。
- `assign_problem(problem|None)`：保存权威副本（含 source_note）并发信号；`confirm_problem_solved` 先 `_finish_session` 再 emit（模态弹窗不阻塞收尾）；`cancel_problem_exit` 回专注；自然完成/急事/force_release 统一走 `_finish_session` 并清 pending。
- `force_release_if_locked(prefix="zzoi")`：仅 zzoi 锁定态放行——接通"提交成功解除锁定"的历史缺口。

### 3. UI 面板（ui/focus_view.py）
- `_ProblemWorker(QObject)+QThread`：task="pick"/"check"；**信号连接绑定方法**（receiver=主线程 view，AutoConnection 排队），杜绝 lambda 直连跨线程 UB；thread/worker 存引用防 GC + 代次校验清引用。
- 双闸门丢弃迟到结果：`_problem_flow_closed`（closeEvent 置位，因 Qt 不撤销已入队投递）+ 引擎 pending 态检查（取消后不复活轮询）。
- 面板：来源+题名+警示、打开题目(QDesktopServices)、我已AC·检测、换一题、继续专注；忙碌时只提示不改按钮态；自动轮询 60s 且以面板可见性为准；closeEvent 断开新信号 + 在途线程 quit+wait(1500)；重开窗口经引擎权威副本恢复面板（已有题目不重复选题）。

### 4. 配置端口与文案
- `AppSettings.problem_exit_enabled: bool = True` + bool_fields 归一；FocusTab 复选框 + collect/_load 接通；`write_daily_markdown` 渲染 exit_started/solved_exit/cancelled 三种事件。

### 5. 子AGENT审查与终验
- 一轮：1×P1（分页误锁回归）+10×P2 全部修复；二轮：确认 8 项修复落地，新增 C-1(分页误锁)/C-2(解除通路历史缺口) 两 P1 与 5×P2，全部修复并补测试。
- 终验：test_r52_overhaul 72/72（×3 稳定）、test_r48_overhaul 151/151、audit_static 死配置[]/stub0/import0、运行时探针 9/9（_last_fetch_ok 四路径 + 解除链路 + 兼容旧 fake engine）。子AGENT服务故障期间由主 Agent 以探针代行终验。

## 技术架构

### 1. AI对话配置系统
- **模型配置**: 支持任意 OpenAI 兼容格式
  - 基础 URL 自定义
  - 模型名称自定义
  - API Key 配置
- **Flash模型**: 轻量级意图识别模型，用于快速判断用户输入性质

### 2. 专注模式算法
- **卡住检测**: 基于同一题目连续失败次数（非时间阈值）
- **题目推荐**: 从 ZZOI 题库中按难度和知识点智能匹配
  - 用户水平评估
  - 知识点薄弱点识别
  - 未做过题目优先

### 3. UI布局技术
- **尺寸约束**: 设置窗口最小480×400，最大900×1200
- **弹性布局**: 所有 Tab 使用 QSizePolicy.Expanding + addStretch(1)
- **输入框保护**: 设置最小尺寸避免挤压

### 4. 窗口管理
- **单例模式**: 确保同一时刻只有一个设置窗口
- **关闭逻辑**: 打开新窗口前先关闭所有旧窗口

### 5. 主题系统
- **全局应用**: 所有 UI 组件必须正确响应主题变化
- **侧边栏同步**: 主题颜色变化时侧边栏按钮实时更新

### 6. 错误处理
- **网络降级**: ZZOI 访问失败时提供本地备选题目
- **错误记录**: 所有异常记录到 Err.md 并标记修复状态

## 本轮实现方案（2026-07-29）
### 1. 专注模式单调时钟
- 在 `FocusEngine` 中引入 `QElapsedTimer`/`time.monotonic()` 记录已运行秒数；
- `remaining_seconds()` 以 `max(0, total_sec - elapsed)` 计算，避免依赖 `datetime.now()`；
- `_tick()` 仍每秒触发用于 UI 刷新，但结束判定以单调时钟为准；
- ZZOI 锁定仍使用 `timedelta(days=365)` 作为名义结束时间，但内部通过 `lock_reason` 阻止自然结束。

### 2. 自动截屏发送
- `DialogView` 中将 `shot_btn` 提升为实例属性，`_screenshot_analyze()` 在 `try/finally` 中切换 `setEnabled(False/True)`；
- 分析完成后调用 `self._dialog.send(..., screen_context=result)`，将截图分析结果自动送入对话上下文；
- `FocusEngine` 的 `_on_screen_result()` 在触发卡住 AI 分析时，把最新 `result` 通过 `self._on_ai_stuck_analyze` 的扩展签名 `Callable[[str, dict], None]` 传递出去；
- `main.py` 的卡住回调调用 `ai_dialog.send(..., screen_context=result)`，使 AI 基于真实屏幕内容引导。

### 3. 设置中心 UI 优化
- 所有长 Tab（API/AI/网站名单/用户画像）外部包 `QScrollArea`，内容自适应高度；
- `QFormLayout` 设置 `FieldGrowthPolicy.ExpandingFieldsGrow`，让 `QLineEdit/QSpinBox/QComboBox` 横向拉伸；
- 调整 `APITab` 分组间距与最小宽度，避免小窗口下输入框被压扁；
- 保持底部保存/退出按钮始终可见，滚动区域占据中间剩余空间。

## 本轮实现方案（2026-07-29 round 25：学习文化课模式）

### 1. focus_mode 字段与模式选择器
- `AppSettings` 新增 `focus_mode: str = "oi"`（取值 "oi" / "study"），`study_require_problem: bool = False`，`study_site_whitelist: list`（使用 `field(default_factory=lambda: [...])` 提供默认网课白名单）。
- **（注：`study_require_problem` 已在 r45 全量检修中删除，此段仅作历史记录保留）**
- `from_dict` 的 `filtered` 过滤机制让旧 `config.json` 缺新字段时自动走 dataclass 默认值，**向后兼容已存在的用户配置**。
- `FocusView` 顶部新增 `QComboBox`（OI / 学习），通过 `itemData` 存 mode 字符串；当前 mode 选中时从 `ConfigManager().settings.focus_mode` 同步。
- `_on_mode_changed(idx)` 在专注进行中（`self._engine.is_active`）拒绝切换并弹警告，避免运行中状态混乱；非运行中切换时持久化到 `ConfigManager().save()`。

### 2. Prompt 切换（轻量化原则：不引入外部 prompt 库）
- `core/ai_dialog.py` 新增 `SYSTEM_PROMPT_STUDY`（通用学习文化课规则：禁代码输出 / 引导思路 / 走神识别 / 网课平台识别 / 错误指出到知识点）。
- 新增 `build_system_prompt(mode: str, extra_rules: str) -> str` 工厂函数，根据 mode 选择基础 prompt，追加 `extra_rules` 与"错误摘要"格式要求。
- `_DialogWorker.__init__` 增加 `mode: str = "oi"` 参数；`AIDialog.send()` 创建 worker 时传入 `mode=self.cfg.settings.focus_mode`。
- `core/ai_supervisor.py` 新增 `SUPERVISOR_PROMPT_STUDY`（元监督规则针对学习模式：走神提醒、网课引导、鼓励风格），提供 `_build_supervisor_prompt(mode)`，元监督 `check()` 时按 mode 选 prompt。

### 3. 退出策略分支
- `FocusView._normal_exit_clicked()` 内通过 `ConfigManager().settings.focus_mode` 和 `study_require_problem` 分支：
  - OI 模式（默认）：保持原 `request_normal_exit()` 做题退出；
  - 学习模式 + 未启用做题校验：直接调用 `self._engine._normal_complete()`（语义同自然完成，避免日志被误标 `solved=True`）；
  - 学习模式 + 启用做题：调用 `request_normal_exit()`。
- **（注：上述"做题退出"与 `study_require_problem` 分支已在 r45 全量检修中删除，`_normal_exit_clicked` 现直接调用 `request_normal_exit()` 结束专注，此段仅作历史记录保留）**
- **关键修复**：删除上一轮遗留的旧 `_normal_exit_clicked` 定义，避免新逻辑被 Python 静默覆盖（曾导致学习模式默认退出完全不生效）。

### 4. 网站白名单 UI 与字段
- `AppSettings.study_site_whitelist` 默认值覆盖：腾讯课堂、慕课、icourse163、学堂在线、网易学习、ClassIn、智慧山、学习强国、B 站学习区、Coursera、edX、Khan Academy、Google Docs/Forms、Office、WPS 等。
- `SiteTab` 新增独立分组（"学习模式白名单（网课/学习类）"），与 OI 白名单、黑名单并列，三套名单独立增删。
- `collect()` 返回 `site_whitelist / study_site_whitelist / site_blacklist` 三键。
- **未做**：`site_guard` 模块尚未在 OISystem 中独立存在，`study_site_whitelist` 字段已可读可写但暂无消费方——待后续 round 引入 site_guard 时接入。

### 5. ZZOI Tab 占位
- `ZzoiTab` 顶部插入 `QLabel` 占位说明（"暂保留 OI 字段，学习模式暂不生效"）；
- Tab 标题由 "ZZOI" 改为 "ZZOI/任务源"，提示用户该 Tab 后续将改造为通用学习任务来源。

### 6. DialogView 提示文案
- 初始化时根据 `focus_mode` 选择不同 tip_label 文字：OI 模式保留"AI 不会提供代码，会引导你思考。禁用闲聊"；学习模式改为"学习助手：引导思考、识别走神、不直接给答案。请专注当前学习内容。"

### 7. 文档合并
- 外层 `focus_tools_v2/` 下的 6 个 .md 文件已覆盖为软链接占位说明（每个文件 1 段说明 + 历史归档路径）。
- 原始内容通过 `Copy-Item` 复制到 `OISystem/dev_history/*_legacy_20260720.md`，保留可追溯。
- 后续维护文档时仅需修改 `OISystem/` 内层文档，外层文档保持为占位说明。

## 本轮实现方案（2026-07-29 round 26：Markdown 公式渲染）

### 1. 公式解析与 LaTeX → Unicode
- 在 `ui/md_renderer.py` 中新增 `_LATEX_MAP` 与 `_latex_to_unicode(text)`，覆盖常见数学符号；对输入做 `[:500]` 截断并 try/except 保护，异常时返回原文本。
- 行内公式：在 `_flush_inline` 中通过 `re.sub(r"\$([^$\n]+?)\$", ...)` 匹配 `$...$`，替换为 `<span class="math">...</span>`；`$` 两侧为数字时不匹配，避免误伤价格写法。
- 块级公式：在 `render()` 主循环中新增 `in_math_block` 状态，检测到单独一行的 `$$` 时开启/关闭，块内内容整体交给 `_latex_to_unicode`，输出 `<div class="math-block">...</div>`。
- 代码块/行内代码保护：块级公式逻辑位于代码块检测之后，因此 ``` 内出现的 `$$` 不会被解析；行内代码优先于行内公式处理，反引号内的 `$` 不会转换。

### 2. 主题样式集成
- `BASE_CSS` 新增 `.math` / `.math-block` 规则：等宽字体、稍大字号、轻微背景高亮、居中显示（块级）。
- `DialogView.MessageBubble._toggle_view()` 与 `update_theme()` 的动态主题样式中追加 `.math` / `.math-block` 的 `color` / `background` 规则，保证深色/浅色主题均可读。

### 3. 兼容性与降级
- 未映射的 `\command` 保留原样，复杂公式仍可阅读；用户可切换"源码"查看原始 LaTeX。
- 公式内容中的 HTML 特殊字符（`&<>`）在输出前做转义，避免破坏 HTML 结构。
- 渲染失败时 `_latex_to_unicode` 捕获异常并返回原文本，调用方用 `<span class="math">{escaped}</span>` 兜底。

## 本轮实现方案（2026-07-30 round 32：图论编辑器 uvw 格式输入）

### 1. 解析层（graph_renderer.py）
- 新增 `_UVW_LINE_RE = re.compile(r"^\s*(\S+)(?:\s+(\S+))?(?:\s+(\S+))?\s*$")` 支持 1-3 token 行。
- 新增 `parse_uvw_block(text) -> Optional[Dict]`：循环行，1 token 仅建节点、2 token 建无向边、3 token 建带权无向边；首行 `directed: true/false` 单独声明。
- 新增 `_looks_like_uvw(text) -> bool`：启发式检测，不含 YAML 头且不含简化符号（`-->` / `->` / `→` / `—` / `–` / ` - `）且至少一行匹配 1-3 token → 视为 uvw。
- `parse_graph_block` 顶部插入：`if _looks_like_uvw(text): return parse_uvw_block(text)` 优先走 uvw；命中后立即返回，不会再走 YAML/简化。
- weight 复用 `_WEIGHT_VALID = re.compile(r"^-?\d+(\.\d+)?$")`，非法值丢弃为空。
- 节点 id 走 `_latex_to_unicode_simple` 转换（`\angle A` → `∠A`），与 YAML/简化分支保持一致。

### 2. 编辑器 UI（graph_editor.py）
- 顶部工具栏右侧新增"UVW"按钮，点击切换 `uvw_panel` 折叠状态。
- 左侧新增 `uvw_panel = QWidget`（含提示 `QLabel` + `QPlainTextEdit`），停靠在画布左侧（`QHBoxLayout`：左 uvw_panel | 右 view）。
- 200ms debounce：通过 `QTimer` 单次触发（`setSingleShot(True)`）在用户停止输入后调 `parse_uvw_block` → 调 `scene.from_adjacency` 类的 `from_uvw` 方法（实际复用 `from_adjacency` 的内部思路，但走 uvw 解析）。
- 预览失败：状态栏显示 "UVW 解析失败：xxx"；成功显示 "已加载 N 节点 M 边"。
- 提供"清空 UVW"按钮和"复制 UVW 到剪贴板"按钮。
- 主题：uvw_panel 跟随 `ThemeManager().get_css()`，QPlainTextEdit 用主题 surface 色背景。

### 3. scene 扩展
- `GraphScene.from_uvw(text: str) -> bool`：解析 uvw 文本，`clear_graph` 后按 uvw 数据重建节点和边，返回是否成功。
- 节点位置复用 `from_adjacency` 的环形分布思想，但加入 `force=True` 选项触发 `_force_directed_layout`（即"自动布局"语义），与用户原诉求对齐。

### 4. 兼容性保护
- 旧 YAML/简化文本输入仍走原 `parse_graph_block` 分支，uvw 优先级不会误伤含 `-->`/`->`/` - ` 等明确符号的输入。
- `from_adjacency` 接口保留供旧 `导入` 按钮使用；`导入 UVW` 走新 `from_uvw` 路径。
- 编辑器不强制要求开启 uvw 面板；折叠/隐藏状态下退化为纯画布交互。

## 本轮实现方案（2026-07-31 round 34：AI对话卡死 + 文化课跳转 + 画图前端修复）

### 1. 闲聊/文化课误判修复
- `ai_dialog.py:_is_chat` 在 Flash 模型返回 `true` 后，再用"学习/文化课"关键词白名单做二次校验；若命中白名单则强制判为学习提问，不触发闲聊阻断。
- 白名单覆盖：课文、古诗、文言文、作者、朝代、翻译、赏析、卖炭翁、方程式、函数、几何、代数、化学方程式等。
- `SYSTEM_PROMPT` 增加明确说明："询问语文课文、古诗、文言文、数学/英语/科学等文化课问题属于学习提问，不算闲聊"。
- 领域不匹配检测（`_detect_domain_mismatch`）仍保留，检测到文化课问题在 OI 模式下会显示 banner 提示切换"学习文化课"模式。

### 2. AI 对话卡死修复
- `DialogView._load_more`：加载更早消息后，用 `QTimer.singleShot(0, ...)` 把 `_loading_more` 复位和进度条隐藏推迟到事件循环下一帧，避免滚动事件重入导致死循环。
- `_on_scroll`：增加 `self._loading_more` 与 `_rendered_count` 边界检查，防止阈值反复触发。
- `_reset_render_state` / `_new_dialog` / `_delete_dialog`：统一走 `_reset_render_state`，确保 `_render_start`、`_rendered_count`、layout stretch 全部复位。
- `_on_error` / `_on_chat_detected` 等异常路径：统一恢复 `send_btn` 和 `progress`，避免按钮永久禁用。

### 3. 画图前端修复
- `md_renderer.py` graph 分支：渲染前校验 `_get_graph_renderer()` 可用性；SVG 输出失败时统一回退到 `<pre class="graph-fallback">`，避免空白或异常重绘。
- `graph_renderer.py:render_graph_svg`：
  - 增加对 `theme` 各字段的防御性取色，避免 `None` 进入 SVG 属性。
  - SVG 输出时固定 `height="{height}"` 避免 100% height 在某些 QTextBrowser 中塌陷。
  - 对 `_compute_layout` 返回空字典时返回 None，走上层 fallback。
- `MessageBubble`：保留 `_has_graph` 标志位，主题切换时仅重渲染含 graph 气泡，减少卡顿。
- **uvw 截断误判防护**：新增 `_UVW_LOOKS_RE` 整行正则（支持引号 token），与 `_tokenize_uvw_line` 配合使用；在 `_looks_like_uvw` 启发式判断、`parse_graph_block` 顶层兜底分支、以及 `parse_uvw_block` 内部均二次确认原始行确实只有 1-3 个 token，避免普通多 token 句子被误判为 uvw 图数据。同时与 `parse_uvw_block` 对齐，在 `_looks_like_uvw` 和 `parse_graph_block` 兜底分支中也截断行内 `#` 注释。

## 本轮实现方案（2026-07-29 round 27：前端巡检、图论绘图、领域提醒）

### 1. 前端巡检
- 检查 `ui/sidebar_*.py` 中 `ConfigManager` 使用方式，统一改为 `ConfigManager().settings.xxx`。
- 检查各 sidebar 样式的 `_open_*` 接口一致性；新增 `_open_graph` 方法时同步到所有样式。
- 检查 `main.py`、视图初始化、信号连接等是否存在启动函数名不一致或重复绑定问题，仅修复真实 bug，不盲目重命名 Qt 自带方法。

### 2. 图论编辑器
- 新建 `ui/graph_editor.py`：
  - `GraphScene`/`GraphView` 基于 `QGraphicsScene/QGraphicsView`。
  - 节点 `NodeItem`：圆形、可拖拽、可编辑标签（双击或 Edit 模式下单击）。
  - 边 `EdgeItem`：直线/箭头（有向时）、可编辑权重。
  - 工具模式：Force（力导向自动布局）、Draw（点击空白添加节点、点击节点开始连线）、Edit（编辑标签/权重）、Delete（点击删除）。
  - 支持有向/无向切换、清空画布、从文本导入邻接表。
  - 导出：PNG 图片、`to_adjacency()` 文本邻接表。
- 独立窗口：通过 sidebar 的"图论"按钮打开；对话集成：在 `DialogView` 输入区旁增加"画图"按钮，可将当前图以文本形式插入用户消息。

### 3. 领域检测与模式提醒
- `core/ai_dialog.py` 新增 `_detect_domain_mismatch(text: str) -> Optional[str]`：
  - 使用 flash 模型做零样本分类：输入属于 "oi/programming" 还是 "study/culture"。
  - 返回建议模式字符串（"oi"/"study"）或 None。
  - 失败时走关键词兜底（如"语文""英语""历史""地理""生物""化学""物理"→ study；"代码""算法""题解""编译"→ oi）。
- `AIDialog` 新增信号 `mode_mismatch_suggested = Signal(str)`；`DialogView` 连接后显示顶部非阻塞横幅：
  - 文案："当前为 OI 模式，检测到您可能在问文化课问题，是否切换到学习模式？"
  - 提供"切换"按钮（调用 `FocusView` 的模式切换并刷新 UI）和"忽略"按钮。
- 检测不阻塞正常 AI 回复，仅在用户消息后附加一条系统提示或横幅提醒。

### 4. 数论不误判
- 在 `SYSTEM_PROMPT` 的硬性规则中追加：
  - "数论、组合数学等题目允许进行手推公式、逻辑分析、纯数学讨论；仅在用户明确要求可执行代码时才禁止输出代码块。"
- 同步更新 `SUPERVISOR_PROMPT`，让元监督不将数论手推讨论误判为违规。

### 5. 主题与样式
- 图论编辑器跟随当前主题：背景、节点/边默认色、文字颜色从 `ThemeManager` 读取。
- 所有新增 UI 组件禁用原生滚动条或统一使用主题色，保持与现有风格一致。

## 本轮实现方案（2026-07-31 round 35：回归测试修复与健壮性迭代）

### 1. graph 代码块围栏兼容性
- 新增 `_strip_code_fences(text)` 工具函数，统一去除外层 ```` ```graph ```` / ```` ``` ```` 围栏，返回内部正文；空围栏返回空字符串。
- `parse_graph_block` 与 `parse_uvw_block` 入口处均调用 `_strip_code_fences`，保证直接传入 Markdown 原文（如 `test_r29_regression.py` T3）或编辑器 UVW 输入框粘贴 fences 时都能正确解析。
- 围栏去除后仍保留 100KB 硬限制、行内 `#` 注释截断、`_UVW_LOOKS_RE` 二次确认等既有防护。

### 2. 回归测试同步
- `test_r32_regression.py` 7.2 从断言 `marker-end` 改为断言 `<polygon` 存在且 `marker-end="url` 不存在，与当前 polygon 箭头实现一致。
- 新增 `test_r35_regression.py`：覆盖 uvw/YAML/简化三种格式的 fences 解析、空围栏降级、`render_graph_svg` 渲染 fences、无围栏纯文本兼容、polygon 箭头回归确认。

### 3. 子AGENT查错结论
- 对 `graph_renderer.py`、`graph_editor.py`、`md_renderer.py`、`dialog_view.py`、`ai_dialog.py` 进行健壮性审查。
- 审查发现的疑似问题经代码复核后均已由前期 round（r30/r33/r34）修复或不构成真实缺陷：
  - 空行/全注释行在解析循环中已做 `if not line: continue` 防护；
  - 自环边（u == v）跳过边但保留节点，符合预期；
  - `graph_editor.closeEvent` 已 disconnect debounce 与 force timer；
  - md_renderer 代码块内容已做 HTML 转义。
- 本轮未引入新的 P0/P1 缺陷。

## 本轮实现方案（2026-07-31 round 36：子AGENT查错后的 P0/P1 修复）

### 1. dialog_view.py 窗口生命周期修复
- **P0 修复**：`closeEvent` 中显式 `self._timeout_timer.stop()`，避免窗口销毁后周期性 `_check_chat_timeout` 访问已释放成员。
- **P1 修复**：新增 `_unbind_dialog()` 方法，在窗口关闭时对 7 个已连接信号做 `disconnect`；用 `try/except (TypeError, RuntimeError)` 包容已断开或对象已销毁的场景。`closeEvent` 保持"先存档 → 再断开信号 → 最后 super().closeEvent"的顺序。

### 2. graph_renderer.py 简化格式语义修正
- **em dash / en dash 无向化**：将模块级 `_SIMPLE_RE` 与 `parse_graph_block` 内部 `simple_re` 中的 `—`/`–` 移除，仅保留 `-->` / `→` / `->` 作为有向分隔符。
- **无向横线正则增强**：`—`/`–` 仅由 `_SIMPLE_DASH_RE` 按无向横线处理；该正则支持无空格形式（`A–B` / `A—B`）与两侧空格形式，而普通 hyphen `-` 仍要求两侧有空格，避免误拆带连字符的节点名。
- **directed 判定收窄**：所有 directed 判定只检查 `-->` / `->` / `→`，不再把 `—`/`–` 算作有向箭头。

### 3. graph_renderer.py SVG 颜色字段校验
- 新增 `_safe_color(value, default)`：基于 `_color` 取值后，再用正则校验 `#RGB` / `#RRGGBB` 格式；非法/注入字符串退到 default。
- `render_graph_svg` 中 `text_color`、`border_color`、`accent`、`code_bg`、`bg_color` 全部改用 `_safe_color`，防止异常主题值破坏 SVG 属性边界。

### 4. graph_editor.py 自动布局坐标映射
- `GraphScene.from_uvw` 中，`_compute_layout` 返回以 `(0,0)` 为左上角的相对坐标；`sceneRect()` 原点为 `(-400, -300)`，因此需要加上 `rect.left()` / `rect.top()` 偏移后再 `node.setPos(...)`，使节点簇真正居中于场景。

### 5. r36 回归测试
- 新增 `test_r36_regression.py`：
  - 验证 `A — B` / `A – B` 与无空格 `A—B` / `A–B` 均解析为无向边；`A -> B` 仍为有向。
  - 验证 `_safe_color` 对合法 hex、非法字符串、None 的处理，以及恶意 theme 不会进入 SVG。
  - 通过 `inspect.getsource` 检查 `DialogView.closeEvent` 与 `_unbind_dialog` 包含必要的 stop/disconnect。
  - 通过 `inspect.getsource` 检查 `GraphScene.from_uvw` 包含 `rect.left()` / `rect.top()` 偏移应用。

### 6. 未立即修复项（P2 / Future）
- `ai_dialog.py` 的 `attach_screen_context` / `screenshot_analyze` 仍为同步调用；改为 `QThreadPool` + `QRunnable` 涉及回调链重构，本轮作为 Future 记录，待后续专门迭代。

## 本轮实现方案（2026-07-31 round 37：画图功能 BUG 修复，参考 CS Academy Graph Editor）

### 1. 兼容 CS Academy 第一行节点数格式
- `parse_uvw_block` 在扫描 `directed:` 声明后，把第一行单个正整数 + 后续存在边行的情况识别为 CS Academy 标准格式。
- 预创建 `1..n` 个节点（`n <= MAX_NODES`，当前 20），超大 `n` 回退为普通节点处理，避免 `999999999\n1 2` 拖垮进程。
- `directed: true/false` 可与节点数行共存；仅当首行或中间行显式声明时才改变 directed 状态。

### 2. 力导向模式节点拖拽固定
- `NodeItem` 新增 `_pinned` 与 `_drag_start_pos`。
- 在 `mousePressEvent` 中若当前场景为 force 模式，临时 `_pinned = False`，让本次拖动可立即生效。
- 在 `mouseReleaseEvent` 中计算位移，超过 3px 后 `_pinned = True`，避免单击误固定。
- `GraphScene._force_step` 对 `_pinned` 节点跳过速度/位置更新；`set_mode("force")` 重新启用 force 时清除所有 `_pinned`，允许整体重新布局。

### 3. from_uvw 后有向按钮状态同步
- 新增 `GraphEditor.set_directed(directed)`，统一更新：
  - `directed_btn.setChecked(directed)` 与按钮文字在有向/无向间切换；
  - `self.scene.set_directed(directed)` 同步场景与所有边的 `directed` 属性并重绘箭头。
- `_apply_uvw_preview` 解析成功后调用 `self.set_directed(self.scene._directed)`。

### 4. 子AGENT 健壮性审查修复
- **UVW 注释含简化符号不再阻断解析**：`_has_simple_symbol_in_middle` 先跳过 `#` 开头的注释行，再用 `_strip_inline_comment` 去除行内注释后检测，避免 `# A -> B` 误导全局扫描。
- **YAML `edges:` 可先于 `nodes:` 定义**：`parse_graph_block` 引入 `pending_edges` 暂存缺失节点的边，全部扫描结束后再统一解析；仍缺失的边才进入 `dropped_edges`。
- **简化格式支持负权重**：`_SIMPLE_RE` / `_SIMPLE_DASH_RE` 与 `parse_graph_block` 内局部正则的权重捕获组改为 `(-?[0-9.]+)`，非法值仍由 `_WEIGHT_VALID` 丢弃。
- **from_uvw 后同步 `_id_counter`**：`GraphScene.from_uvw` 提交新图后，把 `_id_counter` 设为当前数字型节点 ID 最大值，避免 `add_node()` 产生重复 ID。
- **UVW 引号内 `#` 不再被截断**：新增 `_strip_inline_comment(line)`，仅在非引号、非转义位置识别 `#`，引号内的 `#` 保留为节点名一部分。

### 5. 回归测试
- 新增 `test_r37_regression.py`：CS Academy 格式、节点数安全限制、 directed 同步、force pinned 代码路径。
- 新增 `test_r37_subagent_check.py`：覆盖上述子AGENT 发现的 4 P1 / 2 P2 问题。
- 修正 `test_r37_regression.py` 超大节点数回退断言（回退后 4 节点而非 3）。
- r29~r37 全量回归测试通过。

## 本轮实现方案（2026-07-31 round 38：UI/UX 全面审查）

### 1. 截屏分析异步化
- `AIDialog` 新增 `_ScreenshotWorker(QObject)` + QThread，把 `screenshot_analyze` / `attach_screen_context` 从同步 `vision_chat` 改为异步。
- 信号链路：worker `finished`/`context_ready`/`failed`/`done` → AIDialog `screenshot_analyzed`/`screen_context_ready` → DialogView `_on_screenshot_analyzed`。
- `ScreenAnalyzer._reset_thread_refs()` 连接到 `thread.finished`，复位 `_thread`/`_worker` 引用，避免访问已销毁 C++ 对象。

### 2. 虚拟滚动增量修复
- `DialogView._add_message` 在滑动窗口分支（窗口已满时去掉最旧、追加最新）后必须 `_render_start += 1`。
- 否则 `_render_start` 永远为 0，`_on_scroll` 中 `_render_start > 0` 判断永不成立，"加载更早消息"永久失效。
- 首次渲染由 `_render_recent()` 设置 `start = max(0, total - RENDER_CHUNK)`；增量路径只更新 `_rendered_count`。

### 3. SVG 边色对比度感知选择
- 主题中 `border` 是为 UI 分隔线设计的，与背景对比度往往 < 3:1，不适合作为图论边线/箭头颜色。
- `text_dim` 在浅色主题（white/cream）下对比度也不足（2.34/2.47 < 3.0）。
- `_pick_edge_color(theme, bg_color)` 依次尝试 `text_dim` / `text` / `border`，用 `_contrast_ratio(c1, c2)`（WCAG 公式）挑选第一个 ≥ 3.0 的颜色。
- renderer 和 editor 统一调用此函数，视觉一致。

### 4. FocusView 唤醒状态同步
- `_bind_engine` 只连接信号，不检查当前引擎状态 → 重开后 UI 显示"未启动"但引擎实际在跑。
- `_sync_state_from_engine()` 检查 `engine.is_active`，调用 `_on_started(total_sec)` 恢复退出按钮，调用 `_on_tick(remaining)` 更新倒计时。

### 5. stuck_count 清零条件
- 原实现：`self._stuck_count = 0` 在 try/except 之外，回调失败也清零 → 用户无法再次获得卡住帮助。
- 修复：清零移入 try 块（仅成功时清零）；except 中折半 `max(0, threshold // 2)`。

## 本轮实现方案（2026-07-31 round 39：潜在崩溃点修复）

### 1. Qt 对象生命周期与信号安全
- 全局单例信号（`FocusEngine.focus_started/focus_ended`、`AIDialog.*`）在窗口关闭后必须显式 `disconnect`，否则后续信号会触发已销毁对象的槽函数。
- 使用 `shiboken6.isValid(widget)` 检查 QWidget 的 C++ 对象是否存活，替代不可用的 `sip` 模块；不可用时退化到 `try/except RuntimeError`。
- `closeEvent` 顺序：先停止所有 QTimer → 再存档/断开信号 → 最后 `super().closeEvent(event)`。

### 2. None / 缺失配置防御
- `getattr(cfg.settings, "field", None)` + `or default` 是标准兜底模式，避免 `AttributeError`/`TypeError`。
- 对可能为 0/None 的除数（如 `ai_supervisor_interval_rounds`、`focus_stuck_threshold`）使用 `max(1, value)`。
- `lock_reason` 等可能为 None 的字符串在调用 `.startswith()` 前先 `(value or "")`。

### 3. 线程安全
- QThread worker 的 `run()` 必须用 `finally` 兜底触发退出信号（如 `done.emit()`），防止 `emit` 自身异常时线程泄漏。
- 外部通过公共接口（如 `is_busy()`）判断线程状态，避免访问私有 `_thread` 属性。

### 4. 列表快照迭代
- 回调可能自注销（`remove_state_observer(cb)`），直接 `for cb in _list` 会抛 `RuntimeError`；统一 `for cb in list(_list)` 快照迭代。

### 5. JSON / 网络响应类型防御
- `json.loads()` 结果不一定是 `dict`，可能是 `list`/`str`/`int`；取值前先 `isinstance(data, dict)`。
- 旧版 requests 的 `resp.json()` 抛 `json.JSONDecodeError`（非 `RequestException` 子类），需单独捕获。

### 6. 力导向布局竞争
- `_force_step` 在 timer 线程中运行，可能与主线程的 `clear_graph` / `from_uvw` 竞争；对 `_nodes` / `_edges` 做快照后再迭代。
- `EdgeItem.boundingRect/paint` 捕获 `RuntimeError`，防止节点被 `removeItem` 后仍被重绘。

### 7. 无屏幕 / RDP 断开防御
- `QApplication.primaryScreen()` 在远程桌面断开、无显示器或某些虚拟化环境下可能返回 `None`；所有基于屏幕几何的定位/截图/弹窗代码均需判空。

### 8. 主题自定义 JSON 防御
- 用户导入的主题可能只含 `_REQUIRED_KEYS`；所有 theme 字段读取使用 `.get(key, default)`。
- `ThemeManager.current_theme` 对 custom 主题返回 `deepcopy`，防止外部修改污染内部 `_custom`。

## 本轮实现方案（2026-07-31 round 42：AI 对话页面与体验深度优化）

### 1. 多行输入框 + 快捷键拦截
- 输入控件由 `QLineEdit` 改为 `QPlainTextEdit`，支持多行编辑与粘贴。
- 通过 `self.input.installEventFilter(self)` + 重写 `eventFilter(obj, event)` 拦截按键：
  - `Qt.ShiftModifier` + `Key_Return`/`Key_Enter` 时 `return False`，交给 `QPlainTextEdit` 处理换行。
  - 无修饰键或 `Qt.ControlModifier` 时调用 `_send()` 并 `return True` 拦截默认行为（避免回车插入换行）。
- 其余按键一律 `return super().eventFilter(obj, event)`，保持默认行为。
- `keyPressEvent` 处理 `Key_Escape` 清空输入框（仅在输入框有焦点时）。

### 2. 自适应高度 + 字符计数
- `_on_input_changed` 监听 `textChanged`，根据 `fontMetrics().height()` 与 `document().lineCount()` 计算所需高度。
- `setFixedHeight(min(160, max(44, base + lines * line_h)))` 同时设上下限：44px 单行，160px 约 6 行。
- 工具行增加 `_char_count` QLabel，实时显示 `len(self.input.toPlainText())` 字数。

### 3. 空状态欢迎页
- `_build_welcome` 构建独立 `QFrame`，覆盖在 `messages_container` 之上，VBoxLayout 居中对齐。
- 包含：图标 QLabel + 标题 + 副标题 + 4 个快捷问题按钮。
- 快捷问题按钮用 lambda 闭包捕获 `text`：`lambda checked, text=q: self._ask_quick_question(text)`，避免闭包变量延迟绑定问题（Python for 循环中 lambda 默认捕获变量名而非值）。
- `_quick_questions` 按当前 `focus_mode` 返回 OI（思路/提示/复杂度/边界）或学习（思路/概念/易错点/总结）模式专属问题。
- `_update_welcome_visibility` 在 `_add_message`（追加后）与 `_reset_render_state`（重置后）统一调用，保证可见性与消息数同步。

### 4. 角色头像 / 时间戳 / 复制按钮
- 模块级 helper `_make_avatar(role, theme)`：24×24 圆形 QLabel，用户=accent 色"我"，AI=warning 色"AI"。
- 模块级 helper `_make_text_button(text, theme, tooltip)`：无边框小按钮，带 hover/checked 样式。
- `MessageBubble.__init__` 增加 `timestamp: QDateTime = None` 参数（默认当前时间），角色行展示头像+角色名+时间戳（`toString("HH:mm")`）。
- AI 气泡增加 `_copy_btn`，`_copy_text` 用 `QApplication.clipboard().setText(self._raw_text)` 写入，1200ms 后用 `QTimer.singleShot` 恢复按钮文字，闭包内 `try/except RuntimeError` 防御已销毁控件。
- `update_theme` 新增角色行控件样式刷新块，用 `try/except RuntimeError` 防御已销毁控件；主题切换时仅重渲染非 graph 气泡，避免卡顿。

### 5. AI 思考状态机
- `_setup_ui` 新增 `_thinking_label`（居中淡色 QLabel），发送时 `setText("AI 正在思考...")` 并 `show()`。
- 发送按钮文字随状态机变化：
  - `_send` → "生成中"（发送中）
  - `_on_reply` / `_on_error` / `_on_chat_detected` / `_on_code_output` → "发送"（恢复就绪）
- `_new_dialog`/`_delete_dialog`/`_finish_screenshot_analyze`/`_upload_file` 统一维护按钮文字与 `thinking_label` 可见性，避免状态残留。
- 状态机原则：任何结束 AI 处理的路径都必须显式恢复 `send_btn` 文字与 `thinking_label.hide()`，不允许依赖隐式清理。

### 6. 输入区布局重组
- InputCard 由 QHBoxLayout 改为 QVBoxLayout：工具行（画图+上传+提示+字数）→ 输入框 → 发送按钮行。
- 顶部工具栏移除上传按钮（移入输入区），用 `setup_src.count("SVG_CLOUD_UPLOAD") == 1` 回归测试保证唯一性。
- 输入区作为整体卡片，背景色取 `theme["surface"]`，圆角与阴影保持与消息气泡风格一致。

## 本轮实现方案（2026-08-15 round 45：全量检修——去掉答题 + 接通"有口没码"端口）

### 1. 去掉"做题退出"（答题）
- **范围**：删除 `FocusEngine.focus_normal_exit_requested` 信号、`_on_simple_problem` 回调、`set_simple_problem_callback()`、`exit_after_problem_solved()`；删除 `FocusView.SimpleProblemDialog`、`_on_normal_requested()`；删除 `ZzoiTracker.get_simple_problem()`；删除 `AppSettings.study_require_problem` 字段与设置页复选框；删除 `main.py` 的 `set_simple_problem_callback` 注入。
- **行为变化**：`FocusEngine.request_normal_exit()` 改为直接结束专注（记录 `focus_normal_exit` 日志 → 停定时器 → 置 inactive → emit `focus_ended`），ZZOI 锁定下仍禁止退出。`FocusView._normal_exit_clicked()` 简化为直接调 `request_normal_exit()`。
- **文案同步**：`exit_flow.py` 提示、`helpers.py:write_daily_markdown`（"✅ 做题通过退出"→"✅ 正常退出"）、`focus_view.py` 按钮/提示/注释统一改为"正常退出=直接结束专注"。

### 2. API role 路由接通（kimi_role / glm_role / deepseek_role）
- `core/ai_client.py` 新增：
  - `resolve_provider_for_role(role, default)`：遍历 `(kimi,kimi_role)/(glm,glm_role)/(deepseek,deepseek_role)`，匹配 role 即返回该 provider，未匹配返回 default。
  - `_resolve_role_target(role, default_provider, override_model_field)`：解析 provider，若 provider 未启用/无 key 回退 default_provider；model 优先用 override 字段（ai_flash_model / ai_dialog_model，均为 deepseek 系模型名），非 default provider 时改用该 provider 自身模型名。
  - `resolve_flash_target()`（knowledge 角色）与 `resolve_dialog_target()`（dialog 角色）两个薄封装。
- `chat()`/`vision_chat()` 的 provider 默认值改为 `None`，内部按 role 解析：chat→"dialog"，vision→"vision"。
- 调用点接入：`_DialogWorker.run()` 用 `resolve_dialog_target()`；`_ScreenshotWorker.run()` / `_AnalyzeWorker.run()` 用 `resolve_provider_for_role("vision","glm")`；`_is_chat`/`_detect_domain_mismatch`/`_summarize`/`_score_with_flash`/`AISupervisor.check` 用 `resolve_flash_target()`。
- **兼容性**：默认配置下 vision→glm、dialog→deepseek 不变；flash 默认走 knowledge 角色（kimi），kimi 未启用/无 key 时回退 deepseek 且模型名回退到 deepseek 配置模型，保证 flash 功能可用。

### 3. 接通死配置
- **watchdog_restart_on_crash**：`watchdog.py` 新增 `restart_enabled()`（读 `ConfigManager().settings.watchdog_restart_on_crash`，读失败默认 True），`run()` 在主进程崩溃后先检查该开关，关闭则 watchdog 直接退出不重启。
- **site_risk_score_threshold**：`core/site_guard.py` 新增 `_is_focus_active()` 与 `_risk_score()`（权重：游戏域 70 > 黑名单 60 > 资讯域 40 > 活动关键词 30，累加上限 100）；`enforce()` 在非专注模式下按阈值判定是否关闭窗口，专注模式下命中即关。
- **TempEdgeItem 主题色**：`ui/graph_editor.py:TempEdgeItem` 预览线颜色改用 `_pick_edge_color(ThemeManager().current_theme, bg)`，异常时回退 `#94a3b8`。

### 4. 回归测试
- 新增 `test_r45_overhaul.py`：52 项断言，覆盖去答题符号移除与 `request_normal_exit` 直接退出、role 路由解析与回退、watchdog 开关、site_guard 风险评分、TempEdgeItem 主题色，以及第二轮子AGENT查错修复（toast/svg_view/视觉模型名/role UI/画像注入）。
- r29~r44 全量 18 个旧回归测试全部通过，无回归。

### 5. 第二轮：子AGENT刁难查错后的修复
- **toast.py**：`show_toast()` 修正 `core.boss_mode` → `core.mute_mode`（`register_toast`/`is_mute_mode`），恢复静音模式隐藏弹窗功能。
- **svg_view.py**：补齐 `_select_all`/`_deselect_all`/`_save`/`_update_count`/`_all_cards`，修复按钮连到不存在方法的 AttributeError。
- **screen_analyzer.py（P1-1）**：`_AnalyzeWorker` 视觉 role 路由增加 kimi/moonshot/glm 子串覆盖 provider，`model=engine or None`，保证模型名与 provider 一致。
- **死配置清理**：删除 `ai_unlimited_chat_kill_delay_sec` 及 AITab `kill_delay` UI。
- **role 字段 UI**：APITab 新增 kimi/glm/deepseek"角色"下拉，接通 role 字段可视化配置。
- **用户画像接通**：`build_system_prompt()` 注入 `user_profile_text`，使画像被 AI 消费。
- **P2-2 / P2-5**：`_resolve_role_target` 非默认 provider 时 model 不回退 deepseek 系模型名；`site_guard` 阈值 0/负数处理为命中即关。

### 6. 第三轮：终检（跨线程 / 兜底缺口）修复
- **跨线程 Qt**：`core/mute_mode.py` 新增 `_MuteToggleBridge`（QObject + `@Slot`），`start_global_hotkey` 在主线程创建 bridge，热键回调经 `QMetaObject.invokeMethod(..., Qt.QueuedConnection)` 把静音切换投递回主线程，避免 pynput 监听线程直接操作 QWidget。
- **视觉路由统一**：`core/ai_client.py` 新增 `resolve_vision_target()`（role + screen_engine 子串覆盖 + deepseek 强制回退 glm + provider 未启用回退），`_AnalyzeWorker`/`_ScreenshotWorker` 统一接入，消除两条视觉入口 provider 解析不一致，并杜绝 deepseek 视觉永久失败。
- **toast 静音兜底**：`show_toast` 静音模式下直接返回（不创建弹窗），避免解除静音后 (0,0) 永驻；`register_toast` 用 `shiboken6.isValid` 清理已销毁弹窗引用。
- **设置页 deepseek 角色**：deepseek"角色"下拉只保留 dialog（deepseek 不支持视觉）；SVG 选择器复选框 `toggled` 绑定计数刷新。

## 本轮实现方案（2026-08-15 round 46：前端补齐对应端口）

### 1. "关联当前页面"按钮（DialogView）
- 工具栏新增 `attach_btn`（SVG_EYE）→ `_attach_screen_context()` 异步调 `AIDialog.attach_screen_context()`。
- 屏幕摘要经 `screen_context_ready` 信号回流 → `_on_screen_context_ready()` 存入 `_pending_screen_context`。
- `_send()` 把 pending 上下文作为 `screen_context` 注入下一条消息并清空；`_bind_dialog`/`_unbind_dialog` 对称连接/断开信号。

### 2. "拉黑"按钮（上下文块）
- `MessageBubble` 新增 `on_blacklist` 参数与"拉黑"文字按钮（`_make_text_button`），`_add_bubble`/`_load_more` 传入 `_blacklist_block` 处理器，接通 `AIDialog.blacklist_block()`。

### 3. SVG 选择器入口
- `ui/settings_view.py` ThemeTab 新增"SVG 图标选择器"按钮 → `_open_svg_picker()` 实例化并 show `SvgPickerView`（原孤儿窗口）。

### 4. 作弊检测 Tab（LogView）
- `ui/log_view.py` 新增"作弊检测"Tab，`_refresh` 用 `CheatDetector().get_summary()`/`get_flagged_problems()` 填充汇总与被标记题目；清理"占位实现"过期注释。

### 5. 回归测试
- `test_r45_overhaul.py` 扩到 69 项断言（新增 T11 前端端口 10 项），r29~r44 全量回归 0 失败。

## 当前实现方案（2026-08-16 round 48：全量检修 + 多轮刁难自检 + 极端测试）

### 1. 配置层脏数据自愈
- `AppSettings.__post_init__` 统一归一化：
  - bool 字段：`"false"/0/off` → False，`"true"/1/on` → True；
  - int 字段：`int()` 失败回退 dataclass 默认，再按 `_INT_RANGES` 夹取（超大值不会让 QSpinBox OverflowError）；
  - list 字段：`screen_custom_rect` 元素转 int 且必须 4 项；`mail_receivers` 非空元素转 str；site_*/study_*/selected_svgs 只留字符串；
  - dict 字段：非 dict 回退 `{}`；其余 str 字段 None→""、非 str→str；
  - `focus_mode` 只允许 oi/study，非法值归一为 oi（该分支 `continue`，避免字符串兜底用旧值覆盖）。
- `ConfigManager._load`：config.json/secrets.json 顶层为 list/str/数字等合法 JSON 非对象时按空配置自愈，不 `{**data}` 崩溃。
- `ThemeManager.current_theme` 经 `_fill_theme_defaults` 补齐全部标准键；`_load_from_cfg` 校验 custom 为 dict 且含必填键。

### 2. 日志层原子写/自愈/并发
- `save_json` 改临时文件 + `os.fsync` + `os.replace` 原子写；失败退回直接写。
- `load_json` 捕获 OSError（权限/磁盘）按缺失处理。
- `_normalize_daily_log`：列表键复制 dict 元素后再修 detail，保证 `normalized != data` 能触发落盘；reminders 与 submission_errors 值执行 `int()` 并夹取非负。
- `log_event` 用 `_DAILY_LOG_LOCK` 串行化读-改-写，避免 ZZOI/存档多线程并发写坏 JSON。

### 3. 线程与生命周期
- AIDialog 对话/截图线程、ScreenAnalyzer 线程的 `_reset_*_thread_refs` 增加代次参数：仅当 finished 线程就是当前线程时才清空，旧线程排队回调不会清掉新线程。
- 截图/关联页面请求携带 `request_id`（AIDialog 信号 `Signal(int, dict)`，worker 信号同样带 id）；DialogView 保存自身代次，旧窗口迟到结果直接丢弃。
- 自动 ZZOI 检查：`_AutoZzoiWorker` + 主线程 `_AutoZzoiBridge`（`@Slot(dict)`），thread/worker/bridge 三元组全部持有引用防 GC；锁定应用在主线程。
- AIDialog 异步存档 `_ArchiveWorker`：摘要+写档在线程内完成；`close_dialog_async/delete_dialog_async` 供 UI 高频操作，退出流程保留同步 `close_dialog()` 兜底。
- 旧 worker 的 chat/code/algo/mode_mismatch 信号槽与 reply/error 一样检查 `_ignore_worker`，新建/删除对话后不污染新对话。

### 4. 状态机修复
- 侧边栏五样式 `_exit_clicked`：专门捕获 FocusLockedError（request_exit 已弹提示），不再 `QApplication.quit()` 绕过专注/ZZOI 锁定；其他异常弹窗提示并 finally 恢复按钮。
- ZZOI 锁定：FocusView `_apply_lock_ui` 隐藏正常/急事/关机按钮；`_sync_state_from_engine` 重开同步锁定态；`request_emergency_shutdown` 锁定下直接拦截。
- `oj_tracker` 三个 fetch 方法维护 `_last_fetch_ok`；网络错误/非 200 时 daily_check 跳过零提交/排行榜锁定。
- ZzoiView 手动检查/登录线程化；登录成功把真实 session cookie 迁移回全局 tracker，避免假登录态。
- DialogView 虚拟滚动：新增 `_user_scrolled_up` 与 `_render_start` 解耦；停在底部时持续滑动渲染，上滚时只追加数据并显示"跳到底部"。

### 5. 端口补齐与消费者
- 设置中心新增「系统集成」Tab：sidebar_style/sidebar_float_hotkey/watchdog 两开关/note.ms 三字段/external_ai_remind_cooldown_min。
- SiteTab 新增资讯域/游戏域名单与 site_risk_score_threshold。
- `selected_svgs` 真正消费：`get_all_svgs_grouped` 加载工作区 `svg_mapping.json` 文件 SVG（带 role 语义），`get_sidebar_buttons()` 按语义把收藏图标覆盖到侧边栏按钮；五个 Sidebar 均调用该函数。
- ContextManager 三个信号经 AIDialog 转发，DialogView 连接并显示提示；ScreenAnalyzer.analyze_failed 接入 toast。
- note.ms API 地址从 `note_ms_base_url` 推导（`{base}/api/notes/{slug}[/content]`）。
- GraphScene.from_uvw 与 from_adjacency 统一 100KB/MAX_NODES/MAX_EDGES，超限拒绝且保留原画布。
- ai_client chat/vision_chat 对非 dict 响应转 AICallError。

### 6. 回归与验收
- 新增 `test_r48_overhaul.py`：151 项断言（T1 配置、T2 虚拟滚动、T3 旧 worker、T4 ZZOI 不误锁、T5 退出拦截、T6 锁定、T7 主题、T8 热键桥、T9 send 异常恢复、T10 ZzoiView、T11 存档、T12 日志、T13 脏配置、T14 graph 极限、T15 信号、T16 selected_svgs、T17 设置端口、T18-T22 二轮项、T23 终检项）。
- r29~r45 全部旧回归脚本 0 失败；子AGENT三轮刁难审查（14+9+3+1 项）全部修复；最终验收轮 P0/P1/P2 均为 0。

## 本轮实现方案（2026-08-20 round 52：全量检修）

### 1. 检查策略
- **静态分析**：使用 `audit_static.py` 检查死配置、死端口、未使用import
- **动态检查**：运行全量回归测试 `test_r48_overhaul.py`，验证151项断言
- **子AGENT查错**：调用上下文故障检测专员，以刁难视角审查代码健壮性
- **前后端配合检查**：逐个检查配置项→设置UI→引擎消费链路是否完整

### 2. 重点检查项
- 配置项消费链路：每个AppSettings字段是否在设置UI可编辑、在引擎/客户端被读取
- 信号/槽连接：所有信号签名是否与槽函数参数匹配，disconnect是否完整
- 线程安全：跨线程Qt调用是否通过桥接（QMetaObject.invokeMethod或信号槽）
- 生命周期：closeEvent是否停止所有QTimer、断开所有信号、释放所有资源
- 错误处理：异常路径是否恢复UI状态、记录错误日志

### 3. 修复策略
- 优先修复P0（崩溃/数据丢失）和P1（功能失效）问题
- P2（体验问题）根据影响范围决定是否修复
- 每轮修复后运行回归测试，确保不引入新问题

### 4. 验收标准
- 最后一轮子AGENT查错报告P0=0、P1=0
- P2问题如果可忽略（不影响核心功能）则通过
- 全量回归测试通过

## 本轮实现方案（2026-09-06 round 55：课堂音频双通道 + VAD + MiMo ASR + 打断决策引擎）

### 1. 纯 ctypes WASAPI 采集（零新依赖）
COM 互操作三件套（`core/audio_capture.py`）：
- `CLSID_MMDeviceEnumerator={BCDE0395-E52F-467C-8E3D-C4579291692E}`，`IID_IMMDeviceEnumerator={A95664D2-9614-4F35-A746-DE8DB63617E6}`，`IID_IAudioClient={1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}`，`IID_IAudioCaptureClient={C8ADBD64-E71E-48a0-A4DE-185C395CD317}`。
- vtable 索引（IUnknown 占 0-2）：
  - IMMDeviceEnumerator：`GetDefaultAudioEndpoint=4`（dataFlow eRender=0/eCapture=1，role eMultimedia=1）
  - IMMDevice：`Activate=3`
  - IAudioClient：`Initialize=3`、`GetBufferSize=4`、`GetCurrentPadding=6`、`GetMixFormat=8`、`Start=10`、`Stop=11`、`GetService=14`
  - IAudioCaptureClient：`GetBuffer=3`、`ReleaseBuffer=4`、`GetNextPacketSize=5`
- 调用方式：`ctypes.cast(obj, POINTER(c_void_p))[0]` 取 vtable 指针，按 `index*8` 偏移取函数指针，用 `WINFUNCTYPE(HRESULT, c_void_p, ...)` 构造原型后调用；原型对象缓存，避免每帧重建。HRESULT 按有符号 int32 判定（`<0` 为失败），`RPC_E_CHANGED_MODE(0x80010106)` 视为"线程已初始化 COM"而非错误。
- `CoInitializeEx(None, COINIT_APARTMENTTHREADED|COINIT_DISABLE_OLE1DDE)` 在**采集线程内**执行，线程退出前 `CoUninitialize`（配对，防 COM 泄漏）。
- Loopback 关键点：设备取 **eRender 默认端点**（不是 eCapture），`Initialize` 的 StreamFlags 带 `AUDCLNT_STREAMFLAGS_LOOPBACK=0x00020000`，shareMode=SHARED(0)，buffer 时长 200ms（单位 100ns，即 2_000_000），periodicity=0，format 传 `GetMixFormat` 结果，eventHandle=NULL。
- 采集循环：`GetCurrentPadding → GetBuffer → 处理 → finally ReleaseBuffer`（处理段抛异常也必须 Release，否则该客户端后续 GetBuffer 全废）；`AUDCLNT_BUFFERFLAGS_SILENT=0x2` 时按等长零数据处理（不读指针内容）。**空闲静音注入（实机实测定案）**：`padding==0` 且连续空闲 ≥250ms 时，按实时速率向 VAD 路径注入合成零样本（推动段尾收段）；瞬时 padding==0 不注入（防静音帧穿插真实语音虚增段时长，实测虚增 ~70%）；注入不进环形缓冲，`stats.idle_seconds` 单独记账。
- 格式解析：`GetMixFormat` 返回 `WAVEFORMATEX*`（18 字节头），`wFormatTag==0xFFFE`（EXTENSIBLE）时读 22 字节偏移处的 SubFormat GUID 判 `IEEE_FLOAT({00000003-...})` 还是 `PCM({00000001-...})`；据 `wBitsPerSample` 选 `float32` / `int16` numpy dtype。ctypes 侧 `WAVEFORMATEX` 结构体必须 `_pack_=1`（C 头 `#pragma pack(1)`，默认对齐会补到 20 字节；当前仅作指针类型用，防御未来实例化）。
- 归一化：多声道 → mono（float 均值），native rate → 16kHz（numpy 线性插值），输出统一 `float32 [-1,1]`。环形缓冲溢出时单块自身超限（公开 `feed()` 注入大块的路径）切块头保留尾部，保证"最近 N 秒"契约无死角。

### 2. VAD 参数（能量法）
20ms 帧；RMS 转 dB；噪声底 = 非语音帧 RMS 的滑动均值（衰减系数 0.05）；语音帧判定 `rms > max(abs_floor, noise_floor*ratio)`（默认 ratio=2.5、abs_floor 对应 -50dBFS）；onset 需连续 3 帧语音；段成立 ≥250ms；段尾静音 ≥600ms 收段；单段 ≥15s 强制切；前置补 200ms。**所有阈值走设置项**，便于按机器麦克风/系统音量调。

### 3. ASR 调用契约（实测锚点）
`transcribe_audio(wav_bytes)`：POST `{xiaomi_base_url}/chat/completions`，payload `{"model": xiaomi_asr_model(默认 mimo-v2.5), "messages":[{"role":"user","content":[{"type":"text","text":"请逐字转写这段音频…只输出转写文字。"},{"type":"input_audio","input_audio":{"data":b64,"format":"wav"}}]}], "max_tokens":512, "reasoning_effort":"none"}`，timeout 120s。**响应解析不走 `_extract_reply_content`**（审查 M1 修复）：它把"结构异常"与"空 content"折叠成同一种 AICallError；ASR 需要区分——空 content（静音段，正常）返回空串由调用方跳过，结构异常（坏响应）抛 AICallError 让 worker 计入 failed 并触发连续失败暂停，否则系统性坏响应会让转写流无声变空。`xiaomi_asr_model` 单列而非复用 `xiaomi_model`：验证用的是 `mimo-v2.5`，`-pro` 是否吃音频未验证。

### 4. 线程与信号模型
- 采集线程 ×2（loopback / mic）→ 有界环形缓冲（30s，deque 分块）
- VAD 在采集线程内同步跑（纯 numpy，微秒级）→ 闭合段投 `queue.Queue`
- **单一转写工作线程**串行消费（requests 同步，不占 Qt 主线程；串行天然限流，避免并发打爆 token plan）
- 结果经 `QObject` 信号回主线程（沿用 r52 整训：**必须连绑定方法，禁用无 receiver 的 lambda**，见 FreqErr「信号生命周期」）
- **停止顺序（审查后定案）**：停采集线程 → VAD flush 冲出尾段入队 → 投毒丸 → join 转写线程 → **最后**置 stop 事件（worker 靠毒丸退出，事件后置让 flush 尾段有机会转写完，兑现"救最后一句"）
- **会话生命周期**：每会话全新 `threading.Event`（旧 worker 绑旧事件自然死亡，杜绝 clear 复活僵尸线程）；start() 先 `_drain_queue()` 排空上会话遗留（含陈旧毒丸），丢弃段计 `dropped_stale`
- **通道看门狗**：worker 队列空闲（`queue.Empty`）分支调 `_check_channels()`，死通道（`cap.running=False`）经 `channel_error(speaker, message)` 通报，`_dead_reported` 集合防重复；start() 时启动失败通道预加入集合

### 5. 打断闸门（可单测的纯函数）
`InterruptEngine.evaluate()` 返回 `(bool, str reject_reason)`，闸门顺序固定：muted → level==off → cooldown → confidence < min → level 过滤（on_error 仅收 error/omission）。决策 AI 调用失败 = 不打断（fail-safe，绝不因解析失败误打断课堂）。

### 6. 隐私与资源红线
原始 PCM 只存在于内存环形缓冲与 VAD 段对象内，**不落盘不上传**；落盘仅 `data/classroom/YYYYMMDD.jsonl` 的转写文字（按天分文件，可整体清理）；环形缓冲与 transcript deque 均有 maxlen，长时间挂机不涨内存。

### 7. 测试策略
- `test_r55_classroom.py`（T1-T16，221 项）：合成信号（正弦=语音、静音、突发噪声）驱动 VAD 断言切分/前置缓冲/强制切段；WAV 头字节级断言；`float32_to_wav` 往返；闸门顺序全组合；transcribe payload 结构（mock，不打真网络）；环形缓冲溢出丢旧；**T16 = 审查修复回归锁**（C1 settings 字段存在性、C2 遗留毒丸排空+会话复活、M4 旧 worker 不复活、H2 interrupt 字符串布尔归一、M2/M3 范围收紧、M5 陈旧上下文拦截、H1 看门狗、dropped_stale 记账）。
- 实机验证：`demo_classroom.py` 播放真实音频 + loopback 抓取 + MiMo 转写闭环（真实网络，输出转写文本）。

### 8. round 55 审查修复清单（两轮 verifier 全量复查 PASS）
- **C1** settings.py 字段合并损坏（screen_capture_region 被挤进注释，截屏分析/设置窗即崩，199 项测试零覆盖）→ 还原独立字段 + T16 存在性锁。教训：**field 定义行改写时注释必须整体挪走，`#` 后面的一切都是注释**。
- **C2** stop/start 会话缺陷（遗留毒丸+新 worker 一口毙，关/开课堂音频一次即触发"running=True 永远零转写"）→ drain + 新 Event + stop 时序重排。
- **H1** 设备拔出通道静默死亡 → 看门狗（自动重建留后续轮次）。
- **H2** `bool("false")=True` 违反 fail-safe → 白名单归一。
- **M1** ASR 结构异常被折叠成静音段 → 语义分级（见 §3）。
- **M2/M3** 设置范围收紧（abs_floor_db 上界 -10；max_segment_sec 上界 30 对齐 MAX_TRANSCRIBE_SEC）。
- **M4** `_stop_evt.clear()` 复活僵尸线程 → 每会话新 Event。
- **M5** 决策上下文新鲜度 → should_consider 第 6 判（最新 teacher 条目 >300s 拒绝，fail-open）。
- **L1/L3/L4/L5/L7** ReleaseBuffer 兜底 / stats 迭代快照 / 落盘去锁 / TTS base64 异常包装 / S_FALSE 配对 CoUninitialize。
- 接受风险（记录在案）：L2 慢 COM 初始化 start 误报（存疑待证）、L6 并发 evaluate 双重触发（单调度线程假设）、L4 注释口径（push_manual 也写盘，append 原子性实践够用）。

## 本轮实现方案（2026-09-07 round 56：课堂 AI 教师联动层 + 设置接线）

### 1. ClassroomCoach（core/classroom_coach.py，新文件）
```
ClassroomCoach(QObject)
  __init__(monitor, engine, overlay, tts_player)     # 依赖注入，全部可 mock
  connect(): engine.interrupt_triggered -> _on_trigger
             tts.stateChanged -> _on_tts_state       # playing 补防护窗（M4）
             _start_hide_hotkey()                    # Ctrl+Alt+H 关卡片
  _on_trigger(payload):                              # payload {source, reason_type, reason, teach_point, speak}
    1) 重入守卫 busy_until（speak=False 的 AI 决策仍板书不朗读）
    2) 卡片先行：_draw_card_header（右上角局部卡片 + "讲解词生成中…"灰字）
    3) _clear_timer 30s 自动清除（重入先停后启）
    4) 讲解词线程（QThread + _SpeechWorker，chat_stream 流式）：
       partial(节流 0.4s/12字) -> _on_speech_partial 正文逐句上板
       成功 ready -> _finish_pipeline：终稿上板 + suppress_teacher + TTSPlayer.speak()
       失败 failed -> 正文直接用 teach_point，照常朗读（fail-safe，绝不沉默放弃）
    5) suppress_teacher：合成前估窗 + playing 补窗 + idle 收尾（取 max 语义）
```
- 讲解词 prompt：system=「你是网课 AI 教师，用口语化中文把要点讲给学生听，40~80 字，只输出讲解词本身」；user=主题/屏幕上下文/课堂文字流（复用 engine.build_prompt 产物，截尾防超长）+ 要补讲的点与理由。`ai_client.chat` 走 `resolve_dialog_target()`（v4-pro 推理任务）。
- 屏幕尺寸：`QGuiApplication.primaryScreen().geometry()` 实时取，兜底 1920x1080；文本换行按屏宽 0.72 手动折行（write_text 无自动换行）。

### 2. suppress_teacher（core/classroom_stream.py 追加）
- `suppress_teacher(seconds)`：置 `self._teacher_suppressed_until = now + max(1, seconds)`；窗口内 `_on_audio(speaker='teacher')` 直接 return（PCM 不进 VAD）；窗口过期后首次回调时 `vad.reset()`（丢前置缓冲，防上一窗口残帧混入）。
- 与看门狗/flush 正交：只影响 teacher 通道入口；`stop()` 时窗口一并失效（`_running=False` 守卫已覆盖）。
- `stats()` 增加 `teacher_suppressed_remaining`（诊断可见）。

### 3. main.py 接线（跟随 tray 初始化之后）
```python
if ENABLE_CLASSROOM_AUDIO and cfg.settings.classroom_audio_enabled:
    try:
        monitor = ClassroomMonitor(); res = monitor.start()
        engine = InterruptEngine(monitor=monitor)
        overlay = ScreenPaintOverlay()
        coach = ClassroomCoach(monitor, engine, overlay, TTSPlayer())
        coach.connect()
        if res.get("ok"): show_toast("课堂 AI 教师已开启", "发现讲错/跳步会补讲，静音热键可随时让 AI 闭嘴")
        else: show_toast("课堂感知降级", "音频通道启动失败，仅保留手动提问", "warning")
    except Exception as e:
        append_err_record("main.py", "课堂感知初始化失败", str(e))   # 不阻断启动
app.aboutToQuit.connect(_shutdown_classroom)   # coach/engine/monitor/overlay 逆序清理
```
- `engine.start_polling()`（若 InterruptEngine 有轮询定时器接口则用之；无则 coach 侧 QTimer 按 interrupt_decision_interval_sec 轮询 evaluate()——以实际接口为准，接线前确认）。

### 4. ClassroomTab（ui/settings_view.py，仿 SystemTab 模式）
- 控件：`classroom_audio_enabled`（QCheckBox）、`classroom_capture_loopback` / `classroom_capture_mic`（QCheckBox）、`interrupt_level`（QComboBox：从不打断=off / 讲错或跳步=on_error / 讲不清楚也打断=on_unclear）、`interrupt_cooldown_sec`（QSpinBox 30~1800 s）、`interrupt_confidence_min`（QDoubleSpinBox 0~1 step 0.05）、`interrupt_speak`（QCheckBox）。
- 提示行：总开关/通道开关"改后需重启应用生效"；分级/冷却/置信度/朗读"保存即生效"。
- `_load()` / `collect()` 命名严格对齐 settings 字段；`_save_all` 的 ConfigManager.update 通道自动持久化。
- 注册：`self._add_tab(ClassroomTab(), "课堂感知")`（"屏幕检测"之后）。

### 5. 测试与验证
- `test_r56_coach.py`（离线，全 mock）：信号→卡片指令序列断言（无 open_page/标题/正文折行/字号钳制/热键提示/clear 顺序）；讲解词失败 fail-safe 回退 teach_point；suppress_teacher 窗口内 teacher 段丢弃 + 过期标志位由采集线程消费；speak=False 不朗读仍板书；重入取消旧清除定时器；设置页 collect() 往返（QApplication offscreen）。**终态 57 项全绿**。
- `demo_coach.py`（实机）：真实三件套 + `engine.request_teaching(...)` → 卡片秒出 + 流式讲解词 + TTS 出声 + 30s 自动清除 + 防护窗三段日志。**终态 5/5 PASS**。
- 子 Agent 审查（verifier）→ 全部修复（见 §8）→ 归档。

### 8. round 56 审查修复清单（verifier 审查 + 实机复现双通道）
- **跨线程 UB ×2（实机 demo 复现"QObject: Cannot create children"）**：`signal.connect(闭包)` 无 receiver = direct connection，槽在 emit 线程执行。coach `_on_ready/_on_failed` 与 **tts_player r53 遗留**同模式 → 全部改绑定方法（queued 回主线程）+ request_id 双参信号 + `_pending` 状态。教训与 FreqErr「信号生命周期」同源——**worker 信号永远连绑定方法**。
- **C1 evaluate 阻塞主线程（Critical）**：QTimer 轮询 → evaluate 内 ai_client.chat 同步网络（v4-pro 5~60s）→ UI 每 45s 冻结一次。修复：决策挪 daemon 工作线程（`_eval_running` 标志防堆积）；engine 信号绑定方法连接自动 queued，信号侧零改动。
- **H2 chat_stream 连接泄漏**：stream=True 无 close，正常/[DONE] break/中途异常三路径全泄漏 → try/finally resp.close()；read-timeout 60→120s（v4-pro 首 chunk 慢，L5）。
- **M3 suppress 竞态**：主线程写/采集线程读 `_teacher_suppressed_until` 无锁 → 持 monitor 锁；过期 VAD reset 改 `_vad_reset_needed` 标志位由**采集线程**在 feed 前执行（VAD 内部无锁，reset 与 feed 必须同线程串行）。
- **M4 防护窗不含合成时延**：est=len/4+2 从合成前起算 → 订阅 TTSPlayer.stateChanged，playing 按词长补窗、idle 补 2s 收尾（suppress_teacher 取 max 语义天然支持多次开窗；实机日志三段窗口 16.5s/17.5s/2.0s 验证）。
- **L6** docstring 残句；**demo 纪律**：QWidget 必须 QApplication（QCoreApplication 直接崩）；**实机 demo 出声/动屏幕前必须先告知用户**（r56 用户实测反馈）。
- 已核查安全：pynput invokeMethod bridge 链（实测通过）；GlobalHotKeys ×3 并存；_speech_thread wait(1500) 后弃引用不炸；partial 保序；monitor.start 失败时 evaluate 被 need_segments 闸门拒绝零副作用；ClassroomTab 字段与 settings 白名单精确对齐；SSE 解析对 reasoning_content 增量/残行容错。

### 9. round 56 实现要点（联动层，体验反馈定稿版）
- 形态：**右上角局部卡片**（比例坐标 X/Y/W=0.54/0.03/0.44，含标题栏+正文区+底部热键提示），不用 open_page 全屏；30s 自动清除 + Ctrl+Alt+H 手动关。
- `chat_stream(messages, ..., on_delta)`：SSE `data:{...}` 行协议，on_delta 收**累积**全文；keep-alive/残行/`reasoning_content` 增量全部忽略不中断流；空全文抛 AICallError（与 chat 同契约）。
- 字号 `_clamp_px` 双向钳制（标题 16-26px、正文 13-19px）；`_wrap_text` 标点优先断行；正文行数 = 卡片高度/行高。
- partial 节流：worker 内 0.4s/12 字阈值（threading.Lock 保护）；partial 只上板不出声，ready 终稿才 suppress+TTS；fail-safe：chat_stream 异常/空 → 直读 teach_point。
- 决策轮询：QTimer（主线程）只负责触发，evaluate 在 daemon 工作线程执行（C1）；`_eval_running` 防堆积；间隔实时重读设置。
- 关闭热键：pynput GlobalHotKeys → `_HotkeyBridge`（回调构造注入 + @Slot）→ `QMetaObject.invokeMethod(Qt.QueuedConnection)` marshal 回主线程；bridge 由 coach 强引用防 GC。
- 重入守卫：`_busy_until = now+90`（讲解词线程挂死兜底）；`_clear_timer` 重入先停后启。

## 本轮实现方案（2026-09-07 round 57：成果互通——课堂笔记同步学习 Agent）

### 1. ClassroomSync（core/classroom_sync.py，新文件）
- 数据源：monitor.recent(400)（转写滚动窗）+ 自身 `_teach_log`（coach 经 `teach_logger` 回调登记——**回调注入而非读 coach 属性**，审查 Critical 教训见 §4）。
- 组装：entries 与 teaches 按 ts 归并排序 → Markdown 时间线（`**[老师]/[学生]**` 逐条 + `> 🤖 AI 补讲` 块）；标题 `课堂笔记 M月D日 HH:MM`；标签按内容提炼（课堂提问/课堂记录）；content 截 100KB（服务端上限 500KB）。
- 上传：`POST {sync_server_url}/api/notes`，body 严格匹配 NoteCreateRequest（服务端 pydantic 未设 extra → 默认 ignore）；返回 `{"id","message"}`。
- **增量水位**：`data/classroom/sync_state.json`（tmp+os.replace 原子写）记 last_synced_ts，成功才推进——服务端无去重，客户端幂等。
- **滚动窗口漏传兜底（审查 High）**：recent(400) 窗口滑动快于同步间隔时早期条目被挤出——检测"窗口最老 ts > 水位"即回读当日+前日 jsonl（round 55 既有落盘）补漏合并（按 ts 去重）；落盘关闭时记警告（该场景无兜底）。
- 触发：QTimer 定时（默认 30min，_INT_RANGES 5-720 钳制）+ 手动；daemon Thread 执行（`_syncing` 防重叠，start 失败复位标志）；网络失败记 Err 不推进水位（下次重试）。

### 2. 接线与设置
- main.py：`classroom_sync_enabled` 开（默认关）时创建 sync、`coach.teach_logger = sync.add_teach`、start()；aboutToQuit 清理。
- ClassroomTab：同步开关/服务器地址/间隔 3 控件；隐私提示（只传文字不传音频；开关与间隔重启生效）。

### 3. 审查修复清单（verifier VERDICT: FAIL → 修复后全绿）
- **Critical 补讲日志链路断裂**：`_collect_teaches` 读 `self._coach._teach_log`——真实 coach 无此属性（数据在 sync 自身），getattr 永远回退空，补讲记录永不上传。测试被 FakeCoach 自带 `_teach_log` 掩盖（mock 与现实脱节）。修复：读自身 `_teach_log` + 真 coach 对象回归锁。
- **High 滚动窗口漏传**：见 §1 兜底机制。
- **M** interval 误放 `_FLOAT_RANGES`（归一化永不执行、脏值透传致同步静默死亡）→ 移入 `_INT_RANGES` + int_fields；课堂中开关关→开不生效 → UI 提示补充。
- **L** note_id 提取路径（服务端返回顶层 id）；Thread.start 失败复位 `_syncing`；水位坏档重传窗口（接受：重复一篇合并笔记优于漏传）。
- 测试夹具教训：**夹具 ts 必须用过去时间**（未来 ts 在 max() 字符串比较中压过当前时间，水位断言假失败）。

