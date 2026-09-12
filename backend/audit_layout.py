# -*- coding: utf-8 -*-
"""前端静态巡检（布局 / 主题 / 鉴权三类"肉眼难发现"的回归）。

用法：
    python backend/audit_layout.py            # 打印报告，有违规时退出码 1
    python backend/audit_layout.py --json     # 机器可读

## 为什么要有这个脚本

FreqErr 里已经记了三条**只能靠人盯**的前端事故：
1. **模板硬编码业务色** → 自定义强调色与暗色主题不生效（"除设置色板与黑白基础色外，
   统一使用全局语义变量"）；
2. **移动断点覆盖过宽**（768px 内把行/网格子项强制 100%）→ 平板/分屏失去信息密度；
3. **前端 fetch 漏带鉴权头** → 该请求恒定 401（R10 就是这么发现 /api/notes 同步从未成功的）。

这三条的共同点：**功能看起来正常**（页面渲染、接口存在），但一到真机/换主题/换密码就坏。
把它们写成可复跑的扫描规则，比写进文档更靠得住 —— 文档不会在 CI 里跑。

规则刻意保守（宁可少报）：只在"高置信度"的模式上报警，避免变成"每次都一堆无关告警"。
"""
import argparse
import json
import os
import re
import sys

BACKEND = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(BACKEND, "templates")
STATIC_JS = os.path.join(BACKEND, "static", "js")
STATIC_CSS = os.path.join(BACKEND, "static", "css")

# 允许的"基础色"：黑白打印内容与实验图米白画布可保留固定基础色（Design.md §2）
_ALLOWED_HEX = {
    "#fff", "#ffffff", "#000", "#000000", "#222", "#222222", "#333", "#333333",
    "#444", "#555", "#666", "#999", "#aaa", "#ccc", "#ddd", "#eee",
    "#f5f0e8",   # 实验图米白画布
    "#141518",   # 暗色文字（_onAccentFor 用）
}
# 只在「样式上下文」里判硬编码色：style= / cssText= / .style.x= / 以及 CSS 属性
# （background/color/border/fill/stroke 等）。这样设置色板的 ACCENT_PRESETS
# （形如 {light: 某色}）不会被误报 —— 那些颜色本来就该硬编码。
# [^\n;<>] 里显式排除换行：否则一次 style= 会一路匹配到后面几行的颜色（误报源）
_HEX_IN_STYLE_RE = re.compile(
    r"(?:style|cssText|style\.[A-Za-z]+)\s*=\s*[^\n;<>]*?(#[0-9a-fA-F]{3,8})\b"
    r"|(?:background(?:-color)?|color|border(?:-[a-z]+)?|fill|stroke|outline(?:-color)?)\s*:\s*[^\n;<>]*?(#[0-9a-fA-F]{3,8})\b",
    re.I,
)

# 文件级豁免（与 backend/test_theme_contract.py 同一约定）：
#   base.html / settings.html —— 它们**定义**主题色板（ACCENT_PRESETS），色值本就该硬编码；
#   templates/_rollback/** —— 历史模板副本，不参与运行。
_SCAN_EXEMPT_NAMES = {"base.html", "settings.html"}
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)

_AUTH_EXEMPT = ("/api/health", "/api/log-frontend-error", "/api/log-paper-error")
_FETCH_RE = re.compile(r"fetch\(\s*['\"]([^'\"]+)['\"]", re.I)
_AUTH_HINT_RE = re.compile(r"X-Auth-Token|authorization|_authToken", re.I)


def _iter_files(root, exts):
    for dirpath, _dirnames, filenames in os.walk(root):
        if "_rollback" in dirpath.split(os.sep):
            continue          # 历史副本，不参与运行也不参与巡检
        for fn in filenames:
            if fn.endswith(exts) and fn not in _SCAN_EXEMPT_NAMES:
                yield os.path.join(dirpath, fn)


def check_hardcoded_colors():
    """模板内联 style/script 中的硬编码业务色（应走 CSS 变量）。"""
    issues = []
    for path in _iter_files(TEMPLATES, (".html",)):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        rel = os.path.relpath(path, BACKEND)
        for block in _STYLE_BLOCK_RE.findall(text) + _SCRIPT_BLOCK_RE.findall(text):
            hits = set()
            for match in _HEX_IN_STYLE_RE.finditer(block):
                hits.add(match.group(1) or match.group(2))
            for hexval in sorted(hits):
                if hexval.lower() in _ALLOWED_HEX:
                    continue
                issues.append({
                    "rule": "hardcoded_color",
                    "file": rel,
                    "detail": "样式上下文里出现硬编码颜色 " + hexval +
                              "（换主题/自定义强调色时不生效；应改用 CSS 语义变量）",
                })
    return issues


def check_mobile_breakpoint_overreach():
    """768px 断点内把行/网格子项强制 100%（FreqErr：移动断点覆盖过宽）。"""
    issues = []
    narrowed = re.compile(r"@media[^{]*max-width:\s*768px[^{]*\{([\s\S]{0,2000}?)\n\}", re.I)
    force_full = re.compile(r"(?:\.(?:row|grid|card-grid)[^{]*\{[^}]*width:\s*100%)|(?:flex:\s*0\s+0\s+100%)", re.I)
    files = list(_iter_files(TEMPLATES, (".html",))) + list(_iter_files(STATIC_CSS, (".css",)))
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        rel = os.path.relpath(path, BACKEND)
        for block in narrowed.findall(text):
            if force_full.search(block):
                issues.append({
                    "rule": "breakpoint_overreach",
                    "file": rel,
                    "detail": "768px 断点内把行/网格子项强制 100% 宽（平板与分屏会失去信息密度；"
                              "应在 481-768px 用可换行双列，仅 480px 以下单列）",
                })
    return issues


def check_fetch_without_auth():
    """前端 fetch('/api/...') 未带鉴权头（恒定 401）。"""
    issues = []
    files = list(_iter_files(STATIC_JS, (".js",))) + list(_iter_files(TEMPLATES, (".html",)))
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        rel = os.path.relpath(path, BACKEND)
        for idx, line in enumerate(lines):
            for url in _FETCH_RE.findall(line):
                if not url.startswith("/api/"):
                    continue
                if any(url.startswith(x) for x in _AUTH_EXEMPT):
                    continue
                window = "".join(lines[idx:idx + 3])
                if _AUTH_HINT_RE.search(window):
                    continue
                issues.append({
                    "rule": "fetch_without_auth",
                    "file": rel,
                    "line": idx + 1,
                    "detail": "fetch('" + url + "') 未携带 X-Auth-Token（服务器配置了密码时恒定 401）",
                })
    return issues


def check_unguarded_clipboard():
    """直接调用 navigator.clipboard 而未做安全上下文判断（http://IP 下恒定不可用）。

    这是"本地开发永远复现不了"的典型：localhost 是安全上下文 → Clipboard API 存在，
    所以写的人测着是好的；一部署到 http://<IP> 就变成按钮点了没反应
    （取属性即抛 TypeError，连 .catch 都进不去）。统一入口是 app.js 的 $copyText。
    """
    issues = []
    files = list(_iter_files(STATIC_JS, (".js",))) + list(_iter_files(TEMPLATES, (".html",)))
    for path in files:
        rel = os.path.relpath(path, BACKEND)
        if os.path.basename(path) == "app.js":
            continue          # $copyText 的实现就住在这里，它自带 typeof 守卫
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for idx, line in enumerate(lines):
            if "navigator.clipboard" not in line:
                continue
            stripped = line.strip()
            # 注释里提到 API 名字是正常的（本文件自己就有一堆说明性注释），只查真实调用
            if stripped.startswith(("//", "/*", "*", "#")):
                continue
            if "typeof navigator.clipboard" in line or "window.$copyText" in line:
                continue
            issues.append({
                "rule": "unguarded_clipboard",
                "file": rel,
                "line": idx + 1,
                "detail": "直接使用 navigator.clipboard（http://IP 下该属性不存在，会同步抛错）；"
                          "请改用全局 $copyText",
            })
    return issues


def check_inline_size_container_width():
    """container-type:inline-size 的容器是否显式声明宽度（FreqErr：尺寸隔离塌缩）。"""
    issues = []
    files = list(_iter_files(STATIC_CSS, (".css",))) + list(_iter_files(TEMPLATES, (".html",)))
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        rel = os.path.relpath(path, BACKEND)
        for m in re.finditer(r"([^<>{}]+)\{([^}]*container-type:\s*inline-size[^}]*)\}", text):
            selector, body = m.group(1).strip(), m.group(2)
            if "width" in body:
                continue
            # 块级 flex 容器默认满宽，不算塌缩场景；只有「同时是 flex 项」
            # （flex:1 / flex-grow）且父容器纵向时才会按最小内容宽度塌缩（FreqErr 原案）。
            if not re.search(r"flex(?:-grow)?\s*:\s*(?:1|[1-9])", body):
                continue
            issues.append({
                "rule": "inline_size_no_width",
                "file": rel,
                "detail": "选择器 [" + selector[:60] + "] 用了 container-type:inline-size 但未声明宽度"
                          "（父容器纵向 flex 时会按最小内容宽度塌缩）",
            })
    return issues


RULES = (
    ("硬编码业务色", check_hardcoded_colors),
    ("移动断点覆盖过宽", check_mobile_breakpoint_overreach),
    ("fetch 漏鉴权头", check_fetch_without_auth),
    ("剪贴板未判安全上下文", check_unguarded_clipboard),
    ("inline-size 容器未声明宽度", check_inline_size_container_width),
)


def run_all():
    out = []
    for _name, fn in RULES:
        try:
            out.extend(fn())
        except Exception as exc:      # 扫描器自身出错必须可见，不能静默少报
            out.append({"rule": "scanner_error", "file": "-",
                        "detail": fn.__name__ + ": " + str(exc)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    issues = run_all()
    if args.json:
        print(json.dumps(issues, ensure_ascii=False, indent=2))
    else:
        if not issues:
            print("前端静态巡检：无违规")
        for it in issues:
            loc = it.get("file", "-") + (":" + str(it["line"]) if it.get("line") else "")
            print("[" + it["rule"] + "] " + loc)
            print("    " + it["detail"])
        print("")
        print("共 " + str(len(issues)) + " 条")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
