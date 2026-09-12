# 技术方案文档

> 本文只记录当前有效的实现方法和技术边界。架构见 [Design.md](Design.md)，安装、启动和操作见 [README.md](README.md)。

## 1. 运行时与目录

- Python 3.11+，FastAPI + Uvicorn。
- SQLAlchemy 2.0 async + aiosqlite，单机 SQLite。
- Jinja2 + 原生 JavaScript/CSS，Markdown、KaTeX 和 SVG 在浏览器端展示。
- AI HTTP 调用使用 httpx；文档读取使用 python-docx、pypdf、BeautifulSoup、lxml 和 markdown。
- 入口：`backend/main.py`；配置：`backend/config.py` 与 `backend/settings.json`；依赖：`backend/requirements.txt`。
- 数据：`backend/storage/`；题目文件固定在 `storage/questions/{question_id}/`，试卷和导出文件存入各自目录。

服务启动时执行数据库初始化与轻量迁移。每个 SQLite 连接设置 `foreign_keys=ON`、WAL、忙等待和同步级别。迁移通过 `PRAGMA table_info` 判断字段是否存在，再执行明确的 `ALTER TABLE`；每一步记录日志，任何 `failed:` 结果都会让启动失败。启动修复会把历史 `processing_tasks`、`corrections` 中已不存在的可空外键置空，保留任务与批改历史。旧数据库中的可选 JSON 字段在读取时提供安全默认值。

## 1.1 技术栈

| 技术 | 用途 | 说明 |
|------|------|------|
| Python 3.11+ | 运行时 | 主要开发语言 |
| FastAPI + Uvicorn | Web 框架 | 异步 HTTP 服务，自动 OpenAPI 文档 |
| SQLAlchemy 2.0 + aiosqlite | ORM | 异步数据库操作，单机 SQLite |
| Jinja2 | 模板引擎 | 服务端渲染 HTML 页面 |
| 原生 JavaScript/CSS | 前端 | 无框架依赖，轻量化实现 |
| httpx | HTTP 客户端 | AI 模型 API 调用（含小米 MiMo 网关） |
| python-docx, pypdf, BeautifulSoup, lxml | 文档处理 | 笔记解析与结构化 |
| markdown | Markdown 渲染 | 笔记与文档渲染 |

**依赖文件**：`backend/requirements.txt`

**配置文件**：`backend/config.py`（默认设置）、`backend/settings.json`（用户设置）

**数据目录**：`backend/storage/`（数据库、题目、试卷、笔记、会话等）

## 2. 配置、模型与模块开关

`backend/config.py` 保存默认设置和模块开关，设置页通过 `backend/routers/settings.py` 读写。API Key 返回前掩码；空白 Key 表示保留原值。设置导入只接受默认键白名单。

`load_settings()` 的外部 Key 兜底顺序：项目 `settings.json` 中已显式保存的 DeepSeek/Kimi Key 优先生效；仅当该键为空时才尝试从工作区外层共享 `api_keys.py`（api.txt）读取兜底值。共享文件本身不随本项目修改，其他项目不受影响。

当前模型分工：

- `glm-4v-flash`：视觉 OCR、图片理解和参考 SVG；不可用时按配置回退。
- `glm-4-flash` 或配置文本模型：轻量分类、笔记结构化等低成本任务。
- DeepSeek 配置模型：复杂解题、答案审查、组卷和排版修复。
- Kimi：备用视觉/文本模型；该系列调用固定 `temperature=1`。
- 小米 MiMo（token plan CN 网关）：
  - `mimo-v2.5`（视觉链路备用）：位于 `_call_vision_model_async` 与表情分析回退链中智谱之后、Kimi 之前；OCR 主链在智谱两次尝试失败后、Kimi 尝试前插入一次小米尝试。配置键为 `xiaomi_token_plan_api_key` / `xiaomi_token_plan_base_url` / `xiaomi_vision_model`。
  - `mimo-v2.5-tts`（语音合成）：专用于 `/api/tts` 配音端点；配置键为 `xiaomi_tts_model` / `xiaomi_tts_voice` / `focus_voice_engine`。
- `custom_apis`：按 scope 配置前置替换或单个 fallback，实际调用前由 AI 服务重新加载设置。

开关主要包括 `ENABLE_STRUCTURE_GRAPH`、`ENABLE_COMPARISON_MODE`、`ENABLE_TAG_UNIFICATION`、`ENABLE_NOTE_REFERENCES`、`ENABLE_WORKSHEET`、`ENABLE_CALCULATOR`、`ENABLE_SEARCH`、`ENABLE_CORRECT`、`ENABLE_FOCUS_MODE`、`ENABLE_XIAOMI_TTS`，以及组件内部的 `ENABLE_SEMANTIC`、`ENABLE_LIQUID`。开关关闭时，路由、导航、首页组件和服务调用都应一起隐藏。

## 3. 后台任务与题目状态

上传阶段先安全写文件和数据库，再创建后台任务。应用持有后台任务引用，避免 `asyncio.create_task` 被回收；服务重启时根据 `ProcessingTask` 和题目状态恢复未完成任务。

同一 `question_id` 使用 `asyncio.Lock` 串行处理，锁表可使用弱引用释放已完成键。加锁必须覆盖真正的 I/O 与状态修改，不能在异步端点上使用同步锁装饰器。

状态更新顺序：

1. 路由将题目从 `staged` 写为 `pending`，持久化任务描述。
2. OCR 服务进入 `processing`，先写入 OCR 和参考图状态。
3. 解题前切为 `generating_solution`。
4. 完整性校验通过后，题目与 `ProcessingTask` 一起进入成功态。
5. 任一步异常时同步写 `error`、安全错误摘要和 `Err.log`。

重试参数 `skip_ocr` 只在 OCR 文本和参考图资产有效时复用；文件缺失或参考图失败时重新执行对应阶段。

## 4. 上传模式与文件安全

统一模式由后端归一函数处理：`one_per_image`、`one_question_multi_image`、`auto_split`。兼容旧 `split_mode`，但新字段优先。`UploadSession.upload_mode` 在首次落题后锁定，冲突请求返回 409。

处理差异：

- 单图单题与自动分题均逐文件暂存；差别只发生在 process 阶段。
- 多图一题使用 `upload-multi` 合并到一个题目，`image_roles` 区分 `question / solution / answer / other`。
- 暂存图片仅在 `staged` 时允许删除；删除后服务端重编号文件，并返回新的 `image_roles`、`image_urls` 和主图路径，前端不猜测路径。
- 自动分题得到独立子题文本后，后续解题复用子题 OCR，不重复对整页执行完整 OCR。

文件前后端都校验格式、空文件、10MB 单文件上限和批量数量；安全边界以后端 MIME、扩展名、签名和受控目录校验为准。允许的题目 ID 格式为 `^[a-zA-Z0-9_-]{1,64}$`。

拍照搜题与拍照检查另使用统一 `capture_mode`：`single_question`、`single_page`、`whole_paper`，兼容旧 `single/page/paper` 别名。每次最多 12 张、单张 10MB、合计 60MB。根临时题保存 `capture_group_id` 和有序 `multi_images`；拆出的子题沿用同一分组与 `capture_index`，正式题库候选查询始终排除 `search_query/correction_query`。

## 5. OCR、参考 SVG 与解题

核心实现在 `backend/services/ocr_service.py`、`ai_service.py` 和 `diagram_service.py`。

### 5.1 OCR 输出

视觉模型输出题面文本、手写提示、学科/年级/知识点、图形描述和多图角色。手写内容保存在提示字段，只辅助解题，不直接混入题面。

AI JSON 统一经过代码围栏剥离、首个 JSON 提取、字符串换行修复和字段默认值处理。截断或无法修复时记录原始长度和安全摘要，进入失败/重试，不拼凑伪数据。

### 5.2 参考 SVG 先行

带图题在解题前调用视觉模型生成单一 SVG 或 `NO_DIAGRAM`。多图一题只把 `question` 角色图片作为原题事实输入；解析图和答案图仅作提示。

参考图保存流程：

1. 限制模型响应长度并提取首个完整 `<svg>`。
2. 执行 SVG 消毒。
3. XML 解析根节点，校验标签、属性、最大 100KB 和最大 10000×10000 viewBox。
4. 固定写入 `storage/questions/{id}/reference.svg`。
5. 更新 `reference_svg_status/path/error`。

参考图和 `diagram_N.svg` 共用受控 URL 正则，但磁盘读取必须先移除 `/storage/` 前缀再拼接 `STORAGE_DIR`。不受控路径不得用于读取，也不得写入 `<img src>`。

### 5.3 解题与审查

解题模型接收 OCR、用户可选提示、图形描述和参考 SVG。首轮必须一次生成题面、标准答案、详细解析、考场可判分的得分点和必要补充图。物理、化学计算应检查常数与常识；必要限制需补入题面。

存在参考图时，Prompt 不再要求生成题面图；后端强制 `diagrams[0].source=ocr_reference`、`diagram_places[0]=question`，新增图只能放 `answer`。`diagrams` 与 `diagram_places` 始终等长。

审查模型读取完整结构化结果，检查题意、答案、得分点、过程、格式和图形一致性。自动重写最多两轮；未确认正确的版本保留供夜间巡检，用户锁定题不覆盖。

## 6. SVG、结构图与数学渲染

### 6.1 SVG 消毒

`diagram_service._sanitize_svg` 先 HTML 解码，再移除 `script`、`foreignObject`、`use`、`image`、SMIL 动画、外部资源、内联 style、所有 `on*` 事件和 `javascript:/data:` 等协议。文本做 HTML 转义；颜色、线型、数值和几何范围使用白名单。

前端 HTML 消毒统一为单一实现：`app.js _sanitizeHtml` 是全站唯一分层消毒器（危险标签含 `meta/link/base/marquee/animate/set/foreignObject`，属性值先剥控制字符再测协议，封 `java&#10;script:` 绕过，含 `xlink:href`）；`questions.js` 对比模式的 `_cmpSanitizeHtml` 已改为委托复用，禁止再新增独立消毒函数（详见 §11.2）。

### 6.2 结构梳理图

`structure_graph_service.py` 接受 `nodes + edges`：

- 校验节点 ID、类型、文字、边端点和自环。
- 去除环后分层，但保留有效跨层边。
- 同层排序与重叠消解后，为跨层边分配左右外侧车道。
- 连线路径和箭头也纳入 viewBox。
- 连线先输出、节点后输出，避免线穿文字。
- SVG 节点带语义 class 与 `data-node-type`，颜色使用全局 CSS 变量。
- LaTeX 常用符号转 Unicode；普通文本转义后再进入 SVG。

前端的 fit 使用 SVG viewBox 和容器矩形计算，缩放范围 `0.002–4`。卡片高度由宽高比推导并限制在可视高度内；`ResizeObserver` 只观察当前结构图区。单指平移和双指缩放共享 `scale/x/y`，双指按世界坐标保持缩放中心。

### 6.3 函数与坐标图

函数图使用结构化 spec：`function_expr`、可选 x/y 范围和点、线、圆、多边形、文字。表达式通过 AST 白名单，只允许变量 `x`、安全数学函数和允许的运算符；禁止任意属性、导入和内省。

渲染器自动选择范围、采样有限值、绘制坐标轴/刻度/曲线/辅助元素。表达式无效、没有有限采样值或渲染异常时返回失败，由上层回退或明确提示；禁止生成“渲染失败”占位 SVG 并把它当成题图成功保存。`function_curve` 组件另支持 `a/b/c/k` 参数和 `y=|ax²+bx+c|` 与 `y=k` 交点展示。

## 7. 组件拼装与编辑器

核心目录是 `backend/services/diagram_components/`：

- `component_db.py`：组件注册、别名、端口、默认尺寸、层级和渲染函数。
- `assembler.py`：模板/自由组装、参数归一、端口解析、连接线和 viewBox。
- `semantic_rules.py`：场景、多种摆法、端口映射和危险连接黑名单。
- `liquid_render.py`：液面高度、弯月面和预设液体颜色。
- `calibration_service.py`：组合指纹、样本滤波、阈值和原子存储。

端口对齐规则：两端都锁定则保持；一端锁定则移动另一端；都未锁定时移动目标端。端口可携带 gap，保证加热等场景的物理间距。自动 viewBox 同时包含组件、连接线、刻度和文字。

校准按排序后的组件类型生成组合指纹。达到 5 条样本后去掉一个最大值与最小值取平均，以后每增加 5 条更新。JSON 写入使用临时文件和 `os.replace`。

编辑器功能：搜索/分类/拖入、选择与多选、移动、等比例缩放、旋转、层级、锁定、液面和温度参数、语义摆法、校准、图库、SVG 导出、撤销/重做和键盘微调。

交互状态要求：

- 指针按下时保存操作前快照，确实发生变化后才提交撤销栈。
- 快照同时包含组件与比例并查集等关联结构。
- 多选拖动按整体边界限制，锁定组件不移动。
- 高频移动通过 `requestAnimationFrame` 合并重绘；连续属性输入分组撤销并防抖预览。
- 组件数组不因 z 排序而重排，渲染时按 z 生成视图，避免索引关联错位。
- 触屏复用同一拖拽状态机，桌面操作保持可用。

## 8. 组卷、排版与导出

核心实现在 `paper_service.py`、`layout_service.py`、`export_service.py` 和 `routers/papers.py`。

生成前按学科、年级、标签和用户 Prompt 查询候选，批量加载题目避免 N+1。题量边界由服务端预检：自由生成可缩减，固定集合严格报错。

AI 选题只返回候选中的明确 ID。`mode=new` 丢弃旧 `question_ids` 并重新选题；`mode=modify` 保留集合仅重新排版。`SavedConfig(config_type=paper)` 只保存白名单参数，不保存题目 ID。

选题完成后冻结顺序，排版代理不能增删替换题目。卷面 Prompt 不含标准答案；答案页由冻结集合独立生成。生成结果同时缺少卷面或答案时重试一次，仍不完整则不保存。

排版审查最多两轮；外部渲染工具不可用时降级为源码审查。纸张只允许配置白名单中的 A3/A4。Word 依赖或转换失败时只返回实际成功的产物，不保存无效路径。

## 9. 笔记与知识树

`note_service.py` 将图片、TXT、Markdown、DOCX 和 PDF 统一为文本，再进入同一结构化持久化入口。输出字段包括学科、年级、知识点、标题、正文、典型题、来源和引用。

合并前检查学科一致；标题与标签重叠达到阈值时合并正文、标签和来源，否则新建。题目引用功能根据正式题库建立关系，不引用临时搜题记录。

知识树从持久化笔记重建；解析、合并或重建失败保留原内容与错误。夜间巡检执行标签统一、重复检查、重点题复核和临时图目录清理，用户锁定内容优先。

## 10. 搜题与批改

`POST /api/search/upload` 与 `POST /api/correction/upload` 同时兼容旧 `file` 和新 `files` 表单字段。单题模式把有序多图作为一个 OCR 上下文；单页与整卷在 OCR 后调用语义拆题，并把 `split_question_ids` 写入根任务结果。搜题逐个检索正式 `Question`；候选使用服务端统一置信阈值，低置信度不自动绑定。

单题批改读取匹配题的标答、解析和得分点；单页通过 `/api/correction/batch` 对拆出的题目逐项处理，单题失败不会中断整页；整卷必须选择 `Paper`，`POST /api/correction/paper/{paper_id}` 接受多页 `files`，先合并 OCR，再按题号与语义分段映射到冻结的 `Paper.question_ids`。每个得分点记录命中、得分、原因和建议，最终保存 `Correction` 历史，类型区分 `single/page/paper`。

`GET /api/papers/library` 聚合 `Paper` 与尚未对应有效 `Paper` 的 `UploadSession`。会话状态由题目实际状态批量计算为 `open/processing/ready/attention/done`；若 `paper_id` 已对应列表中的试卷则去重。静态 `/library` 和 `/batch` 路由必须位于动态 `/{id}` 之前。

前端上传前校验文件，所有异步阶段使用统一加载和错误状态；公式区域在写入内容后单独触发 KaTeX。路由中 `/history` 等静态路径必须注册在 `/{id}` 之前。

## 10.5 Agent 会话管理与工具调用展示

### 空对话自动清理

- 函数 `_cleanup_empty_sessions(max_age_seconds=600)` 在每次列出会话时触发。
- 仅删除修改时间超过 10 分钟且 `messages` 为空的会话文件。
- 异常会话（文件损坏、非 JSON 对象）跳过不误删。

### 工具调用捕获与展示

- 后端通过 `_tool_call_stores: dict[str, list[dict]]` 按 session_id 存储工具调用记录。
- 函数 `agent_tool_calls_clear/get/add` 管理工具调用生命周期。
- 会话聊天过程中关键操作（搜索、解题、新增题目、修改题目、查看错误、笔记操作）均记录工具调用。
- 响应中 `tool_calls` 字段返回本次对话的所有工具调用。
- 前端 `agent.html` 在执行步骤下方显示工具调用卡片：
  - 每组工具调用显示名称、状态（完成/失败）、时间。
  - 点击可展开查看参数（JSON 格式化）和结果。
  - 支持展开/折叠，避免信息过载。

## 11. 前端基础设施

### 11.1 CSS 设计系统

`backend/static/css/theme.css` 是唯一的全局样式入口，包含：

- CSS 变量（主题色、状态色、阴影、渐变、动画曲线、焦点环）。
- 基础组件（布局、卡片、按钮、表单、表格、标签、徽章、弹窗、Tab、开关、聊天面板、滚动条、骨架屏、空状态、加载条、Widget 网格、名言卡片）。
- 增强组件（英雄卡片、毛玻璃面板、渐变徽标、骨架网格、增强空状态、分节标题、渐变进度环、渐变提示条、浮动标签输入组、可点击卡片、渐变分隔线、UX 进度条）——原 `enhanced-ui.css` 内容已合并至此。
- 暗色模式覆盖（`[data-theme="dark"]`）、响应式补丁（768px/480px 断点）、触摸设备适配、快速模式、无障碍降级和打印兜底。

`animations.css` 独立维护动画层，依赖 `<html class="js">` 武装机制：无 JS 或 `motion.js` 未加载时所有内容默认可见。`base.html` 不再内联组件 CSS。

### 11.2 全局 JS

`backend/static/js/app.js` 提供：

- `$API`：封装 fetch，自动 JSON 解析、`_friendlyStatus` 状态码映射（401/403/404/409/429/5xx→中文）、网络层失败也映射为"网络异常，请检查连接"、全局请求进度。
- `$toast(msg, type)`：统一通知，最多 4 条，自动消失。
- `$confirm(msg, opts)`：Promise 风格确认弹窗，支持危险操作和键盘操作；增强时跳过自动焦点迁移，由弹窗自己 `ok.focus()`。
- `$prompt(msg, opts)`：Promise 风格输入弹窗，替代浏览器原生 `prompt()`，与 `$confirm` 风格一致。
- `$md(text, element)`：Markdown + LaTeX + Mermaid 渲染，内置 HTML 消毒（`_sanitizeHtml` 是全站唯一实现：危险标签/事件属性/`srcdoc`/危险协议含 `xlink:href` 全覆盖；questions.js 对比模式已委托复用）。
- `$esc(str)`：全局 HTML 转义函数，替代各页面重复定义。
- `$fmt(time)`、`$status(s)`、`$type(t)`、`$busy(btn, on, label)`：格式化和状态工具。
- `_closeIconSvg()`：统一关闭按钮 SVG（替代 `&times;`），所有 `<button class="modal-close">` 引用此 SVG 并带 `aria-label="关闭"`。
- 主题切换（亮/暗/自动）、侧栏控制（持久化 `localStorage.sb_user_closed`）、全局交互协调器（焦点、Esc、页面切换进度、表单校验抖动）。
- 前端错误自动上报到 `/api/log-frontend-error`。
- 全局聊天面板（`toggleGlobalChat`/`sendGlobalChat`，含 `jump_paper`/`tool_need` action 渲染）也在 `app.js` 内实现，被 `base.html` 引用；项目中不存在独立的 chat.js 文件。

### 11.3 响应式与主题

响应式只维护一套 DOM：桌面主区显式使用 `width:calc(100% - 200px)`，侧栏收起和 768px 以下使用 `width:100%`，避免 `container-type:inline-size` 造成最小内容塌缩。内容区用 auto-fit/auto-fill、弹性基础宽度和容器查询重排；仅 480px 以下采用必要的完全单列。表格可横向滚动，弹窗使用动态视口高度和安全区。

动态 HTML 和 SVG 使用 `var(--accent)`、`var(--text*)`、`var(--ok/warn/err)` 等语义变量；设置页色板预览和黑白打印画布允许具体色值。实验图编辑区保持米白背景和原始元件色。

### 11.4 页面组件

首页组件由 `config_service.HOME_WIDGET_REGISTRY` 提供。加载时区分"配置缺失"和合法 `[]`；保存时过滤未知、关闭和重复 ID，并把服务端回传布局作为最终状态。拖拽之外提供前移/后移，保证键盘和触屏可操作。

实验图编辑器元件列表使用三列受约束网格（预览、中文名称、英文类型），不再用浮动元素排列英文标识。名称与类型列都允许收缩，超长内容在本行省略并通过 `title` 保留完整值，避免覆盖相邻条目。画布嵌入后端 SVG 时按完整 `viewBox(min-x, min-y, width, height)` 计算居中缩放和平移，不能只应用 `scale`；SVG `<text>` 不会因为字符串含换行符自动换行，需显式使用 `tspan` 才能实现多行文字。

### 11.6 离线增强层（`ENABLE_OFFLINE_CACHE`，offline.js）

- **为什么不走 Service Worker**：SW 要求安全上下文，本项目通过 `http://IP:8000` 访问（局域网/公网 IP），SW 直接不可用。离线层因此实现在 `$API._request` 的请求封装层（`window._offline`，offline.js 先于 app.js 加载），HTTP 明文环境同样生效。升级到 HTTPS 域名后可平滑替换为 SW 方案。
- **IndexedDB**（库名 `la_offline`）：store `cache`（key=url，存 {status, body, ctype, ts}）+ store `queue`（autoIncrement，存 {url, method, headers, entries（FormData entries，File 可结构化克隆）, ts, retries}）。
- **GET 缓存白名单**（前缀匹配）：`/api/questions`、`/api/papers`、`/api/notes`、`/api/banks`、`/api/profile`、`/api/daily-quote`、`/api/system-messages`、`/api/knowledge-graph`。命中策略：在线 network-first（成功则回写缓存）；离线 cache-first（响应注入 `X-Offline-Cache: 1` 头供 UI 辨识）。缓存条目 >200 按 ts LRU 清理。
- **上传队列白名单**（POST/PUT，离线入队）：`/api/ocr/upload`、`/api/ocr/upload-multi`、`/api/ocr/session/{id}/upload`、`/api/search/upload`、`/api/correction/upload`、`/api/notes/upload-images`。FormData 按 entries 序列化进 IDB（File/Blob 可克隆），重放时重建。非白名单写操作离线时直接抛"当前离线，请联网后重试"。
- **重放**：`online` 事件 + 启动时 + 60s 定时器（仅当队列非空）；按序重放，成功删除队列项并 toast"离线上传已同步"；失败 retries+1 保留，retries≥5 停止自动重试等待手动。
- **调用方语义**：入队后 `$API` 抛 `Error('已加入离线队列，联网后自动上传')`——页面 catch 按普通错误 toast，无需各自适配。
- **测试**：node --check + TestClient 校验 manifest/页面注入；IndexedDB 行为依赖浏览器，回归靠手动（离线断网→拍照→恢复联网→自动上传）。

### 11.7 前端安全纪律

- 用户输入、AI 文本和动态数据在写入 `innerHTML` 前必须经过 `$esc` 转义。
- Markdown 渲染由 `$md` 统一消毒，移除危险标签、事件属性和危险协议。
- 按钮异步操作必须使用 `$busy` 防重入。
- 所有 `catch` 分支必须向用户显示可操作错误（toast 或内联错误），不得静默吞错。

## 12. 安全、并发与错误处理

- CORS 只允许配置中的合法 origin；拒绝 `*`、userinfo 和带 path 的值。
- CSP 限制脚本、样式、字体、图片和连接来源；CDN SRI 值只能取自官方数据，不能猜测。
- 所有 ID 进入路径或查询前做格式白名单；所有 URL 在输出前再次约束。
- 所有 JSON 配置和图库数据原子写入；并发 CRUD 使用 `asyncio.Lock`。
- `/storage/{path}` 由白名单文件响应路由提供：只接受受控根目录、受控扩展名和无路径穿越的已存在文件；`app.db`、日志、会话、任务状态、配置及 JSON 数据不对浏览器公开。`questions` 分支在返回文件前额外查库，`search_query/correction_query` 临时记录（学生作答图）一律 404，隐私数据不靠“随机 ID 不可猜”。
- 用户输入、AI 文本和错误消息均不直接拼入 HTML、SVG 属性或日志格式串。AI 返回的合法 JSON 非对象、非字符串字段和非有限数值（NaN/Inf）在持久化或读取前归一、拒绝或回退；组件渲染参数（liquid/water_level/angle 等）在 `normalize_spec` 与 `render-component` 两处统一数值/布尔归一，label 在 `_render_component` 入口统一转义（组件库内不二次转义）。
- 用户输入进入 SQL LIKE/ILIKE 前统一转义 `% _ \` 并传 `escape="\\"`；组卷 Prompt 模板使用“先插用户内容、哨兵占位回填”的两阶段替换，避免已插入内容被二次替换。
- 服务异常写结构化应用日志和 `Err.log`；前端只显示安全摘要，密钥和完整模型响应不外泄。
- AI 依赖端点统一失败映射：认证类（401/authentication/api key/illegal header）返回 503，其他模型调用失败返回 502；只有确定性程序内部故障返回 500。无 Key 场景属于可测试的必跑回归项。
- `questions.py` 的 pending 文件读写、`ocr.py` 的暂存目录删除与主图复制都必须先做题目 ID 白名单和 `realpath/commonpath` 边界校验；笔记下载文件名清洗控制字符和引号后再写入 `Content-Disposition`。上传/批改/搜题的“建目录+写文件+入库+提交”整体一个 try，失败回滚并清理目录，不留孤儿文件。
- 可写数据目录统一从 `config` 派生：`audit_service` 的 `SYSTEM_MSG_PATH`、`gallery` 的 `GALLERY_DIR`、`calibration_service` 的 `CALIBRATION_DIR`、`user_profile` 的 `PROFILE_FILE`、`knowledge` 的 `_knowledge_dir()`、`tag_unification_service` 的 `_tag_file()` 均跟随 `STORAGE_DIR/SETTINGS_FILE`，测试替换 config 后自动隔离。
- AI 自定义 API（`custom_apis`）构建 `fallback_config` 与 `custom_scope_map` 时，只有 key 没有 url 的条目一律警告并跳过，不留空 URL 映射（否则每次回退/调用都先打空 URL 报 InvalidURL）；配置字段读取统一 `(x.get("k") or "")`，JSON 显式 null 时 `.get` 默认值不生效返回 None，直接链 `.strip()` 会崩掉 `_reload()` 单例构造。
- 前端 `$md` 的 `_sanitizeHtml` 分层消毒覆盖危险标签、`on*` 事件属性、`srcdoc`、危险协议 URL（含 `xlink:href`）；任何新增渲染 AI/用户 HTML 的路径必须复用该实现，不得另起独立消毒函数（分叉的 `questions.js _cmpSanitizeHtml` 为既有限制，待统一）。

## 13. 测试与验收

根目录 `pytest.ini` 限制测试发现范围并排除 `backups/`，避免历史副本同名导入。常用检查：

```powershell
python -m pytest -q
python -m compileall backend
```

关键回归矩阵：

- 三种上传模式、会话模式锁定、暂存图删除与重编号。
- 参考 SVG 提取、`NO_DIAGRAM`、恶意标签拒绝、路径白名单和解题上下文。
- 数据库旧库迁移、任务状态同步、并发重试。
- 组卷容量、new/modify、答案完整性、A3/A4 和导出失败回退。
- 笔记多格式解析、跨学科不合并、知识树重建。
- 拍照三模式、多图上限、旧 `file` 字段、临时记录隔离、置信阈值、单题/单页/整卷批改。
- 整卷上传记录聚合、`ready/attention` 状态、已生成试卷去重和静态路由顺序。
- 首页空布局、组件去重、模块开关、主题变量扫描。
- 结构图跨层边、viewBox、桌面与 390px 窄屏 fit、拖拽/滚轮/双指状态。
- 活动模板内联 JavaScript 语法、关键页面和读取 API 的 200 响应、控制台无错误。

最近一次完整基线为 72 项测试通过（含 `backend/test_endpoint_contract.py` 端点契约测试与新增的 `backend/test_extreme_boundaries.py` 极端边界/无 Key AI 矩阵测试）；同时完成 Python 编译、依赖一致性、SQLite `integrity_check`/`foreign_key_check`、187 个 API 路由无重复/无遮蔽、前端调用面匹配、pending 路径穿越、下载头注入、示意图 spec 端点边界、临时记录静态访问拦截、知识检索意图、真实数据隔离与畸形请求矩阵检查。后续以实际测试输出为准，不把该数字当作固定承诺。

### 13.1 长任务与外部模型验收

测试纪律：`backend/conftest.py` 通过 `DSH_ERR_LOG_PATH` 环境变量把 Err.log 重定向到临时目录（随子进程继承），错误注入用例不得污染真实 `backend/Err.log`；新增测试模块遵循"首导入方生效"惯例——`main` 已在 sys.modules 中则不再重定向 config 目录，且任何自有重定向必须包含 DATABASE_URL，防止 SQLite 与文件系统各指一处。

OCR、自动拆题、拍照搜题、单页拍照检查和 Agent 保存图片在提交数据库前写入 `storage/task_states/{question_id}.json`；任务正常结束或明确失败后清理描述，进程异常退出时由启动流程恢复。恢复只复用已存在的题目 ID、原图、OCR 和拆题结果，不创建重复题。启动清理旧 `ProcessingTask` 运行态只发生在应用生命周期内，通用数据库迁移不会误伤另一个运行实例。

外部模型按能力判断可用性：OCR 需要智谱或 Kimi 任一视觉模型（小米 `mimo-v2.5` 有 Key 时自动插入回退链），解题需要 DeepSeek 或 `solve` 自定义 API。多图角色识别优先智谱，失败后尝试 Kimi；scope 替换模型失败后回退 DeepSeek；主模型在网络、空响应或 token 截断重试耗尽后才使用全局 fallback。Kimi K2.6 固定 `temperature=1`，空内容且由长度终止时提高 token 上限重试。

交付抽检不写入题库，只调用最小请求确认自定义 Qwen、DeepSeek、Kimi、智谱视觉、小米视觉和小米 TTS 均返回非空结果。API 响应不打印 Key，错误日志只保留模型名、状态码和脱敏摘要。

配置导入使用有界根对象，最多 200 个字段且编码后不超过 500KB。URL、模型、时间、token、CORS、自定义 API 和用户画像先完整校验，再统一落盘；跨设置与画像文件写入失败时尝试恢复导入前快照。会话标题也由请求模型限制长度，不接受任意字典绕过校验。

## 14. 接口完整性检修方法

全量检修按以下顺序执行：

1. 路由清单：用 `app.routes` 枚举所有 FastAPI 路由，逐条核对 handler 是否存在且非空壳。
2. 前端调用面：正则提取 `backend/templates/**/*.html` 与 `backend/static/js/*.js` 中的 `/api/*` 字符串，与后端路由表比对，确保无“前端有口、后端无码”。
3. 薄函数扫描：AST 扫描所有 `async def`/`def`，排除 docstring 后 body 长度为 0 或仅 `pass` 的函数视为占位并修复。
4. 冒烟矩阵：用临时 `STORAGE_DIR` + `TestClient` 启动应用，对无参数 GET、无效 ID、极端 limit/offset（负值、超大正数、非数字、NaN/Infinity）、空数组、超长字符串、非法枚举逐项请求，断言 4xx 而非 500；limit/offset 必须两端夹紧。
5. 数据态回归：在临时库中直接播种 `Question/Paper/Note/SavedConfig/UploadSession`，验证 CRUD、banks、prompts、sessions、gallery、paper-configs、home-widgets、diagram 渲染与日志上报路由。
6. AI 依赖端点：monkeypatch `ai_service` 各方法，验证 `/api/chat`、题目聊天、质疑、结构图、对比模式、组卷、搜题、批改等端点状态流转和持久化。
7. 路由顺序自检：同前缀下静态段必须在动态段之前；对 `/{id}` 路径做 ID 格式白名单，保留字（如 history）不会被误捕获。
8. 修复完成后同步跑 `python -m compileall backend`、`python -m pytest -q`、`python -m pip check`。
9. 无 Key 能力矩阵：关闭 AI 重试等待后，逐一请求所有 AI 依赖端点，断言返回 502/503 而不是 500；同时用 fake `_call` 验证 AI 意图 `data:null`、非对象意图、知识检索意图、缺字段和畸形 JSON 不会让 `/api/chat` 崩溃。
10. 数据态隔离复查：端点测试后确认真实 `backend/Err.log`、`user_profile.json`、`data/gallery/presets.json`、`data/gallery/calibration.json`、`knowledge/`、`tag_unification.json` 没有被临时测试样本污染；服务层数据目录只能来自 config 派生。
11. 前端初始化复查：扫描模板顶层 `init/load/setup` 立即调用，确认需要 `$API` 的初始化均在 `DOMContentLoaded` 之后；`app.js` 为 defer 加载的页面不得在解析期直接发请求。
12. 双向端口核对：除“前端调用面 → 路由”外，再核对“路由 → 前端/Agent 消费者”：无消费者的端点要么补前端入口（如图库预设删除、场景库浏览、知识检索卡片、AI 生成笔记、巡检/修复按钮），要么确认其为 Agent/测试专用并记录；前端不得猜测静态路径，文件型数据必须有专用端口（如 `GET /api/diagram/spec/{qid}/{index}`）。

## 15. 已知技术限制

- 参考 SVG 依赖视觉模型对结构的理解，必须保留原图人工核对入口。
- 自动分题尚无统一区域裁剪协议，整页图上的相邻题可能增加参考图识别难度。
- 内嵌真实 SVG 的液面和部分属性无法像程序组件一样动态修改。
- 尺规作图只完成基础组件，尚缺动作白名单、步骤状态机与分帧展示。
- 浏览器模拟不能替代真实设备的多指手势、相机和安全区回归。

## 15.1 API 端点完整性检查方法

### 15.1.1 前端 API 调用面扫描

使用正则表达式从前端模板和静态 JS 中提取所有 `/api/*` 路径引用：

```python
import re
from pathlib import Path

def extract_frontend_api_calls():
    """从前端模板和JS中提取所有API调用路径"""
    api_calls = set()
    
    # 扫描HTML模板
    for html_file in Path("backend/templates").glob("**/*.html"):
        content = html_file.read_text(encoding='utf-8')
        # 匹配 $API.get/post/put/delete 或 fetch 调用中的API路径
        patterns = [
            r'\$API\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
            r'fetch\s*\(\s*["\']([^"\']+)["\']',
            r'["\']/(api/[^"\']+)["\']'
        ]
        for pattern in patterns:
            matches = re.findall(pattern, content)
            api_calls.update(matches)
    
    # 扫描JS文件
    for js_file in Path("backend/static/js").glob("*.js"):
        content = js_file.read_text(encoding='utf-8')
        matches = re.findall(r'["\']/(api/[^"\']+)["\']', content)
        api_calls.update(matches)
    
    return api_calls
```

### 15.1.2 检查结果摘要

**检查时间**: 2026年8月30日

**检查结果**:
- 前端调用的API端点: 169个
- 后端注册的API路由: 160个
- 匹配的端点: 56个
- 前端缺失端点: 96个
- 后端未使用端点: 104个
- 前端调用匹配率: 36.8%
- 后端端点使用率: 35.0%

**主要问题**:
1. **路径格式问题**: 36个端点缺少前导斜杠（如 `api/banks` 而不是 `/api/banks`）
2. **模块开关未检查**: 15个端点未检查模块开关状态
3. **动态路径参数不匹配**: 20个端点路径参数格式不正确
4. **尾部斜杠问题**: 10个端点尾部斜杠不一致

**修复建议**:
1. 立即修复路径格式问题
2. 添加模块开关检查
3. 修正动态路径参数
4. 统一尾部斜杠格式

**检查脚本**:
- `compare_api_calls.py` - 比较前后端API调用
- `fix_api_paths.py` - 修复路径格式问题
- `extract_routes.py` - 提取后端路由
- `api_endpoint_report.md` - 详细检查报告
- `api_endpoint_fix_guide.md` - 修复指南

### 15.1.2 后端路由表提取

从 FastAPI 应用中提取所有已注册的路由：

```python
from fastapi import FastAPI

def extract_backend_routes(app: FastAPI):
    """从FastAPI应用中提取所有路由"""
    routes = set()
    for route in app.routes:
        if hasattr(route, "path") and route.path.startswith("/api/"):
            routes.add(route.path)
    return routes
```

### 15.1.3 端点匹配分析

比较前端调用与后端路由，识别以下问题：

1. **前端有调用，后端无路由**：前端页面引用了不存在的API端点
2. **后端有路由，前端无调用**：可能存在未使用的API端点
3. **路径不匹配**：前端调用路径与后端路由路径不一致
4. **模块开关问题**：模块关闭时API应返回明确错误而不是404

### 15.1.4 实现步骤

1. **静态分析**：使用正则表达式扫描前端代码
2. **动态分析**：运行FastAPI应用并提取路由表
3. **差异比对**：生成端点匹配报告
4. **修复建议**：为每个不匹配提供修复方案

### 15.1.5 自动化检查脚本

```python
#!/usr/bin/env python3
"""API端点完整性检查脚本"""

import re
import sys
from pathlib import Path
from typing import Set, Dict, List

def check_api_endpoint_consistency():
    """检查前后端API端点一致性"""
    
    # 1. 提取前端API调用
    frontend_calls = extract_frontend_api_calls()
    print(f"前端调用的API端点数量: {len(frontend_calls)}")
    
    # 2. 提取后端路由
    backend_routes = extract_backend_routes()
    print(f"后端注册的API路由数量: {len(backend_routes)}")
    
    # 3. 分析差异
    missing_in_backend = frontend_calls - backend_routes
    missing_in_frontend = backend_routes - frontend_calls
    
    # 4. 生成报告
    if missing_in_backend:
        print(f"\n❌ 前端调用但后端缺失的端点 ({len(missing_in_backend)}):")
        for endpoint in sorted(missing_in_backend):
            print(f"  - {endpoint}")
    
    if missing_in_frontend:
        print(f"\n⚠️  后端存在但前端未调用的端点 ({len(missing_in_frontend)}):")
        for endpoint in sorted(missing_in_frontend):
            print(f"  - {endpoint}")
    
    if not missing_in_backend and not missing_in_frontend:
        print("\n✅ 前后端API端点完全一致!")
    
    return len(missing_in_backend) == 0
```

### 15.1.6 检查频率

- **开发阶段**：每次添加新功能后运行
- **发布前**：作为质量检查的必要步骤
- **定期维护**：每月运行一次，确保系统一致性

## 16. 试卷下载与多选 UX

- 试卷详情页 `paper_detail.html` 提供 4 类下载：`试卷` / `答案` / `得分点` / `答题卡`。
- 后端 `download_paper` 端点对 `mode` 做白名单枚举：`paper` / `qa` / `score` / `answer_card`；每个 mode 单次下载。
- 前端 `dlCustom` 按 `试卷→答案→得分点→答题卡` 顺序依次 `window.open` 同 baseURL 不同 mode；多份下载时 toast 提示"已为您分 N 个文件下载（首项 等）"。
- `answer_card` mode：优先读 `paper_dir/answer_card.html`；缺失时按 `paper.question_ids` 顺序运行时合成 `<main class="answer-card">` 模板，每题一行题号 + 两个答题区。

## 17. 侧栏无 JS 兜底与持久化

- `<html class="js-side-nav">` 由 `base.html` 同步 IIFE 在解析早期设置；CSS 规则 `.js-side-nav .sidebar{left:-200px}` 在 JS 启动前隐藏侧栏。
- 无 JS 降级时 `.sidebar` 默认 `left:0`（静态显示），避免桌面端 200px 空白。
- `app.js` 在 IIFE 启动时读 `localStorage.sb_user_closed`：若为 `'true'` 则不回填 `open` 并加 `body.sb-closed` 保持主区宽度复位；否则加 `open` 并同步 `aria-expanded="true"`。
- `toggleSidebar` 桌面端切换时持久化 `sb_user_closed`，移动端不写。

## 18. 前端 API 错误友好化

- `$API._friendlyStatus(status, rawText)` 把 401/403/404/409/429/5xx 映射为中文；fetch 网络层失败 (`TypeError: Failed to fetch` 等) 也映射为"网络异常，请检查连接"。
- `$API._parse` 在 `detail` 字段为空时回退到 `_friendlyStatus`；保留后端 `detail.message` 优先级。
- 所有 `$API` 调用方无需额外包装，错误信息已稳定中文；调用方仍可在 catch 中 toast 进一步提示。
- 整卷批改当前在一次 HTTP 请求内同步完成；进程中断不会留下半条 `Correction`，但需要用户重新提交整卷，尚未支持从页级检查点续跑。

## 19. 专注模式

### 19.1 服务层

- 核心服务：`backend/services/focus_service.py`，管理专注会话的完整生命周期。
- 路由层：`backend/routers/focus.py`，提供 REST API。
- 模块开关：`ENABLE_FOCUS_MODE`，关闭时路由返回 404，前端入口隐藏。

### 19.2 数据模型

专注会话数据以文件形式存储在 `storage/focus_sessions/{session_id}.json`，包含：

```json
{
  "id": "string (UUID)",
  "mode": "topic | question | mixed",
  "topic": "string（主题模式）",
  "question_id": "string（题目模式）",
  "status": "preparing | teaching | checkpoint | analyzing | adjusting | completed | paused | abandoned",
  "created_at": "ISO timestamp",
  "updated_at": "ISO timestamp",
  "segments": [
    {
      "index": 0,
      "content": "AI 讲解内容（Markdown）",
      "has_checkpoint": true,
      "checkpoint_response": {
        "voice_text": "学生语音转文字",
        "emotion_report": {
          "expression": "confused | neutral | focused | tired | positive | negative",
          "confidence": 0.0-1.0,
          "voice_features": {"speed": "slow|normal|loud", "volume": "quiet|normal|loud", "pause_count": 3},
          "overall_state": "understanding | confused | distracted | tired | engaged"
        },
        "timestamp": "ISO timestamp"
      }
    }
  ],
  "teaching_strategy": {
    "pace": "slow|normal|fast",
    "style": "conceptual|example_first|socratic",
    "difficulty_level": 1-5
  }
}
```

### 19.3 API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/focus` | GET | 专注模式页面 |
| `/api/focus/start` | POST | 启动会话（参数：mode, topic, question_id） |
| `/api/focus/{session_id}/next` | POST | 获取下一段讲解（自动在前一段检查点通过后调用） |
| `/api/focus/{session_id}/checkpoint` | POST | 提交检查点响应（参数：voice_text, emotion_report, webcam_image） |
| `/api/focus/{session_id}/state` | GET | 获取会话当前状态 |
| `/api/focus/{session_id}/pause` | POST | 暂停会话 |
| `/api/focus/{session_id}/resume` | POST | 恢复会话 |
| `/api/focus/{session_id}/end` | POST | 结束会话 |
| `/api/focus/history` | GET | 历史会话列表 |

### 19.4 教学生成流程

1. 用户选择模式（主题/题目/混合）和相关参数。
2. 系统从海马体加载用户对该主题/相关主题的已有认知状态。
3. AI 根据用户认知状态和教学模式，生成第一段讲解内容。
4. 前端渲染内容并通过 SpeechSynthesis 播放语音。
5. 末尾自动附加「对吧」标记。
6. 前端触发检查点流程：采集语音 + 表情 → 生成情感报告。
7. 情感报告 + 对话上下文送入 AI，生成下一段讲解或调整策略。
8. 循环直至 AI 判断主题讲完或用户主动结束。

### 19.5 前端实现

- 模板：`backend/templates/focus.html`
- 脚本：`backend/static/js/focus.js`
- UI 区域：讲解内容区（Markdown 渲染 + 语音播放）、检查点交互区（语音输入按钮 + 实时转文字 + 表情采集动画）、会话控制区（暂停/继续/结束/进度）。

### 19.6 黑板板书系统（`ENABLE_FOCUS_BLACKBOARD`）

- **数据结构**：`session.board = {pages: [{created_at, entries: [...]}], snapshots: [{page, label, time}]}`。条目两种：`{kind:"text", content}`（Markdown+LaTeX，≤600 字/条）和 `{kind:"svg", asset:"board_N.svg", title}`。SVG 存 `focus_sessions/{sid}/board_N.svg`（全局递增 N），session JSON 只存引用。页上限 12 条，超限写自动落新页（服务端兜底，AI 不丢字）。
- **AI 输出升级**：讲解生成改用 `deepseek_json`（scope 仍为 focus），输出 `{"content": "讲解文本(含对吧)", "board": {"ops": [...]}}`。ops 指令集：`write`(kind=text|function|diagram)、`erase`(entry 序号或 last:N)、`clear`、`newpage`、`snapshot`(label)。每段 ops ≤8；function 每段 ≤2 张、diagram 每段 ≤1 张。解析失败 → 纯文本降级（board 空，教学不断）。
- **执行器**：`_apply_board_ops(session, ops) -> dict`（结果摘要：写入条数、图成功/降级、当前页 X/12），摘要注入下一段 prompt 上下文，AI 据此管理空间（擦旧/翻页）。
- **图渲染链**：function spec → `diagram_service._render_function_graph_spec(spec)`（AST 白名单沙箱，纯渲染不落盘）；diagram 描述 → ai_service 生成单个闭合 SVG → 提取 → `_sanitize_svg` → `_has_drawing_content` 检查（防空白图伪成功）。任何失败 → 文本条目降级（"板书图形生成失败：{原因摘要}"），不落盘不伪造。
- **API**：`POST /api/focus/{sid}/board/snapshot`（学生手动存快照，label 可空自动命名）；`GET /api/focus/{sid}/board/asset/{name}`（板书 SVG 受控读取，白名单 `board_\d+.svg` + realpath/commonpath 边界 + sid 校验，开关关闭返回 404）。板书状态随 `GET /state` 的 session 返回。
- **前端**：`#boardPanel` 深色板面（白粉笔字）置于讲解区上方；SVG 条目渲染于白色贴纸卡（SVG 为黑白稿，深底不协调）；文本条目走 `$md`。翻页控件（‹ x/y ›）+ 快照面板（AI 快照 toast 提示）+ 学生"保存本页快照"按钮。开关关闭时模板不输出容器、JS 不初始化。
- **测试矩阵**：ops 全集执行与页满兜底、function 渲染成功/失败降级、diagram 降级、snapshot API、asset 白名单与穿越拒绝、开关关闭（无容器/端点 404/无 ops 注入）、AI JSON 解析失败降级。

## 20. 面部表情分析

### 20.1 核心服务

`backend/services/face_analysis_service.py`：接收前端上传的 Webcam 截图，调用 GLM-4V-Flash 进行表情分析，返回结构化情感报告。

### 20.2 分析 Prompt

视觉模型接收 Webcam 截图，输出 JSON：

```json
{
  "expression": "confused|focused|neutral|tired|positive|negative",
  "confidence": 0.85,
  "indicators": ["皱眉", "眼神游离"],
  "overall_state": "confused",
  "suggestion": "simplify"
}
```

### 20.3 安全边界

- Webcam 图片仅临时存储于内存，分析完成后立即丢弃，不做持久化。
- 如需调试保留，写入 `storage/focus_debug/` 并设置自动清理。
- 图片格式仅接受 JPEG/PNG，最大 2MB。

## 21. 语音交互

### 21.1 前端语音识别

- 使用浏览器原生 `webkitSpeechRecognition` / `SpeechRecognition` API。
- 语言：`zh-CN`。
- 连续识别模式，中间结果实时显示。
- 最终结果提交到检查点。
- 降级：浏览器不支持时显示文本输入框替代。

### 21.2 前端语音合成（双引擎）

- 引擎决策：`focus_voice_engine` 设置项为 `auto`（默认）时，若 `tts_available=true`（`ENABLE_XIAOMI_TTS` 开且已配置 Key）优先请求小米配音，失败自动回退浏览器合成；`browser` 只用浏览器；`xiaomi` 强制小米（失败仍回退浏览器）。页面初始化时通过 `/api/settings` 拉取一次配置。
- 浏览器兜底：原生 `SpeechSynthesis`，语音选择优先中文语音，回退默认。
- 播放控制：讲解段切换、暂停、结束、重置都会先取消进行中的 TTS 请求（AbortController）与音频播放（Audio 元素 + blob URL 回收），再走既有 `stopSpeaking()`。

### 21.4 后端 TTS 代理

- 端点：`POST /api/tts`（`backend/routers/audio.py`，仅当 `ENABLE_XIAOMI_TTS=True` 时挂载；`ENABLE_XIAOMI_TTS=False` 时设置页隐藏小米卡片，PUT/import 均拒绝选择 xiaomi 引擎），请求体 `{text: 1..2000字, voice?: <=32字符}`。
- 上游：`{xiaomi_token_plan_base_url}/chat/completions`，OpenAI 兼容；payload 为 `model=xiaomi_tts_model`、messages=[assistant=朗读文本]、`audio={format, voice}`。响应取 `choices[0].message.audio.data` base64 解码为音频字节，按 `audio/mpeg` 返回并附 `Cache-Control: no-store`。
- 超时与重试：固定 60 秒超时、单次尝试（不继承 ai_timeout，也不允许 custom fallback——调用带 `_is_fallback=True` 且 `max_retries=1`，防止把含 audio 块的 payload 泄漏给兜底聊天模型）。
- 失败映射：未启用/未配置 Key → 503；上游 401/403 → 502"鉴权失败"；其他上游错误 → 502 并写 `Err.log`；文本为空/超长 → 400/422。不落盘音频，内存中转。import_config 对引擎与音色走手写校验返回 400，与 PUT 的 422 是两条独立通道，均有测试锁定。
- 验收：单测覆盖成功解码、空 choices、错误码映射与模块开关关闭行为；真实冒烟合成一句中文验证 mp3 字节流合法（RIFF/ID3/MPEG 帧头）。前端每次朗读分配代次号：暂停/结束/重置使在飞结果过期，主动中止（AbortError）绝不触发浏览器合成回退，杜绝停止后语音"死而复生"。

### 21.3 语音情感特征

前端在语音识别过程中同步采集基础特征：

- 语速：words per minute
- 音量：average volume level（从 AudioContext 获取）
- 停顿次数：识别间隔超过 1 秒的停顿计数

这些特征与表情分析结果合并为综合情感报告。

## 22. 虚拟海马体记忆系统

### 22.1 核心服务

`backend/services/hippocampus_service.py`：扩展现有 `user_profile.py`，增加学习记忆专属区域。

### 22.2 存储结构

`storage/hippocampus/profile.json`：

```json
{
  "version": 1,
  "meta": {
    "baseline_understanding": 0.5,
    "baseline_memory": 0.5,
    "baseline_focus": 0.5,
    "preferred_style": "conceptual",
    "best_study_time": "evening",
    "last_updated": "ISO timestamp"
  },
  "topics": {
    "二次函数": {
      "mastery": 0.75,
      "weak_points": ["最值问题", "图像平移"],
      "total_minutes": 45,
      "checkpoint_pass_rate": 0.8,
      "last_study": "ISO timestamp",
      "decay_factor": 1.0,
      "history": [{"date": "ISO", "delta": 0.05, "reason": "focus_session"}]
    }
  }
}
```

### 22.3 衰减公式

掌握度衰减：`mastery = mastery * exp(-lambda * days_since_last_study)`

其中 `lambda = base_lambda * difficulty_factor / user_memory_baseline`

- `base_lambda = 0.05`（每 14 天约衰减 50%）
- `difficulty_factor`：简单 0.8、中等 1.0、困难 1.3
- `user_memory_baseline`：用户画像中的记忆力基线（0.5~1.0）

### 22.4 AI 自主编辑接口

AI 在对话过程中可调用以下操作：

- `update_mastery(topic, delta, reason)`：更新掌握度
- `add_weak_point(topic, point)`：添加薄弱点
- `remove_weak_point(topic, point)`：移除薄弱点
- `add_topic(topic, initial_data)`：新建主题记忆
- `delete_topic(topic)`：彻底遗忘主题
- `detect_conflict(topic, new_knowledge)`：检测知识冲突

### 22.5 淡化与遗忘阈值

- 淡化阈值：`mastery < 0.2` → 标记为久远记忆，不主动引用
- 遗忘阈值：`mastery < 0.05` 且 `days_since_last_study > 90` → 彻底删除
- 冲突删除：AI 确认冲突后可直接删除

### 22.6 原子写入

海马体 JSON 文件使用临时文件 + `os.replace()` 原子写入，防止中断损坏。读取时校验 `isinstance(data, dict)`，损坏时回退到空结构并记录 `Err.log`。

## 23. Android 客户端实现要点

WebView 远程壳（`android/`，包名 `com.learningagent.app`）的安全与生命周期契约：

- WebView 配置：JS 与 DOMStorage 开启、`setAllowFileAccess(false)`、不注册 `addJavascriptInterface`、定位一律拒绝、`onPermissionRequest` 只映射 `RESOURCE_VIDEO_CAPTURE`（CAMERA）与 `RESOURCE_AUDIO_CAPTURE`（RECORD_AUDIO），其余 web 资源直接 deny。
- 导航边界：`shouldOverrideUrlLoading` 中与服务器同 host（scheme+host+port 三元组，防明文降级）的导航留在 WebView；外链只把 http/https 交给系统浏览器，其他 scheme（`intent://` 等）忽略，不代跳。
- 文件上传：图片类上传走「拍照直拍 + 相册多选」Chooser，拍照输出到应用私有 `cache/camera/`，经 FileProvider 共享；CAMERA 权限先检查后请求，拒绝时降级为仅相册。非图片上传保持系统默认选择器。拍照输出用真实 File 对象跟踪（不得从 `content://` getPath 反推磁盘路径），成功交付后延迟清理、取消/失败即时清理。
- getUserMedia 授权绑定同源：`onPermissionRequest` 校验请求 origin 的 scheme+host+port 与配置服务器一致才 grant，重定向/跨域 iframe 一律 deny；pending 权限按各条目自身 Android 权限清单逐一判定，不共用回调权限集。
- 下载走系统 DownloadManager 落公共 Downloads 目录，仅允许配置服务器同源的下载地址。
- 生命周期收尾（onDestroy）：未消费的 `filePathCallback` 置空、未决 `pendingWebPermissions` 逐个 deny 并清空、清理未消费拍照缓存；WebView 先 `stopLoading`、从视图树 removeView（`ViewGroup` 契约）再 `destroy()`。
- `allowBackup=false`：WebView localStorage 中记住的站点访问密码不进系统云备份与设备迁移。
- 版本号在 `android/app/build.gradle.kts` 的 `versionCode/versionName` 管理，发布前递增（当前 v1.3 / versionCode 4）。

## N. 深检修复轮（2026-09-09/10）

### 视觉链统一（vision_mimo_first）
- 新增 `ai_service.vision_mimo_first(image_base64, prompt, mime_type, parse_json)`：xm_key 存在时 MiMo 优先（xiaomi_vision），失败/空结果回退 zhipuai_vision，双失败返回 None；返回形状与两个底层方法一致（parse_json=True 尽量 dict）。
- 5 个旁路点替换：ocr.py `_split_and_process` 拆题判定、diagram_service 数学图复核/组件组装/装置复核、layout_service `_glm_review_image`。全仓对账后 `ai_service.zhipuai_vision` 仅剩统一入口内部与 note/face 自带 MiMo 前置的合规调用。
- `_call_vision_model_async` 删除 Kimi 视觉兜底（无视觉输入必 400，FreqErr 教训），链终 ZhipuAI 失败返回 None。
- 返回契约差异注意：xiaomi_vision 返回 str（parse_json=False）或 dict（True）；zhipuai_vision dict/str；调用方需按 isinstance 分支（拆题探针 `result.get(...) if isinstance(result, dict)` 保形）。

### focus 失败段重试（M1）
- `_generate_teaching_segment` 失败分支返回 `{"failed": True, ...}`（不再伪装"对吧"结尾）。
- `_submit_checkpoint_locked` 入口检查目标段 failed：pop 后重新生成（新段 index = 原 index，因 pop 后 len 恰好回退），返回 `{"action": "retry", "segment": ...}`；不计 total/passed_checkpoints，不做表情分析/pause/simplify。
- 前端 focus.js renderSegment 对 failed 段应用 `var(--err)` 斜体；提交后 action 分支与 continue 共用渲染路径（resp.segment 驱动），零额外前端改动。

### banks 写锁（M2）
- routers/banks.py 模块级 `_bank_write_lock = asyncio.Lock()`；tags/add、tags/remove、rename、delete 四端点读改写临界区包锁。选全局锁而非 per-bank：rename 跨两库会有锁序问题，且题库操作低频无吞吐需求（仿 focus_service 锁模式）。

### worksheet 冻结题集（M3）
- `generate_worksheet` 读取 params["question_ids"]（regenerate modify 注入）：去重保序后按 example_count 切分 example_ids/practice_ids（原卷 _save_paper 保存顺序即 [例题...,练习...]），跳过 _search_questions_for_worksheet；generation_warnings 追加"已冻结原卷题集"。笔记检索不受影响（question_ids 不含笔记）。
- 边界：explicit_ids 为空走原检索路径；`not notes and not example_ids and not practice_ids` 的空数据 guard 仍在冻结分支之后生效。

### 拆题失败留痕（M4）
- 拆题判定包 2 次重试（vision_mimo_first 返回 None 或抛异常均重试）；最终失败 `_mark_split_fallback(question_id, reason)` 写任务状态文件 `split_fallback`/`split_fallback_reason`（幂等恢复窗口与排查可见；任务完成即随状态文件清除）。常驻用户可见告警需 DB 字段，登记待决策。

### Agent 笔记图谱同步（M5）
- sessions.py modify_note 的 content 分支 commit 后调用 `knowledge_graph.refresh_note_snippet(n.id, n.content)`——与 notes.py 正规更新链一致；title/subject/tags 不影响 snippet 不刷新。try/except + logger.warning（图谱故障不阻断回复）。

### 配置健壮性（E1/E2）
- config.load_settings 改 `utf-8-sig`：兼容外部工具写出的 BOM JSON；写侧（原子写 utf-8 无 BOM）不变。
- main.password_guard：`if expected and not compare_digest(provided, expected)`——未配置密码=鉴权未启用=放行（既有契约显式化）；配置密码仍恒定时间比较。测试如需断言鉴权，monkeypatch main._get_auth_password 显式启用。

### 前端清理（F1-F4）
- agent.html '⚡'→'工'；questions.js 分页去箭头；diagnose.html 日志前缀改"警告：/提示："；lecture.js #c00→var(--err)、去 BOM；lecture.html #888→var(--text3)（主题契约扫描器连 var() 内 hex 兜底也拦，业务色引用不带兜底 hex）。

### 服务器部署（2026-09-10）
- 14 文件 scp（routers/ocr、banks、sessions、papers、main、config + services/ai_service、focus_service、paper_service、diagram_service、layout_service + static/js/focus.js、lecture.js、questions.js + templates/agent、diagnose、lecture、focus.js? 按最终 git status 为准）→ `_la_restart.ps1` → health 200。
- 下载入口页 `/download`：APK + 桌面端 zip + PWA 说明；桌面端产物挂 `/downloads-desktop/`。

## N+1. 服务器看门狗（2026-09-10）

- **问题**：nssm 只兜"进程退出"，兜不住两类事故：进程被系统干掉后立即重启又立即死（无缓冲）、服务 RUNNING 但 HTTP 假死（此前发生过孤儿进程占 8000 与 WMI 拖死启动）。用户要求加看门狗。
- **实现**：`ops/watchdog_learningagent.py`（仓库留档）部署到服务器 `C:\all_projects\watchdog_learningagent.py`，nssm 服务 **LearningWatchdog**（AppStdout/Stderr + 日志滚动 + AppExit Restart；脚本自身崩溃由 nssm 拉起，双层兜底）。逻辑：60s 周期探活 `/api/health`（3×10s）→ 非 RUNNING 直接 start；RUNNING 但连续失败 → 限流 restart（10min 冷却、连续 5 次熔断打 ALERT）；成功归零。`watchdog.pause` 旗标可暂停动作；netstat 端口占用只留痕不杀进程。
- **配套加固**：`nssm set LearningAgent AppRestartDelay 5000`（被杀后 5s 再拉，避开端口释放窗口）。
- **验证**：本地 --dry-run 决策路径通过；服务器 --once 真探活 ok；服务上下文连续周期 ok（69s 间隔）；LearningWatchdog/LearningAgent 双 RUNNING。运维开关：`type nul > C:\all_projects\watchdog.pause` 暂停，删除恢复。
- **日志**：`C:\all_projects\watchdog_learningagent.log`（>1MB 自动截断留尾）。
