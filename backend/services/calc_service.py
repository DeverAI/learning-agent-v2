"""安全符号计算器服务

供 AI 在解题推理过程中调用的内部工具：
- 支持基础四则运算、幂、开方、三角函数、对数、阶乘、绝对值、gcd/lcm
- 保留符号：pi、e、sqrt(n) 等
- 有理数结果自动化为最简分数
- 单轮对话内支持 prev_result 引用上一次结果
"""

import ast
import math
import re
import time
from fractions import Fraction
from logger import get_logger

logger = get_logger()

MAX_EXPR_LEN = 400
MAX_RESULT_LEN = 200
MAX_FACTORIAL = 50
MAX_POWER_EXP_ABS = 1000          # 幂运算指数绝对值上限
MAX_POWER_DIGITS = 10000          # 幂运算结果十进制位数上限
MAX_SESSION_CACHE = 1000          # 全局会话缓存上限

_ALLOWED_BIN_OPS = {
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow
}
_ALLOWED_UNARY_OPS = {ast.UAdd, ast.USub}
_ALLOWED_FUNCS = {
    "sqrt", "sin", "cos", "tan", "asin", "acos", "atan",
    "log", "ln", "abs", "factorial", "gcd", "lcm",
}


class CalculatorError(Exception):
    pass


class _SymbolExpr:
    """轻量级符号表达式，保留 pi/e/sqrt 等无法化为简单分数的结果。"""

    def __init__(self, latex: str, value=None):
        self.latex = latex
        self.value = value

    def __repr__(self):
        return f"_SymbolExpr({self.latex})"


class _CalculatorSession:
    """单轮对话内的计算器会话，支持 prev_result 引用。"""

    def __init__(self):
        self.last_result = None
        self.call_count = 0

    def evaluate(self, expr: str) -> str:
        if not expr or not isinstance(expr, str):
            raise CalculatorError("表达式为空或类型错误")
        if len(expr) > MAX_EXPR_LEN:
            raise CalculatorError("表达式过长")

        self.call_count += 1
        # 安全替换 prev_result 为上一次的完整结果
        if "prev_result" in expr:
            if self.last_result is None:
                raise CalculatorError("prev_result 没有前值可用")
            if not _is_safe_result_for_substitution(self.last_result):
                raise CalculatorError("prev_result 前值包含不安全字符，无法引用")
            expr = expr.replace("prev_result", f"({self.last_result})")
            if len(expr) > MAX_EXPR_LEN:
                raise CalculatorError("替换 prev_result 后表达式过长")

        result = _eval_expr(expr)
        result_str = _format_result(result)
        # last_result 保留完整结果，只对外返回做截断
        self.last_result = result_str
        if len(result_str) > MAX_RESULT_LEN:
            return result_str[:MAX_RESULT_LEN] + "..."
        return result_str


def _is_safe_result_for_substitution(result: str) -> bool:
    """检查结果字符串是否只包含可安全拼接到表达式中的字符。"""
    if not result:
        return False
    # 允许数字、空格、基本运算符、括号、sqrt、pi、e、/、.、-、_ 等
    return bool(re.fullmatch(r"[\d\s\+\-\*/\(\)\^\!\.a-zA-Z_]+", result))


def _normalize_expr(expr: str) -> str:
    """规范化表达式：统一一些常见写法，便于 AI 输入。"""
    # 去掉 $ 和多余空白
    expr = expr.replace("$", "").strip()
    # 替换 Unicode 符号：√2 -> sqrt(2)，√(x) -> sqrt(x)
    expr = _replace_sqrt_unicode(expr)
    expr = expr.replace("π", "pi")
    expr = expr.replace("×", "*").replace("÷", "/")
    expr = expr.replace("^", "**")
    expr = expr.replace("\u00b2", "**2").replace("\u00b3", "**3")
    expr = expr.replace("{", "(").replace("}", ")")
    return expr


def _replace_sqrt_unicode(expr: str) -> str:
    """将 Unicode 根号 √ 替换为 sqrt(...)。处理 √数字、√(表达式) 两种情况。"""
    result = []
    i = 0
    while i < len(expr):
        if expr[i] == "√":
            j = i + 1
            # 跳过空白
            while j < len(expr) and expr[j].isspace():
                j += 1
            if j >= len(expr):
                result.append("sqrt")
                i += 1
                continue
            if expr[j] == "(":
                # 找到匹配的右括号
                depth = 1
                k = j + 1
                while k < len(expr) and depth > 0:
                    if expr[k] == "(":
                        depth += 1
                    elif expr[k] == ")":
                        depth -= 1
                    k += 1
                result.append("sqrt")
                result.append(expr[j:k])
                i = k
            else:
                # 读取连续数字/小数/字母
                k = j
                while k < len(expr) and (expr[k].isalnum() or expr[k] in "._"):
                    k += 1
                result.append(f"sqrt({expr[j:k]})")
                i = k
        else:
            result.append(expr[i])
            i += 1
    return "".join(result)


def _format_result(value) -> str:
    """将计算结果格式化为字符串：优先最简分数，其次符号，其次浮点。"""
    if isinstance(value, _SymbolExpr):
        return value.latex
    if isinstance(value, Fraction):
        if value.denominator == 1:
            return str(value.numerator)
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value):
            # 尝试化为分数
            try:
                frac = Fraction(value).limit_denominator(10000)
                if abs(float(frac) - value) < 1e-9:
                    return _format_result(frac)
            except Exception:
                pass
            return str(value)
        raise CalculatorError("计算结果为非有限值")
    return str(value)


def _eval_expr(expr: str):
    """安全求值表达式，返回 Fraction、int、float 或 _SymbolExpr。"""
    expr = _normalize_expr(expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise CalculatorError(f"表达式语法错误: {e}")
    return _eval_node(tree.body)


def _eval_node(node):
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BIN_OPS:
            raise CalculatorError(f"不允许的运算符: {type(node.op).__name__}")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _apply_bin_op(left, right, node.op)

    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _ALLOWED_UNARY_OPS:
            raise CalculatorError(f"不允许的一元运算符: {type(node.op).__name__}")
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.USub):
            return _apply_unary_neg(operand)

    if isinstance(node, ast.Call):
        return _eval_call(node)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise CalculatorError("布尔值不允许作为数学常量")
        if isinstance(node.value, (int, float)):
            return node.value
        raise CalculatorError(f"不支持的常量类型: {type(node.value).__name__}")

    if isinstance(node, ast.Name):
        name = node.id
        if name == "pi":
            return _SymbolExpr("pi", math.pi)
        if name == "e":
            return _SymbolExpr("e", math.e)
        if name == "prev_result":
            raise CalculatorError("prev_result 只能在会话上下文中使用")
        raise CalculatorError(f"未定义的标识符: {name}")

    raise CalculatorError(f"不支持的表达式节点: {type(node).__name__}")


def _to_number(value):
    """把符号或数值转为可运算对象。"""
    if isinstance(value, _SymbolExpr):
        if value.value is not None:
            return value.value
        raise CalculatorError(f"无法对符号 {value.latex} 进行数值运算")
    if isinstance(value, Fraction):
        return float(value)
    return value


def _apply_unary_neg(value):
    if isinstance(value, _SymbolExpr):
        if value.value is not None:
            return _SymbolExpr(f"-{value.latex}", -value.value)
        return _SymbolExpr(f"-{value.latex}")
    if isinstance(value, Fraction):
        return -value
    return -value


def _apply_bin_op(left, right, op):
    # 只要任一边是符号表达式，结果就尽量保留符号
    left_sym = isinstance(left, _SymbolExpr)
    right_sym = isinstance(right, _SymbolExpr)

    if left_sym or right_sym:
        # 尝试数值运算；若结果为简洁有理数/整数，直接返回数值
        try:
            ln = _to_number(left)
            rn = _to_number(right)
            res = None
            if isinstance(op, ast.Add):
                res = ln + rn
            elif isinstance(op, ast.Sub):
                res = ln - rn
            elif isinstance(op, ast.Mult):
                res = ln * rn
            elif isinstance(op, ast.Div):
                if rn == 0:
                    raise CalculatorError("除零错误")
                res = ln / rn
            elif isinstance(op, ast.Pow):
                res = ln ** rn
            else:
                raise CalculatorError("该运算符不支持符号表达式")
            # 如果数值结果是简洁有理数，返回精确值
            try:
                frac = Fraction(res).limit_denominator(10000)
                if abs(float(frac) - res) < 1e-9 and frac.denominator <= 1000:
                    return frac
            except Exception:
                pass
            # 否则保留符号表示
            left_repr = left.latex if isinstance(left, _SymbolExpr) else _format_result(left)
            right_repr = right.latex if isinstance(right, _SymbolExpr) else _format_result(right)
            if isinstance(op, ast.Add):
                sym = f"{left_repr}+{right_repr}"
            elif isinstance(op, ast.Sub):
                sym = f"{left_repr}-{right_repr}"
            elif isinstance(op, ast.Mult):
                sym = f"{left_repr}*{right_repr}"
            elif isinstance(op, ast.Div):
                sym = f"({left_repr})/({right_repr})"
            elif isinstance(op, ast.Pow):
                sym = f"({left_repr})^({right_repr})"
            else:
                sym = f"({left_repr})?({right_repr})"
            return _SymbolExpr(sym, res)
        except CalculatorError:
            # 除零等确定性错误必须上报，不能伪装成符号结果继续推理
            raise
        except (OverflowError, TypeError, ValueError, ZeroDivisionError, ArithmeticError):
            # 数值上溢/复数等无法数值化的情况才退回保守符号表示
            op_sym = "?"
            if isinstance(op, ast.Add): op_sym = "+"
            elif isinstance(op, ast.Sub): op_sym = "-"
            elif isinstance(op, ast.Mult): op_sym = "*"
            elif isinstance(op, ast.Div): op_sym = "/"
            elif isinstance(op, ast.Pow): op_sym = "^"
            left_repr = left.latex if isinstance(left, _SymbolExpr) else _format_result(left)
            right_repr = right.latex if isinstance(right, _SymbolExpr) else _format_result(right)
            return _SymbolExpr(f"({left_repr}){op_sym}({right_repr})")

    # 纯数值运算：优先使用 Fraction 保持精确
    left_frac = _to_fraction(left)
    right_frac = _to_fraction(right)

    if isinstance(op, ast.Add):
        return left_frac + right_frac
    if isinstance(op, ast.Sub):
        return left_frac - right_frac
    if isinstance(op, ast.Mult):
        return left_frac * right_frac
    if isinstance(op, ast.Div):
        if right_frac == 0:
            raise CalculatorError("除零错误")
        return left_frac / right_frac
    if isinstance(op, ast.FloorDiv):
        if right_frac == 0:
            raise CalculatorError("除零错误")
        return left_frac // right_frac
    if isinstance(op, ast.Mod):
        if right_frac == 0:
            raise CalculatorError("除零错误")
        return left_frac % right_frac
    if isinstance(op, ast.Pow):
        return _apply_pow(left_frac, right_frac)

    raise CalculatorError(f"不支持的运算符: {type(op).__name__}")


def _apply_pow(left_frac: Fraction, right_frac: Fraction):
    """安全执行幂运算，防止资源耗尽和非法复数结果。"""
    # 0 的负指数 = 除零
    if left_frac == 0 and right_frac < 0:
        raise CalculatorError("0 的负指数无意义")

    # 整数指数尽量保持分数精确
    try:
        exp = int(right_frac)
        if right_frac == exp:
            if abs(exp) > MAX_POWER_EXP_ABS:
                raise CalculatorError(f"指数绝对值过大（最大 {MAX_POWER_EXP_ABS}）")
            # 估算结果位数，同时考虑分子和分母
            if left_frac != 0 and exp != 0:
                num_digits = len(str(abs(left_frac.numerator)))
                den_digits = len(str(abs(left_frac.denominator)))
                base_digits = max(1, num_digits, den_digits)
                est_digits = base_digits * abs(exp)
                if est_digits > MAX_POWER_DIGITS:
                    raise CalculatorError("幂运算结果位数过大，拒绝计算")
            return left_frac ** exp
    except CalculatorError:
        raise
    except Exception:
        pass

    # 非整数指数：底数必须为非负数，否则会得到复数
    ln = float(left_frac)
    rn = float(right_frac)
    if ln < 0:
        raise CalculatorError("负数不能进行非整数次幂运算")
    return ln ** rn


def _to_fraction(value):
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        return Fraction(1 if value else 0)
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value).limit_denominator(10000)
    raise CalculatorError(f"无法转换为分数: {value}")


def _eval_call(node: ast.Call):
    if not isinstance(node.func, ast.Name):
        raise CalculatorError("只支持简单函数调用")
    func_name = node.func.id
    if func_name not in _ALLOWED_FUNCS:
        raise CalculatorError(f"未定义的函数: {func_name}")

    args = [_eval_node(a) for a in node.args]
    if any(isinstance(a, _SymbolExpr) for a in args):
        raise CalculatorError(f"函数 {func_name} 暂不支持符号参数")

    nums = [_to_number(_to_fraction(a)) for a in args]

    if func_name == "sqrt":
        if len(nums) != 1:
            raise CalculatorError("sqrt 需要 1 个参数")
        v = nums[0]
        if v < 0:
            raise CalculatorError("sqrt 不支持负数")
        # 尝试化为根式符号
        try:
            frac = Fraction(v).limit_denominator(10000)
            if frac.denominator == 1:
                n = frac.numerator
                sq = int(math.isqrt(n))
                if sq * sq == n:
                    return sq
                # 化简 sqrt(n)：提取完全平方因子
                a, b = _simplify_sqrt(n)
                if a == 1:
                    return _SymbolExpr(f"sqrt({n})", math.sqrt(n))
                if b == 1:
                    return a
                return _SymbolExpr(f"{a}*sqrt({b})", a * math.sqrt(b))
        except Exception:
            pass
        return _SymbolExpr(f"sqrt({v})", math.sqrt(v))

    if func_name == "sin":
        if len(nums) != 1:
            raise CalculatorError("sin 需要 1 个参数")
        return math.sin(nums[0])
    if func_name == "cos":
        if len(nums) != 1:
            raise CalculatorError("cos 需要 1 个参数")
        return math.cos(nums[0])
    if func_name == "tan":
        if len(nums) != 1:
            raise CalculatorError("tan 需要 1 个参数")
        return math.tan(nums[0])
    if func_name == "asin":
        if len(nums) != 1:
            raise CalculatorError("asin 需要 1 个参数")
        if nums[0] < -1 or nums[0] > 1:
            raise CalculatorError("asin 参数必须在 [-1, 1] 范围内")
        return math.asin(nums[0])
    if func_name == "acos":
        if len(nums) != 1:
            raise CalculatorError("acos 需要 1 个参数")
        if nums[0] < -1 or nums[0] > 1:
            raise CalculatorError("acos 参数必须在 [-1, 1] 范围内")
        return math.acos(nums[0])
    if func_name == "atan":
        if len(nums) != 1:
            raise CalculatorError("atan 需要 1 个参数")
        return math.atan(nums[0])
    if func_name == "log":
        if len(nums) == 1:
            if nums[0] <= 0:
                raise CalculatorError("log 参数必须大于 0")
            return math.log(nums[0])
        if len(nums) == 2:
            if nums[0] <= 0:
                raise CalculatorError("log 真数必须大于 0")
            if nums[1] <= 0 or nums[1] == 1:
                raise CalculatorError("log 底数必须大于 0 且不等于 1")
            return math.log(nums[0], nums[1])
        raise CalculatorError("log 需要 1 或 2 个参数")
    if func_name == "ln":
        if len(nums) != 1:
            raise CalculatorError("ln 需要 1 个参数")
        if nums[0] <= 0:
            raise CalculatorError("ln 参数必须大于 0")
        return math.log(nums[0])
    if func_name == "abs":
        if len(nums) != 1:
            raise CalculatorError("abs 需要 1 个参数")
        v = nums[0]
        if isinstance(v, Fraction):
            return abs(v)
        return abs(v)
    if func_name == "factorial":
        if len(nums) != 1:
            raise CalculatorError("factorial 需要 1 个参数")
        n = nums[0]
        if not float(n).is_integer() or n < 0:
            raise CalculatorError("factorial 只接受非负整数")
        n = int(n)
        if n > MAX_FACTORIAL:
            raise CalculatorError(f"factorial 参数过大（最大 {MAX_FACTORIAL}）")
        return math.factorial(n)
    if func_name == "gcd":
        if len(nums) < 2:
            raise CalculatorError("gcd 至少需要 2 个参数")
        for v in nums:
            if not float(v).is_integer():
                raise CalculatorError("gcd 参数必须是整数")
        vals = [int(v) for v in nums]
        return math.gcd(*vals)
    if func_name == "lcm":
        if len(nums) < 2:
            raise CalculatorError("lcm 至少需要 2 个参数")
        for v in nums:
            if not float(v).is_integer():
                raise CalculatorError("lcm 参数必须是整数")
        vals = [abs(int(v)) for v in nums]
        if 0 in vals:
            return 0
        res = vals[0]
        for v in vals[1:]:
            res = res * v // math.gcd(res, v)
        return res

    raise CalculatorError(f"未实现的函数: {func_name}")


def _simplify_sqrt(n: int):
    """将 sqrt(n) 化简为 a*sqrt(b) 形式，返回 (a, b)。"""
    if n <= 0:
        return (0, 0)
    a = 1
    b = n
    i = 2
    while i * i <= b:
        while b % (i * i) == 0:
            a *= i
            b //= i * i
        i += 1
    return (a, b)


# 全局会话管理：按调用标识（如 question_id 或临时 key）存储
_sessions: dict[str, _CalculatorSession] = {}


def get_session(key: str) -> _CalculatorSession:
    """获取或创建一个会话。key 建议使用 question_id 或一次性 uuid。"""
    if key not in _sessions:
        # 简单 LRU：超过上限时清空最老的会话
        if len(_sessions) >= MAX_SESSION_CACHE:
            try:
                oldest = next(iter(_sessions))
                _sessions.pop(oldest, None)
            except Exception:
                pass
        _sessions[key] = _CalculatorSession()
    return _sessions[key]


def clear_session(key: str):
    _sessions.pop(key, None)


def evaluate_in_session(key: str, expr: str) -> dict:
    """供外部调用的统一入口，返回 {success, result, error, call_count}。"""
    session = get_session(key)
    try:
        result = session.evaluate(expr)
        return {
            "success": True,
            "result": result,
            "error": None,
            "call_count": session.call_count,
        }
    except CalculatorError as e:
        logger.warning("Calculator error for expr '%s': %s", expr, e)
        return {"success": False, "result": None, "error": str(e), "call_count": session.call_count}
    except Exception as e:
        logger.exception("Calculator unexpected error for expr '%s'", expr)
        return {"success": False, "result": None, "error": f"计算异常: {e}", "call_count": session.call_count}


def evaluate(expr: str) -> dict:
    """无状态单次计算（不保留 prev_result）：使用一次性会话并在计算后立即清除。"""
    key = f"__stateless__{time.time_ns()}"
    try:
        return evaluate_in_session(key, expr)
    finally:
        clear_session(key)


# Function Calling 工具定义
CALCULATOR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": (
            "精确数学计算器。用于解题过程中的中间计算。"
            "支持 + - * / // % **、sqrt、sin/cos/tan/asin/acos/atan、log/ln、abs、factorial、gcd/lcm。"
            "使用 pi、e 表示圆周率和自然常数。"
            "如需引用上一次计算结果，在表达式中写 prev_result。"
            "结果保留符号（如 sqrt(2)、pi）和最简分数。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式，例如 'sqrt(2)/2' 或 'prev_result + 1/3'",
                }
            },
            "required": ["expression"],
        },
    },
}


def calculator_tool_call(arguments: dict, session_key: str = "") -> dict:
    """Function calling 工具调用入口。session_key 为空时使用一次性无状态会话。"""
    expr = arguments.get("expression", "") if isinstance(arguments, dict) else ""
    if not session_key:
        return evaluate(expr)
    return evaluate_in_session(session_key, expr)
