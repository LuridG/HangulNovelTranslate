#!/usr/bin/env python3
"""把单体模块按职责拆成包子模块，并生成回导出的 __init__.py。

用法：
    python tools/split_package.py <src.py> <pkg_dir> <mapping.json>

mapping.json 形如：
    {
      "submodule_name": ["TopLevelName", "..."],
      ...
    }

行为：
  * 读取 <src.py> 顶层 def/class/赋值节点；
  * 按名字把每个顶层节点分到对应子模块；
  * 每个子模块保留原文件的外部 import（相对层级 +1），并自动补上对
    同包兄弟模块 / 常量模块的显式 import；
  * 生成 __init__.py，把所有名字（含下划线开头）回导出，保持对外接口不变。
"""

from __future__ import annotations

import ast
import json
import os
import sys
from collections import OrderedDict


def _node_text(src_lines: list[str], node: ast.AST) -> str:
    """按行号摘取节点源码，并向前吸收紧邻的注释/空行。"""
    # 装饰器位于函数/类定义上方，必须一并摘取。
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        decorator_lines = [d.lineno for d in getattr(node, "decorator_list", [])]
        start = min([node.lineno] + decorator_lines) - 1
    else:
        start = node.lineno - 1
    end = node.end_lineno
    # 向前吸收紧邻的注释和空行，保持可读性。
    i = start - 1
    while i >= 0:
        stripped = src_lines[i].strip()
        if stripped == "" or stripped.startswith("#"):
            i -= 1
        else:
            break
    start = i + 1
    block = src_lines[start:end]
    return "\n".join(block)


def _defined_names(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        targets = getattr(node, "targets", None)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _collect_load_names(node: ast.AST) -> set[str]:
    """收集节点中所有以 Load 形式出现的基础名字。"""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            names.add(n.id)
        elif isinstance(n, ast.Import):
            for alias in n.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for alias in n.names:
                names.add(alias.asname or alias.name)
    return names


def _parse_imports(tree: ast.Module) -> tuple[list[ast.stmt], dict[str, ast.stmt]]:
    """顶层 import 语句列表，以及“名字 -> import 语句”映射，用于外部依赖注入。"""
    imports: list[ast.stmt] = []
    provide: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    provide[alias.asname or alias.name.split(".")[0]] = node
            else:
                for alias in node.names:
                    if alias.name != "*":
                        provide[alias.asname or alias.name] = node
    return imports, provide


def _adjust_import(stmt: ast.stmt) -> str:
    """把 import 语句按原样输出；对局部相对 import 层级 +1。"""
    if isinstance(stmt, ast.Import):
        return "import " + ", ".join(_alias(a) for a in stmt.names)
    if isinstance(stmt, ast.ImportFrom):
        module = stmt.module or ""
        # 绝对导入（level=0）不加点；相对导入进入子包后层级 +1。
        if stmt.level == 0:
            dots = ""
        else:
            dots = "." * (stmt.level + 1)
        text = f"from {dots}{module} import "
        aliases = ", ".join(_alias(a) for a in stmt.names)
        return text + (f"({aliases})" if len(stmt.names) > 1 else aliases)
    raise ValueError("非 import 节点")


def _alias(alias: ast.alias) -> str:
    if alias.asname:
        return f"{alias.name} as {alias.asname}"
    return alias.name


def _assign_nodes_to_submodules(nodes, mapping) -> dict[str, list[ast.AST]]:
    assigned: dict[str, list[ast.AST]] = OrderedDict((k, []) for k in mapping)
    deferred: list[ast.AST] = []
    for node in nodes:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    name = t.id
                    break
        if name is None:
            deferred.append(node)
            continue
        placed = False
        for sub, names in mapping.items():
            if name in names:
                assigned[sub].append(node)
                placed = True
                break
        if not placed:
            deferred.append(node)
    for node in deferred:
        assigned.setdefault("_consts", []).append(node)
    return assigned


def _write_module(path: str, header: str, body: str) -> None:
    content = header + body
    if not content.endswith("\n"):
        content += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def run(src_path: str, pkg_dir: str, mapping: dict[str, list[str]]) -> list[str]:
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=src_path)
    src_lines = src.splitlines()

    header_nodes: list[ast.stmt] = []
    body_nodes: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            header_nodes.append(node)
        else:
            body_nodes.append(node)
    imports, provide = _parse_imports(tree)

    # 顶层名字 -> 其所在子模块
    assigned = _assign_nodes_to_submodules(body_nodes, mapping)
    sub_by_name: dict[str, str] = {}
    for sub, nodes in assigned.items():
        for name in _defined_names(nodes):
            sub_by_name[name] = sub

    os.makedirs(pkg_dir, exist_ok=True)
    written: list[str] = []
    all_names: list[str] = []

    # 模块级 docstring（若有）放到 __init__
    docstring = ast.get_docstring(tree, clean=False) or ""

    export_lines: list[str] = []
    for sub, nodes in assigned.items():
        if not nodes:
            continue
        names = sorted(_defined_names(nodes))
        all_names.extend(names)

        # 收集本子模块引用的外部名字
        referenced: set[str] = set()
        for node in nodes:
            referenced |= _collect_load_names(node)
        # 去掉本子模块自己定义的
        referenced -= set(names)

        body: list[str] = []
        for node in nodes:
            body.append(_node_text(src_lines, node))

        # 外部 import
        ext_imports: list[str] = []
        seen_stmt: set[int] = set()
        for stmt in header_nodes:
            provided = {a.asname or a.name.split(".")[0] for a in stmt.names}
            if provided & referenced:
                if id(stmt) not in seen_stmt:
                    ext_imports.append(_adjust_import(stmt))
                    seen_stmt.add(id(stmt))

        # 同一包内兄弟/常量模块引用
        sibling_imports: list[str] = []
        for ref in sorted(referenced):
            owner = sub_by_name.get(ref)
            if owner is not None and owner != sub:
                # 目标模块里的这个 ref 一定 _defined
                if ref in _defined_names(assigned.get(owner, [])):
                    if ref not in sub_by_name_by_imports(sibling_imports):
                        sibling_imports.append(f"from .{owner} import {ref}")

        # 4 空格缩进的正则处理：对 __future__ 与 import 排序
        header_blocks: list[str] = []
        for line in ext_imports:
            if line.startswith("from __future__"):
                header_blocks.insert(0, line)
            else:
                header_blocks.append(line)

        module_header = "# 由 tools/split_package.py 拆分生成\nfrom __future__ import annotations\n"
        if header_blocks:
            module_header += "\n".join(header_blocks) + "\n"
        if sibling_imports:
            module_header += "\n".join(sibling_imports) + "\n"
        full_body = "\n\n".join(body)
        out = os.path.join(pkg_dir, f"{sub}.py")
        _write_module(out, module_header, full_body)
        written.append(out)

        for name in sorted(names):
            export_lines.append(f"from .{sub} import {name}")

    init_body = "\n".join(export_lines)
    init_content = (
        "# 由 tools/split_package.py 拆分生成\n"
        '"""自动回导出包：保持对外接口与旧模块一致。"""\n'
        "from __future__ import annotations\n"
        f"\n{init_body}\n"
        f"\n__all__ = [\n"
        + "".join(f"    {name!r},\n" for name in sorted(all_names) if not name.startswith("_"))
        + "]\n"
    )
    init_path = os.path.join(pkg_dir, "__init__.py")
    _write_module(init_path, "", init_content)
    written.append(init_path)
    return written


def sub_by_name_by_imports(imports: list[str]) -> set[str]:
    already = set()
    for line in imports:
        target = line.rsplit(" import ", 1)[-1]
        already.add(target.strip())
    return already


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    src_path, pkg_dir, mapping_path = argv[1:4]
    with open(mapping_path, encoding="utf-8") as f:
        mapping = json.load(f)
    written = run(src_path, pkg_dir, mapping)
    for path in written:
        print("WROTE", os.path.relpath(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
