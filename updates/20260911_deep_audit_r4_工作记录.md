# 深度检修轮 R4 工作记录（2026-09-11）

> 触发：「修一下学习agent（双端架构）+ 深度检修 & 优化前端设计 & 绘图等，大胆想象，小心选择，谨记备份。
> 帮我**字面意义**上的'检查'，不是冒烟而是看代码、想到问题、验证、修复、记录。中间及时写好备份 & 文档更新。注意多端同步 & 检修。」
> 指令：「全都啃，干完再说」
> 状态：**主体完成**（剩余项见 §7）

## 0. 备份与探针留档

`backups/audit_r4_20260911/` —— **270 文件 / 8.97 MB**（backend 全套 + 根目录文档 + 本轮方案）

探针 / 补丁脚本（全部可重跑）：

| 脚本 | 作用 |
|------|------|
| `_probe_svg_unescape.py` | SVG 无条件 unescape + 占位伪成功 |
| `_probe_test_modules.py` | AST 判定测试文件真实模块级副作用（含 BOM 检测） |
| `_probe_board_escape.py` | 板书转义 + 落盘原子性 |
| `_patch_escape_messages.py` | 11 处 message 转义补丁（带出现次数断言） |
| `_patch_modal_aria.py` | 3 个静态弹窗 ARIA 补丁（带断言） |
| `_diag_frag.py` | 片段不匹配时的字符级诊断 |

## 1. 架构决策落定（用户确认）

写入 `Agent双端架构设计.md`：后台并发上限 **5**；**15 个工具全部注册** + 交互关系表（§4.2）；提前备课改为**用户手动触发**、产出「课包」（可筛题目/试卷，§8）；前台第二种决策「继续钻研 or 放弃」（§8.6）；多端同步纪律（§10.1）。

## 2. 审计执行方式

三路**只读**子 Agent 并行审计（`explore`，无写权限）：A 路由+模型 10 项 / B 服务层+绘图 9 项 / C 前端 14 项。**子 Agent 结论只作线索**，每项均由本人独立复核后才采信。

## 3. 回归基线的修复（P0）——本轮最关键

### 3.1 全量 pytest 之前根本跑不起来

`python -m pytest backend` → `INTERNALERROR: SystemExit: 0` + **`no tests ran`**（整个会话中断）。

**根因**：`test_r60_lecture_api.py:156` 与 `test_r60b_board.py:104` 是独立 harness 脚本（模块顶层建库、跑断言、末尾 `sys.exit`），文件名却符合 `test_*.py` 收集规则 → pytest 导入即执行脚本体 → SystemExit。

**修法**：`backend/conftest.py` 加 `collect_ignore = [...]`。**不改名**（避免 `updates/done_*.md` 里的历史引用失效）、不删文件、可逆；两个文件内部**均无 `def test_*`**，排除不丢任何测试。

### 3.2 我自己先犯了一个错，纠正后才动手

最初用正则 `^\s*sys\.exit\(` 统计，得出「**6 个文件**、改名会丢 **23 个真用例**」的结论——**两项都是错的**。实际有 4 个文件把待运行脚本存成**三引号字符串常量**（如 `_ABUSE_SCRIPT`）再交给 subprocess，正则不懂字符串上下文。**AST 判定**后真相是：只有 2 个文件会崩，且它们的 pytest 用例数都是 **0**。

**若按错误结论落地，会去"修" 4 个本来正常的文件。** 已作为独立 FreqErr 条目记录。

### 3.3 新基线

| 跑法 | 结果 |
|------|------|
| 修复前 全量 | **崩溃**，no tests ran |
| 修复前 排除 6 文件 | 137 passed |
| **修复后 全量** | **160 passed / 22s / 零失败** |

原记录的「145/145」不可复现；**以 160 为准**。

### 3.4 附带修掉：子进程编码

3 处 `subprocess.run(text=True)` 缺 `encoding` → Windows 按 gbk 解码 UTF-8 输出，实测抛 `UnicodeDecodeError`（以 `PytestUnhandledThreadExceptionWarning` 形式出现，**断言拿到的 stdout 已被静默截断 → 失败被伪装成通过**）。同仓 2 处写对了，属不一致。已统一补 `encoding="utf-8", errors="replace"`。

## 4. 已修复清单（全部验证）

| # | 问题 | 严重度 | 修复 | 验证方式 |
|---|------|--------|------|----------|
| 1 | **`_sanitize_svg` 解析前无条件 `html.unescape`** → 合法转义 `&amp;` 被打成 ParseError → 静默降级 400×300 空画布；且 `_write_svg` 落盘不校验 → 空图当成功写入。`save_reference_svg` 路径落盘空图后 DB 仍标 `ready`，之后回读**空图当事实基准**喂给解题模型 | 高 | 改为「先原样提取解析，仅在提取失败且含实体特征时才 unescape」 | 探针：已转义输出 125 字符不降级；畸形裸 `&` 仍正确降级 |
| 2 | **`GET /api/settings/export` 明文导出 `api_password`**（遮蔽规则只认 `"key"`） | **高（安全）** | 单点判据 `is_sensitive_setting_key()` | 真值表 11 例全对 |
| 3 | **`PUT /api/settings/import` 可清空 `api_password`** → 密码为空即「鉴权未启用」（`main.py` 的 `if expected and ...`）→ **一次导入关掉全站鉴权**。与 #2 构成闭环 | **高（安全）** | 导入循环开头敏感键 `continue`；并清理被短路的遗留判据 | 同上 |
| 4 | **`PUT /api/questions/{id}` 缺 `is_resolved` 锁定守卫**（同仓约 20 处同类端点都有，该函数体内 `is_resolved` 出现 0 次） | 高 | 补 `409 题目已锁定` 守卫 | 读码 + 回归 |
| 5 | **`assemble()` 空 spec 的 400×300 占位卡片能骗过 `_has_drawing_content`**（正则存在性判断必放行），且 3 处调用点都不校验就落盘返回成功 URL | 高 | 质检函数排除占位签名；3 处调用点补显式校验 | 探针：占位三例均 `has_content=False`（修复前 True） |
| 6 | **板书 shape 的 `title`/`labels` 消毒后拼接未转义** → 含 `&`/`<` 时整图被静默降级为空画布；且「先补标题后判内容」的顺序让空画布骗过质检、失败计为成功 | 中 | 拼接前 `html.escape`；判定顺序改为**先判内容后补标题** | 探针 6/6：转义后不降级、无 `<script>` 泄漏、非法输入仍报错 |
| 7 | **`_save_board_svg` 非原子直写**（`O_EXCL` 后直接 `f.write`）→ 进程中途被杀留**截断**的 `board_N.svg`，而 `O_EXCL` 让它永不被覆盖、取号还把它算作已存在 | 中 | 改临时文件 + `os.replace`（对齐 `_atomic_write_json` 惯例） | 探针：无临时残留，`board_1.svg` 正常 |
| 8 | **`chat.py` 解题链路守卫顺序**：先写 `audit_flags` + 落盘 SVG，之后才做锁定判定 → 锁定期内产生「文件在盘上、DB 不认」的悬挂产物 | 中 | 守卫前移到**首个副作用之前** | 读码 + 回归 |
| 9 | **11 处 `e.message`/`r.message` 未转义直入 `innerHTML`**（`questions.js` 7 处 + `batch_upload.html` 4 处）。污染源已确证：`app.js` 把服务端 `detail` 与响应体前 80 字拼进 `Error.message` | 中 | 全部经 `$esc` | 补丁脚本断言 + 零残留复查 |
| 10 | **编辑器调色板只能 HTML5 拖拽加元件** → 触屏（Android WebView 无 DnD）与键盘完全加不上，而 `addComponent` 全仓唯一调用点在 `drop` 里 | 高（功能不可用）| 补 click / Enter 兜底（落点在画布中心，与 drop 同款偏移） | 读码 + 回归 |
| 11 | **`cmpFullScreen()` 无能力检测** → 无 Fullscreen API 的环境同步抛 `TypeError`（`.catch` 捕不到同步异常），`:fullscreen` 布局整个分支不可达 | 中 | 补能力检测 + try/catch + 前缀兼容 | 读码 + 回归 |
| 12 | **离线层 `_start` 全仓零调用点** → 断网/恢复提示永不触发；上次会话遗留队列不会自动补传（`scheduleReplay` 只在入队时被动调用） | 中 | 在 IIFE 末尾自启动（`enable_offline` 已天然门控） | 读码 + 回归 |
| 13 | **对比模式对 viewBox 原点重复扣除**，与**同文件 L1333-1334 已写明的约定**矛盾（结构梳理遵守、对比模式没遵守）→ 原点非 0 时整图偏右下 | 中 | 去掉 `- minX*scale` / `- minY*scale` | 读码 + 回归 |
| 14 | **`role="dialog"` / `aria-modal` 在 templates 下命中数为 0**（app.js 动态弹窗有、静态模板没有） | 中 | 3 个静态弹窗补齐（`notes.html` 用 `aria-labelledby`） | 脚本复查 6 处（修复前 0） |
| 15 | **mermaid 是唯一 CDN 依赖且用 `defer`** → CDN 被墙/悬挂时阻塞 `DOMContentLoaded`，拖住整页交互（marked/katex 都已 vendor，建议一并本地化） | 中 | 改 `async` + `onerror` + app.js 侧**重试渲染**（沿用 KaTeX 同款写法）并补 `startOnLoad:false` 初始化 | 读码 + 回归 |
| 16 | **`&times;` 关闭按钮残留**（editor.js 5 处）+ 标签删除控件是 `<span>` 非 button、无 aria | 低 | editor.js 全量迁到 `window._closeIconSvg`；标签控件改真 `button` + `aria-label` | grep 复查 |

## 5. 已登记（FreqErr 新增 7 条）

`[测试脚本误入 pytest 收集]`、`[正则误判模块级副作用]`、`[导入期 chdir 污染]`、`[子进程编码未指定]`、`[SVG 无条件 unescape]`、`[失败占位伪成功·复发]`、`[敏感字段判据不一致]`、`[截断计数当总量]`

> 注：`[敏感字段判据不一致]` 与 `[截断计数当总量]` 的首轮修复中，`"token"` 一词曾误伤 `*_max_tokens`(int) 与 `xiaomi_token_plan_base_url`(str)，被测试逮出（`mask_key` 收到 int → `TypeError`）。已收窄为 `key`/`password`/`secret` 并加 `isinstance(str)` 防御。

## 6. 多端同步影响面

| 改动 | 网页 | Android WebView | 桌面端 |
|------|------|-----------------|--------|
| `diagram_service.py` / `focus_service.py` | 生效 | 生效 | **需单独同步** |
| `routers/`（settings / chat / questions） | 生效 | 生效 | **需单独同步** |
| `conftest.py` + `test_*.py` | 仅测试 | — | — |
| `templates/` + `static/js/` | 生效 | **同模板，一起生效，必须一起回归** | 副本需单独同步 |

`desktop/` 是独立副本，后端/前端改动**不会自动传导**。出包（APK / 桌面端 zip）需在本轮收尾时重做。

## 7. 剩余项（未做，已定位）

| # | 项 | 说明 |
|---|----|------|
| 1 | **A-4/A-6/A-7 性能**：`search_service._find_matches` 无 LIMIT 全表加载 + **同步** `SequenceMatcher` 直接阻塞事件循环（会拖慢所有接口）；Notes 三处全表加载且对每条笔记全文跑 `resolve_note_references`；`/api/papers/library` 深分页最多加载 10000 行实体 | 需定型语义后再改，`FUTURE.md` 已有登记 |
| 2 | **低危批剩余**：非 button 可点击元素（约 26 处）、`prompts.html` 数据驱动色值内联、id 拼进内联 `onclick`、导出 SVG 兼容性 | 逐处改，量大风险低 |
| 3 | **mermaid 本地 vendor** | 彻底去掉唯一外网依赖，对齐 marked/katex |
| 4 | **收尾**：出包 + 同步服务器；`done.md` 汇总 | — |

## 8. 未能在本环境验证的事项（诚实声明）

- **前端交互类修复（#10–#15）只做了静态验证与回归**，本环境无浏览器，**未做真机/浏览器冒烟**。建议在浏览器里各点一遍：编辑器调色板点击加件、对比模式全屏与重置、离线提示、mermaid 图表渲染。
- mermaid 从 `defer` 改 `async` 后，**图表渲染时序需实测确认**（已用重试兜住晚到，但未在真实网络下验证）。
