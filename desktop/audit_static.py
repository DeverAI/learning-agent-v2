"""OISystem 静态端口检修工具（audit_static.py）。

不修改任何源码；扫描 live 代码（core/config/ui/utils/main/watchdog）并输出：
  1. 所有 `.connect(self.<method>)` / `.connect(<obj>.<method>)` 的槽函数是否真实存在；
  2. 源码中的占位/未实现/TODO 注释行；
  3. AppSettings 每个字段在非 settings/非测试/非备份代码中的消费次数（有口没码检测）；
  4. 声明但从未 emit 的 Qt Signal、体为 pass 或恒等常量的疑似 stub 函数；
  5. 被 import 但疑似不存在的模块/属性（轻量检查，误报以 grep 复核为准）。

运行：python audit_static.py
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_DIRS = ("core", "config", "ui", "utils")
LIVE_FILES = ("main.py", "watchdog.py")


def live_py_files():
    out = []
    for d in LIVE_DIRS:
        for base, dirs, files in os.walk(os.path.join(ROOT, d)):
            dirs[:] = [x for x in dirs if x not in ("__pycache__",)]
            for fn in sorted(files):
                if fn.endswith(".py") and not fn.endswith("_backup.py"):
                    out.append(os.path.join(base, fn))
    for fn in LIVE_FILES:
        p = os.path.join(ROOT, fn)
        if os.path.exists(p):
            out.append(p)
    return out


def read_src(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def iter_classes(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node


def class_methods(node):
    out = {}
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[stmt.name] = stmt
    return out


def all_defined_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def audit_slots():
    print("=" * 70)
    print("A. 槽函数存在性检查（.connect(...self.<method>)）")
    print("=" * 70)
    problems = 0
    files = live_py_files()
    for path in files:
        src = read_src(path)
        tree = ast.parse(src)
        file_classes = {}
        for cls in iter_classes(tree):
            file_classes[cls.name] = class_methods(cls)
        # 类内 self._method 连接：逐类校验
        for cls_name, methods in file_classes.items():
            # 找该类在源码中的 def 块并切出大致文本（简化：用类节点的行号范围）
            for cls in iter_classes(tree):
                if cls.name != cls_name:
                    continue
                seg = ast.get_source_segment(src, cls) or src
                for m in re.finditer(r"\.connect\(\s*self\.([A-Za-z_]\w*)\s*\)", seg):
                    method = m.group(1)
                    if method not in methods:
                        # 可能是父类方法：列出待复核
                        print(f"  [待复核] {path}:{cls_name}.connect(self.{method}) 类内未定义（可能来自父类）")
                        problems += 1
        # 模块级/实例对象 obj.method 连接
        for m in re.finditer(r"\.connect\(\s*(\w+)\.([A-Za-z_]\w*)\s*\)", src):
            obj, method = m.group(1), m.group(2)
            if obj in ("self", "QTimer", "QApplication"):
                continue
            print(f"  [信息] {path}:{m.start(0)} connect({obj}.{method})")
    print(f"A 类待复核数: {problems}")


def audit_placeholder_comments():
    print("=" * 70)
    print("B. 占位/未实现/TODO 注释")
    print("=" * 70)
    pat = re.compile(r"#.*(TODO|FIXME|XXX|占位|暂未|未实现|待接入|待后续|留待|stub)", re.IGNORECASE)
    n = 0
    for path in live_py_files():
        for i, line in enumerate(read_src(path).splitlines(), 1):
            if pat.search(line):
                print(f"  {os.path.relpath(path, ROOT)}:{i}: {line.strip()}")
                n += 1
    print(f"B 命中行数: {n}")


def audit_config_consumers():
    print("=" * 70)
    print("C. AppSettings 字段消费次数（只统计 live 非 settings/测试代码）")
    print("=" * 70)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cfg_scan", os.path.join(ROOT, "config", "settings.py"))
    mod = importlib.util.module_from_spec(spec)
    # 不执行：直接 AST 提取字段名
    src = read_src(os.path.join(ROOT, "config", "settings.py"))
    tree = ast.parse(src)
    fields = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AppSettings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
    text_by_file = {}
    for path in live_py_files():
        if "settings.py" in os.path.basename(path):
            continue
        if "test_" in os.path.basename(path):
            continue
        text_by_file[path] = read_src(path)
    zero = []
    for f in fields:
        cnt = sum(t.count(f) for t in text_by_file.values())
        mark = "" if cnt else "  <-- 疑似死配置"
        print(f"  {f}: {cnt}{mark}")
        if cnt == 0:
            zero.append(f)
    print(f"C 疑似死配置: {zero}")


def audit_stub_functions():
    print("=" * 70)
    print("D. 疑似 stub 函数（体为空 / 恒定返回值 / NotImplemented）")
    print("=" * 70)
    n = 0
    for path in live_py_files():
        src = read_src(path)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") and node.name in ("__init__", "__new__", "__repr__"):
                continue
            body = [s for s in node.body if not isinstance(s, ast.Expr) or not isinstance(s.value, ast.Constant)]
            body_text = ast.unparse(node).lower() if hasattr(ast, "unparse") else ""
            if "notimplemented" in body_text:
                print(f"  {os.path.relpath(path, ROOT)}:{node.lineno} {node.name}: NotImplemented")
                n += 1
            elif len(body) == 0:
                print(f"  {os.path.relpath(path, ROOT)}:{node.lineno} {node.name}: 空函数体")
                n += 1
            elif len(body) == 1 and isinstance(body[0], ast.Return):
                v = body[0].value
                if isinstance(v, ast.Constant) and v.value in (None, False, True, 0, ""):
                    print(f"  {os.path.relpath(path, ROOT)}:{node.lineno} {node.name}: 恒返回 {v.value!r}")
                    n += 1
    print(f"D 疑似 stub 数: {n}")


def audit_signal_emit():
    print("=" * 70)
    print("E. Signal 声明与 emit 交叉检查（类内粗略）")
    print("=" * 70)
    for path in live_py_files():
        src = read_src(path)
        tree = ast.parse(src)
        for cls in iter_classes(tree):
            for node in cls.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    ann = node.annotation
                    ann_txt = ""
                    try:
                        ann_txt = ast.unparse(ann)
                    except Exception:
                        ann_txt = ""
                    if "Signal" in ann_txt:
                        name = node.target.id
                        seg = ast.get_source_segment(src, cls) or ""
                        if f".emit(" not in seg or f"{name}.emit(" not in seg:
                            print(f"  [信息] {os.path.relpath(path, ROOT)}:{node.lineno} Signal {name} 类内未 emit")


def audit_imports():
    print("=" * 70)
    print("F. 可疑 import（模块/属性存在性）")
    print("=" * 70)
    import importlib
    problems = 0
    sys.path.insert(0, ROOT)
    for path in live_py_files():
        tree = ast.parse(read_src(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith("."):
                    continue
                if mod.split(".")[0] not in ("core", "config", "ui", "utils", "PySide6", "typing", "dataclasses"):
                    continue
                try:
                    m = importlib.import_module(mod)
                except Exception as e:
                    print(f"  [错误] {os.path.relpath(path, ROOT)}:{node.lineno} from {mod} import ... -> {e}")
                    problems += 1
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if not hasattr(m, alias.name):
                        print(f"  [待复核] {os.path.relpath(path, ROOT)}:{node.lineno} from {mod} import {alias.name}")
                        problems += 1
    print(f"F 可疑 import 数: {problems}")


def main():
    audit_slots()
    audit_placeholder_comments()
    audit_config_consumers()
    audit_stub_functions()
    audit_signal_emit()
    audit_imports()
    print("审计完成")


if __name__ == "__main__":
    main()
