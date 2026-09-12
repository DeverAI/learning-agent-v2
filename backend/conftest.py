"""pytest 共享夹具（backend/ 目录级）。

在收集任何测试模块之前执行：
1. 通过 DSH_ERR_LOG_PATH 环境变量把 Err.log 重定向到临时目录。选环境变量而非
   仅改模块属性：test_bugfix_round 等会启动真实子进程，属性修改不会继承，
   环境变量会随进程继承，保证错误注入用例的回显绝不落入真实 backend/Err.log
   （2026-08-27 全局复查结论 M2，约定已录入 FreqErr.md）。
2. 数据目录重定向由各测试模块按"首导入方生效"惯例自行处理；本文件不重复
   重定向 STORAGE_DIR / SETTINGS_FILE / DATABASE_URL，避免与既有文件分叉。
"""
import os
import tempfile

# ---------------------------------------------------------------------------
# 收集排除：下列文件是「独立 harness 脚本」，不是 pytest 用例。
# 它们在模块顶层建库、跑断言、最后 sys.exit()，靠 `python backend/<file>` 单独运行；
# 一旦被 pytest 按 test_*.py 规则收集，导入期就会执行整个脚本体并抛 SystemExit，
# 使 `python -m pytest backend` 直接 INTERNALERROR / no tests ran（整个会话中断，
# 连别的模块都跑不到）。两者内部**均无任何 def test_***，排除不会丢失任何测试。
#
# 判定方式是 AST 而非正则：早先按 `^\s*sys\.exit\(` 正则统计，把三引号字符串字面量
# 内部的脚本（如 test_abuse_simulation 的 _ABUSE_SCRIPT）误判为模块级代码，得出
# "6 个文件"的错误结论。AST 不解析字符串内部，只有这 2 个文件是真的模块级 sys.exit。
# 见 FreqErr.md [测试脚本误入 pytest 收集]。
collect_ignore = [
    "test_r60_lecture_api.py",
    "test_r60b_board.py",
]

_err_dir = tempfile.mkdtemp(prefix="dsh_test_err_")
os.environ["DSH_ERR_LOG_PATH"] = os.path.join(_err_dir, "Err.log")

# 当前进程同样生效（若 logger 已被更早导入也一并修正）
import logger  # noqa: E402

logger.ERR_LOG_PATH = os.environ["DSH_ERR_LOG_PATH"]

