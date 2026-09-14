# -*- coding: utf-8 -*-
"""30 分钟巡检：服务器健康 / 题目试卷报错 / 问题反馈 / Err.log。

输出人类可读摘要；有问题返回非空列表供 Agent 修复。
"""
import os, sys, sqlite3, urllib.request, json
from datetime import datetime, timedelta

BASE = r"C:\all_projects\learningAgent\backend"
DB = os.path.join(BASE, "storage", "app.db")
ERR = os.path.join(BASE, "Err.log")

def get(url):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000" + url, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)

def main():
    issues = []
    print("=== 巡检", datetime.now().strftime("%Y-%m-%d %H:%M"), "===", flush=True)

    # 1 health
    st, body = get("/api/health")
    print("health", st, body[:80], flush=True)
    if st != 200:
        issues.append("health 非 200: " + body[:100])

    # 2 DB: recent errors
    if os.path.isfile(DB):
        con = sqlite3.connect(DB, timeout=20)
        try:
            # questions error last 6h
            cutoff = (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
            rows = con.execute(
                "select id,status,substr(error_message,1,80),updated_at from questions "
                "where status='error' and updated_at>=? order by updated_at desc limit 15",
                (cutoff,)).fetchall()
            print("q_errors_6h", len(rows), flush=True)
            for r in rows:
                print("  Q", r, flush=True)
                issues.append("题目错误 %s: %s" % (r[0][:8], r[2]))

            # papers
            try:
                rows = con.execute(
                    "select id,title,substr(paper_html,1,40) from papers order by created_at desc limit 5"
                ).fetchall()
                print("papers_recent", len(rows), flush=True)
            except Exception:
                pass

            # open feedback
            try:
                rows = con.execute(
                    "select id,kind,title,client,status,created_at from feedbacks "
                    "where status='open' order by created_at desc limit 20"
                ).fetchall()
                print("open_feedback", len(rows), flush=True)
                for r in rows:
                    print("  F", r, flush=True)
                    if r[4] == "open":
                        issues.append("未处理反馈 %s: %s" % (r[0][:8], r[2]))
            except Exception as e:
                print("feedback table?", e, flush=True)

            # generating stuck > 2h
            stuck = con.execute(
                "select id,status,updated_at from questions "
                "where status='generating_solution' and updated_at < datetime('now','-2 hours') "
                "limit 10"
            ).fetchall()
            print("stuck_generating", len(stuck), flush=True)
            for r in stuck:
                issues.append("卡住的解题 %s since %s" % (r[0][:8], r[2]))
        finally:
            con.close()
    else:
        issues.append("数据库不存在: " + DB)

    # 3 Err.log
    if os.path.isfile(ERR):
        txt = open(ERR, encoding="utf-8", errors="replace").read().strip()
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        print("err_log_lines", len(lines), flush=True)
        for ln in lines[-8:]:
            print("  ", ln[:160], flush=True)
        # 只报最近 12 小时内的（Err.log 时间戳形如 [09-13 19:24]）
        recent = []
        for ln in lines:
            if ln.startswith("[") and "]" in ln:
                recent.append(ln)
        if recent:
            issues.append("Err.log 有 %d 条，最新: %s" % (len(recent), recent[-1][:80]))
    else:
        print("no Err.log", flush=True)

    print("=== ISSUES", len(issues), "===", flush=True)
    for i in issues:
        print("!", i, flush=True)
    return len(issues)

if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
