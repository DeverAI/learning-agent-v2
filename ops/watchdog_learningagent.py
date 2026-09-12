# -*- coding: utf-8 -*-
"""LearningAgent 服务器看门狗（2026-09-10）。

目标：防止 uvicorn 进程被系统干掉后无人拉起、以及"服务 RUNNING 但 HTTP 已死"
的假死状态无人发现。

策略（保守三层）：
1. 每 60s 对 http://127.0.0.1:8000/api/health 探活（3 次尝试 × 10s 超时）。
2. 探活失败 → 查 nssm 状态：非 RUNNING 直接 start；RUNNING 但连续失败 →
   限流 restart（10 分钟冷却，最多连续 5 次）。
3. 熔断：连续 5 次 restart 无效后停止重启并打 ALERT（防抖动循环，
   等人工处理；写 C:\\all_projects\\watchdog.pause 可暂停看门狗动作）。

端口占用只记录 netstat 线索，不杀进程（避免误杀合法进程）。
nssm 自身会在进程退出时自动重启看门狗（本脚本崩溃不影响兜底）。

用法：
  python watchdog_learningagent.py            # 常驻循环（nssm 托管）
  python watchdog_learningagent.py --once     # 单轮（部署验证用）
  python watchdog_learningagent.py --once --dry-run   # 只记录决策不执行
环境变量：LA_HEALTH_URL / LA_INTERVAL 可覆盖探活地址与间隔（测试用）。
"""
import datetime
import os
import subprocess
import sys
import time
import urllib.request

HEALTH_URL = os.environ.get("LA_HEALTH_URL", "http://127.0.0.1:8000/api/health")
BASE_DIR = r"C:\all_projects"
LOG_PATH = os.path.join(BASE_DIR, "watchdog_learningagent.log")
PAUSE_FLAG = os.path.join(BASE_DIR, "watchdog.pause")
SERVICE = "LearningAgent"
INTERVAL = int(os.environ.get("LA_INTERVAL", "60"))
TIMEOUT = 10
TRIES = 3
RESTART_COOLDOWN = 600          # 两次 restart 之间最小间隔（秒）
MAX_CONSEC_RESTARTS = 5         # 连续 restart 上限，超过即熔断告警
LOG_MAX = 1_000_000             # 日志超 1MB 截断
LOG_KEEP = 200_000              # 截断时保留末尾字节数

DRY_RUN = "--dry-run" in sys.argv
ONCE = "--once" in sys.argv


def log(msg):
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            with open(LOG_PATH, "rb") as f:
                f.seek(-LOG_KEEP, 2)
                tail = f.read()
            with open(LOG_PATH, "wb") as f:
                f.write(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # 日志失败不阻断看门狗主循环
    print(line, flush=True)


def nssm(*args):
    """执行 nssm 命令，返回 (exit_code, 输出)。nssm 输出可能是 UTF-16。

    必须用绝对路径：服务账户 PATH 常不含 nssm，裸调 `nssm` 会
    WinError 2「找不到文件」→ 看门狗永远拉不活服务（R28 实测）。
    """
    candidates = [
        os.environ.get("NSSM_PATH") or "",
        r"C:\dsh\nssm.exe",
        r"C:\nssm\win64\nssm.exe",
        r"C:\nssm\nssm.exe",
        "nssm",
    ]
    exe = "nssm"
    for c in candidates:
        if c and (c == "nssm" or os.path.isfile(c)):
            exe = c
            break
    try:
        p = subprocess.run([exe] + list(args), capture_output=True, timeout=60)
        out = (p.stdout or b"").decode("utf-8", "replace").replace("\x00", "").strip()
        if not out:
            out = (p.stderr or b"").decode("utf-8", "replace").replace("\x00", "").strip()
        return p.returncode, out
    except Exception as e:
        return -1, f"nssm error: {e}"


def health_ok():
    for _ in range(TRIES):
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=TIMEOUT) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def exec_action(name, *args):
    if DRY_RUN:
        log(f"[dry-run] would {name}: {' '.join(args)}")
        return 0, "dry-run"
    return nssm(*args)


def log_port8000_owner():
    """端口占用线索只记录不处置，供人工判断是否孤儿进程占口。"""
    try:
        p = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=30)
        lines = [l.strip() for l in p.stdout.decode("gbk", "replace").splitlines()
                 if ":8000" in l and "LISTENING" in l.upper()]
        if lines:
            log("port8000: " + " | ".join(lines[:3]))
    except Exception as e:
        log(f"netstat probe error: {e!r}")


def cycle(state):
    if os.path.exists(PAUSE_FLAG):
        log("pause flag present, skip cycle")
        return

    ok = health_ok()
    if ok:
        if state["consec_fail"]:
            log(f"recovered after {state['consec_fail']} fail cycle(s)")
        state["consec_fail"] = 0
        state["consec_restarts"] = 0
        state["alerted"] = False
        log("ok")
        return

    state["consec_fail"] += 1
    log(f"health FAIL ({state['consec_fail']} consecutive)")

    code, out = nssm("status", SERVICE)
    if "SERVICE_RUNNING" not in out.replace(" ", ""):
        log(f"service not RUNNING (status={out[:60] or code}); starting")
        c2, o2 = exec_action("start", "start", SERVICE)
        log(f"nssm start -> exit={c2} {o2[:80]}")
        return

    # 服务 RUNNING 但 HTTP 死：限流 restart
    if state["consec_restarts"] >= MAX_CONSEC_RESTARTS:
        if not state["alerted"]:
            log("ALERT: restart limit reached; service RUNNING but unhealthy. "
                "Check port owner / DB / AI hang manually; "
                f"create {PAUSE_FLAG} during maintenance.")
            state["alerted"] = True
        return

    now = time.time()
    if now - state["last_restart"] < RESTART_COOLDOWN:
        log("in restart cooldown, waiting")
        return

    log_port8000_owner()
    state["last_restart"] = now
    state["consec_restarts"] += 1
    log(f"restarting {SERVICE} (restart #{state['consec_restarts']})")
    c3, o3 = exec_action("restart", "restart", SERVICE)
    log(f"nssm restart -> exit={c3} {o3[:80]}")


def main():
    state = {"consec_fail": 0, "consec_restarts": 0,
             "last_restart": 0.0, "alerted": False}
    log(f"watchdog start pid={os.getpid()} url={HEALTH_URL} "
        f"interval={INTERVAL}s dry={DRY_RUN} once={ONCE}")
    while True:
        try:
            cycle(state)
        except Exception as e:
            log(f"cycle error: {e!r}")
        if ONCE:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
