"""桌面讲课引擎（round 58）：从学习 Agent 服务器拉取题目/试卷，本地 AI 开讲。

设计：Design.md「桌面讲课：从学习 Agent 拉题讲解（round 58）」。

- 数据通道：GET /api/papers、/api/papers/{id}、/api/questions/{id}（X-Auth-Token
  取自 settings.sync_server_password；requests 在工作线程，不阻塞 UI）
- 讲解计划：每题一次 v4-pro 调用 → JSON {steps:[{title, speech}], summary}
  （教学式 3~6 步）；AI 失败/解析失败降级为两步（读题面/读标准答案），绝不沉默
- 播放状态机：plan_ready 后逐步播放——每步卡片板书 + TTS 朗读，idle 驱动
  自动连播；pause/next/prev/stop 手动控制
"""
import json
import threading

from PySide6.QtCore import QObject, Signal

from config.settings import ConfigManager
from utils.helpers import logger, append_err_record

import requests

# 拉取上限
PAPERS_LIMIT = 50
# 讲解词单步最长字符（TTS/卡片共用）
STEP_SPEECH_MAX = 400
# 题面/答案送入 AI 的长度上限
CONTEXT_MAX = 4000

SYSTEM_PROMPT = (
    "你是一位擅长讲题的一对一 AI 教师。学生会看着屏幕听你讲解一道题。"
    "请输出 JSON（不要任何额外文字）："
    '{"steps":[{"title":"步骤名","speech":"对学生的口语讲解","placement_hint":"top_right"},'
    '"summary":"一句话收尾总结"}。'
    "要求：\n"
    "1. steps 3~6 步，教学式递进：题意分析→解题思路→逐步解法→答案核对与得分点→易错提醒（如适用）；\n"
    "2. 每题只讲这一道题，引用题目已知条件，不假设额外条件；\n"
    "3. 每步 speech 口语化、可直接朗读（40~120 字），不含 Markdown 符号、LaTeX 定界符；\n"
    "4. 每步 placement_hint 是黑板摆放的大概位置提示，九宫格取值：top_left/top_center/top_right/"
    "mid_left/mid_center/mid_right/bottom_left/bottom_center/bottom_right（相邻步骤换区域避免重叠）；\n"
    "5. summary 一句话收尾，10~40 字；\n"
    "6. 无法解答时如实说明，不给虚构答案。"
)


def _clean_for_speech(text: str) -> str:
    """去除 HTML/LaTeX/Markdown 噪音，保留可朗读文本。"""
    import re
    t = str(text or "")
    t = re.sub(r"<[^>]+>", " ", t)                     # HTML 标签
    t = re.sub(r"\\[\(\)\[\]]", "", t)                 # LaTeX 定界符
    t = t.replace("$", "").replace("#", " ").replace("|", "，")
    t = re.sub(r"[ \t]+", " ", t).replace("\n\n", "\n")
    return t.strip()


class LectureEngine(QObject):
    """讲解引擎：服务器题目数据 → 结构化讲解计划 → 逐步播放。"""

    papers_ready = Signal(list)          # [{id, title, subject, question_count}]
    paper_ready = Signal(dict)           # {id, title, question_order:[{id,number}]}
    plan_ready = Signal(list, str)       # ([{title, speech}], summary)
    step_started = Signal(int, str, str) # (index, title, speech)
    lecture_finished = Signal()
    error = Signal(str)

    def __init__(self, tts=None, overlay=None, parent=None):
        super().__init__(parent)
        from core.tts_player import TTSPlayer
        from core.screen_paint import ScreenPaintOverlay
        self._tts = tts if tts is not None else TTSPlayer()
        self._overlay = overlay if overlay is not None else ScreenPaintOverlay()
        self._steps = []
        self._step_idx = -1
        self._playing = False
        self._paused = False
        self._tts_failed = False   # F5：上一次朗读是否失败（决定 resume 是重试本步还是前进）
        # 每步自动连播：TTS 播完(idle)且还有下一步时继续
        self._tts.stateChanged.connect(self._on_tts_state)
        self._seq = 0                     # 生成代次，迟到结果丢弃

    # ---------- 服务器数据 ----------

    def _headers(self) -> dict:
        pwd = str(getattr(ConfigManager().settings, "sync_server_password", "") or "").strip()
        h = {"Content-Type": "application/json"}
        if pwd:
            h["X-Auth-Token"] = pwd
        return h

    def _base(self) -> str:
        return str(getattr(ConfigManager().settings, "sync_server_url", "")
                   or "").strip().rstrip("/")

    def fetch_papers(self):
        """试卷列表（工作线程）。"""
        threading.Thread(target=self._fetch_papers_job, daemon=True,
                         name="LecturePapers").start()

    def _fetch_papers_job(self):
        try:
            base = self._base()
            resp = requests.get(f"{base}/api/papers",
                                params={"limit": PAPERS_LIMIT},
                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
            items = resp.json() if isinstance(resp.json(), list) else []
            papers = [{
                "id": p.get("id", ""),
                "title": str(p.get("title", "") or "未命名试卷"),
                "subject": str(p.get("subject", "") or ""),
                "question_count": len(p.get("question_ids") or []),
            } for p in items if p.get("id")]
            self.papers_ready.emit(papers)
        except Exception as e:
            msg = _err(e)
            logger.warning(f"拉取试卷列表失败: {msg}")
            self.error.emit(f"拉取试卷列表失败: {msg}")

    def fetch_paper(self, paper_id: str):
        threading.Thread(target=self._fetch_paper_job, args=(str(paper_id),),
                         daemon=True, name="LecturePaper").start()

    def _fetch_paper_job(self, paper_id: str):
        try:
            base = self._base()
            resp = requests.get(f"{base}/api/papers/{paper_id}",
                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            order = data.get("question_order") or []
            paper = {
                "id": data.get("id", paper_id),
                "title": str(data.get("title", "") or "未命名试卷"),
                "question_order": [{
                    "id": (o.get("id") if isinstance(o, dict) else o),
                    "number": (o.get("number", i + 1) if isinstance(o, dict) else i + 1),
                } for i, o in enumerate(order)],
            }
            self.paper_ready.emit(paper)
        except Exception as e:
            msg = _err(e)
            logger.warning(f"拉取试卷详情失败: {msg}")
            self.error.emit(f"拉取试卷详情失败: {msg}")

    def fetch_question(self, question_id: str) -> dict:
        """同步拉取题目详情（讲解生成前调用；由调用方在工作线程执行）。
        失败返回 None（调用方降级）。"""
        try:
            base = self._base()
            resp = requests.get(f"{base}/api/questions/{question_id}",
                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return {
                "id": data.get("id", question_id),
                "ocr_text": _clean_for_speech(data.get("ocr_text", ""))[:CONTEXT_MAX],
                "question_html": _clean_for_speech(data.get("question_html", "")),
                "standard_answer": _clean_for_speech(
                    data.get("standard_answer", ""))[:CONTEXT_MAX],
                "score_points_html": _clean_for_speech(
                    data.get("score_points_html", ""))[:CONTEXT_MAX],
                "subject": str(data.get("subject", "") or ""),
                "grade": str(data.get("grade", "") or ""),
            }
        except Exception as e:
            logger.warning(f"拉取题目详情失败: {e}")
            return None

    # ---------- 讲解计划 ----------

    def build_plan(self, question: dict):
        """生成讲解计划（工作线程；questions fetch 同步调用后触发）。"""
        self._seq += 1
        seq = self._seq
        self._steps = []
        self._step_idx = -1

        def _job():
            steps, summary = self.build_plan_sync(question)
            if seq != self._seq:
                return                     # 迟到结果丢弃
            self.plan_ready.emit(steps, summary)

        threading.Thread(target=_job, daemon=True, name="LecturePlan").start()

    def build_plan_sync(self, question: dict) -> tuple:
        """同步生成讲解计划（可在 QThread worker 内调用；供 UI 面板使用）。"""
        plan = None
        try:
            plan = self._ask_plan(question)
        except Exception as e:
            logger.warning(f"讲解计划生成失败，降级直读: {str(e)[:150]}")
            try:
                append_err_record("core/lecture_engine.py", "讲解计划失败", str(e)[:300])
            except Exception:
                pass
        steps, summary = self._finalize(question, plan)
        self._steps = steps
        self._step_idx = -1
        self._playing = False
        return steps, summary

    def _ask_plan(self, q: dict) -> dict:
        """v4-pro 生成结构化讲解计划。"""
        from core import ai_client
        question_text = (q.get("question_html") or q.get("ocr_text") or "（无题面文本）")
        parts = [
            f"题目：{question_text[:CONTEXT_MAX]}",
        ]
        if q.get("standard_answer"):
            parts.append(f"标准答案/解析：{q['standard_answer'][:CONTEXT_MAX]}")
        if q.get("score_points_html"):
            parts.append(f"得分点：{q['score_points_html'][:1200]}")
        provider, model = ai_client.resolve_dialog_target()
        raw = ai_client.chat(
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": "\n\n".join(parts)}],
            provider=provider, model=model, temperature=0.3, max_tokens=1500)
        data = json.loads(str(raw or "{}").strip())
        if not isinstance(data, dict):
            raise ValueError("讲解计划非对象")
        return data

    def _finalize(self, q: dict, plan: dict | None):
        """规整计划；plan 无效时降级为两步直读（fail-safe 不沉默）。"""
        if plan and isinstance(plan.get("steps"), list):
            steps = []
            for i, s in enumerate(plan["steps"]):
                if not isinstance(s, dict):
                    continue
                title = str(s.get("title", "") or "讲解").strip()[:30]
                speech = str(s.get("speech", "") or "").strip()[:STEP_SPEECH_MAX]
                if not speech:
                    continue
                hint = str(s.get("placement_hint", "") or "").strip()
                if hint not in ("top_left", "top_center", "top_right", "mid_left",
                                "mid_center", "mid_right", "bottom_left",
                                "bottom_center", "bottom_right"):
                    hint = ["top_right", "mid_left", "bottom_right"][i % 3]   # 默认轮换避重叠
                steps.append({"title": title, "speech": speech, "placement_hint": hint})
            summary = str(plan.get("summary", "") or "").strip()[:STEP_SPEECH_MAX]
            if steps:
                return steps, summary
        # 降级：读题面 + 读标准答案（位置轮换避免自重叠）
        question_text = (q.get("question_html") or q.get("ocr_text") or "（无题面文本）")
        steps = [{"title": "题目", "speech": question_text[:STEP_SPEECH_MAX],
                  "placement_hint": "top_right"}]
        if q.get("standard_answer"):
            steps.append({"title": "答案", "speech": q["standard_answer"][:STEP_SPEECH_MAX],
                          "placement_hint": "bottom_right"})
        return steps, ""

    # ---------- 播放 ----------

    def start_lecture(self, steps: list, summary: str = ""):
        """开始逐步讲解（每步 = 卡片板书 + TTS 朗读，idle 自动连播）。"""
        if not steps:
            self.error.emit("没有可讲解的内容")
            return
        self._steps = list(steps)
        self._summary = summary
        self._step_idx = -1
        self._paused = False
        self._playing = True
        self._tts_failed = False
        self._advance()

    def _advance(self):
        if not self._playing:
            return
        self._step_idx += 1
        if self._step_idx >= len(self._steps):
            self._playing = False
            self.lecture_finished.emit()
            return
        step = self._steps[self._step_idx]
        self._place_and_render(step)
        self.step_started.emit(self._step_idx, step["title"], step["speech"])
        # 朗读（由 _on_tts_state idle 驱动下一步）
        try:
            self._tts.speak(step["speech"], style_instruction="像老师讲课一样，口语自然")
        except Exception as e:
            logger.warning(f"讲解朗读失败，继续下一步: {str(e)[:120]}")
            self._advance()

    def _place_and_render(self, step: dict):
        """放置代理回路（round 59）：全模态选位 → 渲染 → 截图自检（≤2 轮）。

        任意失败降级为直接渲染（fallback 位置），绝不阻断讲解。"""
        hint = step.get("placement_hint", "top_right")
        fallback = {"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32}
        try:
            from core.placement import PlacementAgent
            agent = PlacementAgent(self._overlay)
            result = agent.place_with_selfcheck(
                content_text=step["speech"],
                hint=hint,
                fallback_rect=fallback,
                render_fn=lambda rect: self._render_step_at(rect, step),
            )
            if result["rounds"] > 0:
                logger.info(f"讲题卡片放置调整 {result['rounds']} 轮，"
                            f"剩余重叠 {len(result['overlaps'])}")
        except Exception as e:
            logger.warning(f"放置代理异常，降级直接渲染: {str(e)[:150]}")
            self._render_step_at(fallback, step)

    def _render_step_at(self, rect: dict, step: dict):
        """在指定位置渲染卡片（先擦上一张卡片区域——换位不残留）。"""
        from core.classroom_coach import _wrap_text, _clamp_px, _screen_geometry
        try:
            sw, sh = _screen_geometry()
            if getattr(self, "_last_card_rect", None):
                old = self._last_card_rect
                self._overlay.clear_region(
                    max(0.0, old["x"] - 0.01), max(0.0, old["y"] - 0.01),
                    old["w"] + 0.02, old["h"] + 0.06)
            self._last_card_rect = dict(rect)
            rx, ry, rw, rh = (rect.get("x", 0.54), rect.get("y", 0.03),
                              rect.get("w", 0.44), rect.get("h", 0.32))
            title_size = _clamp_px(sh * 0.024, 16, 26)
            body_size = _clamp_px(sh * 0.016, 13, 19)
            chars_per_line = max(12, int(rw * sw / body_size))
            lines = _wrap_text(step["speech"][:STEP_SPEECH_MAX], chars_per_line)
            ops = [
                {"op": "paint_rect", "x": int(rx * sw), "y": int(ry * sh),
                 "w": int(rw * sw), "h": int(0.055 * sh), "color": "#1b3a6b"},
                {"op": "write_text", "x": int((rx + 0.012) * sw), "y": int((ry + 0.012) * sh),
                 "text": f"AI 教师讲题：{step['title']}"[:40],
                 "size": title_size, "color": "#ffffff"},
                {"op": "paint_rect", "x": int(rx * sw), "y": int((ry + 0.055) * sh),
                 "w": int(rw * sw), "h": int((rh - 0.055) * sh), "color": "#F2FFFFFF"},
            ]
            y = int((ry + 0.077) * sh)
            for line in lines[:8]:
                ops.append({"op": "write_text", "x": int((rx + 0.012) * sw), "y": y,
                            "text": line, "size": body_size, "color": "#222222"})
                y += int(0.032 * sh)
            ops.append({"op": "write_text", "x": int((rx + 0.012) * sw),
                        "y": int((ry + rh - 0.028) * sh),
                        "text": "自动逐讲 · 可关闭", "size": 12, "color": "#888888"})
            self._overlay.apply_ops(ops, screen_w=sw, screen_h=sh)
        except Exception as e:
            logger.warning(f"讲解板书失败（不影响朗读）: {str(e)[:120]}")

    def _on_tts_state(self, state: str):
        if state == "idle" and self._playing and not self._paused:
            self._advance()
            return
        if state == "error" and self._playing and not self._paused:
            # F5 修复：TTS 报错时原实现**什么都不做** → 自动连播就此停下，而界面没有任何提示
            # （LectureView 也没订阅 error 信号），用户只看到"卡在第 N 步"。
            # 这里如实上报原因；**不自动跳到下一步**——学生什么都没听到，
            # 静默推进等于伪造教学进度。置 _tts_failed，使「继续」变成**重试本步**而非跳过。
            self._tts_failed = True
            logger.warning("讲解朗读失败（TTS error），已停在第 %s 步", self._step_idx + 1)
            try:
                self.error.emit(
                    f"配音失败，已停在第 {self._step_idx + 1} 步："
                    "请检查小米 TTS 密钥/网络，然后点「继续」重试本步。"
                )
            except Exception:
                pass

    def pause(self):
        self._paused = True
        try:
            self._tts.stop()
        except Exception:
            pass

    def resume(self):
        if not self._playing:
            return
        self._paused = False
        # F5：上一次朗读失败过 → **重试当前步**，而不是 _advance() 直接跳过去。
        # 若照旧前进，学生会在"什么都没听到"的情况下被推进到下一步（等于伪造进度）。
        if getattr(self, "_tts_failed", False):
            self._tts_failed = False
            if 0 <= self._step_idx < len(self._steps):
                step = self._steps[self._step_idx]
                try:
                    self._tts.speak(step["speech"], style_instruction="像老师讲课一样，口语自然")
                    return
                except Exception as e:
                    logger.warning("重试朗读失败: %s", str(e)[:120])
                    self._tts_failed = True
                    try:
                        self.error.emit(f"重试配音仍失败：{str(e)[:120]}")
                    except Exception:
                        pass
                    return
        self._advance()

    def stop(self):
        self._playing = False
        self._paused = False
        self._tts_failed = False
        try:
            self._tts.stop()
        except Exception:
            pass
        # 顺序很重要（2026-09-11 自审修正）：**必须先 _clear_board() 再 hide_overlay()**。
        # _clear_board 走 apply_ops → ScreenPaintOverlay._repaint()，而 _repaint 里有
        # `if not self.isVisible(): self.show_overlay()` —— 先隐藏再清空，会把刚隐藏的窗口
        # 由 _repaint 自动 show() 回来，hide_overlay 等于没生效（我 R9 那次接线的原顺序就是错的）。
        # 注意：hide 挂在显式 stop() 上而非 lecture_finished —— 正常讲完保留最后一块板书便于回看。
        self._clear_board()
        try:
            self._overlay.hide_overlay()
        except Exception:
            pass
        self.lecture_finished.emit()

    def shutdown(self):
        self.stop()
        try:
            self._tts.shutdown()
        except Exception:
            pass

    def _clear_board(self):
        try:
            self._overlay.apply_ops([{"op": "clear"}], screen_w=1, screen_h=1)
        except Exception:
            pass


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:180]}"
