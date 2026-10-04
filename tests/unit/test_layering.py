"""Enforces the design.md section 1 dependency rule over every module under src/rga."""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
ROOT_PKG = "rga"

# layer -> dotted module prefixes it may import from (its own layer is always allowed)
ALLOWED: dict[str, tuple[str, ...]] = {
    "domain": (),
    "vision": ("rga.domain.types",),
    "db": ("rga.domain.types",),
    "services": ("rga.domain", "rga.vision", "rga.db", "rga.config"),
    "web": ("rga.services", "rga.web", "rga.domain.types", "rga.config"),
}
# (importing module, imported prefix) pairs permitted despite the layer rule
EXCEPTIONS: frozenset[tuple[str, str]] = frozenset({("rga.web.deps", "rga.db.connection")})

LAYERS = frozenset(ALLOWED)


def _module_exists(src_root: Path, dotted: str) -> bool:
    base = src_root.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _module_name(src_root: Path, path: Path) -> tuple[str, str]:
    """Return (dotted module name, dotted containing package) for a source file."""
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


def imported_modules(src_root: Path, path: Path) -> set[str]:
    """Absolute dotted names of every rga module imported anywhere in `path`."""
    _, package = _module_name(src_root, path)
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                targets.add(candidate if _module_exists(src_root, candidate) else module)
    return {t for t in targets if t == ROOT_PKG or t.startswith(ROOT_PKG + ".")}


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def find_violations(src_root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted((src_root / ROOT_PKG).rglob("*.py")):
        importer, _ = _module_name(src_root, path)
        parts = importer.split(".")
        layer = parts[1] if len(parts) > 1 else None
        if layer not in LAYERS:
            continue
        allowed = (f"{ROOT_PKG}.{layer}", *ALLOWED[layer])
        for target in sorted(imported_modules(src_root, path)):
            if target == ROOT_PKG:
                continue
            if any(_under(target, prefix) for prefix in allowed):
                continue
            if any(importer == who and _under(target, what) for who, what in EXCEPTIONS):
                continue
            violations.append(f"{importer} imports {target}")
    return violations


def test_source_tree_respects_layering_rule():
    assert find_violations(SRC_ROOT) == []


def test_checker_sees_the_real_tree():
    domain_files = list((SRC_ROOT / ROOT_PKG / "domain").glob("*.py"))
    names = {p.stem for p in domain_files}
    assert {"types", "units", "calibers"} <= names
    units = SRC_ROOT / ROOT_PKG / "domain" / "units.py"
    assert "rga.domain.types" in imported_modules(SRC_ROOT, units)


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    src = root / "src"
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for pkg in {p for rel in files for p in Path(rel).parents if str(p) != "."}:
        (src / pkg / "__init__.py").touch()
    return src


def test_checker_flags_domain_importing_another_layer(tmp_path):
    src = _write_tree(
        tmp_path,
        {"rga/db/connection.py": "", "rga/domain/bad.py": "from rga.db import connection\n"},
    )
    assert find_violations(src) == ["rga.domain.bad imports rga.db.connection"]


def test_checker_flags_relative_and_function_local_imports(tmp_path):
    src = _write_tree(
        tmp_path,
        {
            "rga/domain/types.py": "",
            "rga/services/groups.py": "",
            "rga/vision/io.py": "",
            "rga/vision/a.py": "from ..services import groups\n",
            "rga/db/b.py": "def f():\n    import rga.vision.io\n",
        },
    )
    assert find_violations(src) == [
        "rga.db.b imports rga.vision.io",
        "rga.vision.a imports rga.services.groups",
    ]


def test_checker_flags_vision_and_db_importing_more_than_types(tmp_path):
    src = _write_tree(
        tmp_path,
        {
            "rga/domain/types.py": "",
            "rga/domain/units.py": "",
            "rga/vision/ok.py": "from rga.domain.types import Blob\nfrom rga.domain import types\n",
            "rga/vision/bad.py": "from rga.domain import units\n",
            "rga/db/bad.py": "from rga.domain.units import round_tenth\n",
        },
    )
    assert find_violations(src) == [
        "rga.db.bad imports rga.domain.units",
        "rga.vision.bad imports rga.domain.units",
    ]


def test_checker_flags_web_importing_db_or_vision_except_deps(tmp_path):
    src = _write_tree(
        tmp_path,
        {
            "rga/db/connection.py": "",
            "rga/vision/io.py": "",
            "rga/services/groups.py": "",
            "rga/web/schemas.py": "",
            "rga/web/deps.py": "from rga.db.connection import connect\n",
            "rga/web/routes/r.py": "from rga.db import connection\nimport rga.vision.io\n",
            "rga/web/routes/s.py": "from rga.services import groups\nfrom rga.web import schemas\n",
        },
    )
    assert find_violations(src) == [
        "rga.web.routes.r imports rga.db.connection",
        "rga.web.routes.r imports rga.vision.io",
    ]


def test_checker_allows_services_to_use_all_lower_layers(tmp_path):
    src = _write_tree(
        tmp_path,
        {
            "rga/domain/units.py": "",
            "rga/vision/pipeline.py": "",
            "rga/db/connection.py": "",
            "rga/config.py": "",
            "rga/services/s.py": (
                "from rga.domain import units\nfrom rga.vision import pipeline\n"
                "from rga.db import connection\nfrom rga import config\n"
            ),
        },
    )
    assert find_violations(src) == []
