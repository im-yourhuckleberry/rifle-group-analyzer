"""Grep gate for B-66: nothing under `src/` may say "session" (the user removed sessions).

Fails on `\\bsessions?\\b|session_id` anywhere in `src/` (py, html, js, css, anything else), except in
`src/rga/db/migrations/`. Allowed, and nothing else:
- `legacy_session*` names (the migrated session id and notes columns);
- one named exception (B-73): the line defining `ShotTimeSource.SESSION = "session"` in
  `src/rga/domain/types.py`, the stored database value.

There is no module-name exception: no module, import, alias or path in `src/` says "sessions".
"""

from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
MIGRATIONS = Path("rga") / "db" / "migrations"

FORBIDDEN = re.compile(r"\bsessions?\b|session_id")
LEGACY = re.compile(r"legacy_session\w*")
BINARY_SUFFIXES = {".onnx"}  # the seg-v1 hole-detector weights (rga/vision/learned/models)

NAMED_EXCEPTION_FILE = Path("rga") / "domain" / "types.py"
NAMED_EXCEPTION_LINE = 'SESSION = "session"'


@dataclass(frozen=True)
class Violation:
    """One forbidden occurrence."""

    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.text.strip()[:120]}"


@dataclass
class Scan:
    """Result of one scan: violations plus how much was inspected."""

    violations: list[Violation]
    files: dict[str, int]
    code_names: int = 0
    prose_chunks: int = 0


def _says_session(text: str) -> bool:
    return FORBIDDEN.search(LEGACY.sub("", text)) is not None


def _scan_python(source: str, relative: Path, scan: Scan) -> None:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        scan.violations.append(Violation(str(relative), 0, f"cannot tokenize: {exc}"))
        return
    lines = source.splitlines()
    logical: list[tokenize.TokenInfo] = []
    for token in tokens:
        if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            _scan_logical_line(logical, lines, relative, scan)
            logical = []
        elif token.type not in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            logical.append(token)
    _scan_logical_line(logical, lines, relative, scan)


def _scan_logical_line(
    logical: list[tokenize.TokenInfo], lines: list[str], relative: Path, scan: Scan
) -> None:
    if not logical:
        return
    first, last = logical[0], logical[-1]
    joined = " ".join(lines[first.start[0] - 1 : last.end[0]]).strip()
    if relative == NAMED_EXCEPTION_FILE and joined == NAMED_EXCEPTION_LINE:
        return
    for token in logical:
        if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_MIDDLE):
            scan.prose_chunks += 1
        elif token.type == tokenize.NAME:
            scan.code_names += 1
        else:
            continue
        if _says_session(token.string):
            scan.violations.append(Violation(str(relative), token.start[0], token.line))


def scan_source_tree(root: Path = SRC) -> Scan:
    """Scan every text file under `root` except the migrations directory."""
    scan = Scan(violations=[], files={})
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        if relative.parts[: len(MIGRATIONS.parts)] == MIGRATIONS.parts:
            continue
        if relative.suffix in BINARY_SUFFIXES:  # model weights: scan the raw bytes instead
            scan.files[relative.suffix] = scan.files.get(relative.suffix, 0) + 1
            if FORBIDDEN.search(path.read_bytes().decode("latin-1")):
                scan.violations.append(Violation(str(relative), 0, "binary file says session"))
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            scan.violations.append(Violation(str(relative), 0, "not valid UTF-8 text"))
            continue
        scan.files[relative.suffix] = scan.files.get(relative.suffix, 0) + 1
        if relative.suffix == ".py":
            _scan_python(source, relative, scan)
        else:
            for number, line in enumerate(source.splitlines(), start=1):
                scan.prose_chunks += 1
                if _says_session(line):
                    scan.violations.append(Violation(str(relative), number, line))
    return scan


# --- the real tree --------------------------------------------------------------------------------


def test_src_never_says_session() -> None:
    scan = scan_source_tree()
    assert [str(v) for v in scan.violations] == []


def test_the_gate_inspected_the_whole_tree() -> None:
    scan = scan_source_tree()
    assert scan.files.get(".py", 0) >= 50
    assert scan.files.get(".html", 0) >= 10
    assert scan.files.get(".js", 0) >= 10
    assert scan.files.get(".css", 0) >= 1
    assert scan.code_names > 20000 and scan.prose_chunks > 4000


def test_the_migrations_directory_is_the_only_excluded_place() -> None:
    migrations = SRC / MIGRATIONS
    assert migrations.is_dir()
    assert any(
        re.search(r"\bsessions?\b", p.read_text(encoding="utf-8")) for p in migrations.glob("*.sql")
    )

    def real_files(folder: Path) -> list[Path]:
        return [
            p
            for p in folder.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ]

    assert sum(scan_source_tree().files.values()) == len(real_files(SRC)) - len(
        real_files(migrations)
    )


def test_no_module_file_or_package_under_src_is_named_for_sessions() -> None:
    named = [
        p for p in SRC.rglob("*") if "session" in p.name.lower() and "__pycache__" not in p.parts
    ]
    assert [str(p.relative_to(SRC)) for p in named] == []


# --- the scanner itself: planted samples ----------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


FORBIDDEN_SAMPLES = {
    "comment": ("rga/a.py", "x = 1  # the session is gone\n"),
    "docstring": ("rga/a.py", '"""Handle one session."""\n'),
    "string": ("rga/a.py", 'LABEL = "sessions"\n'),
    "f-string text": ("rga/a.py", 'x = f"{1} sessions"\n'),
    "session_id": ("rga/a.py", "def f(session_id):\n    return session_id\n"),
    "bare variable": ("rga/a.py", "sessions = []\n"),
    "variable use": ("rga/a.py", "total = len(sessions)\n"),
    "call target": ("rga/a.py", "sessions()\n"),
    "subscript": ("rga/a.py", "first = sessions[0]\n"),
    "uppercase attribute": ("rga/a.py", "x = sessions.Thing\n"),
    "prose after an import": ("rga/a.py", "import os  # sessions live here\n"),
    "dotted module path in an import": (
        "rga/a.py",
        "from rga.services.sessions import GroupView\nimport rga.web.routes.sessions\n",
    ),
    "bare module import": (
        "rga/a.py",
        "from rga.services import sessions as service\nfrom rga.web.routes import sessions\n",
    ),
    "module path chain in code": ("rga/a.py", "view = rga.services.sessions.group_view\n"),
    "module attribute access": (
        "rga/a.py",
        "view = sessions.group_view(conn, 1)\nsessions._private()\nz = sessions.value\n",
    ),
    "docstring mentioning a call": (
        "rga/a.py",
        '"""Calls sessions.group_view(...) to load it."""\n',
    ),
    "module paths in prose": (
        "rga/a.py",
        '"""See routes/sessions.py and services/sessions.py."""  # rga.web.routes.sessions\n',
    ),
    "module paths in a template": (
        "rga/web/templates/x.html",
        "<!-- generated from routes/sessions.py -->\n",
    ),
    "template": ("rga/web/templates/x.html", "<h1>Start a session</h1>\n"),
    "javascript": ("rga/web/static/js/x.js", 'const label = "sessions";\n'),
    "stylesheet": ("rga/web/static/css/x.css", "/* session card */\n.x { color: red; }\n"),
    "other file type": ("rga/notes.txt", "see the sessions page\n"),
    "named exception in another file": (
        "rga/domain/other.py",
        'class E:\n    SESSION = "session"\n',
    ),
    "other definition in the exception file": (
        "rga/domain/types.py",
        'class E:\n    OTHER = "session"\n',
    ),
    "untokenizable python": ("rga/a.py", "def broken(:\n"),
}


@pytest.mark.parametrize("name", FORBIDDEN_SAMPLES)
def test_planted_session_words_are_found(tmp_path: Path, name: str) -> None:
    path, content = FORBIDDEN_SAMPLES[name]
    scan = scan_source_tree(_tree(tmp_path, {path: content}))
    assert scan.violations, f"{name} was not flagged"


ALLOWED_SAMPLES = {
    "legacy columns": (
        "rga/a.py",
        'x = row.legacy_session_id\ny = "legacy_session_notes"  # legacy_session_id\n',
    ),
    "the named exception": (
        "rga/domain/types.py",
        'class ShotTimeSource:\n    SESSION = "session"\n',
    ),
    "migration sql": ("rga/db/migrations/005_x.sql", "UPDATE sessions SET session_id = 1;\n"),
    "bytecode cache": ("rga/__pycache__/a.cpython-314.pyc", "sessions"),
    "clean code": ("rga/a.py", '"""Group context."""\nx = 1  # group\n'),
    "the renamed modules": (
        "rga/a.py",
        "from rga.services import group_records\nfrom rga.db import repo_groups\n"
        "from rga.web.routes.groups import group_out\n",
    ),
}


@pytest.mark.parametrize("name", ALLOWED_SAMPLES)
def test_the_approved_forms_are_allowed(tmp_path: Path, name: str) -> None:
    path, content = ALLOWED_SAMPLES[name]
    scan = scan_source_tree(_tree(tmp_path, {path: content}))
    assert [str(v) for v in scan.violations] == []


def test_the_named_exception_is_exact(tmp_path: Path) -> None:
    exact = {"rga/domain/types.py": 'class ShotTimeSource:\n    SESSION = "session"\n'}
    assert scan_source_tree(_tree(tmp_path / "a", exact)).violations == []
    near = {"rga/domain/types.py": 'class ShotTimeSource:\n    SESSION = "session"  # sessions\n'}
    assert scan_source_tree(_tree(tmp_path / "b", near)).violations
    other = {"rga/domain/types.py": 'class ShotTimeSource:\n    SESSION = "sessions"\n'}
    assert scan_source_tree(_tree(tmp_path / "c", other)).violations
    elsewhere = {"rga/other.py": 'class ShotTimeSource:\n    SESSION = "session"\n'}
    assert scan_source_tree(_tree(tmp_path / "d", elsewhere)).violations
