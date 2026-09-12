import os
import re
import base64
import tempfile
from html import escape as html_escape
from config import STORAGE_DIR
from services.ai_service import ai_service
from logger import get_logger, log_error

logger = get_logger()

PAPER_CSS = {
    "A4": "@page { size: A4; margin: 20mm 15mm; }",
    "A3": "@page { size: A3; margin: 25mm 20mm; }",
}

PRINT_STYLE = """
@media print {
  body { margin: 0; }
  .no-print { display: none !important; }
  .page-break { page-break-after: always; }
  table { page-break-inside: avoid; }
  h2, h3 { page-break-after: avoid; }
}
"""

LATEX_CDN = '<script src="/storage/vendor/katex/katex.min.js"></script>'
LATEX_CSS = '<link rel="stylesheet" href="/storage/vendor/katex/katex.min.css">'
LATEX_AUTORENDER = (
    '<script src="/storage/vendor/katex/auto-render.min.js"></script>'
    '<script>document.addEventListener("DOMContentLoaded",function(){renderMathInElement(document.body,{delimiters:['
    '{left:"$$",right:"$$",display:true},'
    '{left:"$",right:"$",display:false},'
    '{left:"\\\\(",right:"\\\\)",display:false}'
    ']})});</script>'
)

LAYOUT_REVIEW_PROMPT = """你是一位严格的试卷排版审核专家。请仔细检查这张试卷截图，找出所有排版问题。

检查清单：
1. 字体大小是否统一、正文是否清晰可读
2. 行间距和段间距是否合适（不应太挤或太空）
3. 表格/选择题选项是否对齐整齐
4. 试卷标题信息是否完整（科目、年级、分值等）
5. 题号是否连续、清晰
6. 数学公式的位置是否合理（不应溢出或重叠）
7. 整体视觉效果是否像一份正式考试试卷
8. 是否有大片空白区域浪费纸张
9. 页边距是否合适
10. 示意图位置是否正确、大小是否合适

请逐条列出你发现的具体问题。如果没有问题，只返回 LAYOUT_OK。
不要修改 HTML 代码，只描述问题。"""


class LayoutReviewUnavailable(RuntimeError):
    """视觉和源码两条审核链路均不可用。"""


class LayoutService:

    def wrap_html(self, body_html: str, paper_size: str = "A4",
                  title: str = "", is_answer: bool = False) -> str:
        size_css = PAPER_CSS.get(paper_size, PAPER_CSS["A4"])
        bg = "#fafafa" if is_answer else "#ffffff"
        label = html_escape(title or ("参考答案" if is_answer else "试卷"))

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{label}</title>
{LATEX_CSS}
<style>
  .katex {{ font-family: "Times New Roman", "STIX Two Math", serif !important; }}
  .katex .mathrm, .katex .textrm {{ font-family: "Times New Roman", serif !important; }}
  .katex .mathit {{ font-family: "Times New Roman", serif !important; font-style: italic !important; }}
  .katex .mathbf {{ font-family: "Times New Roman", serif !important; font-weight: bold !important; }}
  {size_css}
  {PRINT_STYLE}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "SimSun", "Songti SC", "Noto Serif SC", "Source Han Serif SC", serif;
    font-size: 14px;
    line-height: 2;
    color: #1a1a1a;
    background: {bg};
    max-width: 100%;
    padding: 0 20px;
  }}
  .paper-header {{
    text-align: center;
    margin-bottom: 20px;
    padding-bottom: 10px;
    border-bottom: 2px solid #333;
  }}
  .paper-header h1 {{ font-size: 22px; margin: 0 0 8px 0; font-weight: 700; }}
  .paper-info {{ font-size: 13px; color: #555; margin: 4px 0; }}
  .paper-info span {{ margin: 0 14px; }}
  .question-block {{ margin: 18px 0; padding: 10px 0; border-bottom: 1px dashed #ccc; }}
  .question-num {{ font-weight: bold; margin-right: 6px; }}
  .options {{ margin: 8px 0 8px 24px; }}
  .options .opt {{ display: inline-block; min-width: 120px; margin: 2px 14px 2px 0; }}
  .answer-block {{ margin: 16px 0; padding: 12px; background: #f6f6f6; border-left: 3px solid #333; }}
  .answer-label {{ font-weight: bold; }}
  .highlight {{ background: #f0f0f0; padding: 2px 6px; }}
  .score {{ font-weight: bold; }}
  .diagram {{ text-align: center; margin: 12px 0; }}
  .diagram img, .diagram svg {{ max-width: 80%; height: auto; }}
  .tip {{ font-size: 12px; color: #888; border: 1px solid #e0e0e0; padding: 8px; margin: 8px 0; }}
</style>
</head>
<body>
<div class="paper-header"><h1>{label}</h1></div>
{body_html}
{LATEX_CDN}
{LATEX_AUTORENDER}
</body>
</html>"""

    @staticmethod
    def _sanitize_diagram_labels(html: str) -> str:
        """移除遮挡图形内容的 '图一' '备用图' 等标签，以及不规范的图形标记如 ΔDEF"""
        # 移除独立位置的"图一""图二""备用图"等遮挡标签（仅匹配行首或标签包裹的独立文本，不误删"如图X所示"）
        html = re.sub(
            r'(?:^|<br\s*/?>|</p>|<p>|<div>|</div>)\s*(?:图[一二三四五六七八九十]|备用图)\s*(?:[：:]?\s*)(?:</p>|<br\s*/?>|</div>|$)?',
            '', html, flags=re.MULTILINE
        )
        # 将独立一行的 ----- 或 ____ 系列（证明题中的横线）替换为合适的留白区域
        html = re.sub(
            r'(?:<[^>]*>)?\s*[-_]{4,}\s*(?:</[^>]*>)?',
            '<div style="height:60px;"></div>', html
        )
        # 替换裸 Unicode ΔXXX 为 $\\triangle XXX$（仅替换未被 $ 包裹或已转义的情况）
        html = re.sub(r'(?<![\\$])Δ([A-Za-z]+)', r'$\\triangle \1$', html)
        # 替换裸 Unicode ∠XXX 为 $\\angle XXX$
        html = re.sub(r'(?<![\\$])∠([A-Za-z]+)', r'$\\angle \1$', html)
        return html

    def fix_layout(self, html: str, paper_size: str = "A4",
                   title: str = "", is_answer: bool = False) -> str:
        if not html or not html.strip():
            return html
        # 先清理内容
        html = self._sanitize_diagram_labels(html)
        # If it already has full HTML structure, extract body content
        if '<!DOCTYPE html>' in html or '<html' in html.lower():
            m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
            body_html = m.group(1) if m else html
        else:
            body_html = html
        # Wrap with minimal scaffold (no duplicate header - AI generates its own)
        bg = "#fafafa" if is_answer else "#ffffff"
        label = html_escape(title or ("参考答案" if is_answer else "试卷"))
        size_css = PAPER_CSS.get(paper_size, PAPER_CSS["A4"])
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{label}</title>
<link rel="stylesheet" href="/storage/vendor/katex/katex.min.css">
<style>
{size_css}
{PRINT_STYLE}
*{{box-sizing:border-box}}
body{{font-family:"SimSun","Songti SC","Noto Serif SC","Source Han Serif SC","STIX Two Math",serif;font-size:14px;line-height:2;color:#1a1a1a;background:{bg};max-width:100%;padding:0 20px;margin:0;overflow-wrap:break-word;word-wrap:break-word}}
.katex{{font-family:"Times New Roman","STIX Two Math",serif!important}}
.katex .mathrm,.katex .textrm,.katex .text,.katex .textnormal{{font-family:"Times New Roman",serif!important}}
.katex .mathit,.katex .textit{{font-family:"Times New Roman",serif!important;font-style:italic!important}}
.katex .mathbf,.katex .textbf{{font-family:"Times New Roman",serif!important;font-weight:bold!important}}
img,svg{{max-width:100%;height:auto;display:block;margin:0 auto}}
table{{page-break-inside:avoid;border-collapse:collapse;width:100%}}
h2,h3{{page-break-after:avoid}}
.page-break{{page-break-after:always}}
.no-print{{display:none!important}}
.proof-blank{{min-height:60px;display:block}}
</style>
</head>
<body>
{body_html}
<script src="/storage/vendor/katex/katex.min.js"></script>
<script src="/storage/vendor/katex/auto-render.min.js"></script>
<script>document.addEventListener("DOMContentLoaded",function(){{renderMathInElement(document.body,{{delimiters:[{{left:"$$",right:"$$",display:true}},{{left:"$",right:"$",display:false}},{{left:"\\\\(",right:"\\\\)",display:false}}],throwOnError:false}})}});</script>
</body>
</html>"""

    async def review_and_fix(self, paper_html: str, answer_html: str,
                              paper_size: str, title: str,
                              max_rounds: int = 2) -> tuple:
        """对试卷 body HTML 进行最多 max_rounds 轮排版审核与修复。

        输入应为 body HTML（可含/不含外层 html/head/body），返回修复后的 body HTML，
        由调用方统一进行一次 fix_layout 包装，避免嵌套 html/body。
        """
        current_paper = paper_html
        current_answer = answer_html
        for round_num in range(max_rounds):
            logger.info("Layout review round %d/%d...", round_num + 1, max_rounds)
            issues = await self._review_layout(current_paper, title, paper_size)
            if issues.strip().startswith("无问题") or "LAYOUT_OK" in issues:
                logger.info("Layout review passed at round %d", round_num + 1)
                break
            logger.info("Layout issues found: %.200s...", issues)
            fixed = await self._fix_with_deepseek(current_paper, issues, title, paper_size)
            if fixed and len(fixed) > 100:
                current_paper = fixed
        return current_paper, current_answer

    async def _review_layout(self, html: str, title: str, paper_size: str) -> str:
        # 若传入完整 HTML，先提取 body 用于审查，避免 wrap_html 嵌套包装
        body_html = html
        if '<!DOCTYPE html>' in html or '<html' in html.lower():
            m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
            if m:
                body_html = m.group(1)
        preview_html = self.wrap_html(body_html, paper_size, title, False)
        img_base64 = await self._html_to_image(preview_html)

        failures = []
        if img_base64:
            try:
                return await self._glm_review_image(img_base64)
            except Exception as exc:
                failures.append(f"视觉审核失败: {exc}")
                logger.warning("Falling back to source layout review: %s", exc)
        try:
            return await self._deepseek_review_source(body_html)
        except Exception as exc:
            failures.append(f"源码审核失败: {exc}")
            raise LayoutReviewUnavailable("；".join(failures)) from exc

    async def _html_to_image(self, html: str) -> str:
        import asyncio
        try:
            from weasyprint import HTML as _WP

            def _render_weasy() -> bytes:
                return _WP(string=html).write_png()

            # weasyprint 渲染是 CPU 密集同步操作，放线程池避免冻结事件循环
            img = await asyncio.to_thread(_render_weasy)
            return base64.b64encode(img).decode()
        except Exception:
            # 新版 weasyprint 已移除 write_png（AttributeError），
            # 任何失败都应继续落到 wkhtmltoimage 回退，不能让异常冒泡
            pass

        temp_path = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        png_path = temp_path.name.replace(".html", ".png")
        try:
            temp_path.write(html)
            temp_path.close()
            import subprocess

            def _render_wk() -> int:
                result = subprocess.run(
                    ["wkhtmltoimage", "--width", "800", temp_path.name, png_path],
                    capture_output=True, timeout=30
                )
                return result.returncode

            # subprocess.run 最长阻塞 30s，同样必须离开事件循环线程
            returncode = await asyncio.to_thread(_render_wk)
            if returncode == 0 and os.path.exists(png_path):
                with open(png_path, "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                return data
        except Exception as e:
            logger.warning("html_to_image failed: %s", e)
        finally:
            for leftover in (temp_path.name, png_path):
                try:
                    if os.path.exists(leftover):
                        os.unlink(leftover)
                except OSError:
                    pass
        return ""

    async def _glm_review_image(self, img_base64: str) -> str:
        # 视觉审核走 MiMo-first 统一助手（Fact.md 模型分工定规，2026-09-09）
        result = await ai_service.vision_mimo_first(img_base64, LAYOUT_REVIEW_PROMPT, parse_json=False)
        review = result if isinstance(result, str) else str(result)
        if not review.strip():
            raise RuntimeError("视觉模型返回空审核结果")
        return review

    async def _deepseek_review_source(self, html: str) -> str:
        prompt = (
            "请检查以下HTML试卷的排版。逐一列出问题：\n"
            "1. 字体大小是否统一\n"
            "2. 各元素间距是否合理\n"
            "3. 表格和选项是否对齐\n"
            "4. 题号是否连续\n"
            "5. 公式区域是否有溢出\n"
            "6. 标题信息是否完整\n"
            f"---\n{html[:15000]}\n---\n"
            "直接列出问题，不要修改代码。如果无问题请回 'LAYOUT_OK'。"
        )
        result = await ai_service.deepseek_chat(
            [{"role": "user", "content": prompt}], max_tokens=2048, scope="paper")
        if not (result or "").strip():
            raise RuntimeError("源码审核模型返回空结果")
        return result

    async def _fix_with_deepseek(self, html: str, issues: str,
                                  title: str, paper_size: str) -> str:
        prompt = (
            "你是HTML试卷排版修复专家。请根据审核意见修复这份试卷HTML。\n\n"
            f"审核意见：\n{issues}\n\n"
            f"当前HTML（body部分）：\n{html[:20000]}\n\n"
            "修复要求：\n"
            "1. 只修改排版相关CSS和HTML结构\n"
            "2. 不要改变题目内容和题号\n"
            "3. 数学公式保持 $ 和 $$ 格式不变\n"
            f"4. 生成适合{paper_size}纸打印的排版\n"
            "5. 直接输出修复后的完整body HTML（不含html/head/body标签）"
        )
        try:
            result = await ai_service.deepseek_chat(
                [{"role": "user", "content": prompt}], max_tokens=32768, scope="paper")
            return result
        except Exception as e:
            log_error("layout", f"Fix failed: {e}")
            return ""


layout_service = LayoutService()
