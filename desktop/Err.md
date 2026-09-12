# Err — Bug / 问题追踪记录

> 自动记录运行时 Bug 和待修复问题。

## 自动记录

| 日期 | 模块 | 问题 | 详情 | 状态 |
|------|------|------|------|:----:|

## 手动记录 — 本轮修复

| # | 模块 | 问题 | 修复 | 状态 |
|---|------|------|------|:----:|
| 1 | ui/focus_view.py | worker 信号用 lambda 连接（无 receiver），回调在 worker 线程直接执行并操作 UI（跨线程 UB），且形参错位导致 payload 丢失 | 改连绑定方法 `_on_problem_worker_sig/_fail_sig`（主线程排队） | ✅ |
| 2 | ui/focus_view.py | closeEvent 断不掉 lambda 连接 + Qt 不撤销已入队投递事件，窗口销毁后迟到结果复活轮询定时器 | `_problem_flow_closed` 旗标 + 槽入口统一丢弃 | ✅ |
| 3 | core/oj_tracker.py | 翻页部分失败时 `_last_fetch_ok` 被第 1 页置 True，整页返回 [] 被 daily_check 误判零提交 → 365 天误锁（二轮引入的安全退化，子AGENT沙箱复现） | `all_pages_ok/reached_empty_page` 重构：任一页失败整体 False | ✅ |
| 4 | core/focus_engine.py + main.py + ui/zzoi_view.py | force_release 无生产调用方，ZZOI 锁定后唯一出路是重启进程（历史遗留缺口） | 新增 `force_release_if_locked`；daily_check 检出当日提交经记录器+主线程桥应用解除 | ✅ |
| 5 | core/focus_engine.py | confirm 先 emit solved，UI 模态弹窗阻塞 _finish_session，期间倒计时空转 | 先收尾会话再发信号 | ✅ |
| 6 | core/oj_tracker.py | parse_oj_time 失败兜底返回 now，时间未知比赛被误判"进行中"优先选用 | fetch_problem_pool 内 `_strict_parse_end` 严格解析 | ✅ |
| 7 | core/oj_tracker.py | 提交记录时间列取 cells[-1]（可能是语言列）；日期格式不对称漏计 | 按日期正则扫描单元格 + 补零 ISO 归一比对；翻页最多 3 页 | ✅ |
| 8 | core/oj_tracker.py | 表格兜底通道把序号"1""2"当题号产出垃圾 pid | 要求含数字且长度≥3 | ✅ |
| 9 | core/oj_tracker.py | 题库兜底页未确保登录；已结束比赛题目可能禁提交却高优先级 | 补 `_ensure_logged_in`；降级链重排为 进行中比赛→作业→题库→已结束比赛(带警示) | ✅ |
| 10 | ui/focus_view.py | 取消/会话结束后迟到任务结果复活轮询；忙碌串行丢请求致按钮假死；取消时按钮文案滞留 | pending 态闸门；忙碌只提示不改按钮态；先 cancel 再刷新文案 | ✅ |
| 11 | ui/focus_view.py | 退出应用时在途 QThread 无等待 | closeEvent 对存活线程 quit+wait(1500) | ✅ |
| 12 | core/focus_engine.py + utils/helpers.py | report_problem_status 死代码；MD 缺 focus_problem_exit_started 渲染；assign_problem 剥离 source_note | 删死代码；补渲染行；权威副本保留 source_note | ✅ |

> 注：round 52 全量检修共修复 子AGENT两轮审查 12 类缺陷（1×P1 引入回归 + 历史 P1 解除通路缺口 + 10×P2）；
> 自审另捕获 lambda 信号形参错位与跨线程 UB 两项 P0 级隐患。
> 终验：test_r52_overhaul.py 72/72、test_r48_overhaul.py 151/151、audit_static 死配置[]/stub0/import0、运行时探针 9/9。
> 自动记录表已清空（保留表头）。
