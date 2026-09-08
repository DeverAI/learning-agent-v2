import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
STATIC_CSS = ROOT / "static" / "css"
COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")


def _permitted_base_color(value: str) -> bool:
    value = value.lower().replace(" ", "")
    if value in {"#000", "#000000", "#fff", "#ffffff"}:
        return True
    if value.startswith(("rgb(0,0,0", "rgba(0,0,0", "rgb(255,255,255", "rgba(255,255,255")):
        return True
    return False


def test_active_ui_colors_use_theme_tokens():
    """Page-level UI may only hardcode black/white; palette values live centrally."""
    files = [
        path for path in TEMPLATES.rglob("*.html")
        if "_rollback" not in path.parts and path.name not in {"base.html", "settings.html"}
    ]
    files.extend(path for path in STATIC_CSS.glob("*.css") if path.name != "theme.css")

    violations = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in COLOR_RE.finditer(text):
            if not _permitted_base_color(match.group(0)):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(ROOT)}:{line} {match.group(0)}")
    assert not violations, "Independent UI colors found:\n" + "\n".join(violations)


def test_responsive_rules_are_fluid_and_keep_desktop_layout():
    """流体布局规则统一由 theme.css 提供（base.html 不再承载样式）。"""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    theme = (STATIC_CSS / "theme.css").read_text(encoding="utf-8")

    assert ".main{margin-left:200px;flex:1;padding:clamp(" in theme
    assert "container-type:inline-size;container-name:main" in theme
    assert "width:min(94vw,900px)" in theme
    assert ".row>*{flex:1 1 220px;min-width:min(100%,160px)}" in theme
    assert "repeat(auto-fit,minmax(min(100%,180px),1fr))" in theme
    for name in ("notes.html",):
        assert "container-name:" in (TEMPLATES / name).read_text(encoding="utf-8")
    assert "@media(max-width:768px)" in theme
    assert ".row>*{min-width:100%" not in theme
    assert ".modal{width:100%" not in theme

    adaptive_templates = {
        "questions.html", "notes.html", "editor.html", "paper_detail.html",
        "paper_generate.html", "search.html", "correct.html", "batch_upload.html",
    }
    for name in adaptive_templates:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "@media(max-width" not in text.replace("@media (max-width", "@media(max-width")
