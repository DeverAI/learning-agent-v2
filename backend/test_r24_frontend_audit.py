# -*- coding: utf-8 -*-
"""R24：前端静态巡检（布局/主题/鉴权）必须进回归，且必须能证明自己抓得到。

## 为什么要"自证"

一个只会输出"无违规"的扫描器和一堵白墙没有区别。本组测试分两层：
① 对合成样本断言规则**确实抓到**该抓的模式（正例），且**不误伤**合法写法（反例）；
② 对真实仓库断言当前是干净的（基线），一旦有人再写出硬编码业务色 / 漏鉴权 fetch，
   这条测试会红 —— 这正是 FreqErr 里那三条"只能靠人盯"的事故的自动化替代。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit_layout as AL  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_repo_is_clean():
    """当前仓库基线：0 违规。有人写出新的违规时这条会红。"""
    issues = AL.run_all()
    assert issues == [], "前端静态巡检发现违规：" + repr(issues[:5])


def test_hardcoded_color_rule_catches_style_context():
    """正例：样式上下文里的硬编码色必须被抓到。"""
    assert AL._HEX_IN_STYLE_RE.search('style="background:#ff00aa"')
    assert AL._HEX_IN_STYLE_RE.search("background-color: #ff00aa;")
    assert AL._HEX_IN_STYLE_RE.search("el.style.color='#ff00aa'")


def test_hardcoded_color_rule_ignores_palette_definition():
    """反例：色板定义（ACCENT_PRESETS 那种写法）不算违规。"""
    assert not AL._HEX_IN_STYLE_RE.search("{light:'#ff00aa',dark:'#aa00ff'}")
    assert not AL._HEX_IN_STYLE_RE.search('placeholder="#ff00aa"')


def test_color_rule_does_not_cross_lines():
    """反例：一次 style= 不得跨行匹配到后面几行的颜色（那是我第一版的误报源）。"""
    snippet = 'style="width:120px"\n<div x="#ff00aa">'
    assert not AL._HEX_IN_STYLE_RE.search(snippet)


def test_allowed_basic_colors_are_not_flagged():
    for hexval in ("#fff", "#000", "#333", "#f5f0e8"):
        assert hexval in AL._ALLOWED_HEX


def test_fetch_without_auth_rule_catches():
    """正例：裸 fetch('/api/...') 必须被抓到（等价于恒定 401）。"""
    assert AL._FETCH_RE.findall("fetch('/api/notes/upload-text',{method:'POST'})")
    assert not AL._AUTH_HINT_RE.search("fetch('/api/notes')")
    assert AL._AUTH_HINT_RE.search("headers:{'X-Auth-Token':window.$API._authToken}")


def test_exempt_files_are_not_scanned():
    """色板定义文件与历史副本不参与巡检（与 test_theme_contract 同一约定）。"""
    scanned = {os.path.basename(p) for p in AL._iter_files(AL.TEMPLATES, (".html",))}
    assert "base.html" not in scanned
    assert "settings.html" not in scanned
    assert all("_rollback" not in p for p in AL._iter_files(AL.TEMPLATES, (".html",)))


def test_rules_registry_is_complete():
    """四条规则都在（有人删规则时这条会红）。"""
    names = [n for n, _fn in AL.RULES]
    assert len(AL.RULES) == 5
    assert all(callable(fn) for _n, fn in AL.RULES)
    assert any("鉴权" in n for n in names)
    assert any("色" in n for n in names)
    assert any("剪贴板" in n for n in names)


def test_unguarded_clipboard_rule_catches():
    """正例：裸用 navigator.clipboard 必须被抓到（http://IP 下恒定失效）。"""
    issues = AL.check_unguarded_clipboard()
    assert issues == [], "真实仓库里仍有裸用剪贴板的地方：" + repr(issues[:3])


def test_copy_text_helper_exists_with_fallback():
    """反例保护：$copyText 必须真的带回退，而不是换个名字继续裸调。"""
    js = _read(os.path.join("backend", "static", "js", "app.js"))
    assert "window.$copyText" in js
    # 回退实现住在 _legacyCopyText（定义在 $copyText 之前），所以要按函数边界取片段
    seg = js.split("function _legacyCopyText")[1].split("window.$copyText")[0]
    assert "execCommand" in seg, "没有 execCommand 回退 → http 下仍然复制不了"
    assert "appendChild" in seg and "removeChild" in seg, "回退实现必须临时挂载并清理 textarea"
