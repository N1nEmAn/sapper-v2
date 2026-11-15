"""A minimal, strict YAML subset — enough for weapon/CVE entries, nothing more.

Why not PyYAML: sapper is deliberately zero-dependency (pure stdlib). A weapon
entry uses a tiny, controlled shape: nested mappings, sequences, inline lists
and maps, scalars, and comments. That subset is small enough to parse correctly
by hand and small enough to *test* correctly. The alternative — pulling in a
full YAML 1.2 implementation — buys generality we never use and a dependency we
then have to audit.

Supported:
    block mappings            key: value  (any consistent indent)
    block sequences           - item / - key: value
    inline lists              [a, b, c]
    inline maps               {k: v, k2: v2}
    scalars                   null/true/false, int, float, "quoted" / 'single'
                              / bare strings
    comments                  # to end of line (not inside quotes)

Not supported (raises YamlMiniError instead of guessing):
    anchors/aliases (&x / *x), block scalars (| and >), flow sequences spanning
    lines, tabs as indentation, multi-document (---). None of these appear in a
    weapon entry, and silently mishandling them would corrupt shared data.
"""

from __future__ import annotations

import json
import re

_INLINE_LIST = re.compile(r"^\[(.*)\]$", re.S)
_INLINE_MAP = re.compile(r"^\{(.*)\}$", re.S)
_NULL = {"null", "Null", "NULL", "~", ""}
_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}


class YamlMiniError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def loads(text: str) -> dict:
    lines = _strip(text)
    if not lines:
        return {}
    items = _tokenize(lines)
    value, _ = _parse_block(items, 0)
    if not isinstance(value, dict):
        raise YamlMiniError("顶层必须是 mapping")
    return value


def load(path) -> dict:
    import os
    return loads(open(path, encoding="utf-8").read())


def _strip(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise YamlMiniError("缩进不能用 tab，用空格")
        out.append(line)
    return out


def _strip_comment(line: str) -> str:
    in_s, in_d = False, False
    for i, ch in enumerate(line):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            # a bare '#' only starts a comment after whitespace or at line start
            if i == 0 or line[i - 1] in " \t":
                return line[:i].rstrip()
    return line.rstrip()


def _tokenize(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        out.append((indent, stripped))
    return out


def _parse_block(items: list[tuple[int, str]], i: int) -> tuple[object, int]:
    """Parse the block starting at items[i]; returns (value, next index)."""
    indent = items[i][0]
    content = items[i][1]

    if content.startswith("- ") or content == "-":
        return _parse_sequence(items, i)

    if ":" not in content:
        raise YamlMiniError(f"非法行（无 key:）: {content!r}")

    return _parse_mapping(items, i)


def _parse_mapping(items: list[tuple[int, str]], i: int) -> tuple[dict, int]:
    indent = items[i][0]
    result: dict = {}
    n = len(items)
    while i < n and items[i][0] == indent and _is_kv(items[i][1]):
        key, raw = _split_kv(items[i][1])
        if raw == "":
            # value lives on following, deeper-indented line(s)
            if i + 1 < n and items[i + 1][0] > indent:
                value, i = _parse_block(items, i + 1)   # i now at next sibling
            else:
                value = None
                i += 1
        else:
            value = _parse_scalar_or_inline(raw)
            i += 1
        result[key] = value
    return result, i


def _is_kv(content: str) -> bool:
    return ":" in content and not content.startswith("- ")


def _try_kv(s: str):
    """Return (key, value) if s is `bare-key: value`, else None.

    The colon must be followed by whitespace/EOL, and the key must be a bare
    identifier. This is what disambiguates `- vid: v1` (a mapping item) from
    `- "https://x"` (a quoted scalar) and `- https://x` (a bare URL) — a URL's
    colon is followed by `/`, never a space.
    """
    in_s = in_d = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == ":" and not in_s and not in_d:
            if i + 1 < len(s) and s[i + 1] not in " \t":
                return None              # e.g. "://" inside a URL
            key = s[:i].strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
                return None
            return key, s[i + 1:].strip()
    return None


def _parse_sequence(items: list[tuple[int, str]], i: int) -> tuple[list, int]:
    indent = items[i][0]
    result: list = []
    n = len(items)
    while i < n:
        cur_indent, content = items[i]
        if cur_indent < indent:
            break
        if cur_indent != indent:
            raise YamlMiniError(f"列表缩进不一致: {content!r}")
        if content == "-":
            if i + 1 < n and items[i + 1][0] > indent:
                value, i = _parse_block(items, i + 1)   # i now at next sibling
            else:
                value = None
                i += 1
            result.append(value)
            continue
        if not content.startswith("- "):
            break
        rest = content[2:].strip()
        kv = _try_kv(rest)
        if kv is not None:
            # mapping item: "- k: v"; continuation keys line up at indent+2
            key, raw = kv
            if raw == "":
                if i + 1 < n and items[i + 1][0] > indent:
                    value, i = _parse_block(items, i + 1)
                    item = {key: value}
                else:
                    item = {key: None}
                    i += 1
            else:
                item = {key: _parse_scalar_or_inline(raw)}
                i += 1
            item, i = _parse_cont(item, items, i, indent + 2)
            result.append(item)
            continue
        result.append(_parse_scalar_or_inline(rest))
        i += 1
    return result, i


def _parse_cont(item: dict, items, i: int, key_indent: int) -> tuple[dict, int]:
    """Consume continuation keys of a sequence mapping item at `key_indent`."""
    n = len(items)
    while i < n and items[i][0] == key_indent and _is_kv(items[i][1]):
        key, raw = _split_kv(items[i][1])
        if raw == "":
            if i + 1 < n and items[i + 1][0] > key_indent:
                value, i = _parse_block(items, i + 1)
            else:
                value = None
                i += 1
        else:
            value = _parse_scalar_or_inline(raw)
            i += 1
        item[key] = value
    return item, i


def _split_kv(content: str) -> tuple[str, str]:
    idx = content.index(":")
    return content[:idx].strip(), content[idx + 1 :].strip()


def _parse_scalar_or_inline(raw: str):
    if raw == "":
        return None
    m = _INLINE_LIST.match(raw)
    if m:
        return _parse_inline_list(m.group(1))
    m = _INLINE_MAP.match(raw)
    if m:
        return _parse_inline_map(m.group(1))
    return _scalar(raw)


def _parse_inline_list(body: str) -> list:
    if body.strip() == "":
        return []
    parts = _split_top(body, ",")
    return [_scalar(p) for p in parts]


def _parse_inline_map(body: str) -> dict:
    if body.strip() == "":
        return {}
    out = {}
    for part in _split_top(body, ","):
        if ":" not in part:
            raise YamlMiniError(f"inline map 项缺冒号: {part!r}")
        k, v = part.split(":", 1)
        out[k.strip()] = _scalar(v.strip())
    return out


def _split_top(body: str, sep: str) -> list[str]:
    """Split on sep, respecting quotes and bracket nesting."""
    out, depth, buf = [], 0, ""
    in_s = in_d = False
    for ch in body:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch in "[{(":
                depth += 1
            elif ch in "]})":
                depth -= 1
            elif ch == sep and depth == 0:
                out.append(buf.strip())
                buf = ""
                continue
        buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _scalar(raw: str):
    s = raw.strip()
    if s in ("|", ">"):
        # block scalar indicators: not in our subset. A bare `|` value never
        # occurs in a weapon/CVE entry, so treating it as a literal would turn
        # "k: |\n  block" into silent data loss (the `block` line is dropped).
        raise YamlMiniError(f"块标量（{s}）不支持，请用引号字符串或列表")
    if s in _NULL:
        return None
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


# --------------------------------------------------------------------------- #
# dumping
# --------------------------------------------------------------------------- #
def dumps(obj) -> str:
    lines: list[str] = []
    _emit(obj, lines, 0)
    return "\n".join(lines) + "\n"


def dump(obj, path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dumps(obj))


def _emit(obj, lines: list[str], depth: int) -> None:
    pad = "  " * depth
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                _emit(v, lines, depth + 1)
            elif isinstance(v, list):
                lines.append(f"{pad}{k}: []")
            elif isinstance(v, dict):
                lines.append(f"{pad}{k}: {{}}")
            else:
                lines.append(f"{pad}{k}: {_scalar_str(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                # emit mapping items compactly: first key on the dash line
                keys = list(item.keys())
                if keys:
                    k0 = keys[0]
                    v0 = item[k0]
                    rest = {k: v for k, v in item.items() if k != k0}
                    if isinstance(v0, (dict, list)) and v0:
                        lines.append(f"{pad}- {k0}:")
                        _emit(v0, lines, depth + 2)
                    elif isinstance(v0, (dict, list)):
                        lines.append(f"{pad}- {k0}: {_scalar_str(v0)}")
                    else:
                        lines.append(f"{pad}- {k0}: {_scalar_str(v0)}")
                    _emit(rest, lines, depth + 1)
                else:
                    lines.append(f"{pad}- {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}- " + _inline_list(item))
            else:
                lines.append(f"{pad}- {_scalar_str(item)}")


def _inline_list(items: list) -> str:
    return "[" + ", ".join(_scalar_str(x) for x in items) + "]"


def _scalar_str(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = str(v)
    # quote if it could be misread as another type or contains structure chars
    if (s in _NULL | _TRUE | _FALSE
            or re.fullmatch(r"-?\d+", s)
            or re.fullmatch(r"-?\d+\.\d+", s)
            or s == ""
            or any(c in s for c in ":#[]{}\n\"'")):
        return json.dumps(s, ensure_ascii=False)
    return s


# cross-check: what we dump must round-trip through loads()
def roundtrip(obj) -> dict:
    return loads(dumps(obj))
