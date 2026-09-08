"""化学实验装置 SVG 组件

每个组件是一个函数，接收 dict 参数，返回 SVG 元素字符串。

通用参数:
  x, y: 组件左上角锚点坐标
  width, height: 组件尺寸
  label: 文字标注 (可选)
  fill: 填充色 (可选)
"""

import math


def test_tube(c):
    """试管: 圆角矩形+半圆底+液体"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 24)
    h = c.get("height", 70)
    r = w / 2  # 底部半圆半径
    liquid = c.get("liquid_level", 0.3)  # 液体高度比例 0-1
    label = c.get("label", "")
    # 试管主体 (矩形+半圆底)
    tube = f'<rect x="{x}" y="{y}" width="{w}" height="{h-r}" rx="2" fill="none" stroke="#333" stroke-width="1.5"/>'
    tube += f'<path d="M{x},{y+h-r} A{r},{r} 0 0,0 {x+w},{y+h-r}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 液体
    if liquid > 0:
        ly = y + h - r - (h-r)*liquid
        lh = (h-r)*liquid + r
        tube += f'<rect x="{x+2}" y="{ly}" width="{w-4}" height="{lh}" rx="1" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    # 管口 (开口)
    tube += f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y-6}" stroke="#333" stroke-width="1.5"/>'
    tube += f'<line x1="{x+w}" y1="{y}" x2="{x+w}" y2="{y-6}" stroke="#333" stroke-width="1.5"/>'
    if label:
        tube += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return tube


def beaker(c):
    """烧杯: 梯形主体+液体"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 50)
    h = c.get("height", 40)
    liquid = c.get("liquid_level", 0.5)
    label = c.get("label", "")
    # 梯形: 上宽下窄
    tw = w * 0.85  # 底部宽度
    offset = (w - tw) / 2
    body = f'<polygon points="{x},{y} {x+w},{y} {x+offset+w*tw},{y+h} {x+offset},{y+h}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 液体
    if liquid > 0:
        ly = y + h - h*liquid
        body += f'<rect x="{x+offset+3}" y="{ly}" width="{tw-6}" height="{h*liquid-3}" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    # 杯口
    body += f'<line x1="{x}" y1="{y}" x2="{x+w}" y2="{y}" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+2}" y1="{y-3}" x2="{x+2}" y2="{y}" stroke="#333" stroke-width="1"/>'
    body += f'<line x1="{x+w-2}" y1="{y-3}" x2="{x+w-2}" y2="{y}" stroke="#333" stroke-width="1"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def alcohol_lamp(c):
    """酒精灯: 梯形主体+灯芯+火焰"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 30)
    h = c.get("height", 36)
    label = c.get("label", "")
    # 主体 (梯形)
    bw = w * 0.7  # 底部宽
    bo = (w - bw) / 2
    body = f'<polygon points="{x+bo},{y+h} {x+w-bo},{y+h} {x+w},{y+h*0.35} {x},{y+h*0.35}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 灯颈
    neck_w = w * 0.25
    n_off = (w - neck_w) / 2
    body += f'<rect x="{x+n_off}" y="{y+h*0.2}" width="{neck_w}" height="{h*0.15}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 灯芯管
    body += f'<line x1="{x+w/2}" y1="{y+h*0.2}" x2="{x+w/2}" y2="{y+h*0.1}" stroke="#333" stroke-width="1.5"/>'
    # 火焰 (水滴形)
    flame_h = h * 0.12
    fx = x + w/2
    fy = y + h*0.1
    body += f'<path d="M{fx},{fy-flame_h} Q{fx+flame_h*0.3},{fy} {fx},{fy+flame_h*0.1} Q{fx-flame_h*0.3},{fy} {fx},{fy-flame_h}" fill="#ff8c00" stroke="#333" stroke-width="0.5"/>'
    body += f'<path d="M{fx},{fy-flame_h*0.6} Q{fx+flame_h*0.15},{fy-flame_h*0.2} {fx},{fy} Q{fx-flame_h*0.15},{fy-flame_h*0.2} {fx},{fy-flame_h*0.6}" fill="#ffd700" stroke="none"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def flask(c):
    """锥形瓶 (三角烧瓶): 锥形主体"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 40)
    h = c.get("height", 50)
    liquid = c.get("liquid_level", 0.3)
    label = c.get("label", "")
    neck_w = w * 0.25
    n_off = (w - neck_w) / 2
    # 瓶颈
    body = f'<rect x="{x+n_off}" y="{y}" width="{neck_w}" height="{h*0.3}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 瓶口
    body += f'<line x1="{x+n_off-2}" y1="{y}" x2="{x+n_off-2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+w-n_off+2}" y1="{y}" x2="{x+w-n_off+2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    # 瓶身 (三角形)
    body += f'<polygon points="{x+n_off},{y+h*0.3} {x+w-n_off},{y+h*0.3} {x+w},{y+h} {x},{y+h}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 液体
    if liquid > 0:
        ly = y + h - h*liquid*0.7
        body += f'<polygon points="{x+n_off+2},{ly} {x+w-n_off-2},{ly} {x+w-2},{y+h} {x+2},{y+h}" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def round_bottom_flask(c):
    """圆底烧瓶: 瓶颈+圆底"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 44)
    h = c.get("height", 50)
    liquid = c.get("liquid_level", 0.2)
    label = c.get("label", "")
    r = w * 0.42
    neck_w = w * 0.22
    n_off = (w - neck_w) / 2
    cx, cy = x + w/2, y + h - r
    # 瓶颈
    body = f'<rect x="{x+n_off}" y="{y}" width="{neck_w}" height="{h*0.35}" fill="none" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+n_off-2}" y1="{y}" x2="{x+n_off-2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+w-n_off+2}" y1="{y}" x2="{x+w-n_off+2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    # 圆底
    body += f'<path d="M{x+n_off},{y+h*0.35} L{x+n_off},{cy} A{r},{r} 0 0,0 {x+w-n_off},{cy} L{x+w-n_off},{y+h*0.35}" fill="none" stroke="#333" stroke-width="1.5"/>'
    if liquid:
        lh = (h*0.65) * liquid
        ly = y + h - lh
        body += f'<rect x="{x+n_off+1}" y="{ly}" width="{w-2*n_off-2}" height="{lh}" rx="1" fill="rgba(70,130,200,0.2)" stroke="none"/>'
        # 液面弧线
        arc_r = (w-2*n_off-2) / 2
        arc_cx = x + w/2
        body += f'<path d="M{x+n_off+1},{ly} A{arc_r},{arc_r*0.15} 0 0,0 {x+w-n_off-1},{ly}" fill="none" stroke="#666" stroke-width="0.8"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def funnel(c):
    """普通漏斗: 上宽下窄管"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 30)
    h = c.get("height", 40)
    label = c.get("label", "")
    neck_w = w * 0.2
    # 上部 (宽口)
    body = f'<polygon points="{x},{y} {x+w},{y} {x+w/2+neck_w/2},{y+h*0.5} {x+w/2-neck_w/2},{y+h*0.5}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 下部 (细管)
    body += f'<rect x="{x+w/2-neck_w/2}" y="{y+h*0.5}" width="{neck_w}" height="{h*0.5}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 液体
    body += f'<rect x="{x+w/2-neck_w/2+1}" y="{y+h*0.5+1}" width="{neck_w-2}" height="{h*0.5-2}" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def separatory_funnel(c):
    """分液漏斗: 球体+上下管"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 34)
    h = c.get("height", 55)
    label = c.get("label", "")
    cx, cy = x + w/2, y + h*0.4
    r = w * 0.45
    neck_w = w * 0.18
    n_off = (w - neck_w) / 2
    # 上管
    body = f'<rect x="{x+n_off}" y="{y}" width="{neck_w}" height="{h*0.18}" fill="none" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+n_off-2}" y1="{y}" x2="{x+n_off-2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    body += f'<line x1="{x+w-n_off+2}" y1="{y}" x2="{x+w-n_off+2}" y2="{y-4}" stroke="#333" stroke-width="1.5"/>'
    # 球体
    body += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 下管+活塞
    body += f'<rect x="{x+n_off}" y="{y+h*0.55}" width="{neck_w}" height="{h*0.35}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 活塞
    piston_y = y + h*0.6
    body += f'<rect x="{x+n_off-3}" y="{piston_y}" width="{neck_w+6}" height="4" fill="#999" stroke="#333" stroke-width="0.5"/>'
    # 管内液体
    body += f'<rect x="{x+n_off+1}" y="{piston_y+5}" width="{neck_w-2}" height="{h*0.35-5}" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    if label:
        body += f'<text x="{cx}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def condenser(c):
    """冷凝管: 长管+外套"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 12)
    h = c.get("height", 60)
    label = c.get("label", "")
    # 内管
    body = f'<rect x="{x}" y="{y}" width="{w*0.5}" height="{h}" rx="2" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 外套
    jacket_w = w * 0.9
    jo = (w - jacket_w) / 2
    body += f'<rect x="{x+jo}" y="{y+4}" width="{jacket_w}" height="{h-8}" rx="1" fill="none" stroke="#666" stroke-width="1"/>'
    # 进出水口
    body += f'<line x1="{x+jo}" y1="{y+h*0.25}" x2="{x-6}" y2="{y+h*0.25}" stroke="#666" stroke-width="1"/>'
    body += f'<line x1="{x+jo+jacket_w}" y1="{y+h*0.75}" x2="{x+w+6}" y2="{y+h*0.75}" stroke="#666" stroke-width="1"/>'
    # 进出水箭头文字
    body += f'<text x="{x-4}" y="{y+h*0.25-2}" font-size="8" fill="#666" text-anchor="end">进水</text>'
    body += f'<text x="{x+w+4}" y="{y+h*0.75-2}" font-size="8" fill="#666">出水</text>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def iron_stand(c):
    """铁架台: 垂直立柱+水平夹"""
    x, y = c.get("x",0), c.get("y",0)
    h = c.get("height", 80)
    w = c.get("width", 6)
    clamp_y = c.get("clamp_y", y + h*0.6)
    clamp_w = c.get("clamp_width", 30)
    label = c.get("label", "")
    # 立柱
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#555" stroke="#333" stroke-width="1"/>'
    # 底座
    body += f'<line x1="{x-15}" y1="{y+h}" x2="{x+w+15}" y2="{y+h}" stroke="#333" stroke-width="3"/>'
    # 铁圈/夹
    body += f'<line x1="{x+w}" y1="{clamp_y}" x2="{x+w+clamp_w}" y2="{clamp_y}" stroke="#333" stroke-width="2"/>'
    body += f'<circle cx="{x+w+clamp_w}" cy="{clamp_y}" r="4" fill="none" stroke="#333" stroke-width="1.5"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def gas_bottle(c):
    """集气瓶: 矩形+玻璃片"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 30)
    h = c.get("height", 50)
    filled = c.get("filled", False)  # 是否装满水(排水法)
    label = c.get("label", "")
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 瓶口玻璃片
    body += f'<line x1="{x-2}" y1="{y}" x2="{x+w+2}" y2="{y}" stroke="#333" stroke-width="2"/>'
    # 水
    if filled:
        body += f'<rect x="{x+2}" y="{y+2}" width="{w-4}" height="{h-4}" rx="1" fill="rgba(70,130,200,0.2)" stroke="none"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def water_tank(c):
    """水槽: 大矩形+水（排水集气法）"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 80)
    h = c.get("height", 40)
    water_level = c.get("water_level", 0.8)
    label = c.get("label", "")
    body = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 水
    wh = h * water_level
    body += f'<rect x="{x+2}" y="{y+h-wh}" width="{w-4}" height="{wh-2}" rx="2" fill="rgba(70,130,200,0.15)" stroke="none"/>'
    # 水面线
    body += f'<path d="M{x+2},{y+h-wh} Q{x+w/2},{y+h-wh-2} {x+w-2},{y+h-wh}" fill="none" stroke="#69c" stroke-width="0.8"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body


def evaporating_dish(c):
    """蒸发皿: 上宽下窄浅碗"""
    x, y = c.get("x",0), c.get("y",0)
    w = c.get("width", 40)
    h = c.get("height", 16)
    label = c.get("label", "")
    body = f'<path d="M{x},{y} Q{x+w*0.5},{y+h*0.8} {x+w},{y} L{x+w},{y} Q{x+w*0.5},{y+h*0.8} {x},{y}" fill="none" stroke="#333" stroke-width="1.5"/>'
    # 底部
    body += f'<ellipse cx="{x+w/2}" cy="{y+h*0.45}" rx="{w*0.35}" ry="{h*0.2}" fill="none" stroke="#333" stroke-width="1"/>'
    # 固体

    if c.get("has_solid", False):
        body += f'<ellipse cx="{x+w/2}" cy="{y+h*0.4}" rx="{w*0.2}" ry="{h*0.1}" fill="#ddd" stroke="#999" stroke-width="0.5"/>'
    if label:
        body += f'<text x="{x+w/2}" y="{y+h+14}" font-size="12" fill="#333" text-anchor="middle">{label}</text>'
    return body
