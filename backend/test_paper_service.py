"""出卷逻辑链基础测试：不依赖数据库，验证参数校验与 Prompt 填充。"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.paper_service import paper_service
from config import ALLOWED_PAPER_SIZES


def _default_diagram_places(count: int) -> list[str]:
    """AI 未返回 diagram_places 时，第一张图放题面，其余放解答。"""
    if count <= 0:
        return []
    return ["question"] + ["answer"] * (count - 1)


def test_allowed_paper_sizes():
    assert "A4" in ALLOWED_PAPER_SIZES
    assert "A3" in ALLOWED_PAPER_SIZES
    assert "Letter" in ALLOWED_PAPER_SIZES
    assert "A0" not in ALLOWED_PAPER_SIZES
    assert "*" not in ALLOWED_PAPER_SIZES


def test_fill_prompt_template():
    template = {
        "content": "年级:{grade} 科目:{subject} 用户:{user_prompt} 数量:{question_count} 知识点:{knowledge_tags} 纸张:{paper_size} 答题空间:{answer_space} {questions}",
        "blocks": ["grade", "subject", "user_prompt", "question_count", "questions", "knowledge_tags", "paper_size"],
    }
    questions_data = [
        {
            "id": "q1", "subject": "数学", "grade": "八年级",
            "knowledge_tags": ["二次函数"], "ocr_text": "", "question_html": "<p>题目1</p>",
            "answer_html": "", "diagrams": [], "region": "", "avg_score": None,
            "standard_answer": "2",
        }
    ]
    params = {
        "grade": "八年级", "subject": "数学", "custom_prompt": "生成试卷", "keyword": "",
        "knowledge_tags": ["二次函数"], "paper_size": "A4", "answer_space": "inline",
        "extra_params": {"total_score": "100", "exam_duration": "90"},
    }
    filled = paper_service._fill_prompt_template(template, params, questions_data, "A4")
    assert "八年级" in filled
    assert "数学" in filled
    assert "生成试卷" in filled
    assert "A4" in filled
    assert "inline" in filled
    assert "题目1" in filled
    # 组卷 prompt 中不应暴露标准答案，防止泄漏到试卷正文
    assert "标准答案" not in filled


def test_format_questions_for_prompt_escapes_none():
    questions_data = [
        {
            "grade": "八年级", "subject": "数学", "knowledge_tags": ["函数"],
            "question_html": "", "ocr_text": "题干", "region": None, "avg_score": None,
            "standard_answer": "",
        }
    ]
    text = paper_service._format_questions_for_prompt(questions_data)
    assert "题干" in text
    assert "平均分" not in text
    assert "标准答案" not in text


def test_default_diagram_places():
    assert _default_diagram_places(0) == []
    assert _default_diagram_places(1) == ["question"]
    assert _default_diagram_places(2) == ["question", "answer"]
    assert _default_diagram_places(3) == ["question", "answer", "answer"]


def test_ai_auto_count_prompt_uses_preselected_questions_exactly_once():
    template = {"content": "数量:{question_count}\n{questions}", "blocks": []}
    questions = [
        {"id": "q1", "subject": "数学", "grade": "八年级", "knowledge_tags": ["函数"],
         "ocr_text": "第一题", "question_html": "", "answer_html": "", "diagrams": [],
         "region": "", "avg_score": None, "standard_answer": ""},
        {"id": "q2", "subject": "数学", "grade": "八年级", "knowledge_tags": ["几何"],
         "ocr_text": "第二题", "question_html": "", "answer_html": "", "diagrams": [],
         "region": "", "avg_score": None, "standard_answer": ""},
    ]
    filled = paper_service._fill_prompt_template(
        template, {"ai_auto_count": True, "extra_params": {}}, questions, "A4"
    )
    assert "数量:2" in filled
    assert "完整使用且每题只出现一次" in filled
    assert "自主挑选适量题目" not in filled


if __name__ == "__main__":
    test_allowed_paper_sizes()
    test_fill_prompt_template()
    test_format_questions_for_prompt_escapes_none()
    test_default_diagram_places()
    print("paper service unit tests passed")
