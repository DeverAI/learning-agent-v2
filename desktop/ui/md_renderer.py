"""轻量 Markdown → HTML 渲染器。

特性：
- 标题 # ~ ######
- 粗体/斜体/行内代码/删除线
- 代码块 (带语言标注)
- 无序/有序列表
- 引用块
- 分割线
- 链接/图片
- 表格（简化）
- Mermaid 代码块保留（供外部渲染）
- 图论图代码块 `graph`（自动渲染为 SVG）
- 数学公式：行内 $...$ / 块级 $$...$$，LaTeX → Unicode 渲染
- 返回 (html, has_mermaid)
"""
import re
from typing import Tuple, Optional, Dict


# 延迟导入 graph_renderer，避免循环依赖
def _get_graph_renderer():
    from ui import graph_renderer
    return graph_renderer


# 常见 LaTeX → Unicode 映射表（缺失项保留原命令，不替换为空）
_LATEX_MAP = {
    # 希腊字母
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\omicron": "ο", r"\pi": "π", r"\varpi": "ϖ", r"\rho": "ρ",
    r"\varrho": "ϱ", r"\sigma": "σ", r"\varsigma": "ς", r"\tau": "τ",
    r"\upsilon": "υ", r"\phi": "φ", r"\varphi": "φ", r"\chi": "χ",
    r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Upsilon": "Υ",
    r"\Phi": "Φ", r"\Psi": "Ψ", r"\Omega": "Ω",
    # 运算符
    r"\times": "×", r"\div": "÷", r"\pm": "±", r"\mp": "∓",
    r"\cdot": "·", r"\cdotp": "·", r"\bullet": "•", r"\circ": "∘",
    r"\oplus": "⊕", r"\otimes": "⊗", r"\odot": "⊙", r"\star": "★",
    r"\ast": "∗", r"\propto": "∝", r"\infty": "∞",
    # 关系符
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\sim": "∼",
    r"\cong": "≅", r"\equiv": "≡", r"\prec": "≺", r"\succ": "≻",
    r"\subset": "⊂", r"\supset": "⊃", r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\in": "∈", r"\notin": "∉", r"\ni": "∋", r"\cup": "∪",
    r"\cap": "∩", r"\emptyset": "∅", r"\varnothing": "∅",
    # 几何/箭头
    r"\triangle": "△", r"\angle": "∠", r"\perp": "⊥", r"\parallel": "∥",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐", r"\to": "→", r"\gets": "←", r"\implies": "⇒",
    r"\iff": "⇔", r"\leftrightarrow": "↔", r"\Leftrightarrow": "⇔",
    r"\uparrow": "↑", r"\downarrow": "↓", r"\mapsto": "↦",
    # 微积分/代数
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\partial": "∂",
    r"\nabla": "∇", r"\sqrt": "√", r"\forall": "∀", r"\exists": "∃",
    r"\neg": "¬", r"\land": "∧", r"\lor": "∨", r"\setminus": "∖",
    # 杂项
    r"\ldots": "…", r"\cdots": "⋯", r"\vdots": "⋮", r"\ddots": "⋱",
    r"\degree": "°", r"\prime": "′", r"\prime\prime": "″",
    r"\%": "%", r"\#": "#", r"\_": "_", r"\&": "&",
}

# 预排序，避免每次调用都重新排序
_LATEX_MAP_SORTED = sorted(_LATEX_MAP.items(), key=lambda x: -len(x[0]))


# 块级公式安全行数上限，防止未闭合 $$ 吞掉整个文档
MAX_MATH_BLOCK_LINES = 50


def _latex_to_unicode(text: str) -> str:
    """将 LaTeX 表达式转换为 Unicode 字符，便于 QTextBrowser 直接显示。

    未映射命令保留原样，避免信息丢失。输入过长或异常时降级返回原文本。
    """
    try:
        if not isinstance(text, str):
            text = str(text)
        # 限制长度，并在最近的安全位置截断
        if len(text) > 500:
            cut = text.rfind(" ", 0, 500)
            if cut < 200:
                cut = 500
            text = text[:cut] + " …"

        # 按长度降序替换（长的先替换，避免 \Rightarrow 被 \Right 截断）
        for latex, uni in _LATEX_MAP_SORTED:
            text = text.replace(latex, uni)
        # 清理单目符号后的多余空格，使 "∠ BAD" 变成 "∠BAD"
        text = re.sub(r"(∠|△|√)\s+", r"\1", text)
        # 清理单目符号后紧跟的 { }，如 √{2} -> √2；支持一层嵌套花括号
        text = re.sub(r"(∠|△|√)\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", r"\1\2", text)
        # 角度上标规范化：90^{∘} / 90^∘ / 90^{o} / 90^o -> 90°
        text = text.replace("^{∘}", "°").replace("^∘", "°").replace("^{o}", "°").replace("^o", "°")
        # 注：之前尝试去掉“纯装饰性”的 {123}，但会误伤 \frac{1}{2}、\binom{n}{k} 等结构，
        # 因此保留花括号，优先保证公式结构不被破坏。
        return text
    except Exception:
        return text if isinstance(text, str) else str(text)


def _escape_html(text: str) -> str:
    """HTML 转义（含双引号）。"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _unescape_html(text: str) -> str:
    """恢复由 _escape_html 转义的字符，用于提取代码/公式等需保留原始内容的区域。"""
    return (text
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"'))


def _render_inline_math(match: re.Match) -> str:
    """行内公式 $...$ 渲染回调。"""
    formula = _unescape_html(match.group(1))
    rendered = _latex_to_unicode(formula)
    return f'<span class="math">{_escape_html(rendered)}</span>'


def _render_link(match: re.Match) -> str:
    """链接 [text](url) 渲染回调，过滤危险 scheme。"""
    link_text = match.group(1)
    url = _unescape_html(match.group(2)).strip()
    # 过滤危险伪协议
    if re.match(r"^(javascript|data|vbscript|file|blob|about):", url, re.IGNORECASE):
        url = "#"
    # _escape_html 已负责转义双引号，此处不再重复替换，避免产生 &amp;quot;
    return f'<a href="{_escape_html(url)}">{link_text}</a>'


def _render_image(match: re.Match) -> str:
    """图片 ![alt](url) 渲染回调：不加载远程图片（安全），渲染为可点击的图片链接占位。

    保留 alt 与 URL 信息，用户可点击在外部浏览器打开；危险伪协议与链接规则一致过滤。
    注意：alt 已由 _flush_inline 整体转义，此处不可重复转义（与 _render_link 一致）。
    """
    alt = match.group(1)
    url = _unescape_html(match.group(2)).strip()
    # 过滤危险伪协议
    if re.match(r"^(javascript|data|vbscript|file|blob|about):", url, re.IGNORECASE):
        url = "#"
    label = alt or "图片"
    return f'<a class="md-img" href="{_escape_html(url)}">[图片: {label}]</a>'


def render(text: str, theme: Optional[Dict] = None) -> Tuple[str, bool, bool]:
    """渲染 Markdown 文本为 HTML。

    返回 (html, has_mermaid, has_graph):
    - has_mermaid 表示文本含 mermaid 代码块
    - has_graph 表示文本含 graph 代码块并被成功渲染为 SVG

    参数：
    - text：要渲染的 Markdown 文本
    - theme：可选主题字典，用于 graph 代码块的 SVG 着色
    """
    if not isinstance(text, str):
        text = str(text)
    lines = text.split("\n")
    result = []
    i = 0
    n = len(lines)
    has_mermaid = False
    has_graph = False

    # 状态
    in_code = False
    code_lang = ""
    code_lines = []
    in_list = None  # "ul" or "ol"
    list_items = []
    in_table = False
    table_lines = []
    in_blockquote = False
    bq_lines = []
    in_math_block = False
    math_lines = []

    def _flush_inline(text: str) -> str:
        """行内格式处理。

        先整体转义 HTML，再对代码/公式等需要保留原始内容的区域做反转义。
        代码与公式会先替换为占位符，避免被后续粗体/斜体/删除线正则破坏。
        """
        text = _escape_html(text)

        # 占位符保护：先识别需保留原始内容的行内元素
        placeholders = {}
        ph_counter = 0

        def _stash(fragment: str) -> str:
            nonlocal ph_counter
            key = f"\x00{ph_counter}\x00"
            ph_counter += 1
            placeholders[key] = fragment
            return key

        # 行内代码 `` ` ``：恢复原始内容后包进 <code>
        def _code_repl(m: re.Match) -> str:
            raw = _unescape_html(m.group(1))
            return _stash(f"<code>{_escape_html(raw)}</code>")

        text = re.sub(r"`([^`]+)`", _code_repl, text)

        # 行内公式 $...$（代码块内已由外层逻辑保护；行内代码已优先处理）
        # 要求 $ 前后不能紧贴数字，避免误伤价格写法如 $100
        # 注意：连续公式如 $a$b$ 当前不支持，会被拆分为一个公式和普通文本
        text = re.sub(r"(?<!\d)\$([^$\n]+?)\$(?!\d)",
                      lambda m: _stash(_render_inline_math(m)), text)

        # 粗体+斜体 ***
        text = re.sub(r"\*\*\*(.+?)\*\*\*", lambda m: f"<b><i>{m.group(1)}</i></b>", text)
        # 粗体 **
        text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"<b>{m.group(1)}</b>", text)
        # 斜体 *
        text = re.sub(r"\*(.+?)\*", lambda m: f"<i>{m.group(1)}</i>", text)
        # 删除线 ~~
        text = re.sub(r"~~(.+?)~~", lambda m: f"<del>{m.group(1)}</del>", text)
        # 图片 ![alt](url) → 占位（必须在链接之前，否则链接正则会先吞掉 [alt](url)，
        # 只剩字面 "!" 前缀，导致非空 alt 的图片永远走不到图片分支）
        text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _render_image, text)
        # 链接 [text](url)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _render_link, text)

        # 恢复占位符
        for key, val in placeholders.items():
            text = text.replace(key, val)
        return text

    def _close_blockquote():
        nonlocal bq_lines
        if bq_lines:
            body = "<br>".join(_flush_inline(l) for l in bq_lines)
            result.append(f"<blockquote>{body}</blockquote>")
            bq_lines.clear()

    def _close_list():
        nonlocal in_list, list_items
        if in_list and list_items:
            tag = in_list
            items_html = "".join(f"<li>{_flush_inline(li)}</li>" for li in list_items)
            result.append(f"<{tag}>{items_html}</{tag}>")
            list_items.clear()
            in_list = None

    def _close_table():
        nonlocal in_table, table_lines
        if in_table and len(table_lines) >= 2:
            rows = []
            for idx, tl in enumerate(table_lines):
                cells = [c.strip() for c in tl.split("|") if c.strip()]
                if idx == 0:
                    rows.append("<tr>" + "".join(f"<th>{_flush_inline(c)}</th>" for c in cells) + "</tr>")
                elif idx == 1 and all(re.match(r"^[-:]+$", c) for c in cells):
                    continue  # 分隔行跳过
                else:
                    rows.append("<tr>" + "".join(f"<td>{_flush_inline(c)}</td>" for c in cells) + "</tr>")
            result.append(f"<table>{''.join(rows)}</table>")
        table_lines.clear()
        in_table = False

    def _close_code():
        nonlocal code_lines, code_lang
        if code_lines:
            lang_tag = f' class="lang-{code_lang}"' if code_lang else ""
            code_body = "\n".join(code_lines)
            # HTML 转义
            code_body = code_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            result.append(f"<pre{lang_tag}><code>{code_body}</code></pre>")
            code_lines.clear()
            code_lang = ""

    def _close_math_block():
        nonlocal math_lines
        if math_lines:
            formula = "\n".join(math_lines)
            rendered = _latex_to_unicode(formula)
            escaped = _escape_html(rendered)
            result.append(f'<div class="math-block">{escaped}</div>')
            math_lines.clear()

    while i < n:
        line = lines[i]

        # 代码块开始/结束
        code_match = re.match(r"^```(\w*)$", line)
        if code_match:
            if not in_code:
                _close_blockquote()
                _close_list()
                _close_table()
                _close_math_block()
                in_code = True
                code_lang = code_match.group(1) or ""
                code_lines.clear()
            else:
                if code_lang.lower() == "mermaid":
                    has_mermaid = True
                    _escaped_mermaid = _escape_html("\n".join(code_lines))
                    result.append(f'<pre class="mermaid-source">{_escaped_mermaid}</pre>')
                elif code_lang.lower() == "graph":
                    # 图论示意图代码块：解析后渲染成 SVG，失败回退到原文
                    gr = _get_graph_renderer()
                    if gr is not None:
                        body = "\n".join(code_lines)
                        # 同一 render 调用内多个 graph 块用唯一 marker id
                        mid = f"garr{i}"
                        try:
                            svg = gr.render_graph_svg(body, theme=theme, marker_id=mid)
                        except Exception:
                            svg = None
                        # r34 修复：增加 SVG 输出合法性校验，避免空/异常字符串破坏 HTML
                        if svg and isinstance(svg, str) and svg.strip().startswith("<svg"):
                            result.append(f'<div class="graph-container">{svg}</div>')
                            has_graph = True
                        else:
                            result.append(
                                f'<pre class="graph-fallback"><code>'
                                f'{_escape_html(body)}'
                                f'</code></pre>'
                            )
                    else:
                        _escaped_code = _escape_html("\n".join(code_lines))
                        result.append(
                            f'<pre class="graph-fallback"><code>'
                            f'{_escaped_code}'
                            f'</code></pre>'
                        )
                else:
                    _close_code()
                # 任何已处理的代码块都需清空状态，避免末尾 _close_code 重复输出
                code_lines.clear()
                code_lang = ""
                in_code = False
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # 单行块级公式 $$...$$（每行仅支持一个；若一行含多个则放弃解析，避免误合并）
        single_math_match = re.match(r"^\$\$((?:(?!\$\$).)+?)\$\$\s*$", line)
        if single_math_match:
            _close_blockquote()
            _close_list()
            _close_table()
            formula = single_math_match.group(1).strip()
            rendered = _latex_to_unicode(formula)
            escaped = _escape_html(rendered)
            result.append(f'<div class="math-block">{escaped}</div>')
            i += 1
            continue

        # 块级公式 $$ ... $$
        if re.match(r"^\$\$\s*$", line):
            if not in_math_block:
                _close_blockquote()
                _close_list()
                _close_table()
                in_math_block = True
                math_lines.clear()
            else:
                _close_math_block()
                in_math_block = False
            i += 1
            continue

        if in_math_block:
            math_lines.append(line)
            # 未闭合保护：超过上限时强制退出公式块，作为纯文本输出，避免二次解释 Markdown
            if len(math_lines) > MAX_MATH_BLOCK_LINES:
                body = "<br>".join(_escape_html(l) for l in math_lines)
                result.append(f"<p>{body}</p>")
                math_lines.clear()
                in_math_block = False
            i += 1
            continue

        # 表格：至少 2 个 |（≥3 列）才判定为表格
        if line.count("|") >= 2:
            _close_blockquote()
            _close_list()
            if not in_table:
                in_table = True
            table_lines.append(line)
            i += 1
            continue
        elif in_table:
            _close_table()

        # 空行
        if not line.strip():
            _close_blockquote()
            _close_list()
            _close_table()
            i += 1
            continue

        # 引用
        if line.startswith("> "):
            _close_list()
            _close_table()
            if not in_blockquote:
                in_blockquote = True
            bq_lines.append(line[2:])
            i += 1
            continue
        elif in_blockquote:
            _close_blockquote()

        # 分割线
        if re.match(r"^[-*_]{3,}$", line):
            _close_blockquote()
            _close_list()
            _close_table()
            result.append("<hr>")
            i += 1
            continue

        # 标题
        h_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if h_match:
            _close_blockquote()
            _close_list()
            _close_table()
            level = len(h_match.group(1))
            result.append(f"<h{level}>{_flush_inline(h_match.group(2))}</h{level}>")
            i += 1
            continue

        # 无序列表
        ul_match = re.match(r"^[\-\*\+]\s+(.+)$", line)
        if ul_match:
            _close_blockquote()
            _close_table()
            if in_list != "ul":
                _close_list()
                in_list = "ul"
            list_items.append(ul_match.group(1))
            i += 1
            continue

        # 有序列表
        ol_match = re.match(r"^\d+\.\s+(.+)$", line)
        if ol_match:
            _close_blockquote()
            _close_table()
            if in_list != "ol":
                _close_list()
                in_list = "ol"
            list_items.append(ol_match.group(1))
            i += 1
            continue

        # 普通段落
        _close_blockquote()
        _close_list()
        _close_table()
        result.append(f"<p>{_flush_inline(line)}</p>")
        i += 1

    # 收尾
    _close_blockquote()
    _close_list()
    _close_table()
    _close_code()
    _close_math_block()

    html = "".join(result)
    return html, has_mermaid, has_graph


# ---------- 基础 CSS ----------
BASE_CSS = """
<style>
  body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 14px;
         line-height: 1.6; padding: 0; margin: 0; }
  h1 { font-size: 1.5em; border-bottom: 2px solid currentColor; padding-bottom: 4px; margin: 12px 0 6px; opacity: 0.9; }
  h2 { font-size: 1.3em; margin: 10px 0 5px; opacity: 0.88; }
  h3 { font-size: 1.15em; margin: 8px 0 4px; opacity: 0.85; }
  h4, h5, h6 { font-size: 1.05em; margin: 6px 0 3px; opacity: 0.82; }
  p { margin: 4px 0; }
  code { background: rgba(128,128,128,0.18); padding: 1px 5px; border-radius: 3px;
          font-family: "Cascadia Code", "Consolas", monospace; font-size: 0.92em; }
  pre { background: rgba(0,0,0,0.25); padding: 10px 14px; border-radius: 6px;
         overflow-x: auto; font-family: "Cascadia Code", "Consolas", monospace;
         font-size: 0.88em; line-height: 1.5; margin: 6px 0; white-space: pre-wrap; }
  pre code { background: transparent; padding: 0; }
  blockquote { border-left: 3px solid rgba(128,128,128,0.5); padding: 4px 12px;
                margin: 6px 0; opacity: 0.85; }
  ul, ol { padding-left: 22px; margin: 4px 0; }
  li { margin: 2px 0; }
  a { color: inherit; opacity: 0.85; }
  hr { border: none; border-top: 1px solid rgba(128,128,128,0.3); margin: 10px 0; }
  table { border-collapse: collapse; width: 100%; margin: 6px 0; }
  th, td { border: 1px solid rgba(128,128,128,0.3); padding: 4px 8px; text-align: left; }
  th { background: rgba(128,128,128,0.15); }
  .md-img { display: inline-block; padding: 2px 8px; background: rgba(128,128,128,0.2);
              border-radius: 3px; font-style: italic; font-size: 0.85em; }
  .mermaid-source { background: rgba(96,165,250,0.12); border-left: 3px solid #60a5fa;
                      padding: 10px 14px; font-family: "Cascadia Code", "Consolas", monospace;
                      font-size: 0.85em; white-space: pre-wrap; }
  .math { font-family: "Cambria Math", "Times New Roman", "Microsoft YaHei", serif;
          font-size: 1.05em; padding: 0 2px; background: rgba(128,128,128,0.12);
          border-radius: 3px; }
  .math-block { font-family: "Cambria Math", "Times New Roman", "Microsoft YaHei", serif;
                font-size: 1.15em; padding: 10px 14px; margin: 6px 0;
                background: rgba(128,128,128,0.12); border-radius: 6px;
                text-align: center; white-space: pre-wrap; }
</style>
"""


def render_full(text: str) -> str:
    """完整渲染：Markdown → 带 CSS 的 HTML 文档。"""
    body, has_mermaid, has_graph = render(text)
    return f"<html><head>{BASE_CSS}</head><body>{body}</body></html>"
