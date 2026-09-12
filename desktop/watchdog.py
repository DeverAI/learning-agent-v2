"""OISystem watchdog 守护进程。

独立运行，监控主进程 PID，主进程崩溃则重启。
正常退出通过 data/exit_signal 信号文件通知 watchdog 退出。
"""
import os
import sys
import time
import argparse
import subprocess
import psutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.helpers import DATA_DIR, ensure_dirs, logger, now_cst, format_time

EXIT_SIGNAL_FILE = os.path.join(DATA_DIR, "exit_signal")
RESTART_COOLDOWN_SEC = 5


def is_normal_exit() -> bool:
    return os.path.exists(EXIT_SIGNAL_FILE)


def clear_exit_signal():
    if os.path.exists(EXIT_SIGNAL_FILE):
        try:
            os.remove(EXIT_SIGNAL_FILE)
        except OSError:
            pass


def main_pid_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def log_watchdog_event(msg: str):
    logger.warning(f"[watchdog] {msg}")
    # 同时写入当日日志
    try:
        from utils.helpers import log_event
        log_event("watchdog", {"msg": msg, "ts": format_time(now_cst())})
    except Exception:
        pass


def restart_main():
    main_py = os.path.join(os.path.dirname(__file__), "main.py")
    try:
        subprocess.Popen(
            [sys.executable, main_py],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log_watchdog_event(f"主进程已重启 {main_py}")
    except Exception as e:
        log_watchdog_event(f"重启失败: {e}")


def restart_enabled() -> bool:
    """读取 watchdog_restart_on_crash 配置，决定主进程崩溃后是否重启。

    全量检修修复：该配置项此前声明但从未消费，导致主进程崩溃后无条件重启。
    读配置失败时默认开启重启（宁可重启不静默放弃）。
    """
    try:
        from config.settings import ConfigManager
        return bool(ConfigManager().settings.watchdog_restart_on_crash)
    except Exception as e:
        log_watchdog_event(f"读取 watchdog_restart_on_crash 失败，默认重启: {e}")
        return True


def run(main_pid: int):
    ensure_dirs()
    # 启动时清理上次崩溃可能残留的 exit_signal，否则会立即判定为正常退出
    clear_exit_signal()
    logger.warning(f"[watchdog] 监控主进程 PID={main_pid}")
    log_watchdog_event(f"watchdog 启动，监控 PID={main_pid}")

    while True:
        time.sleep(2)

        # 正常退出信号
        if is_normal_exit():
            log_watchdog_event("收到正常退出信号，watchdog 退出")
            clear_exit_signal()
            return

        # 主进程还在
        if main_pid_alive(main_pid):
            continue

        # 主进程崩溃
        log_watchdog_event(f"主进程 PID={main_pid} 消失，判定为崩溃")
        if not restart_enabled():
            log_watchdog_event("watchdog_restart_on_crash 已关闭，不重启，watchdog 退出")
            return
        time.sleep(RESTART_COOLDOWN_SEC)
        restart_main()
        # 重启后退出由新 watchdog 接管
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("main_pid", type=int, help="主进程 PID")
    args = parser.parse_args()
    run(args.main_pid)
