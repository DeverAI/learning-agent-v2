"""测试数学公式 HTML 转义：确保 < > 在 $...$ / $$...$$ 内被正确转义，
且 <script>/<style> 块不受影响。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schemas.schemas import escape_math_html


def test_inline_math_lt_gt():
    raw = "当 $a<0$ 时，函数单调递减；$a>3$ 时单调递增。"
    out = escape_math_html(raw)
    assert "$a&lt;0$" in out
    assert "$a&gt;3$" in out
    # 普通文本中的标点 unaffected
    assert "当" in out


def test_display_math_lt_gt():
    raw = "$$f(x) = x^2 - 2ax + a^2 - 1 < 0$$"
    out = escape_math_html(raw)
    assert "$$f(x) = x^2 - 2ax + a^2 - 1 &lt; 0$$" == out


def test_html_tags_untouched():
    raw = '<div class="answer">$x<y$</div><p>$a>b$</p>'
    out = escape_math_html(raw)
    assert '<div class="answer">' in out
    assert "</div>" in out
    assert "$x&lt;y$" in out
    assert "$a&gt;b$" in out


def test_script_block_preserved():
    raw = '<script>var x = "$a<0$"; if(x){console.log("<div>")}</script><p>$b<1$</p>'
    out = escape_math_html(raw)
    # script 内容原样保留
    assert 'var x = "$a<0$"' in out
    assert 'console.log("<div>")' in out
    # script 外的公式正常转义
    assert "$b&lt;1$" in out


def test_idempotent():
    raw = "$x<0$"
    once = escape_math_html(raw)
    twice = escape_math_html(once)
    assert once == "$x&lt;0$"
    assert once == twice


def test_already_escaped_entities():
    raw = "$x &lt; 0$"
    out = escape_math_html(raw)
    # 幂等：最终仍是统一转义后的结果
    assert "$x &lt; 0$" == out


def test_no_math_no_change():
    raw = "<p>普通 HTML 段落，没有公式</p>"
    out = escape_math_html(raw)
    assert out == raw


def test_escaped_dollar_inside_math():
    # \$ 在 LaTeX 中表示字面量 $，不应被当作公式边界
    raw = r"$a\$b$"
    out = escape_math_html(raw)
    assert "$a\\$b$" == out


if __name__ == "__main__":
    test_inline_math_lt_gt()
    test_display_math_lt_gt()
    test_html_tags_untouched()
    test_script_block_preserved()
    test_idempotent()
    test_already_escaped_entities()
    test_no_math_no_change()
    test_escaped_dollar_inside_math()
    print("math escape tests passed")
