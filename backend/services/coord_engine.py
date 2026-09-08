import html
import math
import re
from logger import get_logger

logger = get_logger()


def eval_expression(expr: str, k: float = 50.0) -> float:
    """
    计算坐标表达式。支持：
    - 四则运算 + - * /  ** 幂
    - 括号 ()
    - 比例常数 k
    - 三角函数（度数制）: sin(deg) cos(deg) tan(deg)
    - 反三角（返回度数）: asin(x) acos(x) atan(x) atan2(y,x)
    - sqrt(x), abs(x), log(x), log10(x), exp(x)
    - pow(x,y), hypot(x,y)
    - ceil(x), floor(x)
    - 常量: pi
    """
    if not expr or not isinstance(expr, str):
        return 0.0

    expr = expr.strip()
    safe = expr.replace("k", f"({k})")
    safe = safe.replace("pi", f"({math.pi})")

    def _rad_fn(name):
        def wrapper(arg_str):
            arg_val = _eval_simple(arg_str, k)
            return str(getattr(math, name)(math.radians(arg_val)))
        return wrapper

    def _plain_fn(name):
        def wrapper(arg_str):
            arg_val = _eval_simple(arg_str, k)
            return str(getattr(math, name)(arg_val))
        return wrapper

    def _inv_fn(name):
        def wrapper(arg_str):
            arg_val = _eval_simple(arg_str, k)
            return str(math.degrees(getattr(math, name)(arg_val)))
        return wrapper

    def _two_fn(name):
        def wrapper(arg_str):
            parts = _split_args(arg_str)
            a = _eval_simple(parts[0], k)
            b = _eval_simple(parts[1], k)
            if name == "atan2":
                return str(math.degrees(math.atan2(a, b)))
            elif name == "log":
                return str(math.log(a, b) if len(parts) > 1 else math.log(a))
            return str(getattr(math, name)(a, b))
        return wrapper

    # 多轮替换直到不再变化：内层函数先求值后，外层函数下一轮才能算出
    for _ in range(8):
        progressed = False
        for fn_name, handler in [
            ("sin", _rad_fn("sin")), ("cos", _rad_fn("cos")), ("tan", _rad_fn("tan")),
            ("sqrt", _plain_fn("sqrt")), ("abs", _plain_fn("fabs")),
            ("asin", _inv_fn("asin")), ("acos", _inv_fn("acos")), ("atan", _inv_fn("atan")),
            ("ceil", _plain_fn("ceil")), ("floor", _plain_fn("floor")),
            ("log10", _plain_fn("log10")),
            ("exp", _plain_fn("exp")),
            ("atan2", _two_fn("atan2")), ("hypot", _two_fn("hypot")), ("pow", _two_fn("pow")),
            ("log", _two_fn("log")),
        ]:
            try:
                replaced = _replace_fn(safe, fn_name, handler)
            except Exception:
                # 该函数参数暂不可计算（嵌套/域错误），留给后续轮次或最终 eval
                continue
            if replaced != safe:
                safe = replaced
                progressed = True
        if not progressed:
            break

    try:
        result = _eval_simple(safe, k)
        return float(result)
    except Exception as e:
        logger.debug("Direct eval failed for '%s': %s", expr[:80], e)
        return 0.0


def _replace_fn(expr: str, fn_name: str, handler) -> str:
    """Replace fn_name(arg) with computed value.

    匹配要求 fn_name 前一个字符不是字母/数字/下划线，
    否则 sin( 会错误命中 asin( 的子串，导致反三角函数永远算不出。
    """
    result = []
    i = 0
    prefix = fn_name + "("
    n = len(prefix)
    while i < len(expr):
        if expr[i:i+n] == prefix and (i == 0 or not (expr[i-1].isalnum() or expr[i-1] == '_')):
            # Find matching closing paren
            depth = 1
            j = i + n
            while j < len(expr) and depth > 0:
                if expr[j] == '(':
                    depth += 1
                elif expr[j] == ')':
                    depth -= 1
                j += 1
            if depth == 0:
                arg = expr[i+n:j-1]
                computed = handler(arg)
                result.append(computed)
                i = j
                continue
        result.append(expr[i])
        i += 1
    return ''.join(result)


def _split_args(expr: str) -> list:
    """Split comma-separated function arguments respecting nested parens."""
    parts = []
    depth = 0
    current = []
    for ch in expr:
        if ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            if ch == '(': depth += 1
            elif ch == ')': depth -= 1
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())
    return parts


def _eval_simple(expr: str, k: float = 50.0) -> float:
    """Simple eval with only numbers and basic ops."""
    # Remove all whitespace
    expr = ''.join(expr.split())
    # Only allow safe characters（允许科学计数法，如 1.22e-16 / 5E+10）
    if not re.match(r'^[\d+\-*/().][\d+\-*/().eE]*(?:[eE][+-]?\d+)?$', expr):
        raise ValueError(f"Unsafe expression: {expr}")
    # Replace standalone - with explicit subtraction
    return float(eval(expr, {"__builtins__": {}}, {}))


def calc_all_points(points: list, k: float = 50.0) -> dict:
    """
    输入: [{"id":"A","x":"0","y":"0"},...]
    输出: {"A": (0.0, 0.0), "B": (150.0, 0.0),...}
    """
    result = {}
    for p in points:
        pid = p.get("id")
        if not isinstance(pid, str) or not pid.strip():
            # 缺少 id 的点无法被线段/圆引用，跳过而不是让整张图崩溃
            continue
        x = eval_expression(str(p.get("x", "0")), k)
        y = eval_expression(str(p.get("y", "0")), k)
        result[pid] = (x, y)
    return result


def auto_scale(points: dict, width: int = 400, height: int = 300, pad: int = 30,
               return_scale: bool = False):
    """Scale points to fit viewport, preserving aspect ratio.
    return_scale=True 时返回 (scaled, scale)，供圆半径等长度量同步缩放。"""
    if not points:
        return ({}, 1.0) if return_scale else {}
    xs = [p[0] for p in points.values()]
    ys = [p[1] for p in points.values()]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    range_x = max_x - min_x or 1
    range_y = max_y - min_y or 1

    scale_x = (width - 2 * pad) / range_x
    scale_y = (height - 2 * pad) / range_y
    scale = min(scale_x, scale_y)

    # Center in viewport
    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2

    # Flip Y for SVG (y increases downward)
    scaled = {}
    for pid, (x, y) in points.items():
        sx = width / 2 + (x - cx) * scale
        sy = height / 2 - (y - cy) * scale  # flip Y
        scaled[pid] = (sx, sy)

    if return_scale:
        return scaled, scale
    return scaled


def to_svg(points: dict, lines: list, width: int = 400, height: int = 300,
           show_points: bool = False, circles: list = None) -> str:
    """Generate SVG from points and line specifications. Clean diagram: no title, no auto labels.
    circles: list of {"center":"O","radius":50,"style":"solid"} or {"center":"O","edge":"B"} (edge point)
    """
    scaled, scale = auto_scale(points, width, height, return_scale=True)

    styles = {
        "solid": "stroke:#333;stroke-width:2;fill:none",
        "dashed": "stroke:#666;stroke-width:2;fill:none;stroke-dasharray:6,3",
        "dotted": "stroke:#999;stroke-width:2;fill:none;stroke-dasharray:2,3",
        "segment": "stroke:#333;stroke-width:2;fill:none",
        "ray": "stroke:#333;stroke-width:2;fill:none",
    }

    svg_lines = []
    svg_lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">')
    svg_lines.append(f'<rect width="{width}" height="{height}" fill="white"/>')

    # Draw lines
    for li in lines:
        a = li.get("from", "")
        b = li.get("to", "")
        style_css = styles.get(li.get("style", "solid"), styles["solid"])
        if a in scaled and b in scaled:
            x1, y1 = scaled[a]
            x2, y2 = scaled[b]
            svg_lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" style="{style_css}"/>')

    # Draw circles
    for ci in (circles or []):
        center_id = ci.get("center", "")
        if center_id not in scaled:
            continue
        cx, cy = scaled[center_id]
        if "edge" in ci:
            edge_id = ci["edge"]
            if edge_id in scaled:
                ex, ey = scaled[edge_id]
                r = math.hypot(ex - cx, ey - cy)
            else:
                continue
        else:
            # 半径与点坐标同一坐标系，必须随 auto_scale 同步缩放，
            # 否则圆和线段/三角形比例不一致
            r = ci.get("radius", 50) * scale
        style_css = styles.get(ci.get("style", "solid"), styles["solid"])
        svg_lines.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" style="{style_css}"/>')

    # Draw point labels only if requested
    if show_points:
        for pid, (x, y) in scaled.items():
            lx, ly = x + 6, y - 6
            safe_pid = html.escape(str(pid), quote=True)
            svg_lines.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="10" fill="#333" font-weight="bold">{safe_pid}</text>')

    svg_lines.append('</svg>')
    return '\n'.join(svg_lines)
