import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.diagram_service import diagram_service, _is_valid_question_id


def test_qid_validation():
    assert _is_valid_question_id("abc123") is True
    assert _is_valid_question_id("../x") is False
    assert _is_valid_question_id("") is False
    assert _is_valid_question_id("a" * 65) is False


def test_function_graph_sanitization():
    spec = {
        "function_expr": "abs(x**2 - 1)",
        "x_range": [-3, 3],
        "geometries": [
            {"type": "point", "x": 0, "y": 0, "color": 'red" onload="alert(1)', "label": "A"},
            {"type": "segment", "x1": 0, "y1": 0, "x2": 1, "y2": 1,
             "style": 'dashed" onclick="x', "color": "green"},
            {"type": "circle", "x": 0, "y": 0, "r": 1, "color": "blue"},
            {"type": "text", "x": 1, "y": 1, "text": "test", "font_size": 200},
            {"type": "polygon", "points": [[0, 0], [1, 0], [0.5, 1]], "color": "purple"},
        ],
    }
    svg = diagram_service._render_function_graph_spec(spec)
    assert "onload=" not in svg, svg
    assert "onclick=" not in svg, svg
    # color injection is sanitized back to default
    assert 'fill="#c62828"' in svg, svg


def test_function_graph_ast_sandbox():
    # 危险调用与属性访问应被拦截
    for expr in [
        "__import__('os').system('x')",
        "eval('1+1')",
        "(lambda: 1)()",
        "[x for x in (1,)]",
        "math.__dict__['sin'](0)",
        "factorial(x)",
        "math.factorial(5)",
        ]:
            spec = {"function_expr": expr, "x_range": [-1, 1]}
            svg = diagram_service._render_function_graph_spec(spec)
            assert svg == "", f"expr={expr} should be rejected without a fake diagram"


def test_function_graph_nonfinite_range():
    spec = {
        "function_expr": "x**2",
        "x_range": [float("inf"), float("nan")],
    }
    svg = diagram_service._render_function_graph_spec(spec)
    assert "nan" not in svg.lower(), svg


def test_svg_sanitizer():
    bad = (
        '<svg><rect onclick="alert(1)" x="1"onclick="x" '
        'href="javascript:alert(1)" xlink:href=data:text/html,<script>alert(1)</script>/>'
        '<a href="data:text/html,xxx">x</a></svg>'
    )
    clean = diagram_service._sanitize_svg(bad)
    assert "onclick" not in clean, clean
    assert "javascript" not in clean, clean
    assert "data:" not in clean, clean


def test_svg_sanitizer_html_entities():
    bad = '<svg><rect onload&#61;="alert(1)" href="javascript&#58;alert(1)"/></svg>'
    clean = diagram_service._sanitize_svg(bad)
    assert "onload" not in clean, clean
    assert "javascript" not in clean, clean


def test_svg_sanitizer_style_filter_feimage():
    bad = (
        '<svg><style>@import url(x)</style>'
        '<filter id="f"><feImage xlink:href="data:image/svg+xml,<script>alert(1)</script>"/></filter>'
        '<image href="data:image/png,xxx"/></svg>'
    )
    clean = diagram_service._sanitize_svg(bad)
    assert "<style" not in clean, clean
    assert "<filter" not in clean, clean
    assert "<feImage" not in clean, clean
    assert "<image" not in clean, clean


def test_comparison_regions_sanitization():
    from services.structure_graph_service import _sanitize_comparison_regions
    raw = {
        "regions": [
            {
                "id": 'r1" onload="alert(1)',
                "node_ids": [1, 2, 2, "3"],
                "paragraph_range": [0, 5],
                "color": 'red" onclick="x',
                "color_dark": 'blue" onerror="y',
                "purpose": '<script>alert(1)</script>',
                "extra": "should be removed",
            }
        ]
    }
    result = _sanitize_comparison_regions(raw)
    region = result["regions"][0]
    assert '"' not in region["id"], region
    assert "<script>" not in region["purpose"], region
    # 注入的 color 被回退为安全默认值（HEX 或安全色），不再包含引号/事件处理器
    assert '"' not in region["color"], region
    assert "onload" not in region["color"], region
    assert region["color"].startswith("#") or region["color"] in (
        "red", "green", "blue", "yellow", "orange", "purple", "cyan", "magenta",
        "lime", "pink", "teal", "lavender", "brown", "beige", "maroon", "mint",
        "olive", "coral", "navy", "grey", "gray", "black", "white", "gold", "silver",
    )
    assert "extra" not in region, region


def test_svg_sanitizer_inline_style():
    """内联 style 里的**危险构造**必须剔除，但**安全的笔画属性要保留**。

    ⚠️ 这条断言的旧版本是 `assert "style" not in clean` —— 它把"把 style 整条删掉"
    当成了正确行为，**等于用一个测试钉死了另一个 bug**：
    坐标推理 / 折线示意这两条路径产出的线段与圆，笔画**只写在 style 里**
    （`style="stroke:#333;stroke-width:2;fill:none"`），整条删掉后落盘变成
    `<line ... />`，等价于 `stroke:none` —— **线完全不显示**，
    学生看到白底上一堆字母数字，而接口/DB/前端全报成功（2026-09-12 实测确认）。

    现在按**意图**断言：危险的去掉、安全的留下。
    """
    bad = (
        '<svg>'
        '<rect style="background-image:url(javascript:alert(1));fill:red" fill="red"/>'
        '<line x1="0" y1="0" x2="1" y2="1" style="stroke:#333;stroke-width:2;fill:none"/>'
        '</svg>'
    )
    clean = diagram_service._sanitize_svg(bad)
    assert "javascript" not in clean, clean
    assert "background-image" not in clean, clean
    # 安全属性必须留下，否则图上的线会消失
    assert "stroke:#333" in clean, clean
    assert "fill:red" in clean, clean


def test_svg_sanitizer_use_bypass():
    bad = '<svg><use href=foo /><use href="x"/></svg>'
    clean = diagram_service._sanitize_svg(bad)
    assert "<use" not in clean, clean
    assert "</use>" not in clean.lower(), clean


def test_svg_sanitizer_smil():
    bad = (
        '<svg><circle r="1"><animateMotion path="M0,0"/><set attributeName="fill" to="red"/>'
        '<animateColor attributeName="fill"/><mpath href="x"/></circle>'
        '<discard begin="0s"/></svg>'
    )
    clean = diagram_service._sanitize_svg(bad)
    for tag in ("animateMotion", "animateColor", "set", "mpath", "discard"):
        assert tag not in clean, clean


def test_coord_engine_no_code_exec():
    from services.coord_engine import eval_expression
    # 恶意属性访问表达式不应被执行，应安全返回 0.0
    assert eval_expression("().__class__.__bases__[0].__subclasses__()") == 0.0
    assert eval_expression("__import__('os').system('id')") == 0.0
    # 正常数学表达式仍应正确计算
    assert eval_expression("2*k + 3", k=10) == 23.0
    assert abs(eval_expression("sin(30)") - 0.5) < 1e-9


def test_svg_sanitizer_script_self_closing():
    bad = '<svg><script href="evil.js"/><script xlink:href="evil.js"/></svg>'
    clean = diagram_service._sanitize_svg(bad)
    assert "<script" not in clean, clean


def test_svg_sanitizer_image_no_attrs():
    bad = '<svg><image/></svg>'
    clean = diagram_service._sanitize_svg(bad)
    assert "<image" not in clean, clean


def test_cors_origin_validation():
    from config import CORS_ORIGIN_RE
    assert CORS_ORIGIN_RE.match("http://localhost:8000")
    assert CORS_ORIGIN_RE.match("http://127.0.0.1")
    assert CORS_ORIGIN_RE.match("https://example.com:443")
    assert not CORS_ORIGIN_RE.match("*")
    assert not CORS_ORIGIN_RE.match("http://evil.com:80@other.com")
    assert not CORS_ORIGIN_RE.match("http://example.com/path")


if __name__ == "__main__":
    test_qid_validation()
    test_function_graph_sanitization()
    test_function_graph_ast_sandbox()
    test_function_graph_nonfinite_range()
    test_svg_sanitizer()
    test_svg_sanitizer_html_entities()
    test_svg_sanitizer_style_filter_feimage()
    test_svg_sanitizer_inline_style()
    test_svg_sanitizer_use_bypass()
    test_svg_sanitizer_smil()
    test_svg_sanitizer_script_self_closing()
    test_svg_sanitizer_image_no_attrs()
    test_comparison_regions_sanitization()
    test_coord_engine_no_code_exec()
    test_cors_origin_validation()
    print("security fix checks passed")
