#!/usr/bin/env python3
"""把单体类的实例方法拆到若干 mixin 子模块，并用 AST 重建原类继承全部 mixin。

用法：
    python tools/split_class.py <src.py> <class_name> <mixin_pkg_dir> <mapping.json>

mapping.json 形如：
    {
      "ShellMixin": ["_build_layout", "..."],
      ...
    }

行为：
  * 找到 <class_name> 类，把 mapping 中列出的方法搬到对应 mixin 模块的类里；
  * 每个 mixin 带原模块顶层 import（相对层级 +1），并自动补上方法引用的模块级常量；
  * 生成 <pkg_dir>/__init__.py 回导出各 mixin；
  * 重建原文件：类头改为继承所有 mixin + 原基类，类内仅保留未移动的方法（__init__），
    原文件在类之后的模块级函数（如 run_gui）保持不变。
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys

from split_package import _adjust_import, _collect_load_names, _node_text


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _module_level_imports(tree: ast.Module, src_lines: list[str]) -> tuple[list[str], list[str], dict[str, ast.AST]]:
    import_lines: list[str] = []
    try_blocks: list[str] = []
    consts: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.append(_adjust_import(node))
        elif isinstance(node, ast.Try):
            try_blocks.append(_node_text(src_lines, node))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            consts[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node
    return import_lines, try_blocks, consts


def _class_def(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise ValueError(f"找不到类：{name}")


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def run(src_path: str, class_name: str, pkg_dir: str, mapping: dict[str, list[str]]) -> list[str]:
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=src_path)
    src_lines = src.splitlines()
    cls = _class_def(tree, class_name)
    methods = _methods(cls)

    mixin_of: dict[str, str] = {}
    for mixin, names in mapping.items():
        for name in names:
            if name not in methods:
                raise ValueError(f"{class_name} 没有方法 {name}")
            mixin_of[name] = mixin

    import_lines, try_blocks, consts = _module_level_imports(tree, src_lines)
    os.makedirs(pkg_dir, exist_ok=True)

    written: list[str] = []
    mixin_files: dict[str, str] = {}
    for mixin, names in mapping.items():
        refs: set[str] = set()
        for name in names:
            refs |= _collect_load_names(methods[name])
        const_src: list[str] = []
        for ref in sorted(refs):
            if ref in consts:
                const_src.append(_node_text(src_lines, consts[ref]))
        used_ctk = "ctk" in refs

        header: list[str] = ["# 由 tools/split_class.py 拆分生成", "from __future__ import annotations"]
        if import_lines:
            header.append("\n".join(import_lines))
        if try_blocks and used_ctk:
            header.append("\n".join(try_blocks))
        if const_src:
            header.append("\n".join(const_src))

        method_text = "\n\n".join(_node_text(src_lines, methods[name]) for name in names)
        mod_name = _snake(mixin)
        mixin_files[mixin] = mod_name
        content = "\n\n".join(header) + "\n\n\nclass " + mixin + ":\n" + method_text + "\n"
        out = os.path.join(pkg_dir, mod_name + ".py")
        with open(out, "w", encoding="utf-8", newline="\n") as out_fh:
            out_fh.write(content)
        written.append(out)

    init_content = (
        "# 由 tools/split_class.py 拆分生成\n"
        '"""视图 mixin 包：App 通过继承聚合各 tab 的实现。"""\n'
        "from __future__ import annotations\n\n"
        + "".join(f"from .{mixin_files[m]} import {m}\n" for m in mapping)
        + f"\n__all__ = {list(mapping)!r}\n"
    )
    init_path = os.path.join(pkg_dir, "__init__.py")
    with open(init_path, "w", encoding="utf-8", newline="\n") as out_fh:
        out_fh.write(init_content)
    written.append(init_path)

    # 重建原文件
    lines = src.splitlines()
    head = lines[: cls.lineno - 1]
    init_lines = _node_text(src_lines, methods["__init__"]).split("\n") if "__init__" in methods else []
    run_gui_lines: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno > cls.end_lineno:
            run_gui_lines = _node_text(src_lines, node).split("\n")

    base_text = ast.unparse(cls.bases[0]) if cls.bases else "object"
    class_header = f"class {cls.name}({', '.join(list(mapping) + [base_text])}):"
    reexport = "from .views import " + ", ".join(list(mapping))

    rebuilt = list(head)
    rebuilt.append(reexport)
    rebuilt.append("")
    rebuilt.append(class_header)
    rebuilt.extend(init_lines)
    if run_gui_lines:
        rebuilt.append("")
        rebuilt.extend(run_gui_lines)

    with open(src_path, "w", encoding="utf-8", newline="\n") as out_fh:
        out_fh.write("\n".join(rebuilt) + "\n")
    print("REBUILT", src_path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(__doc__)
        return 2
    src_path, class_name, pkg_dir, mapping_path = argv[1:5]
    with open(mapping_path, encoding="utf-8") as fh:
        mapping = json.load(fh)
    for path in run(src_path, class_name, pkg_dir, mapping):
        print("WROTE", os.path.relpath(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
