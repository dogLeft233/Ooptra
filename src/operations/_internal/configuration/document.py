"""配置文档补丁与可信代码求值校验；字段准入由调用方预先解析。"""

from __future__ import annotations

import ast
import io
import json
import tokenize
from typing import Any


def _split_field_path(field: str) -> list[str]:
    return [part for part in str(field).split(".") if part]


def _python_literal(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            f"{_python_literal(str(key))}: {_python_literal(item)}"
            for key, item in value.items()
        ]
        return "{" + ", ".join(parts) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    return offsets


def _byte_col_to_char_index(line: str, byte_col: int) -> int:
    total = 0
    for index, char in enumerate(line):
        total += len(char.encode("utf-8"))
        if total > byte_col:
            return index
        if total == byte_col:
            return index + 1
    return len(line)


def _node_span(lines: list[str], offsets: list[int], node: ast.expr) -> tuple[int, int]:
    end_lineno = node.end_lineno
    end_col_offset = node.end_col_offset
    if end_lineno is None or end_col_offset is None:
        raise RuntimeError("配置语法节点缺少结束位置")
    start_col = _byte_col_to_char_index(lines[node.lineno - 1], node.col_offset)
    end_col = _byte_col_to_char_index(lines[end_lineno - 1], end_col_offset)
    return offsets[node.lineno - 1] + start_col, offsets[end_lineno - 1] + end_col


def _dict_assignments(tree: ast.AST, sources: dict[str, str]) -> dict[str, ast.Dict | None]:
    references: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names = [node.name]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names = node.names
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names = [node.name]
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names = [node.rest]
        for name in names:
            references.setdefault(name, []).append(node)

    # A mentioned name is not a missing group. Arbitrary references can alias or
    # mutate its dictionary; only one standalone top-level literal is editable.
    assignments: dict[str, ast.Dict | None] = dict.fromkeys(references)
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        target = targets[0]
        if references.get(target.id) == [target] and isinstance(node.value, ast.Dict):
            assignments[target.id] = node.value
    if "*" in references:
        for name in sources.values():
            assignments[name] = None
    return assignments


def _dict_entries(dict_node: ast.Dict) -> dict[str, ast.expr]:
    entries: dict[str, ast.expr] = {}
    for key_node, value_node in zip(dict_node.keys, dict_node.values, strict=True):
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            entries[key_node.value] = value_node
        else:
            raise RuntimeError("配置字典包含解包或动态键，无法安全修改")
    return entries


def _merge_nested(base: dict[str, Any], field: str, value: Any) -> None:
    path = _split_field_path(field)
    node = base
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _patch_field_inplace(
    dict_node: ast.Dict,
    field: str,
    literal: str,
    lines: list[str],
    offsets: list[int],
    replacements: list[tuple[int, int, str]],
    insertions: dict[int, list[str]],
) -> bool:
    """把字段写进已有字典；路径完整时就地改叶子，返回 False 表示需要整段重写。"""
    path = _split_field_path(field)
    current = dict_node
    for part in path[:-1]:
        child = _dict_entries(current).get(part)
        if not isinstance(child, ast.Dict):
            return False
        current = child
    leaf = path[-1]
    value_node = _dict_entries(current).get(leaf)
    if value_node is not None:
        start, end = _node_span(lines, offsets, value_node)
        replacements.append((start, end, literal))
        return True
    end_lineno = current.end_lineno
    if end_lineno is None:
        return False
    closing_line = lines[end_lineno - 1]
    closing_pos = _node_span(lines, offsets, current)[1] - 1
    closing_col = closing_pos - offsets[end_lineno - 1]
    prefix = closing_line[:closing_col]
    inline = bool(prefix.strip())
    position = closing_pos if inline else offsets[end_lineno - 1]
    chunks = insertions.setdefault(position, [])
    if not chunks and current.values:
        last_end = _node_span(lines, offsets, current.values[-1])[1]
        dict_start = _node_span(lines, offsets, current)[0]
        dict_text = "".join(lines)[dict_start:closing_pos + 1]
        token_offsets = _line_offsets(dict_text.splitlines(keepends=True))
        # 完整字典保留匹配的左括号，避免 Python 3.10 对尾部片段报 TokenError。
        # 只检查值之后的运算符；注释里的逗号不能作为分隔符。
        tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(dict_text).readline)
            if token.type == tokenize.OP
            and dict_start + token_offsets[token.start[0] - 1] + token.start[1] >= last_end
        ]
        has_comma = any(
            token.type == tokenize.OP and token.string == ","
            for token in tokens
        )
        if not has_comma:
            # AST 的值节点不含外围括号，分隔逗号必须放在括号外。
            for token in reversed(tokens):
                if token.type == tokenize.OP and token.string == ")":
                    last_end = dict_start + token_offsets[token.end[0] - 1] + token.end[1]
                    break
            if last_end == position:
                chunks.append(",")
            else:
                replacements.append((last_end, last_end, ","))
    entry = f"{_python_literal(leaf)}: {literal},"
    chunks.append(f" {entry}" if inline else f"{prefix}    {entry}\n")
    return True


def patch_config_text(
    text: str, updates: dict[str, dict[str, Any]], *, sources: dict[str, str],
    filename: str = "config.py",
) -> str:
    """按给定分组映射修补已解析字段；拒绝动态/别名目标，保留无关正文。"""
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError as exc:
        raise RuntimeError(f"config.py 存在语法错误，无法保存: {exc}") from exc

    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    assignments = _dict_assignments(tree, sources)
    replacements: list[tuple[int, int, str]] = []
    insertions: dict[int, list[str]] = {}

    for group, values in updates.items():
        source_name = sources[group]
        dict_node = assignments.get(source_name)
        if source_name in assignments and dict_node is None:
            raise RuntimeError(f"{source_name} 不是唯一的字典字面量定义，无法安全修改")
        if dict_node is None:
            # config.py 尚无该分组时，文件末尾追加空字典再写入
            appended = f"\n{source_name} = {{\n}}\n"
            text = text + appended
            tree = ast.parse(text, filename=filename)
            lines = text.splitlines(keepends=True)
            offsets = _line_offsets(lines)
            assignments = _dict_assignments(tree, sources)
            dict_node = assignments.get(source_name)
            if dict_node is None:
                raise RuntimeError(f"config.py 找不到 {source_name}，无法写入")

        for node in ast.walk(dict_node):
            if isinstance(node, ast.Dict):
                _dict_entries(node)

        pending: dict[str, dict[str, Any]] = {}
        for field, value in values.items():
            literal = _python_literal(value)
            if _patch_field_inplace(
                dict_node, field, literal, lines, offsets, replacements, insertions
            ):
                continue
            # 合并同一缺失子树的字段，只插入一次，不重写其它配置表达式。
            path = _split_field_path(field)
            current = dict_node
            for index, part in enumerate(path[:-1]):
                child = _dict_entries(current).get(part)
                if isinstance(child, ast.Dict):
                    current = child
                    continue
                prefix = ".".join(path[: index + 1])
                if child is not None:
                    raise RuntimeError(f"配置项 {prefix} 不是字典字面量，无法安全修改其子项")
                subtree = pending.setdefault(prefix, {})
                _merge_nested(subtree, ".".join(path[index + 1 :]), value)
                break

        for field, value in pending.items():
            _patch_field_inplace(
                dict_node, field, _python_literal(value), lines, offsets,
                replacements, insertions,
            )

    edits: list[tuple[int, int, str]] = list(replacements)
    for position, chunks in insertions.items():
        edits.append((position, position, "".join(chunks)))
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def validate_config_text(text: str, *, filename: str = "config.py") -> None:
    """求值部署方可信的配置代码；不用于校验不可信上传脚本。"""
    namespace: dict[str, Any] = {}
    try:
        # 沿用可信部署配置的求值规则；此处不是不可信脚本的安全验证器。
        exec(compile(text, filename, "exec"), namespace)
    except Exception as exc:
        raise RuntimeError(f"新配置无法求值，已取消保存: {exc}") from exc
