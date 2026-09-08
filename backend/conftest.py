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

_err_dir = tempfile.mkdtemp(prefix="dsh_test_err_")
os.environ["DSH_ERR_LOG_PATH"] = os.path.join(_err_dir, "Err.log")

# 当前进程同样生效（若 logger 已被更早导入也一并修正）
import logger  # noqa: E402

logger.ERR_LOG_PATH = os.environ["DSH_ERR_LOG_PATH"]

