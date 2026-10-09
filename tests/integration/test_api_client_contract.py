"""T4.12: the API <-> JS client contract.

Every `apiGet` / `apiSend` / `apiUpload` call in `src/rga/web/static/js/*.js` is extracted
statically and checked against the server's own definitions:

* the method and normalized path exist in `ROUTE_SCHEMAS` and in the app's OpenAPI routes;
* the query-string parameters the JS builds are fields of the route's query model;
* each request body the JS builds fits the route's request model: every key (nested object
  literals included) is a model field, every required field is sent, and a body made of the
  guaranteed keys with schema-derived sample values validates;
* every response field the JS reads (followed through variables, loops, array callbacks,
  destructuring and same-file function parameters) exists on the response model.

The extractor is deliberately small. Anything it cannot read (a computed path, a spread in a body,
an API function used as a value) is a test failure, so a new call can never slip through unchecked.
`TestVerifyTheVerifier` runs the whole check on scratch copies of the JS with a renamed path, a
wrong method, a renamed body key and a missing response field planted, and requires each to fail.
"""

from __future__ import annotations

import re
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from rga.config import Settings
from rga.web.app import create_app
from rga.web.schemas import ROUTE_SCHEMAS

JS_DIR = Path(__file__).resolve().parents[2] / "src" / "rga" / "web" / "static" / "js"
API_FUNCTIONS = ("apiGet", "apiSend", "apiUpload")
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
LIST_ATTRIBUTES = {
    "length", "map", "filter", "forEach", "find", "findIndex", "some", "every", "flatMap", "slice",
    "sort", "reverse", "concat", "join", "includes", "indexOf", "reduce", "push", "at", "entries",
    "keys", "values",
}  # fmt: skip
LIST_PRESERVING = {"filter", "slice", "sort", "reverse", "concat"}
ELEMENT_CALLBACKS = {
    "map",
    "filter",
    "forEach",
    "find",
    "findIndex",
    "some",
    "every",
    "flatMap",
    "sort",
}


class Unparsed(AssertionError):
    """A JS construct the contract extractor cannot read; the test fails instead of skipping it."""


# ---------------------------------------------------------------------------------- JS scanning


def _skip_string(src: str, i: int) -> int:
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    raise Unparsed("unterminated string")


def _skip_template(src: str, i: int) -> int:
    i += 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and src[i + 1 : i + 2] == "{":
            i = _skip_braces(src, i + 1)
        else:
            i += 1
    raise Unparsed("unterminated template literal")


def _skip_braces(src: str, i: int) -> int:
    """Index after the `}` matching the `{` at `i`, skipping strings, templates and comments."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c in "'\"":
            i = _skip_string(src, i)
            continue
        if c == "`":
            i = _skip_template(src, i)
            continue
        if src.startswith("//", i):
            i = src.find("\n", i)
            i = len(src) if i < 0 else i
            continue
        if src.startswith("/*", i):
            i = src.find("*/", i) + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise Unparsed("unbalanced braces")


def _skip_regex(src: str, i: int) -> int:
    i += 1
    in_class = False
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < len(src) and src[i].isalpha():
                i += 1
            return i
        i += 1
    raise Unparsed("unterminated regex")


REGEX_PREFIX = set("(,=:[!&|?{};+-*%<>~^")


def mask_js(src: str) -> str:
    """Same length as `src`; comments become spaces and string, template and regex bodies `\\0`."""
    out = list(src)
    i, last = 0, ""
    while i < len(src):
        c = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            end = len(src) if end < 0 else end
            out[i:end] = " " * (end - i)
            i = end
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i) + 2
            out[i:end] = [" " if ch != "\n" else "\n" for ch in src[i:end]]
            i = end
            continue
        if c in "'\"":
            end = _skip_string(src, i)
        elif c == "`":
            end = _skip_template(src, i)
        elif c == "/" and (last == "" or last in REGEX_PREFIX or last == "\0r"):
            end = _skip_regex(src, i)
        else:
            if not c.isspace():
                last = c
            i += 1
            continue
        if c == "`":
            _mask_template(src, i, end, out)
        else:
            out[i + 1 : end - 1] = ["\0" if ch != "\n" else "\n" for ch in src[i + 1 : end - 1]]
        last = "\0"
        i = end
    return "".join(out)


def _mask_template(src: str, start: int, end: int, out: list[str]) -> None:
    """Mask the text of the template literal at `src[start:end]` but keep its `${...}` code."""
    j = start + 1
    while j < end - 1:
        if src[j] == "\\":
            out[j : j + 2] = ["\0", "\0"]
            j += 2
        elif src.startswith("${", j):
            close = _skip_braces(src, j + 1)
            out[j + 2 : close - 1] = list(mask_js(src[j + 2 : close - 1]))
            j = close
        else:
            out[j] = "\0" if src[j] != "\n" else "\n"
            j += 1


def _match(masked: str, i: int) -> int:
    """Index of the bracket closing the one at `i` in masked text."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[masked[i]]]
    j = i + 1
    while j < len(masked) and stack:
        c = masked[j]
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if c != stack[-1]:
                raise Unparsed(f"mismatched bracket near offset {j}")
            stack.pop()
        j += 1
    if stack:
        raise Unparsed("unbalanced brackets")
    return j - 1


def split_top(masked: str, start: int, end: int, separator: str = ",") -> list[tuple[int, int]]:
    """Spans of `masked[start:end]` split on top-level `separator` characters."""
    spans, depth, begin = [], 0, start
    j = start
    while j < end:
        c = masked[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == separator and depth == 0:
            spans.append((begin, j))
            begin = j + 1
        j += 1
    spans.append((begin, end))
    return [(a, b) for a, b in spans if masked[a:b].strip()]


@dataclass
class JsFile:
    """A JS file with its masked twin and helpers for scoping and expression slicing."""

    name: str
    src: str
    masked: str = field(init=False)

    def __post_init__(self) -> None:
        self.masked = mask_js(self.src)

    def line(self, pos: int) -> int:
        return self.src.count("\n", 0, pos) + 1

    def text(self, span: tuple[int, int]) -> str:
        return self.src[span[0] : span[1]].strip()

    def blocks_around(self, pos: int) -> list[tuple[int, int]]:
        """Brace blocks (start, end) containing `pos`, outermost first."""
        found, depth_stack = [], []
        for j, c in enumerate(self.masked[: pos + 1]):
            if c == "{":
                depth_stack.append(j)
            elif c == "}" and depth_stack:
                depth_stack.pop()
        for start in depth_stack:
            found.append((start, _match(self.masked, start)))
        return found

    def scope_end(self, pos: int) -> int:
        blocks = self.blocks_around(pos)
        return blocks[-1][1] if blocks else len(self.masked)


# ------------------------------------------------------------------------------ call extraction


@dataclass
class CallSite:
    file: JsFile
    pos: int
    function: str
    args: list[tuple[int, int]]
    close: int

    @property
    def where(self) -> str:
        return f"{self.file.name}:{self.file.line(self.pos)}"


def load_js_files(directory: Path = JS_DIR) -> list[JsFile]:
    return [JsFile(p.name, p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.js"))]


def find_calls(js: JsFile) -> list[CallSite]:
    if js.name == "api.js":
        return []
    calls: list[CallSite] = []
    for match in re.finditer(r"\b(apiGet|apiSend|apiUpload)\b", js.masked):
        if _in_import(js, match.start()):
            continue
        paren = re.match(r"\s*\(", js.masked[match.end() :])
        if not paren:
            raise Unparsed(f"{js.name}:{js.line(match.start())}: {match.group(1)} used as a value")
        open_at = match.end() + paren.end() - 1
        close = _match(js.masked, open_at)
        calls.append(
            CallSite(
                js, match.start(), match.group(1), split_top(js.masked, open_at + 1, close), close
            )
        )
    return calls


def _in_import(js: JsFile, pos: int) -> bool:
    """True when `pos` lies inside an `import { ... } from "..."` statement."""
    start = js.masked.rfind("import", 0, pos)
    return start >= 0 and ";" not in js.masked[start:pos] and "{" in js.masked[start:pos]


def forbidden_fetch(files: list[JsFile]) -> list[str]:
    return [
        f"{js.name}:{js.line(m.start())}"
        for js in files
        if js.name != "api.js"
        for m in re.finditer(
            r"\bfetch\s*\(|\bXMLHttpRequest\b|\bnavigator\.sendBeacon\b", js.masked
        )
    ]


# ---------------------------------------------------------------------------- path resolution


def _placeholder(template: str) -> str:
    """Replace every `${...}` in a template literal body with `{}`."""
    out, i = [], 0
    while i < len(template):
        if template.startswith("${", i):
            end = _skip_braces(template, i + 1)
            out.append("{}")
            i = end
        else:
            out.append(template[i])
            i += 1
    return "".join(out)


def _literal_value(text: str) -> str | None:
    text = text.strip()
    if len(text) >= 2 and text[0] in "'\"`" and text[-1] == text[0]:
        if text[0] == "`":
            return _placeholder(text[1:-1])
        return text[1:-1]
    return None


def find_function(js: JsFile, name: str) -> tuple[int, int] | None:
    """Span of the body (inside the braces, or the expression) of a same-file function."""
    patterns = [
        rf"\bfunction\s+{re.escape(name)}\s*\(",
        rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:async\s*)?(?:function\s*)?\(",
        rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:async\s*)?[A-Za-z_]\w*\s*=>",
    ]
    for pattern in patterns:
        m = re.search(pattern, js.masked)
        if not m:
            continue
        params_open = js.masked.find("(", m.start())
        if "=>" in pattern and "(" not in pattern.split("=>")[0][-4:]:
            arrow = js.masked.find("=>", m.start())
        else:
            params_close = _match(js.masked, params_open)
            arrow = js.masked.find("=>", params_close)
            brace = js.masked.find("{", params_close)
            if "function" in m.group(0) or (arrow < 0 or (0 <= brace < arrow)):
                return (brace + 1, _match(js.masked, brace))
        body_start = arrow + 2
        while js.masked[body_start].isspace():
            body_start += 1
        if js.masked[body_start] == "{":
            return (body_start + 1, _match(js.masked, body_start))
        end = _arg_end(js.masked, body_start)
        return (body_start, end)
    return None


def _arg_end(masked: str, start: int) -> int:
    depth, j = 0, start
    while j < len(masked):
        c = masked[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif c in ",;" and depth == 0:
            return j
        j += 1
    return j


def function_params(js: JsFile, name: str) -> list[str] | None:
    m = re.search(
        rf"(?:\bfunction\s+{re.escape(name)}\s*|\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:async\s*)?(?:function\s*)?)\(",
        js.masked,
    )
    if not m:
        return None
    open_at = m.end() - 1
    close = _match(js.masked, open_at)
    return [js.src[a:b].split("=")[0].strip() for a, b in split_top(js.masked, open_at + 1, close)]


def declaration(js: JsFile, name: str, pos: int) -> tuple[int, int] | None:
    """Span of the initializer of the nearest preceding `const|let|var name = ...` visible at `pos`."""
    visible = {(0, len(js.masked))} | set(js.blocks_around(pos))
    best = None
    for m in re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=\s*", js.masked):
        if m.start() >= pos:
            break
        if all(blk in visible for blk in js.blocks_around(m.start())):
            best = (m.end(), _arg_end(js.masked, m.end()))
    return best


def resolve_path(call: CallSite) -> tuple[str, str]:
    """(normalized path with `{}` for every parameter, query text) for the call's path argument."""
    js = call.file
    index = 1 if call.function == "apiSend" else 0
    if len(call.args) <= index:
        raise Unparsed(f"{call.where}: no path argument")
    return _path_from(js, call.args[index], call.pos)


def _path_from(js: JsFile, span: tuple[int, int], pos: int) -> tuple[str, str]:
    text = js.text(span)
    literal = _literal_value(text)
    query_sources = [text]
    if literal is None:
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", text, re.S)
        if m and (body := find_function(js, m.group(1))):
            returns = re.findall(r"return\s+", js.masked[body[0] : body[1]])
            ret = re.search(
                r"return\s+(`[^;]*`|\"[^;]*\"|'[^;]*')\s*;", js.src[body[0] : body[1]], re.S
            )
            if not ret or len(returns) != 1:
                raise Unparsed(f"{js.name}: cannot read the path returned by {m.group(1)}()")
            literal = _literal_value(ret.group(1))
            query_sources = [js.src[body[0] : body[1]]]
        elif re.fullmatch(r"[A-Za-z_]\w*", text) and (init := declaration(js, text, pos)):
            literal = _literal_value(js.src[init[0] : init[1]])
        if literal is None:
            raise Unparsed(
                f"{js.name}:{js.line(span[0])}: path is not a readable literal: {text!r}"
            )
    path, _, query = literal.partition("?")
    if not path.startswith("/api/"):
        raise Unparsed(f"{js.name}:{js.line(span[0])}: path {path!r} does not start with /api/")
    if query and "{}" in query:
        for ident in re.findall(
            r"\$\{\s*([A-Za-z_]\w*)", text if query_sources == [text] else query_sources[0]
        ):
            init = declaration(js, ident, pos)
            if init:
                query_sources.append(js.src[init[0] : js.masked.find(";", init[0])])
            for assign in re.finditer(rf"\b{ident}\.push\(([^;]*)\);", js.src):
                query_sources.append(assign.group(1))
    return path, "\n".join(query_sources) if query else ""


def query_names(query_text: str) -> set[str]:
    return set(re.findall(r"(?:^|[?&`'\"\s])([a-z][a-z_]*)=", query_text))


def method_of(call: CallSite) -> str:
    if call.function == "apiGet":
        return "GET"
    if call.function == "apiUpload":
        return "POST"
    if not call.args:
        raise Unparsed(f"{call.where}: apiSend without arguments")
    method = _literal_value(call.file.text(call.args[0]))
    if method is None or method.upper() not in HTTP_METHODS or method != method.upper():
        raise Unparsed(f"{call.where}: method is not an upper-case string literal")
    return method


# --------------------------------------------------------------------------------- body parsing


Tree = dict  # key -> Tree | None


@dataclass
class Variant:
    guaranteed: Tree
    possible: set[str] = field(default_factory=set)
    dynamic: bool = False


def _entries(js: JsFile, brace: int) -> list[tuple[str, tuple[int, int] | None]]:
    close = _match(js.masked, brace)
    entries = []
    for a, b in split_top(js.masked, brace + 1, close):
        raw = js.masked[a:b].strip()
        if raw.startswith("..."):
            raise Unparsed(f"{js.name}:{js.line(a)}: spread in an object literal")
        m = re.match(r"\s*(?:([A-Za-z_]\w*)|['\"`]\0*['\"`])\s*(:|$)", js.masked[a:b])
        if m is None and re.match(r"\s*['\"]", js.masked[a:b]):
            quoted = re.match(r"\s*(['\"])(.*?)\1\s*:", js.src[a:b], re.S)
            if not quoted:
                raise Unparsed(f"{js.name}:{js.line(a)}: unreadable object key")
            entries.append((quoted.group(2), (a + quoted.end(), b)))
            continue
        if m is None or m.group(1) is None:
            raise Unparsed(f"{js.name}:{js.line(a)}: computed or method key in an object literal")
        key = m.group(1)
        entries.append((key, None if m.group(2) == "" else (a + m.end(), b)))
    return entries


def parse_literal(js: JsFile, brace: int) -> Tree:
    tree: Tree = {}
    for key, value_span in _entries(js, brace):
        tree[key] = None
        if value_span is None:
            ident, init = key, declaration(js, key, brace)
        else:
            raw = js.masked[value_span[0] : value_span[1]]
            offset = value_span[0] + len(raw) - len(raw.lstrip())
            nested = _nested_literal(js, offset, value_span[1])
            if nested is not None:
                tree[key] = parse_literal(js, nested)
                continue
            ident = raw.strip()
            init = declaration(js, ident, brace) if re.fullmatch(r"[A-Za-z_]\w*", ident) else None
        if init is not None:
            nested = _nested_literal(js, init[0], init[1])
            if nested is not None:
                tree[key] = parse_literal(js, nested)
    return tree


def _nested_literal(js: JsFile, start: int, end: int) -> int | None:
    """Offset of the first object literal inside a value expression, or None."""
    j = start
    while j < end:
        c = js.masked[j]
        if c == "{":
            before = js.masked[start:j].rstrip()
            if before == "" or before[-1] in "(?:":
                return j
            j = _match(js.masked, j)
        if c in "([":
            inner = _nested_literal(js, j + 1, _match(js.masked, j))
            if inner is not None:
                return inner
            j = _match(js.masked, j)
        j += 1
    return None


def _returns(js: JsFile, body: tuple[int, int]) -> list[int]:
    return [body[0] + m.end() for m in re.finditer(r"\breturn\s+", js.masked[body[0] : body[1]])]


def _string_arrays(text: str) -> set[str]:
    """Identifier-like strings found in array literals of `text` (the keys a loop may assign)."""
    names: set[str] = set()
    for m in re.finditer(r"\[([^\[\]]*)\]", text):
        names.update(re.findall(r"['\"]([A-Za-z_]\w*)['\"]", m.group(1)))
    return names


def body_variants(js: JsFile, span: tuple[int, int], pos: int, depth: int = 0) -> list[Variant]:
    """Every shape the expression at `span` can take, as key trees."""
    if depth > 6:
        raise Unparsed(f"{js.name}:{js.line(span[0])}: body expression is too indirect")
    text = js.text(span)
    start = span[0] + (
        len(js.masked[span[0] : span[1]]) - len(js.masked[span[0] : span[1]].lstrip())
    )
    masked = js.masked[start : span[1]].strip()
    question = _top_level(masked, "?")
    if question is not None:
        colon = _top_level(masked, ":", question + 1)
        if colon is None:
            raise Unparsed(f"{js.name}:{js.line(span[0])}: unreadable conditional body")
        a, b = start + question + 1, start + colon
        return body_variants(js, (a, b), pos, depth + 1) + body_variants(
            js, (start + colon + 1, span[1]), pos, depth + 1
        )
    if masked.startswith("{"):
        tree = parse_literal(js, start)
        return [Variant(tree, set(tree))]
    call = re.fullmatch(r"([A-Za-z_]\w*)\s*\(.*\)", masked, re.S)
    if call:
        body = find_function(js, call.group(1))
        if body is None:
            raise Unparsed(
                f"{js.name}:{js.line(span[0])}: body builder {call.group(1)}() not found"
            )
        found: list[Variant] = []
        for ret in _returns(js, body):
            end = _arg_end(js.masked, ret)
            found.extend(body_variants(js, (ret, end), ret, depth + 1))
        if not found:
            raise Unparsed(f"{js.name}: {call.group(1)}() returns no object")
        return _with_dynamic_arrays(js, body, found)
    ident = re.fullmatch(r"[A-Za-z_]\w*", masked)
    if ident:
        init = declaration(js, masked, pos)
        if init is None:
            raise Unparsed(f"{js.name}:{js.line(span[0])}: cannot find the declaration of {masked}")
        variants = body_variants(js, init, init[0], depth + 1)
        return _apply_assignments(js, masked, init[0], pos, variants)
    raise Unparsed(f"{js.name}:{js.line(span[0])}: unreadable body expression {text[:60]!r}")


def _top_level(masked: str, char: str, start: int = 0) -> int | None:
    depth = 0
    for j in range(start, len(masked)):
        c = masked[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == char and depth == 0:
            if char == "?" and masked[j : j + 2] in ("?.", "??"):
                continue
            return j
    return None


def _apply_assignments(
    js: JsFile, name: str, decl: int, pos: int, variants: list[Variant]
) -> list[Variant]:
    scope = js.scope_end(max(decl - 1, 0))
    window = js.masked[decl:scope]
    for m in re.finditer(rf"\b{name}\.([A-Za-z_]\w*)\s*=(?!=)", window):
        for v in variants:
            v.possible.add(m.group(1))
    for m in re.finditer(rf"\b{name}\[\s*(['\"])\0*\1\s*\]\s*=(?!=)", window):
        key = re.match(rf"\b{name}\[\s*(['\"])(.*?)\1", js.src[decl + m.start() :])
        if key:
            for v in variants:
                v.possible.add(key.group(2))
    dynamic = re.search(rf"\b{name}\[\s*[A-Za-z_]\w*\s*\]\s*=(?!=)", window)
    if dynamic:
        enclosing = js.blocks_around(max(decl - 1, 0))
        names = _string_arrays(js.src[enclosing[0][0] : enclosing[0][1]] if enclosing else js.src)
        for v in variants:
            v.dynamic = True
            v.possible |= names
    return variants


def _with_dynamic_arrays(
    js: JsFile, body: tuple[int, int], variants: list[Variant]
) -> list[Variant]:
    text = js.masked[body[0] : body[1]]
    if not re.search(r"\b[A-Za-z_]\w*\[\s*[A-Za-z_]\w*\s*\]\s*=(?!=)", text):
        return variants
    pool = js.src[body[0] : body[1]]
    for called in set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", pool)):
        inner = find_function(js, called)
        if inner:
            pool += js.src[inner[0] : inner[1]]
    for const in set(re.findall(r"\b([A-Z][A-Z_]+)\b", pool)):
        decl = re.search(rf"\bconst\s+{const}\s*=\s*(\[[^\]]*\])", js.src)
        if decl:
            pool += decl.group(1)
    names = _string_arrays(pool)
    for v in variants:
        v.dynamic = True
        v.possible |= names
    return variants


# ------------------------------------------------------------------------ pydantic type helpers


def unwrap(tp: typing.Any) -> list[typing.Any]:
    """Annotated and Optional/Union members, flattened."""
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        return unwrap(typing.get_args(tp)[0])
    if origin in (typing.Union, types.UnionType):
        return [m for arg in typing.get_args(tp) for m in unwrap(arg) if m is not type(None)]
    return [tp]


def models_in(tp: typing.Any) -> list[type[BaseModel]]:
    return [m for m in unwrap(tp) if isinstance(m, type) and issubclass(m, BaseModel)]


def list_elements(tp: typing.Any) -> list[typing.Any]:
    elements = []
    for member in unwrap(tp):
        if typing.get_origin(member) in (list, typing.Sequence):
            elements.extend(unwrap(typing.get_args(member)[0]))
    return elements


def field_names(model: type[BaseModel]) -> dict[str, typing.Any]:
    """Accepted input names (name and alias) mapped to the field's annotation."""
    names = {}
    for name, info in model.model_fields.items():
        names[name] = info.annotation
        if info.alias:
            names[info.alias] = info.annotation
    return names


def required_names(model: type[BaseModel]) -> set[str]:
    return {name for name, info in model.model_fields.items() if info.is_required()}


def check_tree(model: type[BaseModel], tree: Tree, possible: set[str], where: str) -> list[str]:
    """Problems with a body key tree against a request model (keys, nested keys, required fields)."""
    problems: list[str] = []
    fields = field_names(model)
    for key in sorted(possible | set(tree)):
        if key not in fields:
            problems.append(f"{where}: `{key}` is not a field of {model.__name__}")
            continue
        sub = tree.get(key)
        if isinstance(sub, dict):
            candidates = models_in(fields[key]) or [
                m for e in list_elements(fields[key]) for m in models_in(e)
            ]
            if not candidates:
                continue
            attempts = [check_tree(c, sub, set(sub), f"{where}.{key}") for c in candidates]
            if all(attempts):
                problems.extend(min(attempts, key=len))
    missing = required_names(model) - (possible | set(tree))
    for name in sorted(missing):
        problems.append(f"{where}: required field `{name}` of {model.__name__} is never sent")
    return problems


def enum_choices(annotation: typing.Any) -> list[typing.Any]:
    schema = TypeAdapter(annotation).json_schema()
    schema = schema.get("$defs", {}).get(schema.get("$ref", "").rsplit("/", 1)[-1], schema)
    return list(schema.get("enum", []))


STRING_SAMPLES = {"shot_time": "12:30", "utc_offset": "+00:00"}


def sample_value(
    info: typing.Any,
    tree: Tree | None = None,
    index: int = 0,
    pick: dict | None = None,
    field: str = "",
) -> typing.Any:
    """A valid-looking value for a model field (`FieldInfo`) built from its JSON schema.

    Nested JS keys are honoured; integers and numbers grow with `index` so lists hold distinct
    items and `x1 > x0` style checks pass. `pick` overrides enum values by field name.
    """
    annotation = info.annotation
    if isinstance(tree, dict):
        for model in models_in(annotation) + [
            m for e in list_elements(annotation) for m in models_in(e)
        ]:
            fields = model.model_fields
            body = {
                k: sample_value(fields[k], v, i, pick, k)
                for i, (k, v) in enumerate(tree.items())
                if k in fields
            }
            return [body] * 4 if list_elements(annotation) and not models_in(annotation) else body
    full = typing.Annotated[(annotation, *info.metadata)] if info.metadata else annotation
    schema = TypeAdapter(full).json_schema()
    name = field or getattr(info, "title", None) or ""
    return _from_schema(schema, schema.get("$defs", {}), index, pick or {}, name)


def _from_schema(schema: dict, defs: dict, index: int, pick: dict, name: str = "") -> typing.Any:
    if "$ref" in schema:
        return _from_schema(defs[schema["$ref"].rsplit("/", 1)[1]], defs, index, pick, name)
    if "enum" in schema:
        values = schema["enum"]
        return pick.get(tuple(values), values[0])
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _from_schema(options[0], defs, index, pick, name)
    if "allOf" in schema:
        return _from_schema(schema["allOf"][0], defs, index, pick, name)
    kind = schema.get("type")
    if kind in ("integer", "number"):
        whole = kind == "integer"
        low, high = schema.get("minimum"), schema.get("maximum")
        if "exclusiveMinimum" in schema:
            low = schema["exclusiveMinimum"] + (1 if whole else 0)
            if not whole:
                high_bound = schema.get("exclusiveMaximum", schema.get("maximum"))
                low = low + ((high_bound - low) / 10 if high_bound is not None else 0.5)
        if "exclusiveMaximum" in schema:
            high = schema["exclusiveMaximum"] - (1 if whole else 0)
        low = 1 if low is None else low
        value = low + index
        if high is not None and value > high:
            value = high if whole else (low + high) / 2
        return int(value) if whole else float(value)
    if kind == "string":
        if schema.get("format") == "date":
            return "2026-09-30"
        if name in STRING_SAMPLES:
            return STRING_SAMPLES[name]
        return "x" * max(schema.get("minLength", 1), 1)
    if kind == "boolean":
        return True
    if kind == "array":
        items = schema.get("items", {"type": "integer"})
        return [
            _from_schema(items, defs, i, pick, name)
            for i in range(max(schema.get("minItems", 1), 1))
        ]
    if kind == "object" or "properties" in schema:
        required = schema.get("required", [])
        return {
            k: _from_schema(v, defs, i, pick, k)
            for i, (k, v) in enumerate(schema.get("properties", {}).items())
            if k in required
        }
    return None


def validates_with_some_enum_choice(
    model: type[BaseModel], keys: set[str], tree: Tree
) -> str | None:
    """None when a body of `keys` validates for some combination of enum values, else the error."""
    fields = model.model_fields
    enums = {
        k: enum_choices(fields[k].annotation)
        for k in keys
        if k in fields and enum_choices(fields[k].annotation)
    }
    names = sorted(enums)
    combos: list[dict] = [{}]
    for name in names:
        combos = [{**c, name: value} for c in combos for value in enums[name]][:64]
    last = ""
    for combo in combos or [{}]:
        pick = {tuple(enums[n]): combo[n] for n in combo}
        sample = {
            k: sample_value(fields[k], tree.get(k), i, pick, k)
            for i, k in enumerate(sorted(keys))
            if k in fields
        }
        try:
            model.model_validate(sample)
            return None
        except ValidationError as exc:
            last = str(exc.errors()[0])
    return last


# ----------------------------------------------------------------------------- the app's routes


def normalize(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


@dataclass
class Registry:
    schemas: dict[tuple[str, str], typing.Any]
    openapi: set[tuple[str, str]]

    def schema(self, method: str, path: str):
        return self.schemas.get((method, path))


def build_registry(tmp_path: Path) -> Registry:
    schemas = {(m, normalize(p)): s for (m, p), s in ROUTE_SCHEMAS.items()}
    app = create_app(Settings(data_dir=tmp_path / "data"))
    openapi = {
        (method.upper(), normalize(path))
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.upper() in HTTP_METHODS
    }
    return Registry(schemas, openapi)


# ------------------------------------------------------------------------------ response reads


@dataclass
class Binding:
    name: str
    start: int
    end: int
    types: list[typing.Any]
    source: str


class FlowAnalysis:
    """Binds JS variables to response model types and checks every property read on them."""

    def __init__(self, js: JsFile, calls: list[tuple[CallSite, str, str, typing.Any]]):
        self.js = js
        self.bindings: list[Binding] = []
        self.reads: set[tuple[str, str]] = set()
        self.problems: list[str] = []
        self._seed(calls)

    # -- seeding -------------------------------------------------------------------------
    def _seed(self, calls) -> None:
        js = self.js
        for call, method, path, response in calls:
            if response is None:
                continue
            origin = f"{method} {path}"
            window = js.masked[max(0, call.pos - 160) : call.pos]
            statement = window[window.rfind(";") + 1 :]
            head = window[max(window.rfind(";"), window.rfind("{"), window.rfind("}")) + 1 :]
            m = re.search(
                r"(\b(?:const|let|var)\s+)?([A-Za-z_]\w*)\s*=\s*(?:await\s+)?(?:run\(\s*\(\)\s*=>\s*)?(?:\(\s*)?$",
                head,
            )
            if m:
                start, end = self._variable_scope(
                    m.group(2), call.pos, declared_here=bool(m.group(1))
                )
                self._bind(m.group(2), start, end, [response], origin)
                continue
            m = re.search(r"\{([^{}]*)\}\s*=\s*(?:await\s+)?$", statement)
            if m:
                self._check_destructure(m.group(1), [response], origin)
                continue
            paren = re.search(r"([A-Za-z_]\w*)\(\s*(?:await\s+)?$", head)
            if paren and paren.group(1) not in {"run", "guarded", "await"}:
                self._bind_params(paren.group(1), 0, [response], origin)
            after = js.masked[call.close + 1 : call.close + 80]
            chain = re.match(r"\s*\)\s*(\??\.[A-Za-z_]\w*(?:\??\.[A-Za-z_]\w*)*)", after)
            if chain and re.search(r"\(\s*await\s+$", head):
                self._walk([response], chain.group(1), origin)

    def _variable_scope(self, name: str, pos: int, declared_here: bool) -> tuple[int, int]:
        """Scope of a variable assigned from a response: where it was declared, else from here."""
        js = self.js
        if declared_here:
            return pos, js.scope_end(pos)
        visible = set(js.blocks_around(pos)) | {(0, len(js.masked))}
        found = None
        for m in re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\b", js.masked):
            if m.start() >= pos:
                break
            if all(blk in visible for blk in js.blocks_around(m.start())):
                found = m.start()
        if found is None:
            return pos, js.scope_end(pos)
        return found, js.scope_end(found)

    def _bind(self, name: str, start: int, end: int, types: list, origin: str) -> None:
        if not types:
            return
        if any(
            b.name == name and b.start == start and b.end == end and b.source == origin
            for b in self.bindings
        ):
            return
        self.bindings.append(Binding(name, start, end, types, origin))

    def _bind_params(self, function: str, index: int, types: list, origin: str) -> None:
        params = function_params(self.js, function)
        body = find_function(self.js, function)
        if params is None or body is None or index >= len(params):
            return
        name = params[index]
        if re.fullmatch(r"[A-Za-z_]\w*", name):
            self._bind(name, body[0], body[1], types, origin)

    # -- type evaluation -------------------------------------------------------------------
    def lookup(self, name: str, pos: int) -> list[Binding]:
        """Innermost bindings of `name` visible at `pos` (several when a variable is assigned from
        more than one response)."""
        visible = [b for b in self.bindings if b.name == name and b.start <= pos < b.end]
        if not visible:
            return []
        innermost = max(b.start for b in visible)
        return [b for b in visible if b.start == innermost]

    def eval_expr(self, expr: str, pos: int) -> tuple[list[typing.Any], str] | None:
        expr = expr.strip().removeprefix("...").strip()
        while expr.startswith("(") and _match(mask_js(expr), 0) == len(expr) - 1:
            expr = expr[1:-1].strip()
        expr = re.sub(r"\s*\|\|\s*\[\]\s*$", "", expr).strip()
        preserving = "|".join(sorted(LIST_PRESERVING))
        m = re.fullmatch(
            rf"([A-Za-z_]\w*)((?:\??\.[A-Za-z_]\w*)*)((?:\??\.(?:{preserving})\((?:.*)\))*)",
            expr,
            re.S,
        )
        if not m:
            return None
        found = self.lookup(m.group(1), pos)
        if not found:
            return None
        types = [t for b in found for t in b.types]
        if m.group(2):
            types = [
                t for b in found for t in self._walk(b.types, m.group(2), b.source, record=False)
            ]
        return types, " | ".join(sorted({b.source for b in found}))

    def _walk(self, types: list, chain: str, origin: str, record: bool = True) -> list:
        names = re.findall(r"\[\d+\]|[A-Za-z_]\w*", chain)
        path = []
        for name in names:
            if not types:
                return []
            if name.startswith("["):
                types = [e for t in types for e in list_elements(t)]
                path.append(name)
                continue
            path.append(name)
            nxt, found = [], False
            for tp in types:
                if models_in(tp):
                    for model in models_in(tp):
                        fields = field_names(model)
                        if name in fields:
                            found = True
                            nxt.append(fields[name])
                elif list_elements(tp):
                    if name in LIST_ATTRIBUTES:
                        found = True
                else:
                    found = True
            if not found:
                if record:
                    self.problems.append(
                        f"{self.js.name}: reads `.{'.'.join(path)}` of {origin} "
                        f"but {', '.join(sorted({m.__name__ for t in types for m in models_in(t)}))} has no field `{name}`"
                    )
                return []
            if record and nxt:
                self.reads.update((o, ".".join(path)) for o in origin.split(" | "))
            types = nxt
        return types

    # -- propagation -----------------------------------------------------------------------
    def run(self) -> FlowAnalysis:
        for _ in range(6):
            before = len(self.bindings)
            self._loops()
            self._callbacks()
            self._calls()
            self._destructuring()
            if len(self.bindings) == before:
                break
        self._reads()
        return self

    def _loops(self) -> None:
        js = self.js
        for m in re.finditer(r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_]\w*)\s+of\s+", js.masked):
            close = _match(js.masked, js.masked.find("(", m.start()))
            got = self.eval_expr(js.src[m.end() : close], m.start())
            if got:
                elements = [e for t in got[0] for e in list_elements(t)]
                body = js.masked.find("{", close)
                end = _match(js.masked, body) if body >= 0 else close
                self._bind(m.group(1), close, end + 1 if end else close, elements, got[1])

    def _callbacks(self) -> None:
        js = self.js
        for m in re.finditer(
            r"\.(map|filter|forEach|find|findIndex|some|every|flatMap|sort)\s*\(", js.masked
        ):
            open_at = m.end() - 1
            close = _match(js.masked, open_at)
            start = self._receiver_start(m.start())
            got = self.eval_expr(js.src[start : m.start()], start)
            if not got:
                continue
            elements = [e for t in got[0] for e in list_elements(t)]
            if not elements:
                continue
            args = split_top(js.masked, open_at + 1, close)
            if not args:
                continue
            first = js.text(args[0])
            arrow = re.match(r"\(?\s*([A-Za-z_]\w*)\s*(?:,\s*([A-Za-z_]\w*))?\s*\)?\s*=>", first)
            if arrow:
                scope = (args[0][0], args[0][1] + 1)
                self._bind(arrow.group(1), scope[0], scope[1], elements, got[1])
                if m.group(1) == "sort" and arrow.group(2):
                    self._bind(arrow.group(2), scope[0], scope[1], elements, got[1])
            elif re.fullmatch(r"[A-Za-z_]\w*", first):
                self._bind_params(first, 0, elements, got[1])

    def _receiver_start(self, dot: int) -> int:
        j = dot
        masked = self.js.masked
        while j > 0:
            c = masked[j - 1]
            if c in ")]":
                depth, k = 0, j - 1
                while k >= 0:
                    if masked[k] in ")]":
                        depth += 1
                    elif masked[k] in "([":
                        depth -= 1
                        if depth == 0:
                            break
                    k -= 1
                j = k
            elif c.isalnum() or c in "_$.?":
                j -= 1
            else:
                break
        return j

    def _calls(self) -> None:
        js = self.js
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", js.masked):
            name = m.group(1)
            if name in API_FUNCTIONS or name in {
                "if",
                "for",
                "while",
                "switch",
                "catch",
                "function",
                "return",
            }:
                continue
            params = function_params(js, name)
            if params is None:
                continue
            open_at = m.end() - 1
            for index, span in enumerate(
                split_top(js.masked, open_at + 1, _match(js.masked, open_at))
            ):
                got = self.eval_expr(js.src[span[0] : span[1]], span[0])
                if got and got[0]:
                    self._bind_params(name, index, got[0], got[1])

    def _destructuring(self) -> None:
        js = self.js
        for m in re.finditer(
            r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=\s*([A-Za-z_][\w.?]*)\s*;", js.masked
        ):
            got = self.eval_expr(js.src[m.start(2) : m.end(2)], m.start())
            if got:
                self._check_destructure(js.src[m.start(1) : m.end(1)], got[0], got[1])

    def _check_destructure(self, names: str, types: list, origin: str) -> None:
        for item in names.split(","):
            key = item.split(":")[0].strip()
            if key:
                self._walk(types, key, origin)

    def _reads(self) -> None:
        js = self.js
        seen: set[tuple[int, str]] = set()
        reads = r"(?<![\w.$])([A-Za-z_]\w*)((?:\s*\??\.\s*[A-Za-z_]\w*|\[\d+\])+)"
        for m in re.finditer(reads, js.masked):
            found = self.lookup(m.group(1), m.start())
            if not found or (m.start(), m.group(1)) in seen:
                continue
            seen.add((m.start(), m.group(1)))
            chain = re.sub(r"\s", "", js.src[m.start(2) : m.end(2)])
            for binding in found:
                self._walk(binding.types, chain, binding.source)


# ----------------------------------------------------------------------------------- the checker


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    calls: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    reads: set[tuple[str, str]] = field(default_factory=set)
    bodies: dict[str, list[set[str]]] = field(default_factory=dict)
    query_params: dict[tuple[str, str], set[str]] = field(default_factory=dict)


def check_client(files: list[JsFile], registry: Registry) -> Report:
    """Run every contract check over `files`; problems are collected, unreadable code raises."""
    report = Report()
    report.problems += [
        f"{where}: direct fetch() outside api.js" for where in forbidden_fetch(files)
    ]
    for js in files:
        parsed = []
        for call in find_calls(js):
            method = method_of(call)
            path, query = resolve_path(call)
            key = (method, path)
            report.calls.setdefault(key, []).append(call.where)
            schema = registry.schema(method, path)
            if schema is None:
                report.problems.append(f"{call.where}: {method} {path} is not in ROUTE_SCHEMAS")
                continue
            if key not in registry.openapi:
                report.problems.append(f"{call.where}: {method} {path} is not a route of the app")
            _check_query(report, call, key, schema, query)
            _check_body(report, call, key, schema)
            parsed.append((call, method, path, schema.response))
        flow = FlowAnalysis(js, parsed).run()
        report.problems += flow.problems
        report.reads |= flow.reads
    return report


def _check_query(report: Report, call: CallSite, key, schema, query: str) -> None:
    names = query_names(query)
    report.query_params[key] = report.query_params.get(key, set()) | names
    if not names:
        return
    if schema.query is None:
        report.problems.append(
            f"{call.where}: {key[0]} {key[1]} takes no query but the JS sends {sorted(names)}"
        )
        return
    unknown = names - set(field_names(schema.query))
    if unknown:
        report.problems.append(
            f"{call.where}: query parameter(s) {sorted(unknown)} are not fields of {schema.query.__name__}"
        )


def _check_body(report: Report, call: CallSite, key, schema) -> None:
    if call.function == "apiUpload":
        if schema.request is not None:
            report.problems.append(f"{call.where}: apiUpload to a route with a JSON body model")
        return
    body_arg = call.args[2] if call.function == "apiSend" and len(call.args) > 2 else None
    if body_arg is None:
        if schema.request is not None:
            try:
                schema.request.model_validate({})
            except ValidationError:
                report.problems.append(
                    f"{call.where}: {key[0]} {key[1]} needs a body but the JS sends none"
                )
        return
    if schema.request is None:
        report.problems.append(
            f"{call.where}: {key[0]} {key[1]} takes no body but the JS sends one"
        )
        return
    variants = body_variants(call.file, body_arg, call.pos)
    report.bodies.setdefault(f"{key[0]} {key[1]}", []).extend(
        v.possible | set(v.guaranteed) for v in variants
    )
    for variant in variants:
        where = f"{call.where} {key[0]} {key[1]}"
        problems = check_tree(schema.request, variant.guaranteed, variant.possible, where)
        report.problems += problems
        if problems or variant.dynamic:
            continue
        error = validates_with_some_enum_choice(
            schema.request, set(variant.possible) | set(variant.guaranteed), variant.guaranteed
        )
        if error:
            report.problems.append(
                f"{where}: a body with the JS keys {sorted(variant.possible)} does not validate: {error}"
            )


# ====================================================================================== tests


@pytest.fixture(scope="module")
def registry(tmp_path_factory) -> Registry:
    return build_registry(tmp_path_factory.mktemp("contract"))


@pytest.fixture(scope="module")
def report(registry) -> Report:
    return check_client(load_js_files(), registry)


def key(method: str, path: str) -> tuple[str, str]:
    return method, path


REQUIRED_CALLS = {
    # B-37: detach, and the sibling routes the zeroing page calls
    key("DELETE", "/api/zeros/{}/groups/{}"),
    key("POST", "/api/zeros/{}/groups"),
    key("POST", "/api/groups"),
    key("GET", "/api/loads/{}/lots"),
    key("GET", "/api/groups"),
    # B-50: the check group is created through the zero
    key("POST", "/api/zeros/{}/check-groups"),
    key("POST", "/api/zeros/{}/checks"),
    key("POST", "/api/zeros/{}/dialed"),
    key("POST", "/api/zeros/{}/dot"),
    key("GET", "/api/zeros/{}"),
    # saved aggregates (analysis page)
    key("POST", "/api/aggregates"),
    key("GET", "/api/aggregates"),
    key("DELETE", "/api/aggregates/{}"),
    key("POST", "/api/aggregate"),
    # review editor
    key("PUT", "/api/groups/{}/review"),
    key("POST", "/api/groups/{}/detect"),
    key("POST", "/api/groups/{}/finalize"),
    # groups, photos, references, rifles, comparison, dope, search
    key("PATCH", "/api/groups/{}"),
    key("POST", "/api/groups/delete"),  # the Groups tab's Edit mode
    key("POST", "/api/photos"),
    key("POST", "/api/photos/{}/reference/propose"),
    key("POST", "/api/photos/{}/reference/confirm"),
    key("POST", "/api/photos/{}/reference/reset"),  # B-79: change the scale reference
    key("POST", "/api/rifles"),
    key("PUT", "/api/rifles/{}/zero-load"),
    key("POST", "/api/rifles/{}/loads"),
    key("POST", "/api/loads/{}/lots"),
    key("GET", "/api/rifles/{}/comparison"),
    key("GET", "/api/loads/{}/dope"),
    key("GET", "/api/search"),
    # B-65: the suppressor picker saves a new name
    key("POST", "/api/suppressors"),
    # B-77: the Settings page saves the share-card defaults
    key("PUT", "/api/settings/share-defaults"),
}

REQUIRED_READS = {
    ("GET /api/groups", "masked"),  # B-43: the picker reads the server's masking decision
    ("GET /api/groups", "shots_expected"),
    ("GET /api/groups", "zero_id"),
    ("PUT /api/groups/{}/review", "shots"),
    ("GET /api/zeros/{}", "advice"),
    ("GET /api/zeros/{}", "groups"),
    ("POST /api/zeros/{}/checks", "route"),
    ("POST /api/photos", "id"),  # "Add new group" hands the uploaded photo to the New group page
    ("GET /api/rifles/{}/comparison", "loads"),
    ("GET /api/loads/{}/dope", "rows"),
    ("POST /api/aggregate", "shots"),
    ("POST /api/suppressors", "id"),
    ("POST /api/suppressors", "name"),
}


class TestTheContract:
    def test_every_call_matches_a_route_a_query_model_and_a_request_model(self, report):
        assert report.problems == [], "\n".join(report.problems)

    def test_the_extractor_found_the_whole_client(self, report):
        assert len(report.calls) >= 38
        assert sum(len(sites) for sites in report.calls.values()) >= 46
        assert set(report.calls) >= REQUIRED_CALLS, sorted(REQUIRED_CALLS - set(report.calls))

    def test_the_zeroing_page_sibling_routes_carry_the_query_the_page_builds(self, report):
        assert report.query_params[key("GET", "/api/groups")] >= {
            "rifle_id",
            "load_id",
            "status",
        }
        assert report.query_params[key("GET", "/api/rifles/{}/comparison")] >= {
            "load_ids",
            "position",
            "suppressor",
        }
        assert report.query_params[key("GET", "/api/search")] >= {"q"}

    def test_bodies_were_read_for_every_route_that_has_a_request_model(self, report):
        expected = {
            f"{m} {p}"
            for (m, p), site in report.calls.items()
            if m != "GET" and ROUTE_SCHEMAS_NORMALIZED[(m, p)].request is not None
        }
        assert expected <= set(report.bodies), sorted(expected - set(report.bodies))
        assert {"group_id"} in report.bodies["POST /api/zeros/{}/groups"]
        assert {
            "lot_code",
            "shots_expected",
            "notes",
            "shot_date",
            "shot_time",
            "position",
            "suppressor",
            "suppressor_id",
            "photo_id",
        } in report.bodies["POST /api/zeros/{}/check-groups"]
        assert all("session_id" not in body for body in report.bodies["POST /api/groups"])
        assert {
            "load_id",
            "lot_code",
            "shots_expected",
            "distance_yd",
            "position",
            "suppressor",
            "shot_date",
        } <= max(report.bodies["POST /api/groups"], key=len)
        assert {"name", "group_ids"} in report.bodies["POST /api/aggregates"]

    def test_response_fields_the_client_reads_exist_and_the_new_ones_are_covered(self, report):
        missing = sorted(REQUIRED_READS - report.reads)
        assert not missing, f"the flow analysis no longer checks: {missing}"
        assert len(report.reads) >= 100

    def test_no_js_file_calls_fetch_except_api_js(self):
        files = load_js_files()
        assert forbidden_fetch(files) == []
        api = next(f for f in files if f.name == "api.js")
        assert len(re.findall(r"\bfetch\s*\(", api.masked)) == 1
        for name in API_FUNCTIONS:
            assert re.search(rf"export (?:async )?function {name}\b", api.masked), name

    def test_every_route_the_client_calls_is_registered_in_both_places(self, report, registry):
        for method, path in report.calls:
            assert (method, path) in registry.schemas, (method, path)
            assert (method, path) in registry.openapi, (method, path)

    def test_the_server_models_carry_the_fields_the_new_features_need(self):
        group = ROUTE_SCHEMAS_NORMALIZED[("GET", "/api/groups")].response
        element = list_elements(group)[0]
        assert "masked" in field_names(models_in(element)[0])
        detect = ROUTE_SCHEMAS_NORMALIZED[("POST", "/api/groups/{}/detect")].response
        assert "aim_candidates" in field_names(detect)
        assert "masked" in field_names(
            models_in(ROUTE_SCHEMAS_NORMALIZED[("POST", "/api/zeros/{}/check-groups")].response)[0]
        )


ROUTE_SCHEMAS_NORMALIZED = {(m, normalize(p)): s for (m, p), s in ROUTE_SCHEMAS.items()}


# ----------------------------------------------------------------------------- the extractor


def js_file(source: str, name: str = "probe.js") -> JsFile:
    return JsFile(name, source)


def one_call(source: str) -> CallSite:
    [call] = find_calls(js_file(source))
    return call


class TestExtractor:
    def test_masking_hides_comments_strings_templates_and_regexes_but_keeps_length(self):
        src = 'a("x(y"); // apiGet(1)\n/* apiSend( */ b(`${c(")")}`); d(/[)(]\\//g);'
        masked = mask_js(src)
        assert len(masked) == len(src)
        assert "apiGet" not in masked and "apiSend" not in masked
        assert masked.count("(") == masked.count(")") == 4  # a, b, the call inside ${}, d
        assert "c(" in masked and '")"' not in masked

    def test_arguments_split_only_at_top_level_commas(self):
        call = one_call('apiSend("POST", `/api/a/${f(1, 2)}`, { a: [1, 2], b: { c: 3 } });')
        assert [call.file.text(span) for span in call.args][0] == '"POST"'
        assert len(call.args) == 3

    def test_paths_are_normalized_from_literals_templates_and_helper_functions(self):
        assert resolve_path(one_call('apiGet("/api/rifles");'))[0] == "/api/rifles"
        assert resolve_path(one_call("apiGet(`/api/a/${x}/b/${y.id}`);"))[0] == "/api/a/{}/b/{}"
        helper = (
            "function p(id) { const q = []; q.push(`a=${id}`); return `/api/r/${id}/c?${q.join('&')}`; }\n"
            "apiGet(p(1));"
        )
        path, query = resolve_path(one_call(helper))
        assert path == "/api/r/{}/c" and query_names(query) == {"a"}

    def test_a_query_built_in_a_variable_is_followed(self):
        src = (
            'function f() { const query = x ? `&rifle_id=${x}` : ""; '
            "return apiGet(`/api/groups?status=final${query}`); }"
        )
        path, query = resolve_path(one_call(src))
        assert path == "/api/groups" and query_names(query) == {"status", "rifle_id"}

    def test_methods_must_be_upper_case_literals(self):
        assert method_of(one_call('apiSend("PATCH", "/api/a", {});')) == "PATCH"
        assert method_of(one_call('apiGet("/api/a");')) == "GET"
        assert method_of(one_call('apiUpload("/api/a", file);')) == "POST"
        for bad in ('apiSend("patch", "/api/a", {});', "apiSend(verb, '/api/a', {});"):
            with pytest.raises(Unparsed):
                method_of(one_call(bad))

    @pytest.mark.parametrize(
        "source",
        [
            "const f = apiGet;",
            "run(apiSend);",
            "apiGet(somePath);",
            "apiGet(pathFor(1));",
            "apiGet(`/not-api/${x}`);",
            'apiGet("/api/a" + id);',
        ],
    )
    def test_anything_the_extractor_cannot_read_fails_loudly(self, source):
        with pytest.raises(Unparsed):
            for call in find_calls(js_file(source)):
                method_of(call)
                resolve_path(call)

    def test_bodies_follow_literals_ternaries_assignments_helpers_and_loops(self):
        src = (
            "const KEYS = ['p', 'q'];\n"
            "function build() { const body = { n: 1 }; if (x) body.m = 2; "
            "KEYS.forEach((k) => { body[k] = 3; }); return body; }\n"
            "async function go() {\n"
            "  const body = x ? { roi: { a: 1 } } : {};\n"
            "  const other = build(); other.kind = 'k';\n"
            '  await apiSend("POST", "/api/a", body);\n'
            '  await apiSend("POST", "/api/b", other);\n'
            '  await apiSend("POST", "/api/c", { s: shots.map((s) => ({ x: s.x })), t: u ? { v: 1 } : null });\n'
            "}\n"
        )
        js = js_file(src)
        first, second, third = find_calls(js)
        v = body_variants(js, first.args[2], first.pos)
        assert [(sorted(x.guaranteed), x.guaranteed.get("roi")) for x in v] == [
            (["roi"], {"a": None}),
            ([], None),
        ]
        [w] = body_variants(js, second.args[2], second.pos)
        assert (
            w.dynamic and {"n", "m", "p", "q", "kind"} <= w.possible and set(w.guaranteed) == {"n"}
        )
        [x] = body_variants(js, third.args[2], third.pos)
        assert x.guaranteed == {"s": {"x": None}, "t": {"v": None}}

    @pytest.mark.parametrize(
        "body",
        ["{ ...extra }", "{ [k]: 1 }", "{ method() {} }", "thing.make()"],
    )
    def test_bodies_the_extractor_cannot_read_fail_loudly(self, body):
        js = js_file(f'apiSend("POST", "/api/a", {body});')
        [call] = find_calls(js)
        with pytest.raises(Unparsed):
            body_variants(js, call.args[2], call.pos)

    def test_check_tree_reports_unknown_nested_and_missing_required_keys(self):
        model = ROUTE_SCHEMAS_NORMALIZED[("POST", "/api/photos/{}/reference/confirm")].request
        assert (
            check_tree(
                model, {"kind": None, "candidate_index": None}, {"kind", "candidate_index"}, "t"
            )
            == []
        )
        bad = check_tree(
            model, {"kind": None, "manual": {"grid": {"corners": None}}}, {"kind", "manual"}, "t"
        )
        assert any("corners" in problem for problem in bad)
        assert any("required field `kind`" in p for p in check_tree(model, {}, set(), "t"))
        assert any(
            "`surprise`" in p for p in check_tree(model, {"surprise": None}, {"surprise"}, "t")
        )


class Item(BaseModel):
    id: int
    label: str


class Holder(BaseModel):
    items: list[Item]
    title: str
    best: Item | None = None


def flow_problems(source: str, response, extra: str = "") -> tuple[list[str], set]:
    js = js_file(source)
    [call] = find_calls(js)
    flow = FlowAnalysis(js, [(call, "GET", "/x", response)]).run()
    return flow.problems, flow.reads


class TestFlowAnalysis:
    """The response-field reader follows each way the client moves response data around."""

    @pytest.mark.parametrize(
        ("body", "field"),
        [
            ("const r = await apiGet('/api/x'); use(r.title);", "title"),
            ("const r = await apiGet('/api/x'); use(r.best?.label);", "best.label"),
            ("const r = await apiGet('/api/x'); r.items.forEach((i) => use(i.label));", "items"),
            (
                "const r = await apiGet('/api/x'); for (const i of r.items) { use(i.label); }",
                "items",
            ),
            ("const { title } = await apiGet('/api/x'); use(title);", "title"),
            ("const r = await apiGet('/api/x'); const { title, items } = r; use(title);", "title"),
            (
                "const r = await apiGet('/api/x'); show(r); function show(h) { use(h.items); }",
                "items",
            ),
            (
                "const r = await apiGet('/api/x'); r.items.map(label); function label(i) { return i.label; }",
                "items",
            ),
            (
                "let r = null; async function go() { r = await apiGet('/api/x'); } function t() { use(r.title); }",
                "title",
            ),
            (
                "const r = await apiGet(`/api/x`); (r.items || []).filter((i) => i.id > 1).map((i) => i.label);",
                "items",
            ),
            ("const r = await apiGet('/api/x'); use(`${r.items[0].label}`);", "items"),
        ],
    )
    def test_valid_reads_pass_and_are_recorded(self, body, field):
        problems, reads = flow_problems(f"async function main() {{ {body} }}", Holder)

        assert problems == []
        assert ("GET /x", field.split(".")[0]) in reads

    @pytest.mark.parametrize(
        ("body", "missing"),
        [
            ("const r = await apiGet('/api/x'); use(r.titel);", "titel"),
            ("const r = await apiGet('/api/x'); use(r.best?.name);", "name"),
            ("const r = await apiGet('/api/x'); r.items.forEach((i) => use(i.name));", "name"),
            ("const r = await apiGet('/api/x'); for (const i of r.items) { use(i.name); }", "name"),
            ("const { titel } = await apiGet('/api/x');", "titel"),
            ("const r = await apiGet('/api/x'); const { title, titel } = r;", "titel"),
            (
                "const r = await apiGet('/api/x'); show(r); function show(h) { use(h.item); }",
                "item",
            ),
            (
                "const r = await apiGet('/api/x'); r.items.map(label); function label(i) { return i.name; }",
                "name",
            ),
            (
                "let r = null; async function go() { r = await apiGet('/api/x'); } function t() { use(r.titel); }",
                "titel",
            ),
            ("const r = await apiGet('/api/x'); (r.items || []).filter((i) => i.idd > 1);", "idd"),
            ("const r = await apiGet('/api/x'); use(`${r.items[0].name}`);", "name"),
        ],
    )
    def test_a_missing_field_is_reported_wherever_the_data_goes(self, body, missing):
        problems, _ = flow_problems(f"async function main() {{ {body} }}", Holder)

        assert any(f"no field `{missing}`" in p for p in problems), problems

    def test_a_list_response_is_followed_into_its_items(self):
        problems, reads = flow_problems(
            "async function main() { const r = await apiGet('/api/x'); r.forEach((i) => use(i.id, i.nope)); }",
            list[Item],
        )

        assert ("GET /x", "id") in reads and any("no field `nope`" in p for p in problems)

    def test_a_variable_reused_for_something_else_in_another_scope_is_not_checked(self):
        problems, _ = flow_problems(
            "async function a() { const r = await apiGet('/api/x'); use(r.title); }\n"
            "function b() { const r = document.body; use(r.nothing); }",
            Holder,
        )

        assert problems == []


# ---------------------------------------------------------------------- verify the verifier


@pytest.fixture
def scratch_js(tmp_path):
    """A writable copy of the JS directory, and a helper that plants one edit and re-checks it."""
    copy = tmp_path / "js"
    copy.mkdir()
    for path in JS_DIR.glob("*.js"):
        (copy / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return copy


def plant(directory: Path, filename: str, old: str, new: str) -> None:
    target = directory / filename
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{filename}: planted text {old!r} must occur exactly once"
    target.write_text(text.replace(old, new), encoding="utf-8")


PLANTS = {
    "a renamed path": (
        "zeroing.js",
        "`/api/zeros/${zero.id}/check-groups`",
        "`/api/zeros/${zero.id}/check-group`",
        "not in ROUTE_SCHEMAS",
    ),
    "a renamed detach path": (
        "zeroing.js",
        "`/api/zeros/${zero.id}/groups/${groupId}`",
        "`/api/zeros/${zero.id}/members/${groupId}`",
        "not in ROUTE_SCHEMAS",
    ),
    "a wrong method on an existing path": (
        "group_form.js",
        'apiSend("PATCH", `/api/groups/${groupId}`',
        'apiSend("PUT", `/api/groups/${groupId}`',
        "is not in ROUTE_SCHEMAS",
    ),
    "a wrong method for the aggregate delete": (
        "analysis.js",
        'apiSend("DELETE", `/api/aggregates/${target.id}`',
        'apiSend("POST", `/api/aggregates/${target.id}`',
        "is not in ROUTE_SCHEMAS",
    ),
    "a renamed body key": (
        "group_form.js",
        "{ photo_id: photoId }",
        "{ photo: photoId }",
        "`photo` is not a field of GroupUpdate",
    ),
    "a renamed nested body key": (
        "reference.js",
        "{ x_px: x, y_px: y }",
        "{ x: x, y_px: y }",
        "is not a field of",
    ),
    "a dropped required body key": (
        "zeroing.js",
        "{ group_id: group.id }",
        "{}",
        "group_id",
    ),
    "a body sent to a route that takes none": (
        "review.js",
        'apiSend("POST", `/api/groups/${groupId}/finalize`)',
        'apiSend("POST", `/api/groups/${groupId}/finalize`, { done: true })',
        "takes no body",
    ),
    "a renamed query parameter": (
        "zeroing.js",
        "`/api/groups?rifle_id=${zero.rifle_id}&load_id",
        "`/api/groups?rifle=${zero.rifle_id}&load_id",
        "query parameter(s) ['rifle']",
    ),
    "a renamed aggregate list path": (
        "analysis.js",
        'apiGet("/api/aggregates")',
        'apiGet("/api/aggregate")',
        "GET /api/aggregate is not in ROUTE_SCHEMAS",
    ),
    "a renamed aggregate save body key": (
        "analysis.js",
        "{ name, group_ids: getIds() }",
        "{ name, groups: getIds() }",
        "`groups` is not a field of SaveAggregateRequest",
    ),
    "a missing field on a saved aggregate": (
        "analysis.js",
        "saved.es_moa.toFixed(2)",
        "saved.es_moaa.toFixed(2)",
        "has no field `es_moaa`",
    ),
    "a renamed groups path used by the zeroing page": (
        "zeroing.js",
        "`/api/groups?rifle_id=${zero.rifle_id}&load_id",
        "`/api/group?rifle_id=${zero.rifle_id}&load_id",
        "GET /api/group is not in ROUTE_SCHEMAS",
    ),
    "a renamed create-group path on the rifle page": (
        "group_form.js",
        'apiSend("POST", "/api/groups", body)',
        'apiSend("POST", "/api/group", body)',
        "POST /api/group is not in ROUTE_SCHEMAS",
    ),
    "a renamed context key on the group page": (
        "group_context.js",
        '      position: value("c-position"),',
        '      posture: value("c-position"),',
        "`posture` is not a field of GroupUpdate",
    ),
    "a renamed lots path used by the zeroing page": (
        "zeroing.js",
        "`/api/loads/${zero.load_id}/lots`",
        "`/api/loads/${zero.load_id}/lot`",
        "GET /api/loads/{}/lot is not in ROUTE_SCHEMAS",
    ),
    "a renamed groups-list query parameter": (
        "zeroing.js",
        "&status=final`",
        "&state=final`",
        "query parameter(s) ['state']",
    ),
    "a missing field on the zeroing page's group list": (
        "zeroing.js",
        "g.shots_expected <= 2",
        "g.shot_count <= 2",
        "has no field `shot_count`",
    ),
    "a missing response field": (
        "review.js",
        "proposal.bullet_diameter_in);",
        "proposal.bullet_diameter_inch);",
        "has no field `bullet_diameter_inch`",
    ),
    "a missing field on list items": (
        "analysis.js",
        "box.disabled = group.masked;",
        "box.disabled = group.mask;",
        "has no field `mask`",
    ),
    "a missing field read through a function parameter": (
        "review.js",
        "model.expected = detail.shots_expected;",
        "model.expected = detail.shots_wanted;",
        "has no field `shots_wanted`",
    ),
    "a renamed share defaults path": (
        "settings.js",
        'apiSend("PUT", "/api/settings/share-defaults"',
        'apiSend("PUT", "/api/settings/share-default"',
        "PUT /api/settings/share-default is not in ROUTE_SCHEMAS",
    ),
    "a wrong method for the share defaults": (
        "settings.js",
        'apiSend("PUT", "/api/settings/share-defaults"',
        'apiSend("POST", "/api/settings/share-defaults"',
        "POST /api/settings/share-defaults is not in ROUTE_SCHEMAS",
    ),
    "a renamed share defaults body key": (
        "settings.js",
        '      thing_hitter: isOn("thing_hitter"),',
        '      hitter: isOn("thing_hitter"),',
        "`hitter` is not a field of ShareDefaults",
    ),
    "a dropped share defaults body key": (
        "settings.js",
        '      numbers: isOn("numbers"),\n',
        "",
        "numbers",
    ),
    "a missing field on a variable assigned in another function": (
        "zeroing.js",
        "const checked = new Set(zero.checks.map((c) => c.group_id));",
        "const checked = new Set(zero.checkz.map((c) => c.group_id));",
        "has no field `checkz`",
    ),
}


class TestVerifyTheVerifier:
    def test_the_unplanted_copy_passes(self, scratch_js, registry):
        assert check_client(load_js_files(scratch_js), registry).problems == []

    @pytest.mark.parametrize("label", sorted(PLANTS))
    def test_each_planted_defect_is_reported(self, scratch_js, registry, label):
        filename, old, new, expected = PLANTS[label]
        plant(scratch_js, filename, old, new)

        problems = check_client(load_js_files(scratch_js), registry).problems

        assert any(expected in problem for problem in problems), (label, problems)

    def test_a_route_that_exists_in_the_registry_but_not_in_the_app_is_reported(
        self, scratch_js, registry
    ):
        without = Registry(registry.schemas, registry.openapi - {key("GET", "/api/search")})

        problems = check_client(load_js_files(scratch_js), without).problems

        assert any("GET /api/search is not a route of the app" in p for p in problems)

    def test_a_body_with_valid_keys_that_the_model_still_rejects_is_reported(
        self, scratch_js, registry
    ):
        plant(
            scratch_js,
            "reference.js",
            "    kind: kindSelect.value,\n    candidate_index: state.selected,\n",
            "    kind: kindSelect.value,\n    candidate_index: state.selected,\n    manual: { edge_points_px: [] },\n",
        )

        problems = check_client(load_js_files(scratch_js), registry).problems

        assert any("does not validate" in p for p in problems), problems

    def test_a_call_the_extractor_cannot_read_is_a_failure_not_a_skip(self, scratch_js, registry):
        plant(
            scratch_js,
            "dope.js",
            "const result = await apiGet(`/api/loads/${Number(root.dataset.loadId)}/dope`);",
            "const result = await apiGet(pathForLoad(root));",
        )

        with pytest.raises(Unparsed):
            check_client(load_js_files(scratch_js), registry)

    def test_a_new_direct_fetch_is_reported(self, scratch_js, registry):
        (scratch_js / "sneaky.js").write_text('const r = await fetch("/api/rifles");\n')

        assert any(
            "direct fetch" in p for p in check_client(load_js_files(scratch_js), registry).problems
        )

    def test_an_api_function_used_as_a_value_is_a_failure(self, scratch_js, registry):
        (scratch_js / "indirect.js").write_text(
            'import { apiGet } from "/static/js/api.js";\nconst get = apiGet;\n'
        )

        with pytest.raises(Unparsed):
            check_client(load_js_files(scratch_js), registry)

    def test_a_planted_defect_in_every_page_script_is_reachable(self, scratch_js, registry):
        files = {filename for filename, *_ in PLANTS.values()}
        assert {
            "zeroing.js",
            "group_form.js",
            "group_context.js",
            "analysis.js",
            "review.js",
            "reference.js",
        } <= files
