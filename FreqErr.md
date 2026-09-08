# 常见错误类型记录（Common Error Patterns）

## [测试噪声入真实日志] 错误注入用例回显写入 backend/Err.log
- **错误描述**: mock 失败/恶意输入类用例经 log_error 把模拟故障写进真实 Err.log；子进程测试（如端到端下载验证）绕过 conftest 的模块属性修改，污染持续发生，稀释真实故障信号。
- **正确做法**: 统一走 `backend/conftest.py` 设置 `DSH_ERR_LOG_PATH` 环境变量并同步改 logger.ERR_LOG_PATH——环境变量随子进程继承，跨进程生效；logger 以 `os.environ.get(...) or 默认路径` 方式读取，生产不受影响。

## [首导入方绑定] pytest 先收集后执行导致晚导入文件的重定向失效
- **错误描述**: 测试文件在模块导入期各自 mkdtemp 重定向 config.*；pytest 收集阶段按字母序导入全部文件，main/models 只被首个导入方绑定，后续文件的重定向只影响 load_settings 类读取，形成 SQLite 与文件系统各指一处的隐性顺序依赖。
- **正确做法**: 新增测试文件遵循同一惯例：用 `if "main" not in sys.modules` 判断自己是否为本次运行的唯一导入方，是才重定向（且必须包含 DATABASE_URL）；不要无条件覆盖。

## [测试伪影] 静态剥离 Jinja 标签后做 JS 语法检查产生假错误
- **错误描述**: 用正则把模板内联 script 中的 `{% if %}`/`{{ x }}` 替换成 `""` 再 node --check，会把合法条件标签替换成悬空引号，报"语法错误"误导排查（questions.html VALID_TABS 案例）。
- **正确做法**: 必须用真实 Jinja 环境渲染模板后再对渲染产物做语法检查；静态剥离只用于粗筛，结果存疑时以真实渲染为准。

## [响应字段三处一致] 新增设置字段漏改 GET/前端其一
- **错误描述**: 给 /api/settings 新增 has_xiaomi 时只加在 update_settings 的 PUT 响应，GET 响应与前端 init 读取仍缺字段；前端 `s.has_xiaomi` 取值为 undefined 被静默吞掉，测试断言 KeyError 才暴露。
- **正确做法**: 新增设置字段时同轮补齐：SettingsUpdate 模型、get_settings GET 响应、update_settings PUT 响应、import_config 白名单分支、前端 settings.html init/save 五处逐一核对。

## [单例配置漂移] ai_service 单例不随测试临时配置刷新
- **错误描述**: ai_service 在模块导入时执行 _reload() 固化 key/url 到实例属性；测试文件在 import main 后才替换 config.SETTINGS_FILE，单例仍持有首次导入时的配置，mock 层面的断言失败且难以定位。
- **正确做法**: 测试中直接 monkeypatch 单例属性（xm_key 等）；依赖 settings.json 实时值的端点测试则 monkeypatch 路由模块内引用的 load_settings。

## [文件覆盖] insert_diagram 使用固定 index 计算导致覆盖已有文件
- **错误描述**: `new_index = target_diagram_index + 1` 在多次插入后可能与其他 diagram 文件冲突，导致数据丢失。
- **正确做法**: 扫描文件夹内所有 `diagram_N.svg` 文件，取最大 N+1 作为新 index。

## [XSS注入] SVG 清理不充分
- **错误描述**: 只清理 `<script>` 和 `on*` 事件处理器，遗漏 `<foreignObject>`、`<use>` 外部引用、`<image>` onload、`href=javascript:`、`<set>` SMIL 动画等 XSS 向量。
- **正确做法**: 全面清理：移除 script/foreignObject/use(image外部引用)/set/image 标签，清除所有 on* 属性和 javascript: 伪协议。

## [并发安全] 异步端点中使用同步锁（threading.Lock）
- **错误描述**: FastAPI `async def` 端点上使用 `threading.Lock` 装饰器。同步锁在协程创建时获取，在 I/O 执行前释放，锁形同虚设。
- **正确做法**: 使用 `asyncio.Lock` + `async with lock:` 包裹实际 I/O 操作。

## [空spec] assemble 在没有模板和组件时返回空白SVG但不报错
- **错误描述**: `comp_spec = {}` 时生成空白SVG，调用方无法区分失败产物与正常结果。
- **正确做法**: assemble 入口检查是否有 template 或 components，若无则返回包含错误提示的SVG。

## [静默失败] 组件/连接处理异常未记录日志
- **错误描述**: 未知组件类型、未知连接端口、不存在的类型引用等全部通过 `continue` 静默跳过。
- **正确做法**: 每个跳过分支至少输出 `logger.warning`。

## [viewBox遗漏] 自动 viewBox 计算未考虑连接线坐标
- **错误描述**: 自动 viewBox 只遍历 components，connection_lines 延伸到组件区域外时被裁剪。
- **正确做法**: 将 connection_lines 的起点和终点坐标纳入 viewBox 计算范围。

## [时间戳不一致] SVG 内嵌注释时间戳与 .ts 文件中时间戳不同
- **错误描述**: `assemble()` 生成 `<!-- ts:... -->` 用一次 `time.time()`，`_write_timestamp` 又用另一次 `time.time()`，两者毫秒级不一致。
- **正确做法**: `_write_timestamp` 优先从 SVG 注释提取时间戳，保证 .ts 文件与 SVG 注释一致。

## [原子写入缺失] JSON 文件直接覆盖写入，中断时文件损坏
- **错误描述**: `open(GALLERY_FILE, "w")` 直接写入，写入过程中断导致文件损坏、数据永久丢失。
- **正确做法**: 先写入临时文件，然后 `os.replace()` 原子重命名。

## [SQLite JSON函数] SQLite不支持 json_contains 函数
- **错误描述**: SQLAlchemy JSON 列调用 `.contains()` 生成 `json_contains()` SQL函数，SQLite 无此函数导致 `OperationalError`。
- **正确做法**: 使用 `.cast(TEXT).like(f'%"{tag}"%')` 进行字符串模式匹配。

## [SVG cursor 顺序替换陷阱] .replace() 链式调用导致二次匹配
- **错误描述**: `'nw-resize'.replace('nw','nwse').replace('se','nwse')` 中第一次 replace 后的 'nwse' 中的 'se' 被第二次 replace 匹配导致错误结果 'nwnwse-resize'。
- **正确做法**: 使用映射表（`{nw: 'nwse-resize', ...}`）而非链式 replace。

## [Pydantic bool 默认值陷阱] bool=False 使 is not None 永远为 True
- **错误描述**: Pydantic field `dark_mode_auto: bool = False` 在解析请求体时 always has a value，导致 `if data.dark_mode_auto is not None` 永远为 True，空请求会覆盖用户设置。
- **正确做法**: 使用 `Optional[bool] = None`，只有用户显式传值时才更新。

## [索引键错位] 以数组下标为键的关联结构在数组变更后不重映射
- **错误描述**: 并查集 parent/ratio、整数型 link_group 以 components 数组下标为键，数组 sort/splice/整体替换后未重映射，导致比例联动作用于错误元件。
- **正确做法**: 渲染层排序与存储索引解耦（renderCanvas 按 z 序渲染、数组不变）；组件集合变更（删除/加载）后统一调用重置函数清空索引键结构；字符串型组标识（lg_xxx）不依赖索引可保留。

## [恒假条件] helper 返回值语义未验证导致条件恒假
- **错误描述**: `!rs.linkedComponents.length` 恒为 false（_getLinkedComponents 恒含元件自身），导致 resize 操作的 _pushUndo/recordCalibration 成为死代码。
- **正确做法**: 写存在性条件前确认 helper 返回集合的完整语义（是否含自身、是否可能为空）；对"长度>0"类检查改用明确的成员过滤。

## [撤销栈不一致] undo 只快照主数据、漏快照关联结构
- **错误描述**: undoStack 只深拷贝 components，恢复快照后 ratioUF 仍是新状态，比例锁定关系与元件顺序错位。
- **正确做法**: 关联结构随主数据同一时刻快照（{comps, uf} 对象结构），恢复时成对还原。

## [Python变量遮蔽] 函数内 import 导致 UnboundLocalError
- **错误描述**: `assemble()` 函数体内条件块中使用 `import time`，Python 编译器将 `time` 视为整个函数局部变量，条件块不执行时 `time.time()` 报 `UnboundLocalError: cannot access local variable 'time'`。
- **正确做法**: 模块级 `import time` 已存在时，函数内不要再重复 `import time`；直接在条件块外使用已导入的 `time` 模块。

## [设置字段断裂] 前端→后端→AI服务 三端新增字段只在两端落地的假闭环
- **错误描述**: settings.html 前端+bakend router+config.py 三级新增字段（`zhipuai_model`、`*_max_tokens`），但 `ai_service._reload()` 未读取，调用点仍用硬编码→用户看到设置成功实际上无效。
- **正确做法**: 新增 API 设置字段时，必须从 Pydantic 模型→config→ai_service._reload()→实际调用点 四跳全部落地。

## [SRI哈希伪造] KaTeX CDN integrity 值凭猜测写入
- **错误描述**: KaTeX 0.16.11 的 integrity 值为凭空编造（`sha384-pR1t3az...`），浏览器 SRI 校验失败导致全站数学公式不渲染。
- **正确做法**: CDN integrity 必须从官方文档（katex.org/docs/browser.html）获取正确值，不可自行编造。

## [WeakValueDictionary竞态] 锁表访问期间弱引用被回收导致 KeyError
- **错误描述**: `_acquire_lock` 中先 `if key not in weak_dict or weak_dict[key] is None` 再赋值，两次访问之间弱引用可能被 GC 清除，触发 `KeyError`。
- **正确做法**: 使用 `weak_dict.get(key)` 一次性取值并判空，再创建新锁写入。

## [异步会话作用域] async with async_session 块结束后仍使用 db
- **错误描述**: 锁重构/代码移动后，原在 `async with async_session() as db:` 内的 `db.add/commit` 被留在块外，操作已关闭会话报错。
- **正确做法**: 任何 `db.add/commit/refresh/get` 必须位于对应的 `async with async_session() as db:` 块内；跨块操作需重新进入会话。

## [SVG消毒顺序] 先处理 href 再移除 image 标签导致标签残留
- **错误描述**: 伪协议正则可能破坏 image 自闭合结构，导致后续 image 移除正则无法匹配，恶意 `<image href="data:..."/>` 残留。
- **正确做法**: 在 href/事件处理之前先完整移除 `<image>`、`<script>`、`<use>` 等高风险标签。

## [死代码参数失效] 统一渲染入口未被调用导致参数前端可见后端无效
- **错误描述**: `liquid_render.render_liquid` 作为统一液面渲染入口定义了 `liquid_color`/`rotation` 参数，但 grep 确认从未被任何容器 render 调用——容器 render 各自直接画液体并硬编码 `_color("water")`。导致前端 `liquid_color` 参数后端不生效，`rotation` 参数无运行时效果。
- **正确做法**: 新增统一渲染入口后，必须遍历所有应调用方实际接入；或反向在现有 render 内提取 helper（如 `_liquid_fill(p)`）替换硬编码值。验证时用 grep 确认调用链，不能只看函数签名。

## [键表双用途] 同一键表用于"清空"与"判断重建"导致新字段被误清
- **错误描述**: `_SCENE_PARAM_KEYS` 同时用于「应用场景摆法前清旧键」和「undo/redo 后判断是否重建预览」。新增 `temperature`/`liquid_color` 若加入该表，应用摆法时会清空用户已设的温度/液体颜色。
- **正确做法**: 键表用途单一化——场景摆法管理键与实例级渲染键分离；预览重建判断用独立键表（如 `_INSTANCE_RENDER_KEYS`）补充检查，不污染摆法清空逻辑。

## [FastAPI 路由遮蔽] 动态路径捕获静态子路径
- **错误描述**: `GET /api/correction/{correction_id}` 注册在 `GET /api/correction/history` 之前时，访问 `/history` 会被 `{correction_id}` 捕获（correction_id="history"），导致历史接口 404 或拿到错误响应。FastAPI 按注册顺序匹配，先注册的动态段会吃掉后注册的静态段。
- **正确做法**: 同前缀下，静态子路径（如 `/history`、`/summary`）必须注册在动态路径（`/{id}`）之前；或在动态路径内对 id 做格式白名单校验（如 `^[a-zA-Z0-9_-]{1,64}$`）让 "history" 之类保留词直接 400 也不会误命中。新增端点后用 `app.routes` 顺序自检。

## [前端 JS 变量引用时序] 变量在 DOM 加载完成前被引用
- **错误描述**: `onTypeChange` 函数中引用了 `window._pTypes`，但 `_pTypes` 变量定义在后续 DOM 中，导致首次执行时 `window._pTypes` 为 undefined，触发 `TypeError: Cannot read properties of undefined`。
- **正确做法**: 确保 JS 变量在 DOM 加载完成后定义（`DOMContentLoaded` 或 `</body>` 前），引用方先做 null check：`if (typeof window._pTypes !== 'undefined')`；或使用事件委托，确保变量定义在函数调用之前。

## [异步数据初始化空状态] 异步加载未完成前渲染导致空 UI
- **错误描述**: `_widgets` 初始化为空数组 `[]`，`loadWidgets()` 是异步操作，在异步请求完成前，页面渲染已使用空数组，导致可用组件列表为空。用户看到"可用组件为空"的状态。
- **正确做法**: 初始化时预填默认/回退数据（如 `_FALLBACK_WIDGETS`），确保异步加载完成前至少有一个合理的 UI 状态；或使用加载状态指示器，在数据就绪前不渲染最终内容。

## [前后端表单字段不一致] 前端表单字段名与后端 API Schema 不匹配
- **错误描述**: `paper_generate.html` 提交的 `params` 中缺少 `paper_layout` 字段，`paper_size` 字段名与后端 `PaperGenerateRequest` 模型不匹配，导致后端验证失败返回 400 错误。前端和后端各自维护字段名列表，修改时未同步。
- **正确做法**: 新增或修改 API 字段时，必须同步更新：前端表单字段名 → JS 提交参数 → Pydantic Schema → API 路由处理 → Service 层消费；建议在同一个 PR/commit 中完成，避免跨步遗漏。前端提交前做一次字段名完整性校验。

# 2026-08-01 响应式与主题色错误模式

- **移动断点覆盖过宽**：在 768px 内把所有 `.row` 子项强制为 100% 或把弹窗全部全屏，会让平板、分屏窗口和小尺寸电脑失去桌面信息密度。应在 481–768px 使用可换行双列，仅在 480px 以下完全单列。
- **模板硬编码业务色**：页面、动态 HTML 或 SVG 直接写十六进制颜色后，自定义强调色和暗色主题无法完整生效。除设置色板及黑白基础色外，统一使用全局语义变量，并用自动化扫描阻止回归。

## [尺寸隔离塌缩] inline-size 容器只设 flex 未设宽度
- **错误描述**：`.main` 同时使用 `container-type:inline-size`、固定左边距和 `flex:1`，但父容器为纵向 flex 且另有 `max-width/margin-right:auto` 时，桌面主区可能按最小内容宽度收缩，出现标题逐字换行和大片空白。
- **正确做法**：主区显式声明桌面 `width:calc(100% - sidebarWidth)`，侧栏收起和窄屏声明 `width:100%`，并在真实浏览器读取布局矩形，不能只看 CSS 规则。

## [AI 提示冲突] 参考图已存在却仍要求重画题面图
- **错误描述**：前置提示要求使用 OCR 参考 SVG，后置通用提示仍要求 `diagram_prompts` 生成“题面1张”，模型会收到互相矛盾的指令并可能生成另一张不同图。
- **正确做法**：按 `reference_svg / description / no diagram` 分支生成互斥提示；参考图存在时后端强制新增图全部落在 `answer`，不能只依赖模型遵守文字要求。

## [受控 URL 未约束输出] 只在读取文件时检查路径
- **错误描述**：marker 替换逻辑仅在尝试读取 SVG 宽度时验证路径，却仍把原始 `path` 插入 `<img src>`；数据库字段一旦被污染可形成属性注入。
- **正确做法**：先用完整正则得到受控 URL，验证失败直接保留 marker；只有受控 URL 才能用于磁盘读取和 HTML 输出。

## [配置显式空值丢失] 空布局被当作未配置
- **错误描述**：`layout=[]` 与配置缺失都被 `if not layout` 处理，导致用户主动清空首页后又恢复默认组件。
- **正确做法**：区分“无配置/字段非法”和“合法空数组”；只有前者回退默认，后者必须原样保存和返回。

## [文档多源漂移] 同一轮结果复制到多个权威文件
- **错误描述**：设计、技术、done、更新记录和局部 README 同时复制同一功能说明，后续只修改其中一份，产生互相冲突的“当前状态”。
- **正确做法**：按职责单写：Design 只写当前架构，Techniques 只写当前实现，README 只写操作，done 只写本轮结果；合并独有信息后删除副本。

## [AI JSON 截断误修复] 无效响应被拼接成成功数据
- **错误描述**：模型返回代码围栏、纯文本、空响应或被 token 截断的 JSON 时，过度容错会生成字段残缺的题目并进入 done。
- **正确做法**：只执行确定性的围栏剥离、首个 JSON 提取和字符串换行修复；结构与必填字段仍不完整时记录错误并重试，不能伪造成功。

## [模型参数假定] 所有兼容 API 使用同一 temperature/token 约束
- **错误描述**：不同模型对 temperature 和 max_tokens 的范围不同，例如 Kimi K2.6 只接受 temperature=1，GLM 视觉模型也可能有更小 token 上限。
- **正确做法**：按模型配置和供应商约束生成请求；400 响应应记录模型名与安全摘要，并触发受限回退，不重复发送同一非法参数。

## [迁移静默失败] ORM 已引用新列但旧数据库未迁移
- **错误描述**：迁移用裸 `try/except: pass` 吞掉失败，页面直到查询时才出现 `no such column`。
- **正确做法**：启动时先用 `PRAGMA table_info` 检查每列，逐项执行迁移并记录结果；迁移失败阻止相关功能进入伪正常状态。

## [跨生命周期列表遗漏] 上传记录和定稿实体分表后只查询最终表
- **错误描述**：整卷图片已经写入 `UploadSession`，但尚未生成 `Paper`；试卷库只查询 `papers`，导致用户数据看似消失。页面又只识别旧状态值，使 `ready/attention` 显示为未知。
- **正确做法**：列表接口明确聚合两个生命周期，返回 `record_type` 与统一状态；已生成且能找到对应最终实体时去重，未生成记录只提供继续处理操作，不能伪装成可下载试卷。状态必须由子项真实状态批量计算并覆盖全部枚举。

## [浮动元素错位] 窄列表中的长英文标识覆盖相邻行
- **错误描述**：在固定宽度列表项中用 `float:right` 放置英文类型名；较长的下划线标识无法正常收缩，会挤压中文名称或落到下一条目，看起来像换行符造成的位置偏移。
- **正确做法**：使用带 `minmax(0, 1fr)` 的 grid/flex 布局，为文本列设置 `min-width:0`、单行省略和完整 `title`。涉及 SVG 时还需区分文本换行与坐标变换：`<text>` 不会自动解析换行，非零 `viewBox` 则必须同时补偿起点和平移。

## [后台任务丢失] 数据已提交但任务只存在于内存
- **错误描述**：上传接口先提交题目，再用裸 `asyncio.create_task` 启动 OCR；进程在两步之间退出后，数据库永久停在暂存/处理中，重启也不知道如何恢复。
- **正确做法**：在提交可见业务状态前原子写入包含任务类型、题目 ID 和必要参数的恢复描述；所有入口共用受控后台包装器，启动时按描述恢复，成功或明确失败才清理。

## [静默容量截断] 对模型数组直接切片
- **错误描述**：拆题模型返回的题数超过单页/整卷上限时使用 `items[:limit]`，接口仍返回成功，后半题目永久丢失。
- **正确做法**：超过安全上限立即返回明确错误，要求减少页数；容量边界不得用切片伪装成功。组卷题量超过题库容量同理，不重复题、不补造题。

## [失败占位伪成功] 报错文字 SVG 被保存为正式图
- **错误描述**：函数表达式无效时生成带“渲染失败”文字的 SVG，上层只检查存在绘图标签便认为成功，最终把错误卡片混入题面。
- **正确做法**：渲染失败返回空结果或结构化错误；参考 SVG 还要检查至少一个真实绘图元素，空白或仅容器的 SVG 不得落盘。

## [能力检查过严] 固定要求某个供应商 Key
- **错误描述**：流程实际支持视觉和推理模型回退，但入口仍同时要求 DeepSeek 与智谱 Key；Kimi 或自定义 `solve` API 明明可用也无法启动。
- **正确做法**：按能力而非供应商校验：视觉至少一个、推理至少一个；具体供应商失败由受限回退链处理。

## [文件数据库双写] 文件已落盘但数据库提交失败
- **错误描述**：题目、试卷、笔记来源图或拆题子目录先写文件，随后数据库提交失败，留下没有实体引用的孤立目录；反向顺序又可能产生实体指向不存在文件。
- **正确做法**：文件使用临时文件、`fsync` 和原子替换；数据库失败回滚并删除本次新建文件，后台任务描述与数据库状态按固定顺序提交。删除实体时只清理已确认不再被其他实体引用的文件。

## [错误详情泄漏] 500 响应原样返回内部异常
- **错误描述**：把数据库路径、供应商响应、网络地址或转换器异常直接拼入 HTTP 500，既暴露环境信息又使前端依赖不稳定错误文本。
- **正确做法**：完整原因写脱敏日志；客户端只返回稳定的通用失败提示。只有经过业务约束的 `ValueError` 才可作为 400 细节返回。

## [SVG 公开白名单过宽] /storage 公开路由允许任意 .svg 文件名
- **错误描述**：`serve_public_storage_file` 的公共图片扩展名包含 `.svg`，只要题目目录下出现任意 `.svg` 文件（即使未经过消毒）就会被公开 serve，扩大 XSS 面。
- **正确做法**：`.svg` 只允许 `reference.svg` 或 `diagram_N.svg` 这类由消毒流程写入的固定文件名；notes/corrections 只允许光栅图片扩展名；vendor 仅允许 JS/CSS/字体扩展名。

## [锁定守卫缺失] 手动标记结构图未检查 is_resolved
- **错误描述**：`/structure-graph/mark` 等用户写入端点只校验数据格式，未检查题目是否已锁定；已解决题目仍可被覆盖修改，违背锁定语义。
- **正确做法**：所有会修改题目内容/结构图/对比分区的端点，在写入前统一检查 `is_resolved` 并返回 403；AI 生成路径还要在 AI 调用完成后用 FOR UPDATE 重新加载再检查一次。

## [pending 路径穿越] confirm/cancel-pending 直接拼接 URL 参数到文件路径
- **错误描述**：`_pending_path` 只做 `os.path.join(PENDING_DIR, f"{qid}.json")`，`_load_pending`/`_clear_pending` 不校验 qid。`POST /api/questions/..%5Csettings/cancel-pending` 会删除 `STORAGE_DIR/settings.json`。
- **正确做法**：所有 pending 读写统一经过题目 ID 白名单（`^[a-zA-Z0-9_-]{1,64}$`），再对拼接后的绝对路径做 `os.path.realpath` + `os.path.commonpath` 边界校验；失败返回 400，绝不进入文件系统。

## [测试数据污染真实文件] 服务层按 __file__ 自行推导数据目录
- **错误描述**：`user_profile.py`、`audit_service.py`、`gallery.py`、`calibration_service.py` 曾按 `__file__` 推导 `backend/user_profile.json`、`backend/storage/system_messages.json`、`backend/data/gallery/*.json`，测试只替换 `config.STORAGE_DIR` 时，测试样本仍写入真实数据文件。
- **正确做法**：所有可写数据目录只从 `config`（`STORAGE_DIR`/`SETTINGS_FILE`/`GALLERY_DIR`）派生；测试在 import main 前替换 config 后自动隔离，测试后还要抽查真实数据文件未被污染。

## [AI 依赖错误伪装 500] AI 调用未捕获或未区分 502/503
- **错误描述**：多个 AI 依赖端点直接 `await ai_service.xxx()`，无 Key/401/超时时异常冒泡到全局 handler，返回 500；批改、组卷等还把 AI 失败统一包成 500，前端无法区分"没配 Key"和"内部故障"。
- **正确做法**：端点统一捕获 AI 异常；错误文本含 401/authentication/api key/illegal header 时返回 503，其余 AI 调用失败返回 502；只有确定性程序内部故障才返回 500。新增 AI 端点时必须纳入无 Key 测试矩阵。

## [前端 defer 初始化竞态] app.js 为 defer 时页面脚本立即调用 $API
- **错误描述**：`base.html` 用 `<script defer src="/static/js/app.js">`，页面内联脚本在文档解析期立即执行 `init()/load*()`，此时 `$API` 尚未定义，首屏数据为空且控制台报错。
- **正确做法**：页面脚本只定义函数和事件绑定；所有需要 `$API` 的顶层初始化统一挂 `document.addEventListener('DOMContentLoaded', ...)`。defer 脚本在 DOMContentLoaded 前执行，因此该时机一定安全。

## [AI 意图 data 非对象] intent.data 为 null 时后续 .get 崩溃
- **错误描述**：分类器可能返回 `{"type":"style","data":null}`，代码直接 `idata = intent.get("data", {})` 后用 `idata.get(...)`，触发 `AttributeError` 并被全局异常处理成 500。
- **正确做法**：先取 `intent.get("data")`，仅当结果为 dict 时使用，否则归一为 `{}`；`/api/chat` 与 sessions 的意图分发共用这一规则。

## [下载响应头注入] 用户标题直接写入 Content-Disposition
- **错误描述**：笔记下载把 `note.title` 原样写入 `filename="..."`，标题含 CR/LF 时可注入额外响应头。
- **正确做法**：下载文件名先移除 `\r\n"\/:*?<>|` 等字符并限制长度，再写入响应头；其他下载入口也应使用 URL quote 或同类清洗。

## [示意图下标错位] 新生成图混入旧图列表后按绝对下标替换
- **错误描述**：`new_diagrams = list(q.diagrams or [])` 后追加新图，再拿合并列表替换 AI 返回的 `[[DIAGRAM:0]]`，导致标记 0 指向旧 `diagram_0.svg`，新图未被引用。
- **正确做法**：`[[DIAGRAM:N]]` 只对应本次 `diagram_prompts` 生成的新图列表；替换标记使用独立的新图列表，旧图只负责追加持久化。生成失败时删除未替换的占位符。

## [offset 只有下界没有上界] 超大正 offset 仍触发 SQLite OverflowError
- **错误描述**：多个列表接口只做 `max(0, offset)` 下界夹紧，`offset=999999999999999999999` 被原样传入 SQLAlchemy `.offset()`，SQLite 抛 `OverflowError` 成为 500。
- **正确做法**：limit 与 offset 都要同时夹上界（如 `min(max(0, offset), 1_000_000)`，或 Query `ge/le`）；任何会拼入 SQL LIMIT/OFFSET 的参数都必须两端有界。

## [意图对象非 dict] 只归一 data、未归一 intent 本身
- **错误描述**：`_extract_json("null")` 返回 `None`，`/api/chat` 直接 `intent.get("type")` 触发 AttributeError 500。`data:null` 修复只覆盖了 data 字段。
- **正确做法**：提取意图后先确认 `isinstance(intent, dict)`，非 dict 统一回退 `{"type":"chat"}`，再做 data 归一；`/api/chat` 与 sessions 都要遵守。

## [校准样本缺 type] 组件字典缺 type 时 KeyError 500
- **错误描述**：`/api/diagram/calibrate` 收到 `{"components":[{"x":10,"y":20}]}` 时，`_calibration_fingerprint` 和样本记录使用 `comp["type"]`，KeyError 冒泡成 500；`calibration-check` 空列表也会 ValueError。
- **正确做法**：写入型校准接口在进入指纹和样本前显式校验每个组件为 dict 且含 `type`，缺失时返回 400；查询型接口对空列表/缺 type 返回"无校准"而不是抛异常。

## [分支变量未初始化] diagram_prompts 为空时 NameError
- **错误描述**：`regenerate_question` 只在 `if diagram_prompts:` 内定义 `new_diagrams`，后续 `diagram_places` 仍引用旧的 `diagrams` 变量；AI 成功返回且 `diagram_prompts=[]` 时必定 `NameError` 500。
- **正确做法**：与"成功路径"有关的变量必须在分支前初始化（如 `marker_diagrams=[]`、`diagrams=list(q.diagrams or [])`），并用 monkeypatch 成功路径测试，不能只测无 Key 失败路径。

## [NaN 校验绕过] float 比较对 NaN 恒 False，范围检查形同虚设
- **错误描述**：`score = float("NaN")` 后 `score < 0 or score > total` 全为 False，NaN 绕过校验写入画像（落盘成非法 JSON 字面量 NaN），后续任何读取该字段的接口序列化时 500。
- **正确做法**：范围比较前先 `math.isfinite()` 校验，非有限值直接 400；所有从 JSON 导入的数值字段（分数、token、阈值）都要先过有限性检查。

## [AI 合法 JSON 非对象] json.loads 成功但顶层是 list/str/number/null
- **错误描述**：`json.loads("[]")` 不抛异常，只有语法错误才进 except；非 dict 结果随后 `.get()`/下标访问抛 AttributeError/TypeError，被外层包成泛化失败或直接 500，掩盖"模型返回非对象"的真实原因。
- **正确做法**：解析成功后立即 `isinstance(result, dict)` 断言，非 dict 统一走回退/重试并记录原始片段；`_extract_json`、OCR 校验、解题结果透传三处都要覆盖。

## [AI 非字符串字段 .strip()] 真值非字符串字段直接调用字符串方法
- **错误描述**：AI 返回 `{"title": 123}` 时 `(v or default).strip()` 抛 AttributeError；`knowledge_tags` 为字符串时被逐字符迭代成损坏标签。
- **正确做法**：持久化前统一做类型归一（str/int/float/bool → 文本；list 缺失 → 空），`canonicalize_tags` 入口对字符串按逗号拆分、对非 list 返回空。

## [组件 label 未转义] 含 & < 的标签使整图被消毒器静默降级为空白画布
- **错误描述**：组件库多数渲染函数把 label 未转义直接拼入 `<text>`，`&`/`<` 使 `ET.fromstring` 抛 ParseError，`_sanitize_svg` 把整张图降级成空 400×300 画布——静默数据损坏且无报错。
- **正确做法**：在统一渲染入口（`_render_component`）转义 label/label_x/label_y，组件库内部不得再二次转义；新增渲染函数不得自行拼接未转义文本。

## [渲染参数未归一] liquid/angle 传字符串或角度为 0 直接 500
- **错误描述**：`normalize_spec` 只夹 x/y/w/h，`liquid="0.5"` 进 `lq > 0` 抛 TypeError；`angle=0` 进 `rh/math.tan(0)` 抛 ZeroDivisionError；字符串 `"false"` 被当真值。
- **正确做法**：归一化层对 liquid/water_level（clamp 0~1）、angle（数值化+取模）、数值键（isfinite 回退）与布尔键（显式白名单归一）统一处理；渲染函数对 tan=0 等数学退化特判。

## [LIKE 通配符未转义] 用户输入 % _ \ 改变 SQL 匹配语义
- **错误描述**：`contains("%")` 命中全部行、`_` 命中单字符；组卷检索、题库过滤、笔记搜索均受影响。
- **正确做法**：封装 `_escape_like()`（`\`→`\\`、`%`→`\%`、`_`→`\_`），所有 `.like/.ilike/.contains` 用户输入处统一转义并传 `escape="\\"`。

## [占位符二次替换] 先插入用户内容再继续 replace 其它占位符
- **错误描述**：`{questions}`/`{user_prompt}` 内容恰好含 `{paper_size}` 等字面量时，后续循环会把它误替换成真实值，卷面/提示被篡改。
- **正确做法**：两阶段替换——先插入用户可控内容，其余占位符先用唯一哨兵占位、全部完成后统一回填。

## [前端猜测静态路径] 编辑器直接 fetch /static/questions/... 加载题目图
- **错误描述**：spec/svg 实际保存在 storage/questions 且 JSON 不允许经静态路径暴露，前端猜测的 `/static/questions/{qid}/diagram_0.spec.json` 恒 404，功能表现为"该题目没有图片"。
- **正确做法**：题目图/spec 加载统一走专用 API（`GET /api/diagram/spec/{qid}/{index}`）；前端不得猜测服务端文件布局，新增文件型数据必须先问"有没有对应的端口"。

## [学生作答图公开] 临时查询记录图片经公开静态路径可读
- **错误描述**：批改/搜题的作答图写入 `QUESTIONS_DIR`，公开 `/storage/questions/` 白名单只认文件名，学生手写作答可被第三方按 qid 拉取。
- **正确做法**：静态服务对 `questions` 分支额外查库，`source_type in ("search_query","correction_query")` 的临时记录一律 404；隐私数据不能只靠"随机 ID 不可猜"。

## [预览消毒缺失] 下载路径消毒而 iframe 预览路径裸奔
- **错误描述**：`get_paper` 预览只删 script/link/style 就返回给前端 iframe，`on*`/`javascript:`/`<iframe>`/`<meta refresh>` 全部可执行；AI 卷面含用户可控 prompt，构成存储型 XSS。
- **正确做法**：预览与下载共用同等级消毒（去高风险标签、事件属性、伪协议 URL）；前端 iframe 另加 sandbox 双保险。

## [会话/画像文件损坏 500] 合法 JSON 非对象与编码损坏未防护
- **错误描述**：`sessions/_load`、`user_profile.load_profile` 只捕获 JSONDecodeError，文件为 `[]`/`"x"` 时后续 `.get/.update` 抛 TypeError/AttributeError → 500；knowledge 文档字段访问在 try 之外。
- **正确做法**：读取后先 `isinstance(data, dict)` 断言；字段访问整体纳入 try；损坏一律记录并返回安全默认/明确错误。

## [孤儿目录泄漏] 文件写入在回滚 try 之外
- **错误描述**：上传分支先 `makedirs` 再写文件，只有 `db.commit()` 包在 try 里；磁盘满时文件与目录永久残留，且与同文件其它分支（先建后滚）不一致。
- **正确做法**：建目录、写文件、入库、提交整体一个 try，异常时 `rmtree(folder, ignore_errors=True)` 回滚，与兄弟分支结构对齐。

## [提交与状态写之间崩溃] 子题已提交但 split_complete 未落盘
- **错误描述**：自动分题先 `db.commit()`（子题已入库）后写任务状态；两步之间进程崩溃，重启恢复重新切题并新建一批子题，旧子题成孤儿。
- **正确做法**：恢复入口幂等——检测到 `split_complete=True` 的状态文件直接续跑既有 split_question_ids；或调整落盘顺序让"已提交"可被识别。

## [retry 非原子 claim] 读取后直接改状态，无 WHERE 条件抢占
- **错误描述**：`retry` 用 `db.get` + `q.status = "pending"` 提交，两次并发 retry 都通过状态检查，重复写任务状态并双重 spawn；与 process/batch 的 `UPDATE ... WHERE status IN (...) AND rowcount` 抢占不一致。
- **正确做法**：所有"启动处理"类端点统一原子 UPDATE 抢占，`rowcount != 1` 返回 409 后再启动后台任务。

## [导出半成品残留] 多文件导出中后段失败不清前段产物
- **错误描述**：`export_word` 先落盘 paper.docx 再生成 answer.docx，后段失败抛异常时 paper.docx 已残留，可能被误当可用产物。
- **正确做法**：记录本次已生成路径，异常时统一删除并清空对应 DB 字段，不留伪路径。

## [原生 prompt 弹窗] 浏览器原生 prompt() 风格与全站不统一
- **错误描述**：`prompt('重命名对话', default)` 直接用浏览器原生弹窗，样式、键盘行为与 `$confirm/$prompt` 弹窗不一致，部分 webview 拦截或禁用。
- **正确做法**：改用 `await $prompt('重命名对话', {default, title, placeholder})` 统一弹窗；空值校验 `===null || ===undefined`；空字符串提交后给 `$toast` 提示。

## [原生 confirm 弹窗] editor.js 校准提示用 confirm() 风格不一致
- **错误描述**：`editor.js:checkCalibration` 用 `if(confirm('...'))` 调用原生 confirm，部分 webview 拦截或样式不统一。
- **正确做法**：改用 `if(await $confirm('...'))`；`$confirm` 不可用时兜底 `window.confirm` 防止失联。

## [侧栏 JS 抛错阻断全站] defer 脚本访问 DOM 元素未做 null 守卫
- **错误描述**：`app.js` 在 IIFE 外直接 `document.getElementById('sideBar').classList.add('open')`；base.html 结构异常时该行抛 ReferenceError，阻断后续 `$API/$toast/$confirm/$esc` 初始化，全站瘫痪。
- **正确做法**：所有 defer 脚本访问 base.html 模板元素时加 `if(el) el.xxx` 守卫；关键状态（如侧栏默认打开）放在 IIFE 中以便异常局部化。

## [侧栏 FOUC] JS 启动前侧栏默认 left:-200px 隐藏
- **错误描述**：`theme.css` 给 `.sidebar{left:-200px}` 默认隐藏，依赖 JS 加 `.open` 才显示；JS 加载慢或失败时桌面端 200px 空白无入口。
- **正确做法**：默认 `left:0` 静态显示（无 JS 兜底），加 `<html class="js-side-nav">` 同步 IIFE 守卫；CSS 用 `.js-side-nav .sidebar{left:-200px}` 隐藏。JS 启动后 IIFE 按视宽与 `localStorage.sb_user_closed` 决定是否加 `.open`，用户收起偏好持久化，刷新按偏好回填 `body.sb-closed` 与 `aria-expanded`。

## [dashboard 静默 spinner] daily-quote 失败 spinner 永不消失
- **错误描述**：`$API.get('/api/daily-quote')` 只有 `.then` 无 `.catch`，失败时 DOM 仍是 `<span class="spin"></span>`，用户看到永远转圈。
- **正确做法**：加 `.catch` 显示 fallback 文案并替换 `textContent`；`then` 内部也加 `if(r && r.quote)` 守卫防后端返回 `{ok:false}` 渲染 `undefined`；清掉残留的 `<span class="spin">` 子元素。

## [paper_detail 多选下载 else-if 吞答案] 优先级判断 bug
- **错误描述**：`if(score) mode=score; else if(answer) mode=qa;` 当 score+answer 同时勾选时答案被跳过，用户拿不到答案文件。
- **正确做法**：拆为独立 `if`，按 `试卷→答案→得分点→答题卡` 顺序分别 `window.open` 同 baseURL 不同 mode；多份下载时 toast 提示"已为您分 N 个文件下载"。

## [API 错误透出英文] fetch 网络层与 HTTP 状态码未本地化
- **错误描述**：`fetch().catch` 抛 `TypeError: Failed to fetch`；`_parse` 在 detail 缺失时抛 `'500 Internal Server Error: ...'`；中文用户看到英文原始信息。
- **正确做法**：统一 `_friendlyStatus(status, rawText)` 把 401/403/404/409/429/5xx 映射为中文；fetch 网络层失败映射为"网络异常，请检查连接"；保留后端 `detail` 字段优先级。

## [静态弹窗缺 ARIA] base.html 内联 `<div class="modal-overlay">` 没有 role/aria-modal
- **错误描述**：`<div class="modal-overlay" id="detailModal">` 在 HTML 中已存在但 display:none，加 `.show` 才显示；初始无 `role="dialog"` / `aria-modal="true"`，读屏无法识别。
- **正确做法**：`enhanceModal` 改为扫描所有 `.modal-overlay`（不再限定 `.show`），即使隐藏也设 ARIA；可见时再迁移焦点；MutationObserver 加 `attributes:true, attributeFilter:['class']` 监听 class 变化触发增强。

## [enhanceModal 抢 $confirm 焦点] 自动焦点迁移覆盖 ok.focus()
- **错误描述**：`enhanceModal` 在可见时 rAF 迁移到首个可聚焦元素（确认框的"取消"按钮），覆盖 `$confirm` 显式的 `ok.focus()`，用户按 Enter 触发取消。
- **正确做法**：`enhanceModal` 检测 `modal.classList.contains('confirm-overlay')` 直接跳过自动焦点迁移。

## [移动端 16px 防缩放漏网] 仅覆盖 text/number/email 等，未覆盖 time/date
- **错误描述**：iOS Safari 在 font-size < 16px 时会缩放聚焦元素；CSS 只覆盖 `text/number/search/email/password/tel/textarea/select`，`time/date/datetime-local/month/week/url` 在 iPad 横屏仍触发缩放。
- **正确做法**：把所有可能聚焦的 input 类型一并覆盖；补 769~1024 iPad 横屏断点（768px 断点不覆盖此区间）。

## [createBank 假按钮] 题库数据模型与 UI 按钮语义冲突
- **错误描述**：题库由题目 `bank` 字段派生（Design.md），不存在独立实体；但前端"创建题库"按钮调用 `createBank()` 只 toast"题库会在上传题目时自动创建"，对用户透明但又占一个按钮。
- **正确做法**：把按钮文字改为"设为目标"，仅预填 `uploadBank` 输入框，加正则校验名称（1-32 字，字母数字下划线连字符中文）；toast 明确"已设为目标题库，上传第一题后即可在列表看到"。

## [暗色主题次级文本对比度过低] --text3 过深无法辨识
- **错误描述**：暗色主题下 `--text3:#55555e` 对比度约 2.4:1，hint/时间戳等次级文本无法辨识。
- **正确做法**：提亮至 `#7a7a85`（约 4.5:1 达 AA）；保留与 `--text2` 的视觉层级（`--text2` 仍较亮）。

## [toast 遮挡顶栏] 移动端固定 top:16px 覆盖 topbar 关闭按钮
- **错误描述**：`#toastContainer` 在所有视宽下都 `top:16px right:16px`；移动端 topbar 高 52px 内含侧栏关闭按钮，toast 完全覆盖。
- **正确做法**：移动端 `@media(max-width:768px){#toastContainer{top:60px;right:8px;max-width:calc(100vw - 16px)}}`，让出顶栏空间。

## [移动端 modal 无 safe-area-top] 刘海屏遮挡弹窗顶部
- **错误描述**：移动端 `.modal` 只设 `padding:14px`，未使用 `env(safe-area-inset-top)`；刘海/灵动岛机型下顶部按钮被遮挡。
- **正确做法**：`.modal{padding-top:max(14px,env(safe-area-inset-top))}`；与现有 `.modal{padding-bottom:max(18px,env(safe-area-inset-bottom))!important}` 风格统一。

## [&times; 关闭按钮] 不符合全站视觉一致性且无 aria-label
- **错误描述**：`<button class="modal-close">&times;</button>` 用乘号字符作为关闭图标，与全站 SVG 图标风格不一致；缺 `aria-label="关闭"` 时读屏读"乘号"。
- **正确做法**：所有 `.modal-close` 统一使用 `<svg width="14" height="14" viewBox="0 0 24 24">...</svg>` 关闭图标；带 `aria-label="关闭"`；通过 `app.js` 的 `window._closeIconSvg()` 或直接内联 SVG 引用同一份路径。

## [AI 空 choices 数组] data["choices"][0] 在 AI 返回空数组时 IndexError
- **错误描述**：AI API 可能返回 `{"choices": []}` 的合法响应（如内容过滤触发），此时 `data["choices"][0]` 直接抛 IndexError，被外层 except 吞掉后重试，浪费资源且可能无限循环。
- **正确做法**：添加 `choices = data.get("choices") or []` 空数组校验，空数组时 raise ValueError 触发明确的错误处理路径。

## [异常过度吞掉] except Exception 捕获所有异常掩盖编程错误
- **错误描述**：`except Exception as e:` 捕获所有异常（包括 KeyError、AttributeError、TypeError 等编程错误），统一转为重试。代码 Bug 被当作网络错误重试多次后才抛出，极大增加调试难度和不必要的 API 消耗。
- **正确做法**：缩小异常捕获范围，只捕获预期的网络/超时异常：`except (httpx.HTTPError, OSError, asyncio.TimeoutError)`，其余异常直接 raise。

## [递归 fallback 爆炸] _call 在 fallback 后再次递归调用自身
- **错误描述**：`_call` 在重试耗尽后递归调用自身触发 fallback，而 fallback 又会执行 MAX_RETRIES 次重试。若 fallback 也失败，还会再次尝试 fallback，导致无限递归直到栈溢出。
- **正确做法**：添加 `_is_fallback: bool = False` 参数，当已经是 fallback 时不再递归调用，直接执行一次 API 调用。

## [None 解引用] choice.get("message", {}) 在 message 为 None 时返回 None
- **错误描述**：若 AI 返回 `{"choices": [{"message": null}]}`，`choice.get("message", {})` 返回 None（因为 key 存在但值为 None，`.get()` 不会返回默认值），然后 `None.get("content")` 抛 AttributeError。
- **正确做法**：改为 `(choice.get("message") or {}).get("content") or ""`。

## [内部错误泄露] 异常消息直接返回给用户
- **错误描述**：`return final_reply or str(e), True` 将原始异常消息（可能含内部路径、模型名、API 地址）作为 "reply" 返回给用户，存在信息泄露风险。
- **正确做法**：返回通用错误提示，详细错误只记录到日志。

## [路由装饰器缺失] handler 函数忘记加路由装饰器导致端点未注册
- **错误定义**：在 APIRouter 文件中定义了 handler 函数但忘记添加 `@router.get()`/`@router.post()` 等装饰器，导致端点未注册到路由表，前端调用时返回 404。
- **正确做法**：定义 handler 函数时立即添加对应的路由装饰器；新增路由后用 `app.routes` 自检确认端点已注册。

## [fetch 参数结构异常] fetch 调用使用三参数或 headers 位置错误
- **错误描述**：`fetch(url, {headers:...}, {method:'POST',...})` 使用三参数调用 fetch，但 fetch API 只接受两参数（url, options）。headers/method/body 应合并到同一个 options 对象中。
- **正确做法**：`fetch(url, {method:'POST', headers:{...}, body:...})`；所有选项合并到第二个参数对象中。

## [authToken 引用不一致] 模板中使用 window._authToken 而非 window.$API._authToken
- **错误描述**：多个模板文件（paper_detail.html、notes.html、diagnose.html）在 fetch 调用中使用 `window._authToken`，但 `window._authToken` 从未定义，仅靠 `||'Ntmhzsgtc'` 兜底生效。而 `app.js` 中 `$API._authToken` 已正确定义。这种不一致容易在后续维护中引发 bug。
- **正确做法**：统一使用 `window.$API._authToken || 'Ntmhzsgtc'`，与 editor.js 保持一致；或优先使用 `$API` 封装方法避免直接操作 token。

## [空URL配置生效] custom_api 只有 key 没有 url 时映射条目仍写入（fallback/scope 两处同构）
- **错误描述**：`_reload()` 构建 fallback_config 与 custom_scope_map 时只检查 key 是否存在；url 为空字符串时条目照常生效，每次主模型失败/自定义调用都先向空 URL 发请求（httpx InvalidURL），浪费调用并持续写错误日志。settings API 与导入路径有 URL 强校验，仅旧 settings.json 手编数据会触发。
- **正确做法**：构建映射时空 url 一律 `logger.warning + continue` 跳过；读取配置字段统一用 `(api.get("url") or "")` 而非 `api.get("url", "")`——显式 null 时 `.get` 的默认值不生效返回 None，直接 `.strip()` 会崩掉整个 `_reload()` 单例构造。

## [消毒实现分叉] 前端多套 HTML 消毒函数各自漂移
- **错误描述**：questions.js 对比模式有独立 `_cmpSanitizeHtml`，仅单次 `replace(/javascript:/gi,'')`，可被 `javajavascript:script:` 双写重组绕过，也不处理属性名列表（如 xlink:href）；而 app.js `_sanitizeHtml` 是分层消毒（标签+属性+协议）。同一危险向量在两个入口防护等级不一致。
- **正确做法**：新增任何渲染 AI/用户 HTML 的路径必须复用统一消毒实现（或抽成共享模块）；独立消毒函数要么删除要么与主消毒同轮同步升级。修复消毒绕过时先想"还有几套消毒实现"。

## [AI失败静默回退] 结构化失败回退原文但保留 is_structured 标志
- **错误描述**：sessions.py AI 笔记结构化失败时 `except Exception: structured = content` 静默回退原文，无任何日志，且入库仍标 `is_structured=True, source_type="ai_generated"`——失败被伪装成成功，后续排查无从下手（[静默失败]模式的变体）。
- **正确做法**：回退分支必须 `logger.warning` 留痕；若产品语义允许"原文即结果"，标志位应如实反映或补充说明字段。

# 2026-09-05 深夜轮：Android/前端健壮性模式

## [FileProvider getPath 双 bug] 拍照直拍成功后恒返回空结果 + 缓存照片永不清理
- **错误描述**：MainActivity.onActivityResult 里用 `content://` Uri 的 `getPath()` 反推真实文件路径——FileProvider 的 content URI path 与磁盘路径不对应，`File(path).exists()` 恒 false，拍照成功也判定为空；反向 bug：失败/取消路径用同样的 `new File(uri.getPath()).delete()` 清理也删不到真文件，`cache/camera/` 残留堆积。
- **正确做法**：拍照输出用真实 File 对象跟踪（pendingCameraFile: File），URI 只给 Intent 用；清理对 File.delete()。凡是"从 content:// getPath 反推磁盘路径"的代码都要警惕——FileProvider 的 path 前缀映射由 file_paths.xml 决定，不是磁盘路径。

## [getUserMedia 不绑 origin] WebView 授权请求未校验来源
- **错误描述**：onPermissionRequest 对任何 origin 的 getUserMedia 请求都映射 Android 运行时权限并 grant；一旦 WebView 被引导到第三方页面（重定向/iframe），第三方可拿摄像头/麦克风。
- **正确做法**：grant 前校验 `request.getOrigin().getScheme()+host+port` 与配置的服务器地址三元组一致；30x 重定向后 origin 变化要重新校验；不匹配一律 deny。

## [权限回调权限集误判] onRequestPermissionsResult 按回调权限集统一判定
- **错误描述**：多个 pending 权限请求（摄像头+麦克风不同请求）共用一次系统回调，回调里用"本次回调的 permissions 全部 granted"去 grant 所有 pending 请求——本次没请求的权限被误判。
- **正确做法**：pending 请求按各条目自身需要的 Android 权限清单逐一检查授予状态，不用回调入参的权限集做全局判断。

## [同类端点锁定守卫漏配] is_resolved 守卫只加在部分同类端点
- **错误描述**：结构梳理图端点有 is_resolved 守卫，但 diagram.py 的 generate/insert 漏配——已锁定题目仍可被 AI 覆盖图形；同类"写题面"端点群守卫状态不一致。
- **正确做法**：新增修改题目内容的端点时，列出同守卫要求的既有端点清单逐一对齐（grep is_resolved 看邻居），不要只抄相邻一个。

## [gradlew.bat 环境性失败] 中文路径下 wrapper classpath 解析失败
- **错误描述**：项目路径含中文时 `gradlew.bat assembleDebug` 报 classpath 解析失败（环境性问题，非代码错误），容易被误判为工程损坏。
- **正确做法**：绕过 wrapper 启动器，直接 `java -cp gradle\wrapper\gradle-wrapper.jar org.gradle.wrapper.GradleWrapperMain assembleDebug`；构建失败的先排查路径编码，再怀疑工程。

# 2026-09-06 黑板轮模式

## [await 同步函数] 同步函数被 await 调用，TypeError 被宽 except 降级吞掉
- **错误描述**：`_save_board_svg` 是同步函数却在 async 流程里 `await` 调用——await 一个 str 抛 `'str' object can't be awaited`，被 `_apply_board_ops` 的宽 except 捕获后降级为"板书图形生成失败"文本。功能表现为静默降级，真实原因是编程错误，极难从表象定位。
- **正确做法**：命名无法区分 async/同步（无 as_ 前缀约定时），await 前确认目标定义；降级路径的 except 里对 TypeError 等编程错误应 log_error 留痕而非只走用户可见降级文案。

## [MiMo 默认思考耗尽预算] MiMo V2.5 轻任务未关思考导致 content 空转兜底
- **错误描述**：MiMo V2.5 默认开启思考模式，轻任务 max_tokens=256 被思考耗尽 → content 空 → _call 空响应重试 2 次仍空 → 才走兜底，每次轻任务白等约 10 秒且"轻活走 MiMo"的目标实际失效。
- **正确做法**：MiMo 轻调用 payload 必须显式 `"thinking": {"type": "disabled"}`（DeepSeek v4 同理，v4 默认也是思考模式）。接入新模型先查默认 thinking 行为。

## [删 import 漏查引用] 删除导入时只查了短名，漏了同名长名引用
- **错误描述**：清理 chat.py 未用变量时删掉 `from config import load_settings as _load_settings`，只 grep 了 `_s`，漏掉同函数更深处另一处 `_load_settings()` 引用 → 运行时 NameError 500（pytest 子进程冒烟才暴露）。
- **正确做法**：删除任何 import/变量前，同时 grep 短名与完整名（`_s` 与 `_load_settings`）；凡 `as` 别名导入，两个名字都要查。

## [截断窗口吃掉测试样本] 按序截断的逻辑测试把非法样本放列表尾部
- **错误描述**：ops 指令列表按 _BOARD_OPS_MAX 截取前 N 条，测试把非法条目 append 在尾部——截断后非法条目根本不执行，断言 skipped 数永远对不上。
- **正确做法**：测试带截断/配额的逻辑时，先算清截断窗口覆盖哪些样本，非法样本放窗口内；断言基于窗口内实际执行集合。

## [离线缓存掩盖HTTP错误] 缓存回退不区分网络层失败与服务器错误响应
- **错误描述**：离线层 GET 回退逻辑写在 fetch 的 catch 里，而 $API._parse 对 401/404/5xx 抛的错也走同一个 catch——服务器明确返回的错误（已删题目、鉴权失败）被旧缓存数据静默顶替，用户看到的是"看起来正常"的过期数据。
- **正确做法**：缓存回退必须只针对**网络层失败**（fetch reject，打 err.isNetwork 标记）；HTTP 非 2xx 是服务器明确响应，原样抛给错误分支。写"network-first"缓存时，回退条件和写入条件要分别定义清楚。

## [重复重放无在飞守卫] 定时器/事件/启动三条触发路径并发调用重放
- **错误描述**：离线上传队列的 replay 同时被启动补传、online 事件、60s 定时器触发，无在飞守卫时同一队列项被并发 fetch，产生重复上传（图片重复入库）。
- **正确做法**：重放入口加 `_replaying` 布尔守卫，在飞期间直接返回；finally 释放。凡是"多触发源 → 消费同一队列"的设计都要有在飞互斥。

# 2026-09-06 深夜检修轮补充（全量后端审计发现）

## [NaN 钳制方向错误] min/max 夹紧对 NaN/Infinity 反向生效（再现：AI 相似度满分）
- **错误描述**：search.py AI 相似度分数 max(0.0, min(1.0, nan)) 中 nan<1.0 为 False → min 返回 1.0 → 满分；错题被判"高置信匹配"直接进入自动批改。与前两次（profile 分数、face confidence）同一根源第三次再现。
- **正确做法**：凡从 AI/JSON 来的 float 先 math.isfinite，不过即跳过该条或回退默认，禁止依赖 min/max 链夹紧。

## [批量路径 str(exc) 泄漏] 单题路径已映射 502/503、批量循环却原样回传异常文本
- **错误描述**：correction 批量/整卷循环把 str(exc) 写进 results[].error（含 URL/网关信息），同文件单题路径早已用稳定文案映射。
- **正确做法**：批量循环的每项失败也走统一映射助手（如 _safe_correction_error），原文只进日志；新增批量端点时对照同文件单题路径的异常处理等级。

## [SQLite 下 FOR UPDATE 无效] 检查-后-写竞态在 SQLite 上依旧可双入
- **错误描述**：search add-to-bank 用 SELECT...FOR UPDATE 读状态再改写，SQLite 忽略行锁，双击/双标签页都能通过检查并各自启动处理任务。
- **正确做法**：与 batch_process 同款——update(T).where(id==..., status==旧状态).values(status=新状态) 后检查 rowcount == 1，失败返回 409；读取的数据用本地变量承接，不再回读过期 ORM 属性。

## [追加式写库重复] 会话解题以标记追加答案，整条指令重试即堆积重复内容
- **错误描述**：sessions solve_q 把解答按 <!-- AGENT_SESSION_<sid> --> 标记追加到 answer_html；循环中途失败后重试，已写过的题再次追加；且循环无逐题 try，单题 AI 失败拖垮整个请求（前面题目已提交）。
- **正确做法**：写库前检查本会话标记是否已存在（存在即跳过去重）；循环体逐题 try/except，失败题记录占位回复后 continue。

## [逐张校验漏总量上限] 数组字段逐张合法、总和超限
- **错误描述**：notes upload-images 每张 15MB×10 张全部通过单张校验，解码后总量 150MB 全量进内存（Pydantic 一份 + 解码一份）。
- **正确做法**：数组类上传字段在 validator 内逐张累计解码字节数，超总量上限（如 60MB）立即拒绝；单张上限不能替代总和上限。

## [陈旧行号核验] 审查报告的发现不核对当前文件就照单修
- **错误描述**：2026-09-06 核验轮，子 Agent 报告的 15 条"高危"（focus_service:543 裸 ai_chat、_board_path 用 rfind('_') 致路径穿越、offline.js 队列 GET /tasks、FocusApp 未导出等）逐条对当前文件核实后**全部为已修复项或误报**——报告基于同日上午旧快照与过期设计文档行号；若照单"修复"会凭空重构正常代码。
- **正确做法**：任何外部审查结论先做证据核验（read 当前行号、grep 被点名符号、查实际启动命令 `--app-dir` 验证 import 惯例），标注 真缺陷/已修复/误报 三类，只修真的；给审查 Agent 的简报里注明"以当前文件为准，文档行号仅供参考"。
## [Kimi 无视觉兜底链] 视觉回退链末位放 Kimi，全链失败时白烧调用
- **错误描述**：多处以 kimi_chat 发送 image_url 内容块作视觉兜底（kimi_ocr/glm4v_reference_svg/face_analysis/统一视觉入口）——Kimi 无视觉输入能力，该调用必然 400，zhipu/mimo 全挂时白烧 1-2 次调用并拖慢报错。
- **正确做法**：视觉链按 Fact.md 分工 MiMo 全模态优先 → ZhipuAI 视觉回退；Kimi 只做文本搜索，不进任何视觉链。
- **本轮出现**：功能检查轮（2026-09-07），ai_service 三处 + face_analysis 一处全清。

## [惰性路由对象] FastAPI 0.141 include_router 产生 _IncludedRouter，静态对账必须递归展开
- **错误描述**：app.routes 顶层只出现 N 个 _IncludedRouter 复合对象（无 path 属性），逐条读 path 的对账脚本会误报全部 router 路由缺失（96 条假 MISSING）。
- **正确做法**：对 `hasattr(r, "original_router")` 的对象递归展开 original_router.routes；另注意脚本自身 sys.path/chdir 被误删时会加载错误模块，"灵异结果"先查探针自身。
- **本轮出现**：功能检查轮路由对账探针。
