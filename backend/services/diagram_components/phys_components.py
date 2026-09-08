"""物理示意图 SVG 组件

每个组件是一个函数，接收 dict 参数，返回 SVG 元素字符串。
"""

import math


def pulley(c):
    """滑轮: 圆+内部横线+悬挂"""
    x, y = c.get("x",0), c.get("y",0)
    r = c.get("r", 20)
    label = c.get("label", "")
    cx, cy = x + r, y + r
    body = f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 内部横线 (滑轮轭)
    body += f'<line x1="{cx}" y1="{cy-r*0.6}" x2="{cx}" y2="{cy+r*0.6}" stroke="#333" stroke-width="1"/>'
    # 悬挂
    body += f'<line x1="{cx}" y1="{cy-r}" x2="{cx}" y2="{cy-r-10}" stroke="#333" stroke-width="1.5"/>'
    # 绳子 (两侧下垂)
    body += f'<line x1="{cx-r}" y1="{cy}" x2="{cx-r}" y2="{cy+20}" stroke="#333" stroke-width="1"/>'
    body += f'<line x1="{cx+r}" y1="{cy}" x2="{cx+r}" y2="{cy+20}" stroke="#333" stroke-width="1"/>'
    if label:
        body += f'<text x="{cx}" y="{cy+r+16}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def inclined_plane(c):
    """斜面: 直角三角形"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 100)
    h = c.get("height", 60)
    angle = c.get("angle", 30)
    label = c.get("label", "")
    body = f'<polygon points="{x},{y+h} {x+w},{y+h} {x},{y+h-w*math.tan(math.radians(angle))}" fill="#f0f0f0" stroke="#333" stroke-width="1.5"/>'
    # 角度标注
    arc_r = 15
    arc_start = 180 - angle
    body += f'<path d="M{x+arc_r},{y+h} A{arc_r},{arc_r} 0 0,0 {x+arc_r*math.cos(math.radians(arc_start))},{y+h-arc_r*math.sin(math.radians(arc_start))}" fill="none" stroke="#333" stroke-width="0.8"/>'
    body += f'<text x="{x+arc_r+4}" y="{y+h-arc_r/2}" font-size="11" fill="#333">θ</text>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def block(c):
    """物体方块: 矩形可带字母标注"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 40)
    h = c.get("height", 30)
    label = c.get("label", "A")
    fill = c.get("fill", "#f0f0f0")
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="{fill}" stroke="#333" stroke-width="1.5"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h/2+4}" font-size="14" fill="#333" font-weight="bold" text-anchor="middle">{label}</text>'
    return body


def spring(c):
    """弹簧: 锯齿形线"""
    x, y = c.get("x",0), c.get("y",0)
    h = c.get("height", 60)
    w = c.get("width", 16)
    label = c.get("label", "")
    # 锯齿形弹簧
    segs = 8
    seg_h = h / segs
    points = []
    for i in range(segs + 1):
        px = x + (w if i % 2 == 1 else 0)
        py = y + i * seg_h
        points.append(f"{px},{py}")
    body = f'<polyline points="{" ".join(points)}" fill="none" stroke="#333" stroke-width="1.5"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def pendulum(c):
    """单摆: 悬挂线+摆锤"""
    x, y = c.get("x",0), c.get("y",0)
    length = c.get("length", 80)
    angle = c.get("angle", 15)  # 偏转角度 (度)
    radius = c.get("radius", 8)
    label = c.get("label", "")
    # 悬挂点
    pivot_x, pivot_y = x, y
    # 摆锤位置 (根据角度偏转)
    theta = math.radians(angle)
    bob_x = pivot_x + length * math.sin(theta)
    bob_y = pivot_y + length * math.cos(theta)
    # 悬挂线
    body = f'<line x1="{pivot_x}" y1="{pivot_y}" x2="{bob_x}" y2="{bob_y}" stroke="#333" stroke-width="1"/>'
    # 摆锤
    body += f'<circle cx="{bob_x}" cy="{bob_y}" r="{radius}" fill="#f0f0f0" stroke="#333" stroke-width="1.5"/>'
    if label:
        body += f'<text x="{bob_x}" y="{bob_y+radius+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    # 角度弧
    arc_r = 20
    body += f'<path d="M{pivot_x+arc_r},{pivot_y} A{arc_r},{arc_r} 0 0,0 {pivot_x+arc_r*math.cos(theta)},{pivot_y+arc_r*math.sin(theta)}" fill="none" stroke="#333" stroke-width="0.8"/>'
    # 角度标注已在 inclined_plane 中实现
    return body
