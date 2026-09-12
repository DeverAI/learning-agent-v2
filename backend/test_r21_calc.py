# -*- coding: utf-8 -*-
"""R21：计算器的**单位与底数约定**必须被钉住。

## 为什么专门测这个

`calc_service` 的 sin/cos/tan 走 `math.sin` 即**弧度制**，`log`/`ln` **都是自然对数**。
而初中几何题里的角几乎都是度数（30°/45°/60°）。此前工具 schema 只写"支持 sin/cos/tan、log/ln"，
**不写单位、不写底数** —— 模型很可能写 `sin(30)` 期望 0.5，实际拿到 -0.9880316240928618，
而这个错值会直接进入给学生的解题步骤。这是"教错学生"，比功能没做严重。

## 同仓库的另一套约定（别互相看齐）

`coord_engine.eval_expression` 服务于图纸坐标，用的是**度数制**（`sin(30)` 就是 0.5）。
两处约定不同，各自都在 docstring/说明里写明。本文件只钉 `calc_service`。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services import calc_service as C  # noqa: E402


def ev(expr):
    r = C.evaluate(expr)
    assert r.get("success") is True, f"{expr} 计算失败: {r}"
    return r


def val(expr):
    """取数值结果。结果可能是 '1/2' 这种分数串，要能解析。"""
    s = str(ev(expr)["result"]).strip()
    if "/" in s and not s.startswith("("):
        num, _, den = s.partition("/")
        return float(num) / float(den)
    return float(s)


# --------------------------------------------------------------------------
# 1. 三角函数是弧度制 —— 钉住，并给出"度数该怎么写"
# --------------------------------------------------------------------------

def test_trig_is_radians():
    """sin(30) 不是 0.5（弧度制）——这条断言防止有人把实现改成度数制却忘了改说明。"""
    got = val("sin(30)")
    assert abs(got - 0.5) > 0.4, f"sin(30) 竟然接近 0.5，说明约定被改了: {got}"
    assert abs(got - (-0.9880316240928618)) < 1e-9, got


def test_degrees_must_be_converted_explicitly():
    """三种写法都要能用，且都返回精确的 1/2。"""
    for expr in ("sin(radians(30))", "sin(30*pi/180)", "sin(pi/6)"):
        got = val(expr)
        assert abs(got - 0.5) < 1e-12, f"{expr} = {got}"


def test_symbolic_args_are_accepted():
    """`pi` 是符号对象，函数必须能取它的数值 —— 否则 sin(pi/6) 这类最自然的写法会报错。"""
    for expr, want in (("sin(pi/6)", 0.5), ("cos(pi/3)", 0.5), ("tan(pi/4)", 1.0),
                       ("degrees(pi)", 180.0)):
        got = val(expr)
        assert abs(got - want) < 1e-12, f"{expr} = {got}, 期望 {want}"


def test_inverse_trig_returns_radians_and_degrees_helper():
    assert abs(val("degrees(asin(0.5))") - 30.0) < 1e-9
    assert abs(val("asin(0.5)") - 0.5235987755982989) < 1e-12


# --------------------------------------------------------------------------
# 2. log / ln 都是自然对数；常用对数走 log10 / lg
# --------------------------------------------------------------------------

def test_log_is_natural_log_not_common_log():
    assert abs(val("log(100)") - 4.605170185988092) < 1e-9, "log 不是自然对数了？"
    assert abs(val("ln(100)") - 4.605170185988092) < 1e-9
    assert val("log(100)") != 2.0


def test_log10_and_lg_are_common_log():
    assert abs(val("log10(100)") - 2.0) < 1e-12
    assert abs(val("lg(1000)") - 3.0) < 1e-12
    # 两个名字必须等价
    assert abs(val("log10(7)") - val("lg(7)")) < 1e-15


def test_log_with_base_still_works():
    assert abs(val("log(8, 2)") - 3.0) < 1e-12


# --------------------------------------------------------------------------
# 3. 参数校验：错就报错，不能静默给个数
# --------------------------------------------------------------------------

@pytest.mark.parametrize("expr", ["log10(0)", "log10(-1)", "lg(0)"])
def test_common_log_rejects_non_positive(expr):
    r = C.evaluate(expr)
    assert r.get("success") is False, f"{expr} 竟然成功了: {r}"
    assert "大于 0" in r.get("error", "")


@pytest.mark.parametrize("expr", ["radians(1,2)", "degrees()", "log10(1,2)"])
def test_wrong_arity_is_an_error(expr):
    r = C.evaluate(expr)
    assert r.get("success") is False, f"{expr} 竟然成功了: {r}"


# --------------------------------------------------------------------------
# 4. 给模型的说明必须写明单位与底数（否则前面的实现都会被误用）
# --------------------------------------------------------------------------

def test_tool_schema_documents_units_and_bases():
    desc = C.CALCULATOR_TOOL_SCHEMA["function"]["description"]
    assert "弧度" in desc, "schema 没写三角函数用弧度"
    assert "radians" in desc, "schema 没给出角度换算的写法"
    assert "自然对数" in desc, "schema 没写 log/ln 是自然对数"
    assert "log10" in desc or "lg" in desc, "schema 没给出常用对数的写法"


def test_solve_prompt_documents_units_and_bases():
    """解题提示词里也必须写 —— 只改 schema 不够，两条路都要说明。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services", "ai_service.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    assert "sin/cos/tan 用**弧度**" in src or "sin/cos/tan 用**弧度**" in src
    assert "log 与 ln 都是**自然对数**" in src


def test_new_funcs_are_whitelisted():
    for fn in ("radians", "degrees", "log10", "lg"):
        assert fn in C._ALLOWED_FUNCS, f"{fn} 没进白名单，调用会被拒"
