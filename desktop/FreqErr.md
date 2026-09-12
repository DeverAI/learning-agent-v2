# FreqErr — 常见错误类型记录

> 记录项目中重复出现的错误类型及正确做法，供后续开发参考。

## [配置读取] 从 ConfigManager 实例而非 `cfg.settings` 读取字段
- **错误描述**：`ConfigManager()` 返回的是配置管理器单例，实际配置字段都在 `.settings` 对象上。直接从 `cfg.xxx` 读取会报 `AttributeError`。
- **正确做法**：`cfg = ConfigManager(); value = cfg.settings.xxx`
- **本轮出现**：`core/focus_engine.py:start()` 中读取 `screen_capture_interval_sec` 时错用 `getattr(cfg, ...)`，已修复为 `getattr(cfg.settings, ...)`。

## [信号生命周期] 信号签名变更后 connect/disconnect 未同步
- **错误描述**：`Signal` 签名修改后，所有 `connect` 和 `disconnect` 位置必须同步更新，否则槽函数参数不匹配或断开失败。
- **正确做法**：全局搜索该信号的 `connect` / `disconnect` / 发射点，统一更新签名。
- **本轮出现**：`FocusView`  originally 未连接 `focus_ended`，导致自然结束后 UI 不复位，已补连并补断。

## [UI 状态] 异常路径未恢复按钮/进度条状态
- **错误描述**：在禁用按钮、显示进度条后，如果某个分支提前返回或抛异常而未恢复 UI，会导致用户界面卡死。
- **正确做法**：在 `try/finally` 或每个提前返回/异常分支中恢复 UI 状态。
- **本轮出现**：`DialogView._upload_file()` 读取文件失败未恢复 `send_btn` 和 `progress`；`_on_chat_detected` / `_on_code_output` 未恢复，已修复。

## [类型校验] 防护性 `isinstance` 检查在取值之后执行
- **错误描述**：先对变量调用 `.get()` 再判断 `isinstance`，非预期类型会在判断前就抛异常，使防护逻辑形同虚设。
- **正确做法**：先 `isinstance` 判断，通过后再取值。
- **本轮出现**：`DialogView._screenshot_analyze()` 先 `result.get(...)` 后 `isinstance(result, dict)`，已调整顺序。

## [回调兼容] 用宽泛的 TypeError 捕获做旧签名兼容
- **错误描述**：用 `except TypeError` 回退旧签名会吞掉回调内部真正的类型错误，且无法处理带默认值的参数。
- **正确做法**：使用 `inspect.signature` 判断参数数量，仅在确认参数数量不足时才回退。
- **本轮出现**：`FocusEngine._on_screen_result()` 中旧签名回退逻辑，已改为 `inspect.signature` 判断。

## [HTML 转义] 对同一内容重复转义
- **错误描述**：在调用 `_escape_html` 等统一转义函数之前，先对某一部分字符做显式 `replace`（如 `url.replace('"', '&quot;')`），会导致 `&` 被二次转义为 `&amp;quot;`，破坏输出并可能引发安全/显示问题。
- **正确做法**：统一转义交给单一函数完成，不要对同一段内容先局部替换再整体转义。
- **本轮出现**：`ui/md_renderer.py:_render_link` 中先替换引号再 `_escape_html`，已移除冗余 `replace`。

## [图形项] 对 QGraphicsItem 调用 deleteLater()
- **错误描述**：`QGraphicsItem` 派生类（`QGraphicsEllipseItem`、`QGraphicsItem` 自定义类）不支持 `deleteLater()`，会抛 `RuntimeError` 或警告。
- **正确做法**：让 `QGraphicsScene` 自动管理 item 生命周期，从场景 `removeItem()` 后无需手动释放；切勿调用 `node.deleteLater()`。
- **本轮出现**：`ui/graph_editor.py` 中 `remove_node`/`remove_edge` 误用 `deleteLater()`，已移除。

## [资源管理] QPainter 未在异常路径释放
- **错误描述**：`QPainter(pixmap)` 进入绘图上下文后若中途抛异常未 `end()`，下一次使用会报 "QPainter: paint device returned engine that is already painting"。
- **正确做法**：把 painter 初始化置为 `None`，用 `try/finally` 保证 `painter.isActive() and painter.end()` 必定执行。
- **本轮出现**：`ui/graph_editor.py:export_png()` 已用 `try/finally` 修复。

## [UI 缩放] 滚轮缩放无上下界
- **错误描述**：`QGraphicsView.scale()` 累乘后极易达到极小（0.001）或极大（1000+），节点不可见或渲染卡顿。
- **正确做法**：维护 `_scale_factor`，每次缩放前 clamp 到合理区间（如 0.1~10）。
- **本轮出现**：`ui/graph_editor.py:wheelEvent` 已加入 `0.1 <= factor <= 10` 限制。

## [鼠标事件] 子 QGraphicsItem 拦截父节点事件
- **错误描述**：节点上的 `QGraphicsSimpleTextItem` 标签默认接收鼠标事件，导致点击/拖拽节点失败。
- **正确做法**：`label.setAcceptedMouseButtons(Qt.NoButton)`，让事件穿透到父节点。
- **本轮出现**：`ui/graph_editor.py:NodeItem.__init__` 已设置。

## [LaTeX/文本清理] 宽泛正则误伤合法结构
- **错误描述**：用通用正则清理"装饰性"符号时，可能误删数学公式、命令参数等合法结构（如花括号清理误伤 `\frac{1}{2}`）。
- **正确做法**：清理规则应针对具体上下文；无法安全区分时优先保留原始结构，宁可多保留也不破坏语义。
- **本轮出现**：`ui/md_renderer.py:_latex_to_unicode` 中 `(?<![\\\w])\{(\d+)\}` 误删分数 `{2}`，已移除该正则。

## [图解析] 简化模式用 split 处理"节点名含分隔符"
- **错误描述**：用 `line.split('-', 1)` 解析 `node-1 - node-2` 时，第一个 `-` 即被切，导致 src 变 `node`、dst 变 `1 - node-2`。
- **正确做法**：用整体正则匹配整行（`simple_dash_re`），或先识别最长分隔符（`-->` / `->` / `—`）再 split；节点名字符类只排除空白/换行。
- **本轮出现**：`ui/graph_renderer.py:parse_graph_block` 简化分支已重写为正则匹配。

## [图解析] _KV_RE 强制 value 非空
- **错误描述**：`r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$"` 中 `(.+?)` 至少匹配一个字符，导致 `nodes:` 这类无值行匹配失败。
- **正确做法**：用 `(.*?)` 允许空值，空字符串走后续业务判断。
- **本轮出现**：`ui/graph_renderer.py:_KV_RE` 已改为 `(.*?)`。

## [UI 缓存] 代码块闭合后未清状态
- **错误描述**：处理 `mermaid` / `graph` 等特殊代码块时，若不重置 `code_lines / code_lang`，函数末尾兜底的 `_close_code()` 会把已处理内容再次输出为 `<pre>`。
- **正确做法**：特殊分支自己处理完后，必须 `code_lines.clear(); code_lang = ""`，与 `_close_code()` 走统一收口。
- **本轮出现**：`ui/md_renderer.py` 闭合 ``` 时已统一清空。

## [SVG] 主题字段为 None 时拼出 `fill="None"`
- **错误描述**：`str(theme.get("text"))` 当值为 `None` 时得到字面量字符串 `"None"`，导致 SVG 颜色被忽略。
- **正确做法**：颜色取数走 `_color(value, default)` 工具：`isinstance(value, str)` 且非空时返回值，否则返回 default。
- **本轮出现**：`ui/graph_renderer.py:render_graph_svg` 已统一用 `_color` 兜底。

## [主题切换] 子串匹配不可靠
- **错误描述**：`if "graph-container" in self._render_html` 依赖渲染结果字面量；用户消息恰好含该字符串时会误判触发重渲染。
- **正确做法**：维护显式 `self._has_graph: bool` 标志位，在 `_ensure_rendered` 时设置，`update_theme` 直接读。
- **本轮出现**：`ui/dialog_view.py:MessageBubble` 已用 `_has_graph` 替代子串匹配。

## [SVG] marker id 在多气泡中冲突
- **错误描述**：HTML/SVG 规范要求 id 唯一；多个气泡的 graph 用同一 `id="graph-arrow"` 会导致箭头方向渲染异常。
- **正确做法**：marker id 用 uuid / 计数器后缀化（如 `garr{i}`），每气泡独立。
- **本轮出现**：`ui/graph_renderer.py:render_graph_svg` 已支持 `marker_id` 参数；`ui/md_renderer.py` 用 `f"garr{i}"` 注入。

## [图解析] YAML 段不匹配时静默丢项
- **错误描述**：YAML 模式下若段标识已设置（`section="nodes"`），后续行不匹配该段正则时只 `continue`，既不重置段也不尝试另一种格式，导致混排（如 YAML 头后接简化边）丢数据。
- **正确做法**：段不匹配时降级尝试另一种正则（YAML 项 ↔ 简化边）；同时 `section=None` 时按行内容自适配。
- **本轮出现**：`ui/graph_renderer.py:parse_graph_block` 已加段不匹配降级 + 段未知时按行自适配。

## [错误日志] 用 `try/except: pass` 静默 logger 导入失败
- **错误描述**：在 try/except 中调用 `from utils.helpers import logger`，外层 `except Exception: pass` 会把 logger 自身的 import 失败一并吞掉，导致所有错误日志消失。
- **正确做法**：logger 导入失败时降级到 `sys.stderr` + `traceback.print_exc()`，绝不静默。
- **本轮出现**：`ui/graph_renderer.py:render_graph_svg` 异常处理已改为 `logger.warning(..., exc_info=True)` + 兜底 stderr。

## [LaTeX 兜底] 双向词边界与贪婪匹配冲突
- **错误描述**：对未在 map 里的 `\cmd{arg}` 走兜底正则时，贪婪 `([A-Za-z]+)` 会吸收整个前缀（如 `\foofrac{x}` 匹配 `foofrac` 而非 `frac`），加 `(?<![A-Za-z])` 后行也救不回来（`\` 前面没字母）。
- **正确做法**：保留原始贪婪匹配作为已知限制，在文档中说明"未映射命令显示为 `[\cmd]{arg}`"；如需更细粒度，需枚举已知 LaTeX 命令集。
- **本轮出现**：`ui/graph_renderer.py:_latex_to_unicode_simple` 已在 docstring 中标注此限制。

## [布局] 环形布局未应用边界裁剪
- **错误描述**：力导向布局在末尾对坐标做 `max(margin, min(width-margin, x))` 裁剪，但环形布局直接输出余弦/正弦坐标，n 较小时最右节点可能超出 `[margin, width-margin]` 范围。
- **正确做法**：环形布局输出后也跑一次同样的裁剪，与力导向保持一致。
- **本轮出现**：`ui/graph_renderer.py:_circular_layout` 已加裁剪。

## [虚拟滚动] _new_dialog 不重置 _rendered_count 导致消息被吞
- **错误描述**：原 `_new_dialog` 只清空 `_render_start`，但 `_rendered_count` 残留（如 15）。新建对话后第一次 `_add_message` 走"窗口已满"分支，调 `_remove_oldest_bubble`（layout 已空，无事可做）后 `_add_bubble` 写入新消息；**第二条消息时却把刚加的第一条消息删掉**，造成"消息消失"假象。
- **正确做法**：新建/删除对话时统一重置 `_render_start=0`、`_rendered_count=0`、`_last_supervisor_tip_at=0`，并清空 layout。
- **本轮出现**：`ui/dialog_view.py:_new_dialog` / `_delete_dialog` 改用 `_reset_render_state()` 统一处理。

## [虚拟滚动] 窗口滑动时 _render_start > 0 会留下"消息空洞"
- **错误描述**：当用户已"加载更早"（`_render_start > 0`），新消息到来走"窗口已满 → 滑动窗口"路径，**把当前布局最前面的气泡删掉**（并非 `_render_start` 那个），导致新加载的消息和后续新增消息之间出现"消息空洞"。
- **正确做法**：检测到 `_render_start > 0` 时，**只追加数据、不渲染新气泡**（不破坏用户阅读位置），并提示"底部有新消息"。
- **本轮出现**：`ui/dialog_view.py:_add_message` 增加 `_render_start > 0` 分支。

## [虚拟滚动] 内存 trim 仅在 _render_start == 0 时生效，无界增长
- **错误描述**：原 trim 条件 `if total > MAX_RENDERED and _render_start == 0` 当用户已加载过更早时永远不命中，`_all_messages` 无限增长直到内存爆。
- **正确做法**：当 `_render_start > 0` 且总数据超过 `2 * MAX_RENDERED` 时，强制 `_force_compact_data`：trim 数据 + 重置 `_render_start = 0` + 清空 UI + 重渲染。
- **本轮出现**：`ui/dialog_view.py:_force_compact_data` 提供兜底。

## [上下文污染] _new_dialog 不清理 _pending_corrections，跨对话继承
- **错误描述**：元监督对老对话的纠偏（"禁止给代码""引导思考"）被注入到 `_pending_corrections`，新对话发消息时被 `_consume_corrections()` 取出并写入 system prompt，**污染新对话上下文**。
- **正确做法**：`_new_dialog` 中显式 `self._pending_corrections.clear()`。
- **本轮出现**：`core/ai_dialog.py:_new_dialog` 已加清空。

## [窗口位置] 首次 show() 不 move()，Win 下默认 (0,0) 出现在屏幕左上/北面
- **错误描述**：`QWidget.show()` 不带 `move()`，Windows 上无父窗口时默认位置可能是 (0, 0) 或被任务栏遮挡，出现在屏幕"北面"看不见。
- **正确做法**：构造时调 `self.move(...)` 把窗口居中到主屏幕 `availableGeometry`。
- **本轮出现**：`ui/graph_editor.py:GraphEditor._center_on_primary_screen()` 已加。

## [虚拟滚动] _force_compact_data 终态与 _render_recent 不一致
- **错误描述**：原 `_force_compact_data` 设 `_render_start=0, _rendered_count=0` 后调 `_render_recent()`，但 `_render_recent` 立即按 `start = max(0, total - RENDER_CHUNK)` 把 `_render_start` 改回非 0（trim 后剩 30 条数据，start=15）。结果"加载更早"按钮仍亮起提示"15 条未显示"，与"重置"语义相悖。回归测试用 FakeDialogView 自定义逻辑绕过，所以原本假性通过。
- **正确做法**：trim 到 `RENDER_CHUNK`（不是 `MAX_RENDERED`），**不**调 `_render_recent`；自己循环 `add_bubble` 重渲染最后 RENDER_CHUNK 条；最后强制 `_render_start=0, _rendered_count=RENDER_CHUNK, load_more_btn.hide()`。
- **本轮出现**：`ui/dialog_view.py:_force_compact_data` 已改为内联重渲染。

## [图解析] 全局简化符号扫描误把注释内容当作格式标记
- **错误描述**：`parse_graph_block` 为优先识别简化格式（`A -> B`），先全局扫描所有行是否含 `->`/`-->`/`—`/`–`/` - `。若注释行（如 `# A -> B`）含这些符号，会误判为简化格式，导致本应走 UVW 的合法输入被丢弃。
- **正确做法**：全局扫描前先剥离/跳过注释；对每行先去除行首 `#` 注释与行内 `#` 注释，再检测简化符号。
- **本轮出现**：`ui/graph_renderer.py:_has_simple_symbol_in_middle` 已跳过 `#` 开头行并调用 `_strip_inline_comment`。

## [图解析] YAML 单遍扫描导致段顺序敏感
- **错误描述**：`parse_graph_block` 原先单遍处理 YAML；若 `edges:` 段出现在 `nodes:` 段之前，边解析时节点尚未收集，所有边被放入 `dropped_edges`。
- **正确做法**：对缺失节点的边先暂存到 `pending_edges`，全部行扫描结束后再统一解析；仍缺失的边才计入 `dropped_edges`。
- **本轮出现**：`ui/graph_renderer.py:parse_graph_block` 已引入 `pending_edges` 两遍解析。

## [图解析] 简化模式正则权重组不支持负数
- **错误描述**：简化格式权重捕获组用 `([0-9.]+)`，输入 `A - B (-5)` 时负号无法匹配，导致 `(-5)` 被吞入目标节点名，权重丢失。
- **正确做法**：权重捕获组改为 `(-?[0-9.]+)`，并用 `_WEIGHT_VALID` 二次校验，确保与 YAML/UVW 行为一致。
- **本轮出现**：`ui/graph_renderer.py:_SIMPLE_RE` / `_SIMPLE_DASH_RE` 及 `parse_graph_block` 内局部正则已修正。

## [编辑器状态] 批量导入后内部计数器未同步
- **错误描述**：`GraphScene.from_uvw` 提交新图后 `_id_counter` 仍为 0，随后用户点击添加节点时 `add_node()` 从 1 开始编号，可能与导入的节点 `1`、`2`… 重复，导致内部 ID 冲突。
- **正确做法**：导入提交成功后，将 `_id_counter` 更新为当前所有数字型节点 ID 的最大值，下次 `add_node()` 自动递增到不重复 ID。
- **本轮出现**：`ui/graph_editor.py:GraphScene.from_uvw` 已同步 `_id_counter`。

## [文本解析] 行内注释截断误伤引号内特殊字符
- **错误描述**：`parse_uvw_block` 用 `line.find("#")` 直接截断行内注释，未考虑 `#` 出现在引号 token 内的情况，导致 `"A#B" "C#D"` 这类合法节点名被截断。
- **正确做法**：引入引号/转义感知的 `_strip_inline_comment`，仅在非引号、非转义位置识别 `#` 注释。
- **本轮出现**：`ui/graph_renderer.py:parse_uvw_block` 已改用 `_strip_inline_comment`。

## [虚拟滚动] _load_more 后 _rendered_count 未同步
- **错误描述**：`_load_more` 加载 `chunk` 条更早消息后只改 `_render_start`，不更新 `_rendered_count`。后续 `_add_message` 走"已满→滑动窗口"分支，每条新消息都误删"更早已加载"的气泡。
- **正确做法**：`_load_more` 末尾同步 `self._rendered_count = min(self._rendered_count + chunk, RENDER_CHUNK)`。
- **本轮出现**：`ui/dialog_view.py:_load_more` 已加。

## [布局] 多次 addStretch 累积导致气泡被截断
- **错误描述**：`_setup_ui / _reset_render_state / _kill_dialog` 都在 messages_layout 末尾 `addStretch(1)`，且 `_clear_rendered` 只删 MessageBubble 不删 stretch。结果多次 reset/kill 后 layout 内多个 stretch；`_load_more` 用 `removeItem(item) if i == count()-1` 删除的可能是 stretch 末项，多轮后 stretch 被吞，气泡底部没 stretch 直接顶到 layout 边缘。
- **正确做法**：抽 `_ensure_stretch()`：若末尾已有 stretch/spacer 则不再加，否则补一个；所有调用 `addStretch(1)` 的位置改为 `_ensure_stretch()`。
- **本轮出现**：`ui/dialog_view.py:_ensure_stretch()` 已加，`_reset_render_state / _load_more` 改用之。

## [元监督注入] 多 system 消息被部分模型拒绝
- **错误描述**：原 `_consume_corrections()` 后把纠偏作为独立 `{"role": "system", "content": ...}` 插入到 `pruned_messages` 头部；worker 内部再 `[{system_prompt}] + pruned_messages`，最终给模型是 `[system_prompt, system, user, assistant, ...]`。Claude 严格模式 / OpenAI 旧版会拒绝"两条 system 消息"或行为异常。
- **正确做法**：把纠偏内容**追加到** `build_system_prompt(extra_rules=...)` 的 `extra_rules` 字段，worker 内部仍只生成 1 条 system 消息。`AIDialog.send` 内构造 `dynamic_extra = (ai_dialog_extra_rules or "") + "\n\n[元监督提示] ...纠偏..."` 传给 worker。
- **本轮出现**：`core/ai_dialog.py:send` 已改为合并到 extra_rules。

## [外部 AI 检测] copilot 子串误伤合法 IDE 用法
- **错误描述**：原关键词列表含 `"copilot"`，子串匹配导致 "GitHub Copilot in VSCode"、"writing code with copilot" 都被误判为外部 AI。
- **正确做法**：从列表移除 `"github copilot"`；只保留真正的"聊天界面"关键词："copilot chat"、"copilot.microsoft"、"copilot.com"、"bing.com/chat"。
- **本轮出现**：`core/cheat_detector.py:EXTERNAL_AI_KEYWORDS` 已精简。

## [配置] 关键配置项仅在代码里 default
- **错误描述**：`detect_external_ai` 读 `getattr(cfg.settings, "external_ai_remind_cooldown_min", 5)` 兜底，字段在 `AppSettings` dataclass 中**不存在**。用户改不到、设置 UI 也无法展示该选项。
- **正确做法**：在 `AppSettings` 显式声明 `external_ai_remind_cooldown_min: int = 5`，依赖 `from_dict` 的 filtered 过滤机制自动兼容旧 config.json。
- **本轮出现**：`config/settings.py:AppSettings` 已添加字段。

## [力导向布局] 矮画布下子区域 ty > by_ 钉死 y 坐标
- **错误描述**：当 rrh < 2*local_margin=40 时，`ty = rcy - rrh/2 + 20` 反而 > `by_ = rcy + rrh/2 - 20`，后续 `max(ty, min(by_, y+fy*damping))` 会把 y 钉死在 ty → 整条链/树/网络塌陷为水平单行。
- **正确做法**：子区域计算后加 `if by_ < ty: ty, by_ = by_, ty` swap 保护；或在 `_grid_assign_regions` 中检测 rrh < 2*NODE_RADIUS+10 时不切子区域。
- **本轮出现**：`ui/graph_renderer.py:_force_directed_layout` 已加 swap 保护。

## [力导向布局] MAX_NODES 太高导致标准画布节点重叠
- **错误描述**：480x320 画布 + NODE_RADIUS=22 + 30 节点时，最小距离仅 ~32，节点圆圈互相覆盖。
- **正确做法**：MAX_NODES 设为 20；标准画布下 10 节点链可达 min_d > 44，20 节点链因画布限制会有少量重叠（~20px），属于可接受折中。
- **本轮出现**：`ui/graph_renderer.py:MAX_NODES = 20`（r31 从 30 降）。

## [力导向布局] k 缺上限 + 链式布局被压扁
- **错误描述**：当节点数增加或画布变小时，k 应缩小（否则 chain 超出画布）。原代码只设 `max(50, ...)` 下限，无上限。
- **正确做法**：`k = max(40, min(120, sqrt(rrw*rrh/cn)*0.6, chain_max_k))`，其中 `chain_max_k = (rrw-2*local)/(cn-1) * 0.8`。
- **本轮出现**：`ui/graph_renderer.py:_force_directed_layout` 已加双向上限 + 链式保护。

## [力导向布局] cn=2 跳过 FD 导致不会"互斥"
- **错误描述**：原 `_force_directed_layout` 在 cn=2 时直接 `continue`，没有迭代。
- **正确做法**：让 cn=2 也走 FD，初始位置 `(rcx-60, rcy) / (rcx+60, rcy)`，迭代后受中心引力 + 弹簧 + 斥力。
- **本轮出现**：`ui/graph_renderer.py:_force_directed_layout` 已修复。

## [力导向布局] 3 分量 2x2 网格浪费 1 格
- **错误描述**：3 个连通分量用 `cols=ceil(sqrt(3))=2, rows=2`，但只放 3 个到 (0,0)/(0,1)/(1,0)，(1,1) 空着 → 左下角缺一块。
- **正确做法**：3 分量用 1x3 横向；2 分量用左右两栏；4+ 分量用 cols x rows 网格。
- **本轮出现**：`ui/graph_renderer.py:_grid_assign_regions` 已分支处理。

## [图算法] BFS 后 id 字符串排序不友好
- **错误描述**：`_connected_components` 内部 `comp.sort()` 是纯字符串排序，"10" < "2"。
- **正确做法**：`comp.sort(key=lambda x: (len(x), x))`，数字串按 (len, str) 排序 = 数值序。
- **本轮出现**：`ui/graph_renderer.py:_connected_components` 已修复。

## [上下文注入] screen_context 字段无截断保护
- **错误描述**：`send(user_text, screen_context=...)` 中 `progress_delta` 由 AI 在 `screen_analyzer` 自由生成，可能塞 4K 字符，整段进 `content` 后占据大量 token。
- **正确做法**：注入前 `safe_ctx = dict(screen_context)`，对 `progress_delta` 超过 500 字截断到 497 + "..."。
- **本轮出现**：`core/ai_dialog.py:send` 已加 500 字截断。

## [封装] 跨模块直接修改私有字段
- **错误描述**：`AIDialog._new_dialog` 直接 `self.supervisor._round_count = 0` 改私有字段，违反封装；AISupervisor 改内部状态时容易漏改。
- **正确做法**：在 `AISupervisor` 暴露公开方法 `reset_round_count()`，调用方使用之。
- **本轮出现**：`core/ai_supervisor.py:reset_round_count` 已添加；`ai_dialog._new_dialog` 改用之。

## [图编辑] set_directed 必须在 add_edge 之前
- **错误描述**：`add_edge` 内部有去重逻辑：`if not self._directed and e.source is target and e.target is source: return e`（无向模式合并反向边）。如果先 `add_edge` 再 `set_directed(True)`，有向模式下 1→2 和 2→1 会被无向去重逻辑错误地丢弃。
- **正确做法**：先 `set_directed(...)` 再循环 `add_edge`，保证去重逻辑跑在正确的方向性下。
- **本轮出现**：`ui/graph_editor.py:from_uvw` r32 P0-2 修复。

## [图编辑] 解析失败不应清空画布
- **错误描述**：用户已有手绘内容时，输入框解析失败（语法错/空文本）会无条件 `clear_graph`，造成用户工作丢失且无 undo。
- **正确做法**：先 `parse_uvw_block` 验证，**确认成功后才 clear_graph + 重建**；失败返回 False 并保留原图。
- **本轮出现**：`ui/graph_editor.py:from_uvw` r32 P0-1 修复。

## [QTimer] closeEvent 必须 stop 所有 timer
- **错误描述**：窗口关闭后未 stop 的 QTimer 可能在对象销毁后触发回调，访问半销毁的 widget 引发 race。
- **正确做法**：`closeEvent` 顶部 `self._uvw_debounce.stop()`，放在 super().closeEvent 之前。
- **本轮出现**：`ui/graph_editor.py:closeEvent` r32 P1-1 修复。

## [UI 状态] 按钮初始文字与 checked 状态需一致
- **错误描述**：按钮 `setCheckable(True)` + `setChecked(True)` 但文字用折叠指示符 ▸，用户看到 ▸ 以为已折叠，但实际展开。
- **正确做法**：构造时按钮文字用与当前状态一致的指示符（▸ 折叠 / ▾ 展开），或在 `_setup_ui` 末尾 `_toggle_uvw_panel()` 走一次初始化。
- **本轮出现**：`ui/graph_editor.py:uvw_toggle_btn` r32 P1-2 修复。

## [信号回环] setPlainText 会触发 textChanged → debounce → 重绘
- **错误描述**：程序代码 `setPlainText` 设置输入框，会同步触发 `textChanged` 信号；如果外层连接了 `debounce.start()`，200ms 后会把刚写入的文本当作"用户输入"重新解析，造成回环重绘（甚至破坏刚刚的 set 动作）。
- **正确做法**：`setPlainText` 前后用 `blockSignals(True/False)` 包起来；并 `debounce.stop()` 防止残留事件。
- **本轮出现**：`ui/graph_editor.py:_copy_current_to_uvw / _clear_uvw_input / _load_uvw_example` r32 P1-3 + P2-6 修复。

## [启发式判断] 单一 token 行的信号强度
- **错误描述**：用行匹配分累加判断格式时，1-token 行（仅建节点）如果用 0 分，会被误判为"没有有效行"；但它本身是合法的 uvw 行（仅建节点）。
- **正确做法**：1-token 行也加 1 分（与 2-token 等权），3-token 加 2 分（带权是最强信号）。
- **本轮出现**：`ui/graph_renderer.py:_looks_like_uvw` r32 修复。

## [启发式判断] directed 声明不一定是 YAML
- **错误描述**：把 `directed:` 当作 YAML 头会让 `directed: true\n1 2\n2 3` 这类 uvw 格式（带方向声明）误判走 YAML 分支而失败。
- **正确做法**：仅当文本含 `nodes:` 或 `edges:` 头时才视为 YAML；只有 `directed:` 应被视为 uvw 方向声明。
- **本轮出现**：`ui/graph_renderer.py:_looks_like_uvw` r32 修复。

## [启发式判断] 单 KV 行（"key: value"）不构成 uvw 节点/边
- **错误描述**：用 `\S+` 行匹配会把 `directed: true` 视为 2-token uvw 行，错误地判为 uvw。
- **正确做法**：在累加 matched 之前，先用 `_KV_RE.match(l)` 过滤掉"key: value"形式的单行声明。
- **本轮出现**：`ui/graph_renderer.py:_looks_like_uvw` r32 修复。

## [uvw 兜底] 全局简化符号扫描必须排除 YAML 列表项
- **错误描述**：r33 P1-6 修复（"顶层非 KV 行也尝试 uvw 解析"）只做单行 `has_simple_marker` 判断，导致 `1 2\nA -> B` 同时被 uvw 兜底和简化分支双重建边（测试 10.1 失败）。
- **第一次修复**：加全局 `_has_global_simple_marker` 扫描。但又引入新问题：`  - from: 3, to: 4` 含 `" - "`（YAML 列表项前缀）被误判为简化符号，导致 `1 2\nnodes:\n  - id: 3` mixed 模式 uvw 行被丢（测试 15 失败）。
- **正确做法**：全局简化符号检查必须**排除行首位置**。`-->` / `→` / `—` / `–` 是绝对特征；`->` 和 `" - "` 只有不在行首时才视为简化符号（行首 `-` 是 YAML 列表项）。
- **本轮出现**：`ui/graph_renderer.py:parse_graph_block` r33 P0 修复（`_has_simple_symbol_in_middle` 内联函数）。

## [QTimer] closeEvent 必须 disconnect scene._timer.timeout
- **错误描述**：`scene.stop_force()` 只调 `_timer.stop()` 取消未触发事件，但 **timeout 信号连接仍存在**。如果事件队列中已入队的 timeout 事件未被 stop 取消（极端时序），`_force_step` 会在窗口销毁后访问半销毁的 `_nodes`。
- **正确做法**：`closeEvent` 中显式 `self.scene._timer.timeout.disconnect(self.scene._force_step)`，然后再 `stop_force()`。
- **本轮出现**：`ui/graph_editor.py:closeEvent` r33 P1-4 修复。

## [UI 状态] 状态栏在 widget 销毁后 setText 会抛 RuntimeError
- **错误描述**：debounce 定时器 200ms 触发时若窗口已 close，`self.status.setText(...)` 访问已销毁 widget 抛 RuntimeError。
- **正确做法**：所有 `setText` / `toPlainText` 调用都包一层 `try/except: pass` 静默吞掉（widget 关闭后调任何 Qt 方法都是合理可失败的）。
- **本轮出现**：`ui/graph_editor.py:_apply_uvw_preview` r33 P1-2 修复。

## [token 解析] 退化路径不能用 .strip('"') 暴力去引号
- **错误描述**：`_tokenize_uvw_line` 未闭合引号时退化为 `\S+ split`，再用 `r.strip('"')` 会去掉 token **首尾所有引号**，破坏 `a"b"c` 中间含合法引号的 token。
- **正确做法**：分别判断 `startswith('"')` 减 1 和 `endswith('"')` 减 1；且 token 满 3 个时 `break`（不要 `continue` 跳过剩余 token）。
- **本轮出现**：`ui/graph_renderer.py:_tokenize_uvw_line` r33 P1-5 修复。

## [输入保护] parse_graph_block 也需要 100KB 硬限制
- **错误描述**：r33 在 `parse_uvw_block` 入口加了 100KB 限制，但 `parse_graph_block` 没有。粘贴 1MB 简化文本走简化分支（每行正则匹配）会冻 UI。
- **正确做法**：两个 parse 入口对齐 100KB 限制，超限直接返回 None。
- **本轮出现**：`ui/graph_renderer.py:parse_graph_block` r33 P1-1 修复。

## [图解析] _tokenize_uvw_line 截断多 token 行导致误判
- **错误描述**：`_tokenize_uvw_line` 为限制最多 3 个 token 会截断行（如 6-token 句子只返回前 3 个 token）。`_looks_like_uvw`、`parse_graph_block` 的 uvw 兜底分支以及 `parse_uvw_block` 仅检查返回的 token 列表非空，导致普通英文句子被误判为 uvw 图数据，非法 graph 代码块被错误渲染为 SVG；不同入口对行内 `#` 注释的截断处理不一致还会导致同一行产生不同图结构。
- **正确做法**：
  1. 在启发式判断、兜底解析和实际解析器中，均用整行正则 `_UVW_LOOKS_RE` 二次确认原始行确实只有 1-3 个 token（支持引号包裹的 token），避免截断造成误判。
  2. 所有 uvw 相关入口对行内 `#` 注释的处理保持一致（`find('#')` 截断）。
- **本轮出现**：`ui/graph_renderer.py` 已新增 `_UVW_LOOKS_RE`，并在 `_looks_like_uvw`、`parse_graph_block` 顶层兜底分支、`parse_uvw_block` 中启用；同时统一了 `#` 注释截断逻辑。

## [图解析] graph 解析入口未处理 Markdown 代码块围栏
- **错误描述**：`parse_graph_block` / `parse_uvw_block` 只接受纯图文本，直接传入 ```graph\n...\n``` 原文时解析失败，导致直接调用 `render_graph_svg("```graph\n...\n```")` 的测试或外部接口返回 None。
- **正确做法**：在 parse 入口统一去除外层 ```` ```graph ```` / ```` ``` ```` 围栏后再走后续解析；空围栏直接返回 None；抽取为 `_strip_code_fences(text)` 工具函数供两个入口复用，避免重复实现。
- **本轮出现**：`ui/graph_renderer.py` r35 修复，`parse_graph_block` 与 `parse_uvw_block` 均调用 `_strip_code_fences`。

## [测试维护] 回归测试断言与实现不同步
- **错误描述**：实现已将 SVG 有向箭头从 `marker-end` 改为直接绘制 `polygon`，但 `test_r32_regression.py` 仍断言 `marker-end`，导致测试误报失败。
- **正确做法**：每次改动渲染/输出格式后，同步检查并更新对应回归测试；对关键行为变化可在测试注释中说明原因（如"QTextBrowser 不支持 marker"）。
- **本轮出现**：`test_r32_regression.py` 7.2 已同步为 `<polygon` 断言，并新增 `test_r35_regression.py` 做 polygon 箭头回归确认。

## [窗口生命周期] closeEvent 未停止周期性 QTimer
- **错误描述**：窗口关闭时只停了部分 timer（如 single-shot / debounce），漏了周期性的 `_timeout_timer`。窗口销毁后 timer 仍触发回调，访问已释放的 widget 成员，导致 `RuntimeError` 或崩溃。
- **正确做法**：`closeEvent` 顶部统一停止所有由该窗口拥有的 `QTimer`（周期性和单次），然后再做存档、断开信号、调用 `super().closeEvent(event)`。
- **本轮出现**：`ui/dialog_view.py:closeEvent` r36 修复，增加 `self._timeout_timer.stop()`。

## [信号生命周期] 全局单例信号未在窗口关闭时断开
- **错误描述**：`DialogView` 连接了全局 `AIDialog` 单例的多个信号；窗口关闭/销毁后槽函数仍挂在单例上，后续信号发射时访问已销毁对象。
- **正确做法**：窗口关闭时显式 `disconnect` 所有由本窗口 `connect` 的槽函数，并用 `try/except (TypeError, RuntimeError)` 包容已断开或对象已销毁场景。
- **本轮出现**：`ui/dialog_view.py` r36 新增 `_unbind_dialog()`，在 `closeEvent` 中统一断开 7 个信号。

## [图解析] em dash / en dash 与有向箭头语义混淆
- **错误描述**：简化格式正则把 `A — B` / `A – B`（无向横线）与 `A -> B` / `A → B`（有向箭头）一并作为有向分隔符处理，导致无向图被错误标记为 `directed=True`。
- **正确做法**：有向正则只包含 `-->` / `->` / `→`；`—` / `–` / `-` 仅由无向横线正则处理；directed 判定也仅检查真正的箭头符号。无向横线正则应同时支持两侧空格与无空格形式，但普通 hyphen 仍要求两侧空格以避免误拆节点名。
- **本轮出现**：`ui/graph_renderer.py` r36 修复，`_SIMPLE_RE` 与 directed 判定均移除 `—`/`–`；`_SIMPLE_DASH_RE` 增强为 `(?:\s+-\s+|[–—])` 以支持无空格 em/en dash。

## [SVG 安全] 颜色字段未校验直接拼入属性
- **错误描述**：主题/配置颜色直接拼入 SVG 属性，若值为异常字符串（如含引号、尖括号、脚本）会破坏 SVG 结构甚至造成注入。
- **正确做法**：SVG 颜色字段使用专门的 `_safe_color(value, default)` 函数，仅接受 `#RGB` / `#RRGGBB` 等安全格式；非法值退到 default。
- **本轮出现**：`ui/graph_renderer.py` r36 新增 `_safe_color` 并在 `render_graph_svg` 中统一使用。

## [坐标系] 布局坐标未映射到 sceneRect 实际原点
- **错误描述**：`_compute_layout` 返回以 `(0,0)` 为左上角的相对坐标，但 `QGraphicsScene.sceneRect()` 原点可能不是 `(0,0)`（如 `(-400, -300)`），直接 `setPos(x, y)` 导致节点簇偏移。
- **正确做法**：自动布局后加上 `rect.left()` / `rect.top()` 偏移：`node.setPos(rect.left() + x, rect.top() + y)`。
- **本轮出现**：`ui/graph_editor.py:GraphScene.from_uvw` r36 修复。

## [UI 阻塞] 同步调用 vision_chat 冻结 UI
- **错误描述**：`AIDialog.screenshot_analyze` / `attach_screen_context` 在主线程直接调用 `vision_chat`，阻塞 UI 数秒。
- **正确做法**：使用 QThread + QObject worker 异步执行，通过信号回传结果。线程结束后 `_reset_thread_refs` 清理引用。
- **本轮出现**：`core/ai_dialog.py` r38 修复。

## [虚拟滚动] 滑动窗口后 _render_start 未递增
- **错误描述**：`_add_message` 在窗口已满时去掉最旧气泡、追加最新，但 `_render_start` 未 `+= 1`，导致 `_render_start` 永远为 0，"加载更早消息"永久失效。
- **正确做法**：滑动窗口分支后必须 `self._render_start += 1`。
- **本轮出现**：`ui/dialog_view.py:_add_message` r38 修复。

## [主题对比度] border 不适合作为图论边线颜色
- **错误描述**：`graph_renderer.py` 用 `theme["border"]` 作为边线/箭头颜色，border 是为 UI 分隔线设计的，在多数主题下与背景对比度 < 3:1，导致边/箭头几乎不可见。`text_dim` 在浅色主题（white/cream）下对比度也不足。
- **正确做法**：使用 `_pick_edge_color(theme, bg_color)` 对比度感知选择，依次尝试 `text_dim` / `text` / `border`，挑选对比度 ≥ 3.0 的颜色。
- **本轮出现**：`ui/graph_renderer.py` + `ui/graph_editor.py` r38 修复。

## [状态同步] FocusView 重开不同步引擎状态
- **错误描述**：`_bind_engine` 只连接信号，不检查当前引擎状态。FocusView 关闭后重开时，UI 显示"未启动"但引擎实际在跑，用户看到倒计时但看不到退出按钮。
- **正确做法**：`_bind_engine` 末尾调用 `_sync_state_from_engine()`，检查 `engine.is_active` 并恢复 UI 状态。
- **本轮出现**：`ui/focus_view.py` r38 修复。

## [卡住检测] stuck_count 无条件清零
- **错误描述**：`FocusEngine._on_screen_result` 在触发 AI 卡住分析后无条件 `self._stuck_count = 0`，回调失败时卡住检测被重置，用户无法再次获得帮助。
- **正确做法**：清零移入 try 块（仅成功时清零）；except 中折半 `max(0, threshold // 2)`。
- **本轮出现**：`core/focus_engine.py` r38 修复。

## [Qt 生命周期] 全局单例信号未在窗口关闭时断开
- **错误描述**：`Sidebar`/`DialogView` 连接了全局单例（`FocusEngine`、`AIDialog`）的信号；窗口关闭/销毁后槽函数仍挂在单例上，后续信号发射时访问已销毁对象，引发 `RuntimeError` 或崩溃。
- **正确做法**：`closeEvent` 中显式 `disconnect` 所有由本窗口 `connect` 的槽函数，并用 `try/except (TypeError, RuntimeError)` 包容已断开或对象已销毁场景。
- **本轮出现**：`ui/sidebar_v2.py` / `ui/dialog_view.py` r39 修复。

## [Qt 生命周期] 使用 sip 检查对象存活性
- **错误描述**：PySide6 不提供 `sip` 模块，直接 `import sip` 会抛 `ModuleNotFoundError`。
- **正确做法**：使用 `shiboken6.isValid(obj)` 检查 QObject/QWidget 的 C++ 对象是否存活；不可用时退化到 `try/except RuntimeError: obj.isVisible()`。
- **本轮出现**：`ui/sidebar_v2.py` / `ui/focus_view.py` r39 修复。

## [配置读取] 缺失字段导致 AttributeError / TypeError
- **错误描述**：直接用 `cfg.settings.field` 或 `max(30, None)` 访问可能缺失/为 None 的配置字段，会抛 `AttributeError` 或 `TypeError`。
- **正确做法**：用 `getattr(cfg.settings, "field", None)` 取值，再 `or default` 兜底；对除数用 `max(1, value)` 防御 0/None。
- **本轮出现**：`core/focus_engine.py`、`core/ai_supervisor.py` r39 修复。

## [类型校验] json.loads 返回非预期类型
- **错误描述**：默认假设 `json.loads()` 返回 `dict`，但 AI 可能返回 JSON 数组或字符串，直接 `.get()` 会抛 `AttributeError`。
- **正确做法**：`json.loads()` 后先 `isinstance(data, dict)`（或 list）校验，再按类型取值。
- **本轮出现**：`core/screen_analyzer.py`、`core/ai_supervisor.py`、`core/context_manager.py` r39 修复。

## [线程管理] worker 异常时未触发退出信号
- **错误描述**：QThread worker 的 `run()` 仅在成功路径 emit `finished`，若 emit 自身抛异常或失败分支遗漏，线程无法退出导致泄漏。
- **正确做法**：`run()` 用 `finally` 兜底触发退出信号（如 `done.emit()`），并捕获 `RuntimeError`；外部连接 `done` 到 `thread.quit()`。
- **本轮出现**：`core/screen_analyzer.py` r39 修复。

## [迭代安全] 回调中自注销修改正在迭代的列表
- **错误描述**：`for cb in _observers:` 迭代时，回调内部调用 `remove_observer(cb)` 会修改列表大小，抛 `RuntimeError: list changed size during iteration`。
- **正确做法**：`for cb in list(_observers):` 快照迭代，或在修改前复制列表。
- **本轮出现**：`core/mute_mode.py` r39 修复。

## [竞争条件] 力导向 timer 与图编辑操作竞争
- **错误描述**：`_force_step` 在 timer 线程中迭代 `_nodes`/`_edges`，主线程可能同时 `clear_graph` 清空列表，导致迭代越界或访问已删除节点。
- **正确做法**：在 `_force_step` 入口对节点/边列表做快照（`list(self._nodes)`），迭代快照；图形项绘制方法捕获 `RuntimeError`。
- **本轮出现**：`ui/graph_editor.py` r39 修复。

## [无屏幕] primaryScreen() 返回 None
- **错误描述**：RDP 断开、无显示器或某些虚拟化环境下 `QApplication.primaryScreen()` 返回 `None`，后续 `.availableGeometry()` 直接抛 `AttributeError`。
- **正确做法**：每次调用 `primaryScreen()` 后立即判空：`if screen is None: return`。
- **本轮出现**：`ui/sidebar_v2.py`、`ui/frame_mixin.py`、`ui/toast.py`、`core/screen_analyzer.py` r39 修复。

## [网络响应] 旧版 requests 的 JSONDecodeError 未捕获
- **错误描述**：`resp.json()` 在旧版 requests 中抛 `json.JSONDecodeError`（非 `RequestException` 子类），外层仅 `except requests.RequestException` 会漏掉。
- **正确做法**：对 `resp.json()` 单独包 `try/except (json.JSONDecodeError, ValueError)`，再抛为业务异常。
- **本轮出现**：`core/ai_client.py` r39 修复。

## [异步回调] singleShot 访问已销毁对象
- **错误描述**：`QTimer.singleShot` 不可取消，窗口销毁后延迟回调仍会执行，访问已释放成员导致崩溃。
- **正确做法**：在闭包内先检查对象存活（`shiboken6.isValid(self)` 或 `try/except RuntimeError`），再访问成员。
- **本轮出现**：`ui/dialog_view.py` r39 修复。

## [主题] 直接 theme[key] 访问缺失字段
- **错误描述**：用户导入的自定义主题 JSON 可能只含部分字段，直接 `theme["surface"]` 会 `KeyError`。
- **正确做法**：所有 theme 字段读取使用 `theme.get(key, default)` 并提供合理默认值。
- **本轮出现**：`ui/settings_view.py` r39 修复。

## [主题] custom 主题返回引用被外部污染
- **错误描述**：`ThemeManager.current_theme` 对 custom 主题返回 `_custom` 引用，调用方修改后会污染内部状态。
- **正确做法**：`current_theme` 统一返回 `copy.deepcopy(...)`，确保所有调用方拿到独立副本。
- **本轮出现**：`ui/themes.py` r39 修复。

## [Unicode] isdigit() 误判 Unicode 数字
- **错误描述**：Python 字符串 `.isdigit()` 对 Unicode 数字（如 `'²'`、`'１'`）返回 True，导致图节点 ID 被错误当作数字处理。
- **正确做法**：数字 ID 校验增加 `.isascii()` 前置条件：`s.isascii() and s.isdigit()`。
- **本轮出现**：`ui/graph_editor.py` r39 修复。

## [闭包陷阱] for 循环中 lambda 延迟绑定
- **错误描述**：在 `for q in questions:` 循环中直接 `btn.clicked.connect(lambda: self._ask(q))`，所有按钮点击时 `q` 都指向循环结束后的最后一个值，导致所有快捷问题按钮都发送同一条问题。
- **正确做法**：用默认参数捕获当前值：`lambda checked, text=q: self._ask_quick_question(text)`，把 `q` 在定义时绑定到 `text` 默认参数。
- **本轮出现**：`ui/dialog_view.py:_build_welcome` r42 修复。

## [状态机] AI 处理结束路径未恢复 UI 状态
- **错误描述**：发送按钮在 `_send` 中改为"生成中"+禁用，但若 `_on_error`/`_on_chat_detected`/`_on_code_output` 等异常结束路径未恢复文字为"发送"+启用按钮，会导致按钮永久禁用，用户无法继续对话。
- **正确做法**：所有结束 AI 处理的路径（成功回复、错误、闲聊检测、代码输出、新对话切换、删除对话、截图分析完成、文件上传完成）都必须显式恢复 `send_btn` 文字与 `_thinking_label.hide()`，不允许依赖隐式清理。
- **本轮出现**：`ui/dialog_view.py` r42 修复，统一 8 个结束路径的状态恢复。

## [主题刷新] 动态新增控件未纳入 update_theme
- **错误描述**：在 `MessageBubble` 中新增角色行（头像/角色名/时间戳/复制按钮/源码切换按钮）后，若 `update_theme` 未刷新这些控件的样式，主题切换后会出现颜色错乱（如浅色主题下头像仍是深色背景）。
- **正确做法**：每次新增带主题色的控件时，同步在 `update_theme` 中追加样式刷新代码；用 `try/except RuntimeError` 包裹防御已销毁控件（窗口关闭后仍可能触发主题刷新信号）。
- **本轮出现**：`ui/dialog_view.py:MessageBubble.update_theme` r42 修复。

## [QPlainTextEdit] eventFilter 必须 return True 拦截回车
- **错误描述**：在 `eventFilter` 中检测到 Enter 键后调用 `_send()` 但 `return False`，QPlainTextEdit 仍会插入换行符，导致发送后输入框残留一个空行。
- **正确做法**：调用 `_send()` 后必须 `return True` 拦截默认行为；`Shift+Enter` 想要换行时则 `return False` 交给 QPlainTextEdit 处理。
- **本轮出现**：`ui/dialog_view.py:eventFilter` r42 修复。

## [自适应高度] setFixedHeight 缺上下限导致 UI 抖动
- **错误描述**：输入框高度随行数动态变化时，若不设上限，粘贴大段文本会让输入框撑满整个对话框；若不设下限，清空后输入框塌陷到 0px 无法点击。
- **正确做法**：`setFixedHeight(min(160, max(44, computed_height)))` 同时设上下限；下限 44px 保证单行可点击，上限 160px 约束 6 行内不溢出。
- **本轮出现**：`ui/dialog_view.py:_on_input_changed` r42 修复。

## [控制台编码] Windows GBK 控制台输出 Unicode 特殊符号崩溃
- **错误描述**：测试脚本 print 含 ▾/▸/❌ 等 Unicode 符号，或 logging StreamHandler 输出含 emoji/中文时，Windows 默认 GBK 控制台抛 `UnicodeEncodeError: 'gbk' codec can't encode character`，导致测试误报失败/程序崩溃；设置 `PYTHONIOENCODING=utf-8` 后又假性通过。
- **正确做法**：脚本入口对 stdout/stderr 执行 `reconfigure(encoding="utf-8", errors="replace")`；logging 的 StreamHandler 创建后同样 reconfigure 其 `ch.stream`；用 `try/except` 包裹兼容无 reconfigure 的环境。
- **本轮出现**：`test_r32_regression.py`、`test_r38_check.py`、`utils/helpers.py:setup_logger` 深度检修修复。

## [上下文裁剪] 按分数挑选后未还原原始对话顺序
- **错误描述**：`prune_with_flash` 用 `kept.sort(key=分数, reverse=True)` 挑选 top N 后直接返回，裁剪后的消息以"保留价值降序"发给主模型，打乱 user/assistant 交替结构，对话上下文错乱。因排序是无条件执行的，即使未裁剪也会破坏顺序。
- **正确做法**：挑选时带原始索引排序（`sorted(enumerate(kept), ...)`），选完 top N 后按原始索引还原；输出序列只反映时间顺序，不反映分数。
- **本轮出现**：`core/context_manager.py:prune_with_flash` r43 审计修复。

## [死配置] 设置页可改但引擎从未消费的配置项
- **错误描述**：`ai_dialog_base_url`、`ai_dialog_max_rounds` 在设置页可编辑且能保存，但对话引擎代码从未读取，用户修改后无任何效果，属隐性欺骗。
- **正确做法**：新增设置项时必须同步接通消费链路（引擎/客户端读取并生效）；审计时用全局 grep 验证每个配置字段在非 settings/UI 代码中是否有真实消费者。
- **本轮出现**：`core/ai_client.py:chat/vision_chat` 新增 `base_url` 覆盖参数、`core/ai_dialog.py` 新增 `_trim_messages_to_max_rounds` 并在 `send` 中执行，r43 审计修复。另有 `kimi_role`/`glm_role`/`deepseek_role` 仍为待接通项（已记录）。

## [会话状态] 登录态标志在抓取失败后不重置导致永久污染
- **错误描述**：`_logged_in` 在登录成功后置 True，但后续抓取异常（网络抖动/HTTP 非 200）只记日志不重置，导致后续调用永远跳过重新登录，抓取永久返回空结果（进而影响零提交锁定检测）。
- **正确做法**：抓取路径捕获 `requests.RequestException` 或遇到非 200 时调用 `_reset_session()` 重建会话并置 `_logged_in=False`，保证下次调用重新登录；解析类错误则不重置。
- **本轮出现**：`core/oj_tracker.py` 三个 fetch 方法与 `check_rank_tail`，r44 全量检修修复。

## [stub 函数] 永久返回固定值的占位实现让配置项失效
- **错误描述**：`check_rank_tail()` 长期为 `return False` 的 stub，导致 `focus_lock_on_rank_tail` 配置与 UI 开关形同虚设，且无任何提示。
- **正确做法**：stub 必须实现或在 todo/文档中显式登记为已知限制；审计时对短小返回固定值的函数重点核查是否有对应配置消费者。锁定类判定实现需保守：异常/解析失败一律返回不触发锁定的值（宁可漏报不误锁）。
- **本轮出现**：`core/oj_tracker.py:check_rank_tail` r44 全量检修补实现（保守解析排行榜首页）。

## [全局监听器] 重复注册前未停止旧实例导致线程泄漏
- **错误描述**：`start_global_hotkey` 直接覆写 `_HOTKEY_LISTENER` 全局变量，若被多次调用，旧 pynput 监听线程永不释放，且新旧监听器同时响应热键。
- **正确做法**：任何“启动监听器”函数入口先检查全局/成员变量是否已有实例，有则先 `stop()` 置空再创建新实例；即使当前只有一个调用点也要防御。
- **本轮出现**：`core/mute_mode.py:start_global_hotkey` r44 全量检修修复（防御性 stop-before-start）。

## [死配置] 配置字段声明后无消费者（"有口没码"）
- **错误描述**：`AppSettings` 声明了 `kimi_role`/`glm_role`/`deepseek_role`、`watchdog_restart_on_crash`、`site_risk_score_threshold` 等字段，但引擎/客户端代码从未读取，用户修改后无任何效果，属隐性欺骗（有口没码）。
- **正确做法**：新增配置字段必须同步接通消费链路；审计时用全局 grep 验证每个字段在非 settings/UI 代码中是否有真实消费者。无法接通的字段应删除或登记 Future，绝不留"只存不读"的哑字段。
- **本轮出现**：r45 全量检修接通 role 路由（`core/ai_client.py:resolve_provider_for_role`）、`watchdog.py:restart_enabled`、`core/site_guard.py:_risk_score`。

## [role 路由] provider 硬编码导致角色配置失效
- **错误描述**：vision/dialog/flash 各调用点 provider 硬编码（vision=glm、dialog/flash=deepseek），导致 `kimi_role`/`glm_role`/`deepseek_role` 永远不生效。
- **正确做法**：用 `resolve_provider_for_role(role, default)` 按 role 解析 provider；flash/对话任务用 `_resolve_role_target` 同时解析 model——非默认 provider 时改用该 provider 自身模型名，避免把 deepseek 系模型名（如 deepseek-v4-pro）发给 kimi/glm 导致调用失败；knowledge/dialog 的默认 provider 未启用或无 key 时回退 deepseek，保证核心功能可用。
- **本轮出现**：`core/ai_client.py` r45 检修接通，`core/ai_dialog.py`、`core/screen_analyzer.py`、`core/ai_supervisor.py`、`core/context_manager.py` 各硬编码调用点均已接入。

## [功能删除] 移除功能时遗漏文案/注释/日志残留
- **错误描述**：删除"做题退出（答题）"后，`exit_flow.py` 提示、`helpers.py` 日志渲染、`focus_view.py` 注释中仍残留"做题退出"字样，造成提示/日志与实际行为不一致。
- **正确做法**：删除功能时全局 grep 相关关键词（做题/答题/simple_problem/study_require_problem），同步清理文案、注释、日志渲染、测试断言，避免"有口没码"的反面——"有码没口"（代码还在但入口/文案已误导）。
- **本轮出现**：r45 去掉答题时统一清理。

## [模块重命名] 旧模块名 import 被 except 吞掉导致功能静默失效
- **错误描述**：`ui/toast.py` 从 `core.boss_mode` 导入（模块已改名 `core.mute_mode`），import 抛 `ModuleNotFoundError` 被外层 `except: pass` 吞掉，导致"静音模式隐藏所有弹窗"整体失效，且无任何报错（`register_toast` 变成死函数）。
- **正确做法**：模块改名/删除后全局 grep 旧模块名的 import 点；`try/except: pass` 包裹的 import 必须记录失败日志（logger.warning + exc_info），绝不允许静默吞掉功能依赖。UI 按钮连接的方法必须存在（否则点击即 AttributeError），可在测试中用 `hasattr` + `inspect.getsource` 断言。
- **本轮出现**：`ui/toast.py:show_toast` r45 检修修复（boss_mode→mute_mode、is_boss_mode→is_mute_mode）；`ui/svg_view.py` 三个按钮连到不存在的方法也一并补齐。

## [跨线程 Qt] 全局热键监听线程直接操作 QWidget
- **错误描述**：pynput 全局热键在监听线程回调 `toggle_mute_mode`，直接对 QWidget 调 `hide()/show()/setChecked`。QWidget 只能在主线程操作，跨线程操作属未定义行为，可能崩溃。该隐患在 toast 接线修复后因 `_TOASTS` 首次被真实填充而激活。
- **正确做法**：全局热键回调只做"投递"，用一个主线程 QObject（`@Slot`）作为桥，经 `QMetaObject.invokeMethod(bridge, slot, Qt.QueuedConnection)` 把状态切换投递回主线程执行；QObject 必须在其所属线程（主线程）创建，因此桥对象要在 `start_global_hotkey`（主线程调用）里提前实例化。
- **本轮出现**：`core/mute_mode.py` r45 检修新增 `_MuteToggleBridge` + `_dispatch_toggle_to_main_thread`。

## [跨线程 Qt] 自动检查 worker 信号直连普通函数导致在子线程操作 QWidget
- **错误描述**：`worker.finished.connect(普通函数)` 在 PySide6 中会在发射线程（worker 线程）直接执行函数；函数内 `FocusEngine.lock_for_zzoi()` 会创建 QTimer 并发信号，Qt 警告 `Cannot create children for a parent that is in a different thread`，可能半启动/崩溃。同时只持有 QThread、不持有 worker 时，PySide6 信号连接不保活 Python wrapper，worker 被 GC 后任务永不执行且线程泄漏。
- **正确做法**：由主线程创建 `QObject` 桥（`@Slot`），worker.finished 连到桥的槽（接收者属于主线程，AutoConnection 自动排队）；thread/worker/bridge 三元组全部存入 job_refs 防 GC；cleanup 时一并移除。
- **本轮出现**：`main.py:_start_auto_zzoi_check` r48 修复；`ui/sidebar_float.py` 悬浮热键同理用 `_FloatHotkeyBridge`。

## [线程代次] 旧 QThread.finished 回调清空新线程引用
- **错误描述**：`_reset_thread_refs` 不校验"finished 的是不是当前线程"。旧线程 finished 信号排队投递期间若新线程已启动并赋给 `_thread`，旧回调会把新线程引用置 None，busy 检查失效，可并发跑两个 worker、重复追加回复。
- **正确做法**：连接时用 `lambda t=self._thread: self._reset_thread_refs(t)` 捕获代次，回调内 `if self._thread is not t: return`；截图线程与 ScreenAnalyzer 同理。
- **本轮出现**：`core/ai_dialog.py`（对话/截图线程）、`core/screen_analyzer.py` r48 修复。

## [迟到异步结果] 全局单例 worker 的结果没有代次过滤会污染新窗口
- **错误描述**：DialogView 关闭后全局 AIDialog 的截图 worker 仍在跑；重开新 DialogView 后旧结果到达，新窗口会把它当作用户当前请求自动 send，写入 `_messages`。
- **正确做法**：请求时生成 request_id，信号携带 `(request_id, result)`；窗口保存自己发起的 request_id，不匹配直接丢弃；closeEvent 里作废代次。
- **本轮出现**：`core/ai_dialog.py` `screenshot_analyzed/screen_context_ready` 改 `Signal(int, dict)`，`ui/dialog_view.py` r48 修复。

## [配置脏数据] 只校验顶层类型不校验元素/范围
- **错误描述**：`screen_custom_rect: ["0","0","640","480"]` 直接进 `grabWindow` 抛 TypeError 被外层吞成截图失败；`mail_receivers: [1,2]` 使 `", ".join()` 抛 TypeError；超大整数让 `QSpinBox.setValue` 抛 OverflowError 打开设置即崩；`focus_mode` 先归一后又被字符串兜底用旧 value 覆盖。
- **正确做法**：在 `AppSettings.__post_init__` 按字段语义归一元素类型（矩形转 int、收件人转 str、名单只留 str），int 字段按 `_INT_RANGES` 夹取；focus_mode 分支处理完 `continue`；ConfigManager 顶层非 dict 按空配置。
- **本轮出现**：`config/settings.py` r48 修复，`test_r48_overhaul.py` T1/T23 覆盖。

## [日志自愈] 原地修改原 dict 导致比较相等跳过落盘
- **错误描述**：归一化日志时 `base[key] = [x for x in v if ...]` 只复制列表，元素仍是原 dict 引用；随后 `entry["detail"] = {}` 把原数据也改了，`normalized == data` 恒成立，坏 detail 永远不写回磁盘。reminders/submission_errors 的数值字符串只认 int/float 会误归 0。
- **正确做法**：先 `[dict(x) for x in v if isinstance(x, dict)]` 复制元素再修改；数值字段统一 `int()` + 异常回退；用 `normalized != data` 决定写回。
- **本轮出现**：`utils/helpers.py:_normalize_daily_log` r48 修复，`test_r48_overhaul.py` T12 覆盖。

## [AI 响应防御] 只捕获 KeyError/IndexError 漏掉非 dict 响应的 TypeError
- **错误描述**：AI 返回合法 JSON 数组/字符串时，`data["choices"]...` 抛原生 `TypeError: list indices must be integers`，用户看到非业务错误，vision_chat 同样。
- **正确做法**：`resp.json()` 后先 `isinstance(data, dict)` 校验，非 dict 直接抛 `AICallError`；取值异常捕获元组加 TypeError。
- **本轮出现**：`core/ai_client.py:chat/vision_chat` r48 修复，`test_r48_overhaul.py` 23.4 覆盖。

## [图导入无上限] 只限制文本长度不限制节点/边数
- **错误描述**：100KB UVW/邻接表可构造上万节点/边，QGraphicsScene 主线程建图冻结百秒；from_adjacency 还会先 clear 后解析，失败丢画布。
- **正确做法**：解析成功后、建图前校验 `len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES`，超限快速拒绝；邻接表先解析后 clear，失败保留原图。
- **本轮出现**：`ui/graph_editor.py:GraphScene.from_uvw/from_adjacency` r48 修复，`test_r48_overhaul.py` T14/T22 覆盖。

## [绘制崩溃] 使用未导入的 Qt 类
- **错误描述**：`sidebar_full.paintEvent` 使用 `QPointF` 但 import 漏了该类；实例化/不绘制时不报错，一旦 show 首次 paint 就 NameError，随后可能访问冲突崩溃。常规实例化冒烟测试必须 `show + processEvents + grab` 才能暴露。
- **正确做法**：Qt 类型与 import 用 pyflakes 扫 undefined name；UI 冒烟必须触发一次真实 paint（grab），不能只构造对象。
- **本轮出现**：`ui/sidebar_full.py` r48 终检修复，`test_r48_overhaul.py` 23.2 覆盖。

## [旧线程信号泄漏] analyze() 被调用时旧线程结果仍被处理
- **错误描述**：`ScreenAnalyzer.analyze()` 被调用时若旧 `_AnalyzeWorker` 仍在运行，返回占位结果。但旧线程的 `finished`/`failed` 信号仍连接着 `_on_finished`/`_on_failed`，迟到结果会更新 `_last_activity`、触发 `CheatDetector` 与 `SiteGuard`
- **正确做法**：为每次 `analyze()` 调用分配 `request_id`，worker 信号携带 `request_id`，槽函数检查 `request_id != self._request_id` 则丢弃
- **本轮出现**：`core/screen_analyzer.py` r49 修复，`test_r49_core_inspection.py` T3 覆盖

## [旧 worker 信号污染新对话] close→new→send 序列中旧 worker 回复注入新对话
- **错误描述**：`AIDialog.send()` 创建新 `_DialogWorker` 前未断开旧 worker 的信号连接；旧 worker 在新 `send()` 重置 `_ignore_worker=False` 后，迟到 `finished` 信号被 `_on_reply` 处理，将旧回复追加到新对话
- **正确做法**：在 `send()` 中创建新 worker 前调用 `_disconnect_old_worker()` 断开旧 worker 的全部 6 个信号
- **本轮出现**：`core/ai_dialog.py` r49 修复，`test_r49_core_inspection.py` T6 覆盖

## [配置敏感值] focus_stuck_threshold=1 导致卡住检测过于敏感
- **错误描述**：`data/config.json` 中 `focus_stuck_threshold=1`（默认 3），导致仅 1 次重复 screen snapshot 即触发 AI 卡住分析，用户正常慢速推进也会被打断
- **正确做法**：该值代表"连续 N 次无进展"才触发，建议设为 3-5；可在设置中心「专注模式」Tab 可视化调整
- **本轮出现**：r49 测试中发现，config.json 默认值为 1（历史测试残留），已修复为 3

## [新对话无法发送] _new_dialog 未重置线程引用
- **错误描述**：`AIDialog._new_dialog()` 未清除 `_thread`/`_worker` 引用，旧线程仍在运行时 `send()` 检查 `isRunning()` 返回 True，拒绝发送
- **正确做法**：`_new_dialog()` 末尾调用 `_disconnect_old_worker()` + 设置 `self._thread = None; self._worker = None`
- **本轮出现**：`core/ai_dialog.py` r49 修复

## [内存 trim 盲区] _render_start>0 时内存无界增长
- **错误描述**：`dialog_view.py` 内存 trim 仅在 `_render_start == 0` 时执行，滑动窗口后（`_render_start > 0`）数据可增长到 2×MAX_RENDERED
- **正确做法**：去掉 `_render_start == 0` 条件，统一 trim 并调整 `_render_start`
- **本轮出现**：`ui/dialog_view.py` r49 修复

## [内存泄漏] Toast 无 parent 且无 DeleteOnClose 导致累积
- **错误描述**：`Toast` 创建时无 parent 且无 `WA_DeleteOnClose`，`close()` 仅隐藏不销毁，C++ 对象持久占用内存；`_TOASTS` 列表随会话时长无界增长
- **正确做法**：添加 `setAttribute(Qt.WA_DeleteOnClose, True)`；`QPropertyAnimation` 设置 parent=self 确保同步销毁
- **本轮出现**：`ui/toast.py` r50 修复

## [信号回调] 缺少 _is_widget_alive 检查导致 C++ 对象已销毁
- **错误描述**：`focus_view.py` 的 `_on_stuck_ai`/`_on_auto_analyze` 等槽函数未检查 C++ 对象存活状态，信号在 closeEvent disconnect 前入队时触发 RuntimeError
- **正确做法**：所有连接全局单例信号的槽函数入口添加 `if not _is_widget_alive(self): return`
- **本轮出现**：`ui/focus_view.py` r50 修复

## [动画生命周期] QPropertyAnimation 未设置 parent
- **错误描述**：`toast.py` 和 `sidebar_v2.py` 中的 `QPropertyAnimation` 未设置 parent，目标 widget 销毁时动画写入已释放内存导致崩溃
- **正确做法**：`QPropertyAnimation(target, propertyName, parent)` 将目标 widget 同时作为动画 parent
- **本轮出现**：`ui/toast.py` + `ui/sidebar_v2.py` r50 修复

## [主题数据丢失] 内置主题保存擦除自定义主题数据
- **错误描述**：`ThemeTab.collect()` 在所有主题下都返回 `theme_custom_data`（内置主题返回 `{}`），导致 `_save_all` 用空 dict 覆盖已有自定义主题数据
- **正确做法**：仅在 `_key == "custom"` 且数据非空时才携带 `theme_custom_data`
- **本轮出现**：`ui/settings_view.py` r50 修复

## [JSON 解析] 嵌套 JSON 对象提取失败
- **错误描述**：`_parse_ai_json` 使用贪婪正则 `\{.*\}` 提取 JSON，多个独立 JSON 对象时匹配跨对象导致解析失败
- **正确做法**：使用花括号深度计数的 `_extract_json_object` 替代正则，正确处理嵌套结构
- **本轮出现**：`core/screen_analyzer.py` r50 修复

## [SVG 选择器] C++ 对象存活检查 shiboken6 不可用时兜底
- **错误描述**：`_open_svg_picker` 中 `isValid` 检查被 `except ImportError: pass` 吞掉，shiboken6 不可用时悬挂引用导致下次调用崩溃
- **正确做法**：shiboken6 不可用时用 `try/except RuntimeError: widget.isVisible()` 兜底检查
- **本轮出现**：`ui/settings_view.py` r50 修复

## [Qt 信号] lambda 连接无 receiver 导致回调在发射线程执行
- **错误描述**：`signal.connect(lambda ...: self.slot(...))` 的 lambda 没有 receiver QObject，Qt 按直连处理——跨线程信号（worker 线程 emit）时回调直接在 worker 线程运行，内部操作 QWidget 属未定义行为；且 lambda 形参与信号签名错位时 payload 静默丢失。绑定方法则自动经 AutoConnection 排队回 receiver 所在线程。
- **正确做法**：跨线程 worker 信号一律连接目标对象的**绑定方法**；需要附带上下文时用实例属性保存任务参数，在槽内读取；绝不用 lambda 携带闭包状态。
- **本轮出现**：`ui/focus_view.py:_ProblemWorker` r52 修复。

## [Qt 生命周期] disconnect 不撤销已入队的投递事件
- **错误描述**：closeEvent 中 disconnect 后，此前已 post 到接收者队列的 QMetaCallEvent 仍会执行槽；配合"关闭后对象仍存活（无 WA_DeleteOnClose）"，迟到结果会复活面板状态/定时器。
- **正确做法**：disconnect 之外必须在槽入口加代次/关闭旗标双保险（如 `_problem_flow_closed`），窗口关闭即置位，槽首行检查直接丢弃。
- **本轮出现**：`ui/focus_view.py` r52 修复。

## [抓取语义] 分页部分成功不得置整体 fetch_ok=True
- **错误描述**：多页抓取在循环体内逐页置成功标志，第 1 页成功第 2 页失败时返回空列表但标志仍为 True → 上层把"部分失败"当成"确认零提交"，触发零提交误锁。
- **正确做法**：多页聚合的成功标志必须是全或无（all-or-nothing）：任一页失败立即整体失败；空页收口视为成功；异常路径显式回写 False。
- **本轮出现**：`core/oj_tracker.py:fetch_today_submissions` r52 修复。

## [时间解析] 兜底返回 now 的解析器不能用于业务判定
- **错误描述**：`parse_oj_time` 解析失败兜底返回 `now_cst()`，调用方无法区分"真的是现在"和"没解析出来"，导致时间未知数据被当有效值参与比较分组。
- **正确做法**：需要严格判定的场景单独写不兜底的解析器（失败返回 None），与展示用途的宽松解析器分离。
- **本轮出现**：`core/oj_tracker.py:fetch_problem_pool/_strict_parse_end` r52 修复。

## [drawText 签名] PySide6 QPainter.drawText 不支持 x,y,flags,text 四参重载
- **错误描述**: screen_paint.py paintEvent 用 `painter.drawText(x, y, flags | Qt.TextWordWrap, text)`——PySide6 无此重载（支持 x,y,w,h,flags,text 或 QRect,flags,text），TypeError 从 paintEvent 抛出后 QPainter 未 end，进程退出时 `QPaintDevice: Cannot destroy paint device that is being painted` 直接崩溃（exit -1073740791）。
- **正确做法**: 用 `drawText(QRect(...), flags | Qt.TextWordWrap, text)` 矩形版；paintEvent 主体包 try/finally 确保 painter.end() 必达。

## [脏数据渲染炸屏] 指令列表里单个坏 op 让整屏覆盖层不可用
- **错误描述**: screen_paint 原型阶段 ops 直接 append AI 给的原始值——一个非法颜色串（QColor 抛 ValueError）或 NaN 坐标（int(nan*W) 抛 ValueError）从 paintEvent 抛出后，**每一帧重绘都失败**，整块黑板全黑且无法恢复，而不是只丢那一条指令；同时 `_ops` 无上限，长课堂全量重绘越来越慢。
- **正确做法**: 指令**入列口**统一净化（`_num` 非有限值回退、`_clamp01` 坐标钳制、`_safe_color` 颜色校验、字号/文本长度 clamp、`_append_op` 总量封顶丢最旧）；字段脏值走兜底保留整条 op（课堂连续性），结构脏（非 dict/缺 op 键）才整条丢弃。paintEvent 里不再有任何可抛异常的输入。
- **本轮出现**: 2026-09-06 核验轮自查发现（verifier 报告未覆盖），`test_screen_paint.py` 6 组脏输入回归锁死行为。

## [dataclass 行合并吞字段] 改写 dataclass 字段行时把相邻字段挤进行尾注释
- **错误描述**: settings.py 里给 `screen_capture_interval_sec` 行改写时，把原本独立一行的 `screen_capture_region: str = "fullscreen"` 合并到了同一行行尾——`#` 后的一切都是注释，字段定义静默消失。`AppSettings` 实例化后该属性不存在，三处消费方（screen_analyzer 截屏、settings_view 设置窗构造）引用即 AttributeError；且 `ConfigManager.update()` 的 hasattr 守卫让保存静默丢值、`to_dict()` 缺键导致下次 save 把用户 config.json 里该键永久抹掉。**最阴险处：离线测试 199 项全绿**（对该字段零覆盖），直到子 Agent 审查 git diff 才暴露。
- **正确做法**: ① 改 dataclass 字段行时**整块读改整节**，新行单独起行，永远不在行尾注释后再接内容；② 改完立即 `python -c "from config.settings import AppSettings; print(AppSettings().screen_capture_region)"` 类冒烟验证关键字段存在性；③ 对"被编辑过的 dataclass 节"补字段存在性回归断言（本轮 T16 已加）。
- **本轮出现**: round 55 settings.py 扩展时引入，verifier 第 1 轮审查捕获（Critical）。

## [queue 毒丸跨会话] worker 用毒丸退出时，Queue 必须随会话重建或启动前排空
- **错误描述**: ClassroomMonitor.stop() 投 `None` 毒丸让转写 worker 退出，但 start() 不清队列直接复用同一 Queue——上次会话遗留的毒丸让新 worker `get()` 后立即 break，**running=True 但永远零转写**，队列满 64 后静默丢段。用户在设置里关/开一次课堂音频即触发。同源问题：`stop_evt.clear()` 会复活 join 超时未死的旧 worker（绑的是同一 Event，clear 后 while 条件重新为真）。
- **正确做法**: ① 每会话**全新 `threading.Event`**（旧 worker 绑旧事件，处理完当前段自然死亡，杜绝 clear 复活）；② start() 生成 worker 前 `_drain_queue()` 排空遗留段与毒丸（丢弃段计数入 `dropped_stale` 保持账目闭合）；③ stop() 顺序改为"停采集 → VAD flush 入队 → 毒丸 → join → **最后**置事件"，让 flush 尾段有机会被转写（兑现"救最后一句"）。
- **本轮出现**: round 55 verifier 审查（Critical，实机复现），test_r55 T16 锁死"排空+会话复活+旧 worker 不复活"三行为。

## [loopback 空闲停产包] WASAPI loopback 无放音时不产数据包，VAD 时间轴会卡死
- **错误描述**: WASAPI loopback 在系统无任何放音（render 流空闲）时**完全不产数据包**，`GetCurrentPadding` 恒 0。若采集循环只 sleep 等待，空闲前最后一个语音段永远达不到"段尾静音 600ms"的收段条件——**段卡在开启态，内容滞留不转写**（实测 11.7s 三句音频只出前两句，末句在 stop flush 才被救回且晚 20s）。反向修正时若每个 padding==0 都注入静音，活跃放音期"消耗快于生产"的瞬时 0 也被注入，静音帧穿插真实语音把段时长虚增 ~70%（2.8s 算成 5.18s）。
- **正确做法**: 连续空闲 **≥250ms**（门槛滤掉瞬时 padding==0）才按实时速率向 VAD 路径注入合成零样本；注入只走 VAD 不进环形缓冲（captured_seconds 只记真实设备数据），`stats.idle_seconds` 单独记账。
- **本轮出现**: round 55 实机端到端 demo 发现（离线测试全绿掩盖不了——没有真实设备行为就没有这个场景），两轮探针定位根因。

## [信号闭包连接] worker 信号 connect(闭包/lambda) 无 receiver = direct connection，槽在 emit 线程执行
- **错误描述**: `_SpeechWorker.ready`/`TTSPlayer 内部 worker.ready/finished` 用**闭包函数**接收——Qt 对无 receiver 的 Python 可调用对象只能 direct connection，槽在 worker 子线程执行；槽内一旦触碰 QWidget/QThread(parent) 等 QObject（`_on_synthesized` 里 `QThread(self)`、`_finish_pipeline` 里 overlay.apply_ops/TTSPlayer.speak）即跨线程 UB，实机日志 `QObject: Cannot create children for a parent that is in a different thread`，偶发崩溃。**r52 已整训过，r56 在 coach（新写）与 tts_player（r53 遗留）两处复发**——写新 worker 时手滑闭包是高频惯性错误。
- **正确做法**: worker 信号**一律连接宿主 QObject 的绑定方法**（AutoConnection → queued 回主线程）；跨槽传递的上下文（point/speak）存宿主实例字段（如 `_pending`）而非闭包捕获；request_id 作为**信号第二参数**显式传递（绑定方法槽没有闭包默认参数可用）；迟到结果用 `rid != self._req` 丢弃。
- **本轮出现**: round 56 实机 demo 复现（coach 新写 1 处 + tts_player r53 遗留 2 处），修复后告警归零。

## [requests stream 泄漏] stream=True 的响应必须显式 close，三条退出路径都要覆盖
- **错误描述**: `chat_stream` 里 `requests.post(stream=True)` 后直接 `iter_lines`，无 with/finally close——正常结束、`[DONE]` break、中途 RequestException 三条路径全部不释放连接，fd 持有直到 GC；长课堂每 45s 一次决策调用，泄漏累积。
- **正确做法**: `resp = requests.post(..., stream=True)` 后紧跟 `try/finally: resp.close()`（或 with 块）；非流式 POST 无此问题（json() 读完后连接可复用）。
- **本轮出现**: round 56 verifier 审查捕获（High），新增 chat_stream 时引入。

## [mock 与现实脱节] 回归测试的 Fake 依赖自带了生产对象没有的属性，掩盖断链
- **错误描述**: classroom_sync `_collect_teaches` 读 `self._coach._teach_log`——真实 ClassroomCoach 根本没有 `_teach_log`（补讲数据经回调存在 sync 自身），`getattr` 永远回退空列表，**补讲记录永不上传**；而测试的 FakeCoach 自带 `_teach_log` 属性，断言全绿，缺陷被 mock 完美掩盖。直到 verifier 用"grep 生产对象的属性"视角才发现。
- **正确做法**: ① 涉及"宿主持有 vs 回调注入"的数据流，回归锁必须用**真实生产对象**（本例：真 ClassroomCoach + stub 依赖注入 overlay/tts，走 `_finish_pipeline → teach_logger → add_teach → _collect_teaches` 全链）；② Fake 只 mock 依赖边界（网络/文件），不 mock 被测数据流的中间载体；③ 新增数据字段时先问"这个属性在哪个对象上、谁写谁读"。
- **本轮出现**: round 57 verifier 审查捕获（Critical）。

## [测试夹具时间戳在未来] 夹具 ts 用"今天晚些时候"会压过当前时间，水位/max 断言假失败
- **错误描述**: 同步器水位用 ts 字符串 max() 推进，测试夹具设为"当天 10:00"，而测试运行在凌晨 02:xx——`max(夹具 ts, add_teach 登记的当前时间 ts)` 字符串比较取了未来夹具，"水位推进到当前"断言恒假失败。字符串 ISO 时间比较在**同日**内按时刻排序，与真实先后不符。
- **正确做法**: 涉及"当前时间 vs 夹具时间"比较的测试，夹具一律用**过去的日期**（如昨天）；或注入可控时钟（now 参数）而非依赖真实时钟。
- **本轮出现**: round 57 test_r57 水位断言，两次试错后定位。
