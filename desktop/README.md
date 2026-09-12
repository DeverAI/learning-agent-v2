# OISystem — 信息学奥赛学习辅助工具集

OISystem 是一个本地运行的 Windows 桌面学习辅助系统（PySide6）。它围绕"专注模式"组织 OI / 文化课学习流程，并提供 AI 对话引导、屏幕分析、ZZOI 提交检测、网站管控、图论示意图编辑器、日志归档与邮件/note.ms 同步等能力。

> 本仓库为源码发布版：`OISystem/` 目录为实际项目根目录；开发过程文档（设计/技术方案/错误追踪等）不随仓库发布。
> 运行时未捕获异常会自动追加记录到 `OISystem/Err.md`（修复完成后程序会清空该文件，保留表头）。

---

## 1. 功能总览

### 1.1 专注模式
- OI 模式 / 学习文化课模式切换（`focus_mode`）。
- 倒计时基于单调时钟（`time.monotonic()`），系统改时间/休眠恢复不会错乱。
- 急事退出（仅退专注，记日志）/ **做题退出**（OI 模式正常退出 = 系统从 ZZOI 真实题目源——进行中比赛 → 作业 → 题库——挑一道可提交的题，AC 后放行结束；可在设置关闭）/ 关机退出（归档后关机）。
- 自动屏幕分析：按 `screen_capture_interval_sec` 截图 + 视觉模型分析，连续 N 次无进展（`focus_stuck_threshold`）触发 AI 卡住引导。
- ZZOI 锁定：当日零提交 / 排行榜末尾 → 锁定专注 365 天（名义），检测到当日有提交经每日检查自动 `force_release_if_locked` 解除；锁定期间所有退出按钮隐藏且引擎拒绝退出，关机退出同样拦截。
- 可配置开关：`focus_emergency_exit_allowed`、`focus_lock_on_no_submission`、`focus_lock_on_rank_tail`、`problem_exit_enabled`。

### 1.2 AI 对话
- 默认 DeepSeek 对话，可任意配置 OpenAI 兼容 API（KIMI / GLM / DEEPSEEK / 小米 MiMo）。
- **小米 MiMo（Token Plan）**：讲题对话主力（mimo-v2.5-pro），专用 Base URL
  `https://token-plan-cn.xiaomimimo.com/v1`（密钥 tp- 前缀）；`xiaomi_role` 同样支持
  knowledge/vision/dialog 三角色；全模态 `mimo-v2.5` 可承担视觉任务。
- **讲题朗读（TTS）**：基于小米 MiMo-V2.5-TTS，assistant 气泡「朗读」按钮即点即读，
  或在设置中开启"AI 回复后自动朗读"；自动剥离代码块/链接/公式定界符只读正文；
  内置音色 mimo_default / 冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean 可选。
- `kimi_role` / `glm_role` / `deepseek_role` / `xiaomi_role` 角色路由：knowledge=flash 判定与摘要、dialog=主对话、vision=屏幕分析。
- 闲聊检测、代码输出检测、新算法想法提醒、领域不匹配模式切换横幅、元监督纠偏（每 N 轮）。
- 上下文分块 + 引用/拉黑；`ai_dialog_max_rounds` 轮数裁剪；`ai_dialog_max_context_blocks` 价值裁剪。
- Markdown 渲染（公式 Unicode 化、graph 代码块自动 SVG、mermaid 源码保留、链接/图片占位）。
- 截图分析 / 关联当前页面 / 文件上传 / 图论编辑器插入对话。
- 对话存档：关闭/新建时在线程内摘要并写 `data/dialogs/`；退出程序时同步兜底存档。

### 1.3 屏幕分析与管控
- 全屏 / 活动窗口 / 自定义矩形截图；清晰度与最大宽度可配置；截图闪光动画。
- 视觉分析结构化结果 → FocusEngine 进展判定、CheatDetector 代码速度异常检测、SiteGuard 网站/窗口关闭。
- SiteGuard：白名单优先（OI 与学习模式独立），黑名单/游戏域/资讯域/活动关键词风险评分；
  - 专注模式命中即关；
  - 非专注模式按 `site_risk_score_threshold` 决定（0/负数=命中即关，>100=不拦截）。

### 1.4 ZZOI 集成
- 基于 **Hydro 架构 OJ 的网页抓取**适配开发：无需 OJ 提供 API 权限，任何 Hydro 架构 OJ 只需在设置中修改 Base URL 与域名前缀即可接入。
- Hydro OJ 登录（sid cookie 优先，密码兜底）、当日提交抓取、作业/比赛列表、排行榜末尾保守判定。
- 手动检查/重新登录在线程中执行，不冻结 UI；30 分钟自动检查经主线程桥异步执行。
- 抓取网络失败 `fetch_ok=False`，**不会误触发零提交锁定**。

### 1.5 图论编辑器与 Markdown 图
- `graph` 代码块支持 YAML / 简化 / UVW（csacademy）三种格式，100KB + 20 节点 / 100 边硬限。
- 图论编辑器：绘制/编辑/删除/力导向模式，有向切换，UVW 实时预览（200ms debounce），导入邻接表（同样有 100KB/20/100 上限），导出 PNG/邻接表，复制/插入对话。
- 所有解析失败/超限均保留原画布或回退原文，不破坏用户工作。

### 1.6 系统集成
- 托盘常驻 + 双击展开侧边栏；4 种侧边栏样式（trapezoid/rect/full/float），悬浮式有全局热键（pynput 经主线程桥投递）。
- 静音模式（默认 `ctrl+shift+q`）：隐藏全部 toast、日志照记、按钮状态同步，热键同样经主线程桥。
- watchdog 子进程：主进程崩溃按 `watchdog_restart_on_crash` 决定是否重启；正常退出写 `exit_signal`。
- 日志：`data/daily/*.json` 结构化日志 + `logs/daily/*.md` 人类可读日志；note.ms 云同步（可配 base URL，默认关闭）、邮件日报、极域快照打包（最多 500 张）。
- SVG 图标选择器：内建 + `svg_mapping.json` 文件 SVG，勾选后按语义应用到侧边栏按钮。

---

## 2. 运行环境

- Windows 10/11（截图/窗口关闭/极域快照等使用 Win32 API；其他平台可运行但仅检测不关窗）
- Python 3.10+（开发环境实测 3.13）
- 依赖见 `requirements.txt`：

```text
PySide6>=6.6.0
mss>=9.0.0
Pillow>=10.0.0
pynput>=1.7.6
pystray>=0.19.5
psutil>=5.9.0
requests>=2.28.0
beautifulsoup4>=4.11.0
schedule>=1.1.0
pywin32>=306
pyinstaller>=6.0.0
```

安装：

```powershell
cd OISystem
python -m pip install -r requirements.txt
```

> 实测开发环境同时存在 PySide6 与 PyQt6 不影响本项目（项目固定 `from PySide6`）。

---

## 3. 首次运行

```powershell
cd OISystem
python main.py
```

启动顺序：
1. 注册全局异常钩子（未捕获异常自动追加到 `Err.md`）。
2. 加载配置：`data/config.json`（公开配置）+ `data/secrets.json`（密钥/密码）。
3. 初始化 FocusEngine、ScreenAnalyzer、AIDialog、ZzoiTracker。
4. 创建侧边栏与托盘图标；启动静音热键；启动 watchdog；60 秒后首次异步 ZZOI 检查，此后每 30 分钟一次。

关闭程序：点击侧边栏「退出」或托盘菜单「退出 OISystem」。退出流程会：
1. 拦截运行中的专注模式（ZZOI 锁定禁止退出）；
2. 同步存档当前 AI 对话；
3. 同步 note.ms / 邮件 / 极域快照；
4. 写 `data/exit_signal` 通知 watchdog 正常退出；
5. 清理全局热键后退出。

---

## 4. 配置说明

设置中心（侧边栏「设置」）含 9 个 Tab，底部「保存所有更改」统一持久化：

| Tab | 内容 |
|---|---|
| 主题风格 | 内置黑/白/米/蓝主题、JSON 导入导出、取色器、实时预览、SVG 图标选择器 |
| 屏幕检测 | 截图频率/区域/自定义矩形/视觉引擎/清晰度/最大宽度 |
| AI 对话 | Base URL、对话模型、轮数上限、上下文块上限、Flash 模型、元监督间隔、非专注提醒分钟、追加规则 |
| 专注模式 | 模式、默认时长、急事退出开关、自动分析间隔、卡住阈值、零提交/排行榜锁定、静音热键 |
| 用户画像 | AI 提取画像编辑（自动注入 system prompt） |
| 网站名单 | OI 白名单、学习白名单、黑名单、资讯域、游戏域、风险阈值 |
| ZZOI/任务源 | UID/密码/SID/Base URL/域名前缀（学习模式扩展任务源见 Future） |
| API/邮件 | KIMI/GLM/DeepSeek/小米MiMo 的启用、Base URL、模型、Key、角色；小米 TTS 模型/音色/自动朗读；SMTP 与极域快照 |
| 系统集成 | 侧边栏样式/悬浮热键、watchdog 开关、note.ms 开关与 Base URL/后缀、外部 AI 提醒冷却 |

敏感字段（`mail_password`、`zzoi_password/sid/sid_sig`、三个 API key）保存到 `data/secrets.json`，其余保存到 `data/config.json`。

### 关键配置语义

- `focus_mode`: `oi` / `study`。
- `kimi_role` / `glm_role` / `deepseek_role`：`knowledge` / `vision` / `dialog`；deepseek 只支持 dialog，视觉任务强制回退 GLM。
- `ai_flash_model`：flash 任务模型名权威；模型名含 "deepseek" 时强制回退 deepseek provider，避免把 deepseek-flash 发给 KIMI/GLM。
- `site_risk_score_threshold`：0/负数=命中即关；1-100=评分阈值；>100=非专注不拦截（专注始终严格）。
- `watchdog_restart_on_crash`：主进程崩溃后是否自动重启。
- `note_ms_base_url`：note.ms 兼容服务的 Base URL（API 路径为 `{base}/api/notes/{slug}[/content]`）。
- `selected_svgs`：SVG 选择器收藏，按语义映射到侧边栏按钮图标。

---

## 5. 目录结构

```text
OISystem/
├── main.py               # 入口：全局钩子、引擎装配、托盘、热键、watchdog、自动ZZOI
├── watchdog.py           # 独立守护进程（崩溃重启/正常退出信号）
├── config/
│   └── settings.py       # AppSettings/ConfigManager（脏数据自愈、敏感字段拆分）
├── core/
│   ├── ai_client.py      # OpenAI 兼容客户端 + role 路由（kimi/glm/deepseek/xiaomi）+ MiMo TTS
│   ├── ai_dialog.py      # 对话引擎/检测器/截图线程/存档线程
│   ├── tts_player.py     # 讲题朗读（MiMo-V2.5-TTS 合成 + winsound 播放，r53 新增）
│   ├── ai_supervisor.py  # 元监督
│   ├── context_manager.py# 上下文分块/裁剪/引用/拉黑
│   ├── focus_engine.py   # 专注倒计时/退出/ZZOI 锁定/卡住检测
│   ├── screen_analyzer.py# 截图 + 视觉分析线程
│   ├── cheat_detector.py # 外部 AI/代码速度异常检测
│   ├── site_guard.py     # 窗口/网站管控
│   ├── oj_tracker.py     # ZZOI 抓取与每日检查
│   ├── exit_flow.py      # 退出/关机流程
│   ├── log_sync.py       # note.ms/邮件/极域快照
│   └── mute_mode.py      # 静音模式与全局热键
├── ui/
│   ├── focus_view.py     # 专注窗口
│   ├── dialog_view.py    # AI 对话窗口（虚拟滚动/Markdown/截图/关联页面）
│   ├── settings_view.py  # 设置中心（9 Tab）
│   ├── log_view.py       # 日志/作弊检测窗口
│   ├── zzoi_view.py      # ZZOI 状态窗口（异步检查）
│   ├── svg_view.py       # SVG 图标选择器
│   ├── graph_editor.py   # 图论编辑器
│   ├── graph_renderer.py # graph 文本 → SVG
│   ├── md_renderer.py    # Markdown/公式渲染
│   ├── sidebar*.py       # 5 种侧边栏实现 + 工厂
│   ├── themes.py         # 主题系统
│   ├── icons.py          # 内建 SVG + 文件 SVG + 侧边栏按钮映射
│   ├── toast.py / tray.py / camera_flash.py / frame_mixin.py ...
├── utils/
│   ├── helpers.py        # 时间/JSON/日志（原子写、多线程锁、自愈）
│   └── exceptions.py
├── data/                 # 运行时生成（config/secrets/日志/对话存档等），不入库
├── logs/daily/           # 运行时生成的人类可读每日日志
└── test_r*.py            # 各轮回归测试（独立脚本，python test_rXX.py 运行）
```

---

## 6. 测试

全部回归测试是独立脚本（非 pytest 收集式），从 `OISystem/` 目录逐个运行：

```powershell
cd OISystem
$env:QT_QPA_PLATFORM = "offscreen"
python test_r29_regression.py
python test_r30_regression.py
# ... 依次运行到 ...
python test_r48_overhaul.py
python test_r52_overhaul.py
```

或一键循环：

```powershell
Get-ChildItem -File -Filter 'test_r*.py' | Sort-Object Name | ForEach-Object {
  python $_.Name; if ($LASTEXITCODE -ne 0) { Write-Error "$($_.Name) FAILED" }
}
```

> 当前仓库包含 4 个回归测试脚本（自 `OISystem/` 目录运行）：
> `test_r48_overhaul.py`（151 项全量检修）、`test_r52_overhaul.py`（做题退出 v2）、
> `test_r53_xiaomi.py`（小米 MiMo / TTS）、`test_r54_frontend_fixes.py`（前端修复）；
> 另有 `audit_static.py` 静态审计（槽函数/死配置/stub/import）。

- `test_r48_overhaul.py` 覆盖 round 48 全量检修（151 项断言）。
- `test_r52_overhaul.py` 覆盖做题退出 v2 + 本轮检修（72 项断言）。
- `audit_static.py` 提供静态端口审计（槽函数/死配置/stub/import），随时可跑 `python audit_static.py`。

---

## 7. 打包为 exe

### 环境要求
- Python 3.10+
- PySide6 >= 6.6.0
- PyInstaller >= 6.0.0

### 快速打包（仓库未内置 spec，可按需自行编写）
```powershell
cd OISystem
pip install -r requirements.txt
pyinstaller --clean -F -w --name OISystem main.py
```
输出文件：`dist/OISystem.exe`（单文件模式；如需随包附带资源，请自行编写 spec 文件）

### 注意事项
- 首次运行会在同目录生成 `data/` 文件夹（配置、日志等）
- 杀毒软件可能误报，需添加信任

---

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 做题退出提示"未获取到可用题目" | ZZOI 未登录/网络失败/无进行中比赛且作业题库为空；可点「换一题」重试，急事退出/关机退出始终可用 |
| 做题退出的题 AC 后没放行 | 检测依赖当日提交抓取（ZZOI record 页），点「我已AC · 检测」手动刷新或等 60s 自动轮询；确认提交的是分配面板上的同一 pid |
| 侧边栏图标不是自己挑的 SVG | 工作区 `svg_mapping.json` 曾遗失，现为占位版（同名 12 图标）；找回原文件放到工作区根目录即可恢复 |
| 启动后设置中心打不开或字段为空 | `data/config.json` 被手改坏；程序会自动按类型自愈（非 dict 顶层按空配置、非法数值回退默认），如仍异常可退出程序后手动删除该文件恢复默认（请先备份） |
| 专注模式无法退出 | 正常：ZZOI 锁定期间必须等提交成功解除；急事退出可在设置中禁用 |
| 截图按钮转圈/无结果 | 视觉 API 未启用或无 key；检查「API/邮件」中 glm/kimi 配置；截图分析会经角色路由自动选择可用视觉 provider |
| AI 一直报"上一次对话仍在进行" | 网络较慢，等待 60s 超时后自动恢复；新建对话会忽略旧 worker 回复 |
| 悬浮侧边栏热键无效 | 检查「系统集成」中热键格式（如 ctrl+shift+s）；pynput 未安装则不可用 |
| note.ms 同步 403 | note.ms 有 Cloudflare 人机验证，默认已关闭；可改自建兼容服务 Base URL 或保持关闭 |
| 邮件发不出 | SMTP 服务器/端口/授权码需与邮箱服务商一致；密码存 `data/secrets.json` |
| 退出流程耗时 | 退出前同步存档对话与日志，网络超时最长约 1-2 分钟；可关闭 note.ms/邮件相关开关减少等待 |
| 全屏侧边栏/某窗口无显示 | 任务栏 RDP 断开或无显示器时窗口会自动跳过定位并保持可运行；恢复显示器后重启程序 |

---

## 9. 安全与数据

- 所有 API Key、邮箱密码、ZZOI 密码只写入 `data/secrets.json`，请勿提交到公开仓库。
  小米 Token Plan Key（tp- 前缀）同样存于 secrets.json；官方建议定期轮换密钥。
- 对话存档位于 `data/dialogs/`，结构化日志位于 `data/daily/`。
- 网站管控会发送 WM_CLOSE 关闭命中窗口；白名单始终优先。
- 未捕获异常自动追加到 `Err.md`，修复完成后该文件会被清空（保留表头）。
