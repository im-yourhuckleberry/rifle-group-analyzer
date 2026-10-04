import ast
import dataclasses
from pathlib import Path

import pytest

from rga.domain import types as t


def test_rejection_reason_has_exactly_one_member():
    assert [m.value for m in t.RejectionReason] == ["not_a_bullet_hole"]
    with pytest.raises(ValueError):
        t.RejectionReason("flier")


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (t.ReferenceKind, ["grid_1in", "circle_1in", "circle_1_5in", "circle_3in"]),
        (t.ShotOrigin, ["detected", "split", "estimated", "manual"]),
        (t.BlobKind, ["single", "split", "ragged"]),
        (t.LoadKind, ["factory", "handload"]),
        (t.Position, ["bench", "prone_off_pack", "kneeling_sitting", "standing_field"]),
        (t.GroupPurpose, ["general", "zero_step1", "zero_step2", "zero_confirm", "zero_check"]),
        (t.GroupStatus, ["draft", "final"]),
        (
            t.ZeroState,
            ["step1", "step2_initial", "step2_confirm", "step3_dot", "complete", "superseded"],
        ),
        (
            t.AdviceKind,
            [
                "correction", "no_adjustment", "shoot_more", "touch_up_ok",
                "confirm_again", "ready_for_dot", "invalid",
            ],
        ),
    ],
)  # fmt: skip
def test_enum_values_match_design_and_are_strings(enum, values):
    assert [m.value for m in enum] == values
    assert all(isinstance(m, str) for m in enum)
    assert [enum(v) for v in values] == list(enum)


def test_reference_sizes_cover_every_kind():
    assert set(t.REFERENCE_SIZE_IN) == set(t.ReferenceKind)
    assert t.REFERENCE_SIZE_IN[t.ReferenceKind.GRID_1IN] == 1.0
    assert t.REFERENCE_SIZE_IN[t.ReferenceKind.CIRCLE_1IN] == 1.0
    assert t.REFERENCE_SIZE_IN[t.ReferenceKind.CIRCLE_1_5IN] == 1.5
    assert t.REFERENCE_SIZE_IN[t.ReferenceKind.CIRCLE_3IN] == 3.0


EXPECTED_FIELDS = {
    "PlanePoint": ["x_in", "y_in"],
    "Offset": ["dx_in", "dy_in"],
    "Shot": ["point", "origin", "confidence", "moved_by_user"],
    "WorkingFrame": ["ppi", "width_px", "height_px"],
    "Blob": ["kind", "contour_px", "area_px", "centers_px", "confidence"],
    "CircleCandidate": ["center_px", "axes_px", "angle_deg", "score"],
    "ReferenceResult": ["kind", "method", "h_px_to_plane", "axis_ratio", "warnings"],
    "GroupStats": [
        "n", "es_in", "es_moa", "mean_dx_in", "mean_dy_in", "mean_dx_mil", "mean_dy_mil",
        "includes_estimated",
    ],
    "Correction": ["up_mil", "right_mil", "text"],
    "Advice": ["kind", "message", "correction", "stats"],
    "ZeroCheckResult": ["passed", "per_shot_inside", "message"],
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(EXPECTED_FIELDS))
def test_dataclass_fields_match_design_in_order(name):
    cls = getattr(t, name)
    assert [f.name for f in dataclasses.fields(cls)] == EXPECTED_FIELDS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_FIELDS))
def test_dataclasses_are_frozen(name):
    assert getattr(t, name).__dataclass_params__.frozen


def test_frozen_instance_rejects_mutation():
    p = t.PlanePoint(1.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.x_in = 3.0  # type: ignore[misc]


def test_shot_defaults():
    s = t.Shot(point=t.PlanePoint(1.0, 1.0), origin=t.ShotOrigin.DETECTED)
    assert s.confidence is None
    assert s.moved_by_user is False


def test_advice_defaults():
    a = t.Advice(kind=t.AdviceKind.SHOOT_MORE, message="x")
    assert a.correction is None
    assert a.stats is None


def test_value_equality_and_hashing():
    assert t.Offset(1.0, 2.0) == t.Offset(1.0, 2.0)
    assert len({t.PlanePoint(1.0, 2.0), t.PlanePoint(1.0, 2.0)}) == 1


def test_position_has_no_sandbags():
    assert "sandbags" not in {m.value for m in t.Position}
    assert not hasattr(t.Position, "SANDBAGS")


DOMAIN_DIR = Path(t.__file__).parent
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GATED_ROOTS = ("src", "tools", "tests/support")


def _public(node) -> bool:
    return not node.name.startswith("_")


def undocumented_public_names(source: str) -> list[str]:
    """Public top-level classes and functions, public methods of public classes (properties
    included), and the module itself, each lacking a docstring."""
    tree = ast.parse(source)
    missing = [] if ast.get_docstring(tree) else ["<module>"]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _public(node):
            if not ast.get_docstring(node):
                missing.append(node.name)
        elif isinstance(node, ast.ClassDef) and _public(node):
            if not ast.get_docstring(node):
                missing.append(node.name)
            for member in node.body:
                if (
                    isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                    and _public(member)
                    and not ast.get_docstring(member)
                ):
                    missing.append(f"{node.name}.{member.name}")
    return missing


def _undocumented_in(paths) -> dict[str, list[str]]:
    return {
        str(path.relative_to(PROJECT_ROOT)): missing
        for path in paths
        if (missing := undocumented_public_names(path.read_text(encoding="utf-8")))
    }


def test_every_public_domain_class_and_function_has_a_docstring():
    files = sorted(DOMAIN_DIR.glob("*.py"))
    assert {"types.py", "units.py", "calibers.py"} <= {f.name for f in files}
    assert _undocumented_in(files) == {}


def test_every_public_name_in_src_tools_and_test_support_has_a_docstring():
    for root in GATED_ROOTS:
        assert any((PROJECT_ROOT / root).rglob("*.py")), f"{root} has no python files"
    files = sorted(p for root in GATED_ROOTS for p in (PROJECT_ROOT / root).rglob("*.py"))
    assert {"detect.py", "eval_detection.py", "live_server.py"} <= {f.name for f in files}
    assert _undocumented_in(files) == {}


def test_docstring_checker_flags_missing_docstrings():
    source = (
        "class Public:\n    pass\n\n"
        "def public():\n    pass\n\n"
        "class _Private:\n    pass\n\n"
        "def _private():\n    pass\n\n"
        'class Ok:\n    """Documented."""\n'
    )
    assert undocumented_public_names(source) == ["<module>", "Public", "public"]


def test_docstring_checker_flags_methods_and_properties_of_public_classes():
    source = (
        '"""Module."""\n\n'
        "class Shape:\n"
        '    """Documented."""\n\n'
        "    @property\n"
        "    def area(self):\n        return 1\n\n"
        "    def _hidden(self):\n        return 2\n\n"
        '    def ok(self):\n        """Documented."""\n\n'
        "    def __repr__(self):\n        return 'x'\n\n"
        "class _Internal:\n"
        "    def method(self):\n        return 3\n"
    )
    assert undocumented_public_names(source) == ["Shape.area"]
