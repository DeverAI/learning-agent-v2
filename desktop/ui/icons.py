"""OISystem 内联 SVG 图标库。

每个图标返回 QByteArray，可直接用于 QPixmap 或 QIcon。
所有 SVG 使用 currentColor，便于通过 stylesheet 着色。
"""
import os
import json
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import Qt, QSize


SVG_FOCUS = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 2a4 4 0 0 1 4 4v2a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/>
<path d="M5 10v2a7 7 0 0 0 14 0v-2"/>
<path d="M12 19v3"/>
</svg>"""

SVG_DIALOG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
</svg>"""

SVG_SETTINGS = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<circle cx="12" cy="12" r="3"/>
<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
</svg>"""

SVG_LOG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
<polyline points="14 2 14 8 20 8"/>
<line x1="8" y1="13" x2="16" y2="13"/>
<line x1="8" y1="17" x2="16" y2="17"/>
</svg>"""

SVG_EXIT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
<polyline points="16 17 21 12 16 7"/>
<line x1="21" y1="12" x2="9" y2="12"/>
</svg>"""

SVG_BOSS = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>
<line x1="1" y1="1" x2="23" y2="23"/>
</svg>"""

SVG_FLAG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/>
<line x1="4" y1="22" x2="4" y2="15"/>
</svg>"""

# 讲课（round 58）：黑板 + 粉笔图形
SVG_LECTURE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<rect x="2" y="4" width="20" height="13" rx="1"/>
<line x1="8" y1="21" x2="16" y2="21"/>
<line x1="12" y1="17" x2="12" y2="21"/>
<path d="M6 12l2-3 2 2 3-4 3 5"/>
</svg>"""

SVG_GRAPH = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<circle cx="6" cy="6" r="3"/>
<circle cx="18" cy="6" r="3"/>
<circle cx="6" cy="18" r="3"/>
<circle cx="18" cy="18" r="3"/>
<line x1="6" y1="9" x2="6" y2="15"/>
<line x1="9" y1="6" x2="15" y2="6"/>
<line x1="9" y1="18" x2="15" y2="18"/>
<line x1="18" y1="9" x2="18" y2="15"/>
</svg>"""

SVG_PIN = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 17v5"/>
<path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>
</svg>"""

# ========== 新增 SVG（从 SVG 选择器收藏） ==========

SVG_CLOUD_UPLOAD = """<svg width="800px" height="800px" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M21.96 13.4199C21.8233 12.3214 21.326 11.2993 20.546 10.5139C19.766 9.72844 18.7474 9.22406 17.65 9.07977C17.1768 7.75468 16.2529 6.63824 15.0399 5.92523C13.8269 5.21223 12.4019 4.94801 11.0139 5.17914C9.62597 5.41026 8.36341 6.12202 7.4469 7.18964C6.53039 8.25726 6.01826 9.61302 6 11.02C4.93913 11.02 3.92172 11.4412 3.17157 12.1913C2.42142 12.9415 2 13.9591 2 15.02C2 16.0808 2.42142 17.0982 3.17157 17.8483C3.92172 18.5985 4.93913 19.02 6 19.02H12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M18.7793 23V15" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M15.5801 18.2L18.7801 15L21.98 18.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""  # 关闭时上传内容+进度条

SVG_CARD_ADD = """<svg width="800px" height="800px" viewBox="-0.5 0 25 25" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M10.58 3.96997H6C4.93913 3.96997 3.92172 4.39146 3.17157 5.1416C2.42142 5.89175 2 6.9091 2 7.96997V17.97C2 19.0308 2.42142 20.0482 3.17157 20.7983C3.92172 21.5485 4.93913 21.97 6 21.97H18C19.0609 21.97 20.0783 21.5485 20.8284 20.7983C21.5786 20.0482 22 19.0308 22 17.97V13.8999" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M10.58 9.96997H2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M5 18.9199H11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M18 10.9199V2.91992" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M14 6.91992H22" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""  # 新建聊天

SVG_BELL = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M4 20q0 1.632 1.6 3.008t4.384 2.208 6.016 0.8 6.016-0.8 4.384-2.208 1.6-3.008q0-1.792-1.984-3.296v-2.688q0-3.040-1.696-5.536t-4.384-3.68q0.064-0.416 0.064-0.8 0-1.632-1.184-2.816t-2.816-1.184-2.816 1.184-1.184 2.816q0 0.384 0.064 0.8-2.688 1.184-4.384 3.68t-1.664 5.536v2.688q-2.016 1.504-2.016 3.296zM12 28q0 1.664 1.184 2.848t2.816 1.152 2.816-1.152 1.184-2.848q0-0.096-0.032-0.32-2.208 0.32-3.968 0.32t-3.968-0.32q0 0.064 0 0.16t-0.032 0.16z"/>
</svg>"""  # 未开启专注模式

SVG_BULB = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M6.016 10.016q0 3.264 1.984 5.92v0.064q0.128 0.128 0.32 0.384l0.064 0.064q1.632 1.92 1.632 3.552v2.016q0 0.832 0.576 1.408t1.408 0.576h8q0.832 0 1.408-0.576t0.608-1.408v-2.016q0-0.064 0-0.224t0.128-0.608 0.288-0.896 0.608-1.088 0.96-1.184q0.544-0.512 0.736-1.216 1.28-2.208 1.28-4.768 0-2.048-0.8-3.872t-2.144-3.2-3.2-2.144-3.872-0.8q-2.72 0-5.024 1.344t-3.616 3.648-1.344 5.024zM10.016 10.016q0-2.496 1.728-4.256t4.256-1.76 4.256 1.76 1.76 4.256q0 1.92-1.12 3.456t-2.88 2.176v2.368q0 0.832-0.608 1.408t-1.408 0.576-1.408-0.576-0.576-1.408v-2.368q-1.792-0.608-2.912-2.144t-1.088-3.488zM12 27.008q0 0.416 0.288 0.704t0.704 0.288h6.016q0.384 0 0.704-0.288t0.288-0.704-0.288-0.704-0.704-0.288h-6.016q-0.384 0-0.704 0.288t-0.288 0.704zM14.016 31.040q0 0.384 0.288 0.704t0.704 0.288h1.984q0.416 0 0.704-0.288t0.32-0.704-0.32-0.704-0.704-0.32h-1.984q-0.416 0-0.704 0.32t-0.288 0.704z"/>
</svg>"""  # AI上方按钮，一键分析当前错误点

SVG_EYE = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M0 16q0.064 0.16 0.16 0.448t0.48 1.056 0.832 1.632 1.248 1.888 1.664 1.984 2.144 1.888 2.624 1.6 3.136 1.088 3.712 0.416 3.712-0.416 3.168-1.088 2.592-1.6 2.144-1.888 1.664-1.984 1.248-1.888 0.832-1.6 0.48-1.12l0.16-0.416q-0.032-0.16-0.16-0.416t-0.48-1.088-0.832-1.6-1.248-1.888-1.664-2.016-2.144-1.856-2.624-1.632-3.136-1.056-3.712-0.448-3.712 0.416-3.168 1.12-2.592 1.6-2.144 1.888-1.664 1.984-1.248 1.888-0.832 1.6-0.48 1.12zM8 16q0-3.296 2.336-5.632t5.664-2.368 5.664 2.368 2.336 5.632-2.336 5.664-5.664 2.336-5.664-2.336-2.336-5.664zM12 16q0 1.664 1.184 2.848t2.816 1.152 2.816-1.152 1.184-2.848-1.184-2.816-2.816-1.184q-0.032 0-0.096 0.032t-0.096 0q0.192 0.576 0.192 0.992 0 1.248-0.864 2.112t-2.144 0.864q-0.384 0-0.96-0.192 0 0.032-0.032 0.096t0 0.096z"/>
</svg>"""  # 新功能-等会告诉你

SVG_BELL_OFF = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M0.992 4.128l27.136 26.624 2.88-2.848-27.136-26.624zM3.776 20q0 1.632 1.632 3.008t4.448 2.208 6.144 0.8q1.984 0 4.064-0.352l-13.984-13.76q-0.256 1.248-0.256 2.112v2.688q-2.048 1.504-2.048 3.296zM10.912 5.376l16.704 16.416q0.608-0.928 0.608-1.792 0-1.792-2.048-3.296v-2.688q0-3.040-1.696-5.536t-4.48-3.68q0.064-0.352 0.064-0.8 0-1.632-1.184-2.816t-2.88-1.184-2.88 1.184-1.184 2.816q0 0.448 0.096 0.8-0.448 0.192-1.12 0.576zM11.936 28q0 1.664 1.184 2.848t2.88 1.184 2.88-1.184 1.184-2.848q0-0.064 0-0.16t0-0.16q-2.272 0.32-4.064 0.32t-4.032-0.32q0 0.064-0.032 0.16t0 0.16z"/>
</svg>"""  # 开启专注模式

SVG_TAG = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M2.016 8q0 0.832 0.576 1.44t1.408 0.576v16q0 2.496 1.76 4.224t4.256 1.76h12q2.464 0 4.224-1.76t1.76-4.224v-16q0.832 0 1.408-0.576t0.608-1.44-0.608-1.408-1.408-0.576h-5.984q0-2.496-1.792-4.256t-4.224-1.76q-2.496 0-4.256 1.76t-1.728 4.256h-6.016q-0.832 0-1.408 0.576t-0.576 1.408zM8 26.016v-16h16v16q0 0.832-0.576 1.408t-1.408 0.576h-12q-0.832 0-1.44-0.576t-0.576-1.408zM12 23.008q0 0.416 0.288 0.704t0.704 0.288 0.704-0.288 0.32-0.704v-8q0-0.416-0.32-0.704t-0.704-0.288-0.704 0.288-0.288 0.704v8zM14.016 6.016q0-0.832 0.576-1.408t1.408-0.608 1.408 0.608 0.608 1.408h-4zM18.016 23.008q0 0.416 0.288 0.704t0.704 0.288 0.704-0.288 0.288-0.704v-8q0-0.416-0.288-0.704t-0.704-0.288-0.704 0.288-0.288 0.704v8z"/>
</svg>"""  # 删除对话

SVG_USER = """<svg width="800px" height="800px" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M12 21.25C17.1086 21.25 21.25 17.1086 21.25 12C21.25 6.89137 17.1086 2.75 12 2.75C6.89137 2.75 2.75 6.89137 2.75 12C2.75 17.1086 6.89137 21.25 12 21.25Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M12.1303 13C13.8203 13 15.1903 11.63 15.1903 9.94C15.1903 8.25001 13.8203 6.88 12.1303 6.88C10.4403 6.88 9.07031 8.25001 9.07031 9.94C9.07031 11.63 10.4403 13 12.1303 13Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M6.5 19.11C6.80719 17.8839 7.51529 16.7956 8.51178 16.0179C9.50827 15.2403 10.736 14.818 12 14.818C13.264 14.818 14.4917 15.2403 15.4882 16.0179C16.4847 16.7956 17.1928 17.8839 17.5 19.11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""  # 新功能-等会说

SVG_STANDBY = """<svg fill="currentColor" width="800px" height="800px" viewBox="0 0 32 32" version="1.1" xmlns="http://www.w3.org/2000/svg">
<path d="M2.016 18.016q0 2.848 1.088 5.44t2.976 4.448 4.48 3.008 5.44 1.088 5.44-1.088 4.48-3.008 2.976-4.448 1.12-5.44q0-4.128-2.208-7.488t-5.792-5.088v4.608q1.856 1.408 2.912 3.488t1.088 4.48q0 2.72-1.344 5.024t-3.648 3.616-5.024 1.344q-2.016 0-3.872-0.8t-3.2-2.112-2.144-3.2-0.768-3.872q0-2.4 1.056-4.48t2.944-3.488v-4.608q-3.616 1.728-5.824 5.088t-2.176 7.488zM14.016 14.016q0 0.832 0.576 1.408t1.408 0.576 1.408-0.576 0.608-1.408v-12q0-0.832-0.608-1.408t-1.408-0.608-1.408 0.608-0.576 1.408v12z"/>
</svg>"""  # 关闭按钮


def render_svg(svg_str: str, size: int = 24, color: str = "#ffffff") -> QPixmap:
    """将 SVG 字符串渲染为 QPixmap。"""
    colored = svg_str.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    if not renderer.isValid():
        # fallback：空白 pixmap
        pm = QPixmap(QSize(size, size))
        pm.fill(Qt.transparent)
        return pm
    pm = QPixmap(QSize(size, size))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pm


# 主侧边栏按钮清单（按显示顺序）
SIDEBAR_BUTTONS = [
    ("focus", "专注", SVG_FOCUS),
    ("dialog", "AI对话", SVG_DIALOG),
    ("graph", "图论", SVG_GRAPH),
    ("settings", "设置", SVG_SETTINGS),
    ("log", "日志", SVG_LOG),
    ("zzoi", "ZZOI", SVG_FLAG),
    ("lecture", "讲课", SVG_LECTURE),
    ("mute", "静音模式", SVG_BOSS),
    ("exit", "退出", SVG_EXIT),
]


# 文件 SVG 选择结果到侧边栏角色的语义映射（round48：让 selected_svgs 真正生效）
_SELECTED_ROLE_BY_NAME = {
    "focus": "focus",
    "dialog": "dialog",
    "graph": "graph",
    "settings": "settings",
    "log": "log",
    "zzoi": "zzoi",
    "mute": "mute",
    "exit": "exit",
    # 文件 SVG（svg_mapping.json）
    "chat-svgrepo-com": "dialog",
    "more-horizontal-circle-svgrepo-com": "settings",
    "set-up-svgrepo-com": "settings",
    "chart-bar-alt-square-svgrepo-com": "log",
    "alt-tag-svgrepo-com": "exit",
    "bell-off-svgrepo-com": "mute",
    "bell-svgrepo-com": "mute",
    "compass-svgrepo-com": "focus",
    "check-badge-svgrepo-com": "graph",
    "circle-user-svgrepo-com": "zzoi",
    "moon-svgrepo-com": "settings",
    "card-add-svgrepo-com": "graph",
}


def _project_root() -> str:
    """ui/icons.py 的上两级是 OISystem，上三级是工作区根目录（存放 svg_mapping.json）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_file_svg_selections() -> list:
    """从工作区根目录 svg_mapping.json 读取已选文件 SVG（含 role 语义）。"""
    candidates = [
        os.path.join(_project_root(), "svg_mapping.json"),
        os.path.join(_project_root(), "backups", "20260720_2030_full", "svg_mapping.json"),
    ]
    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("selections"), list):
                continue
            items = []
            for s in data["selections"]:
                if not isinstance(s, dict) or not s.get("name") or not s.get("svg"):
                    continue
                name = str(s["name"])
                items.append({
                    "name": name,
                    "svg": str(s["svg"]),
                    "label": (s.get("note") or s.get("description") or name),
                    "category": str(s.get("category") or "其他"),
                    "role": _SELECTED_ROLE_BY_NAME.get(name, ""),
                })
            if items:
                return items
        except Exception:
            continue
    return []


def get_all_svgs_grouped() -> dict:
    """返回所有 SVG 按分类分组（内建 + 文件选择结果），供 SVG 选择器使用。"""
    import copy
    groups = {
        "工具": [
            copy.deepcopy({"name": "focus", "svg": SVG_FOCUS, "label": "专注", "role": "focus"}),
            copy.deepcopy({"name": "dialog", "svg": SVG_DIALOG, "label": "AI对话", "role": "dialog"}),
            copy.deepcopy({"name": "graph", "svg": SVG_GRAPH, "label": "图论", "role": "graph"}),
            copy.deepcopy({"name": "settings", "svg": SVG_SETTINGS, "label": "设置", "role": "settings"}),
            copy.deepcopy({"name": "log", "svg": SVG_LOG, "label": "日志", "role": "log"}),
            copy.deepcopy({"name": "zzoi", "svg": SVG_FLAG, "label": "ZZOI", "role": "zzoi"}),
            copy.deepcopy({"name": "mute", "svg": SVG_BOSS, "label": "静音模式", "role": "mute"}),
            copy.deepcopy({"name": "exit", "svg": SVG_EXIT, "label": "退出", "role": "exit"}),
            copy.deepcopy({"name": "standby", "svg": SVG_STANDBY, "label": "关机", "role": ""}),
        ],
    }
    for item in _load_file_svg_selections():
        cat = item.get("category") or "其他"
        groups.setdefault(cat, []).append(item)
    return groups


def get_sidebar_buttons() -> list:
    """按 selected_svgs 收藏结果生成侧边栏按钮清单。

    round48：此前 selected_svgs 只存不读（选择器存完无人消费），现在把用户
    勾选的内建/文件 SVG 按语义角色应用到对应侧边栏按钮；未勾选时保持默认图标。
    """
    buttons = [tuple(b) for b in SIDEBAR_BUTTONS]
    try:
        from config.settings import ConfigManager
        selected = ConfigManager().settings.selected_svgs
    except Exception:
        selected = []
    if not isinstance(selected, list) or not selected:
        return buttons

    items = {}
    for group in get_all_svgs_grouped().values():
        for item in group:
            if isinstance(item, dict) and item.get("name") and item.get("svg"):
                items[str(item["name"])] = item
    # 同一角色有多个勾选时，按选择器分组展示顺序稳定取最后一个（文件 SVG 优先级高于内建）
    overrides = {}
    for name in selected:
        if not isinstance(name, str):
            continue
        item = items.get(name)
        if not item or not item.get("role"):
            continue
        overrides[item["role"]] = item["svg"]

    if not overrides:
        return buttons
    return [
        (key, label, overrides.get(key, svg))
        for key, label, svg in buttons
    ]


