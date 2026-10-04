"""D-14 guard: no mean radius, best-N or group-reduction metric exists anywhere under src/."""

import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
_SEP = r"(?:[\s_-]|&nbsp;|&#160;|&#xa0;)"
FORBIDDEN = re.compile(
    rf"mean{_SEP}*radius"
    rf"|best{_SEP}*of(?![a-z])|(?-i:[bB]estOf[A-Z0-9])"
    rf"|best{_SEP}*n(?![a-z])|(?-i:[bB]estN[A-Z0-9])"
    rf"|best{_SEP}*\d"
    rf"|group{_SEP}*reduc",
    re.IGNORECASE,
)


def scan(root: Path) -> tuple[int, list[str]]:
    """Return (files scanned, 'path:line: text' hits) for every readable text file under root."""
    scanned, hits = 0, []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        if FORBIDDEN.search(path.name):
            hits.append(f"{path}: file name")
        hits.extend(
            f"{path}:{n}: {line.strip()}"
            for n, line in enumerate(text.splitlines(), 1)
            if FORBIDDEN.search(line)
        )
    return scanned, hits


def test_src_contains_no_forbidden_metric():
    scanned, hits = scan(SRC_ROOT)
    assert scanned > 20, "scanner found almost no files; the guard would be checking nothing"
    assert hits == []


def test_scan_covers_python_templates_and_static_js():
    suffixes = {p.suffix for p in SRC_ROOT.rglob("*") if p.is_file()}
    assert {".py", ".html", ".js", ".sql"} <= suffixes


@pytest.mark.parametrize(
    "line",
    [
        "def mean_radius(offsets): ...",
        "MEAN_RADIUS = 1",
        "meanRadius",
        "mean_radius_in",
        "x = best_of_5(shots)",
        "best_of",
        "def best_n(shots, n=5): ...",
        "bestOf3",
        "bestN = 3",
        "group_reduction_pct",
        "GroupReduction = 1",
        "<th>Mean radius</th>",
        "mean-radius",
        "meanRadiusIn",
        "Mean Radius (in)",
        "Best-of-3",
        "Best of 3 groups",
        "best 5 of 10",
        "best_3_groups",
        "bestOfN",
        "bestNShots",
        "best-n",
        "group reduction",
        "Group-Reduction",
        "groupReduction",
        "group_reduce(shots)",
        "BestOfN",
        "BestOfThree",
        "class BestOfGroups:",
        "BestNGroups",
        "<th>Mean&nbsp;radius</th>",
        "Mean&nbsp;&nbsp;Radius",
        "Mean&#160;radius",
        "mean__radius",
        "mean  radius",
        "mean - radius",
        "mean_-radius",
        "<th>Best&nbsp;of&nbsp;3</th>",
        "<th>Group&nbsp;reduction</th>",
        "best  of 3",
        "Best&#160;of&#160;N",
        "best&#xA0;of",
        "best&#xa0;&#xA0;n",
        "Best&nbsp;5 groups",
        "best&#160;3",
        "<th>Group&#160;&#xA0;Reduction</th>",
        "group__reduction",
        "group  -  reduction",
        "Mean&#xA0;radius",
        "mean&#160;&#160;radius",
        "best__of",
        "best - n",
        "best  5 of 10",
        "Best&nbsp;&nbsp;3 groups",
        "best&#160;&#xA0;7",
    ],
)
def test_pattern_flags_each_forbidden_identifier(line, tmp_path):
    (tmp_path / "planted.py").write_text(f"{line}\n", encoding="utf-8")
    _, hits = scan(tmp_path)
    assert len(hits) == 1


@pytest.mark.parametrize("suffix", [".py", ".js", ".html", ".sql", ".md"])
def test_scan_finds_a_plant_in_any_text_file_type(suffix, tmp_path):
    (tmp_path / f"file{suffix}").write_text("ok\nmean_radius\n", encoding="utf-8")
    scanned, hits = scan(tmp_path)
    assert scanned == 1
    assert hits[0].endswith(":2: mean_radius")


@pytest.mark.parametrize(
    ("suffix", "line"),
    [
        (".html", "<th>Mean radius</th>"),
        (".html", "<td>Best-of-3 groups</td>"),
        (".js", 'const label = "Mean radius";'),
        (".js", "const meanRadiusIn = stats.meanRadiusIn;"),
        (".js", "bestOf3(groups)"),
        (".js", "class BestOfGroups {}"),
        (".js", "export const BestNGroups = 3;"),
        (".html", "<th>Mean&nbsp;radius</th>"),
        (".py", "# group reduction vs the zero"),
        (".py", "label = 'best 5 of 10'"),
    ],
)
def test_pattern_flags_prose_labels_in_templates_js_and_python(suffix, line, tmp_path):
    (tmp_path / f"page{suffix}").write_text(f"{line}\n", encoding="utf-8")
    _, hits = scan(tmp_path)
    assert len(hits) == 1


def test_scan_flags_a_forbidden_file_name(tmp_path):
    (tmp_path / "best_of_n.py").write_text("x = 1\n", encoding="utf-8")
    _, hits = scan(tmp_path)
    assert hits == [f"{tmp_path / 'best_of_n.py'}: file name"]


@pytest.mark.parametrize(
    "line",
    [
        "best_offset = 1",
        "bestow",
        "best_effort_cleanup()",
        "extreme_spread_in(offsets)",
        "mean_dx_in, mean_dy_in",
        "group_stats(offsets)",
        "groups_reduce = False",
        "bestOffset = 1",
        "best_offset_in",
        "bestEffort()",
        "best effort cleanup",
        "best_effort",
        "best number of clicks",
        "meanwhile the radius is fixed",
        "mean_dx, radius_in",
        "BestOffset",
        "BestNumber",
        "BEST_OFFSET",
        "BestEffort",
        "class BestPractices:",
        "mean value of the radius_in column",
        "meaningful radius",
        "<th>Best&nbsp;offset</th>",
        "best&nbsp;effort",
        "best&#160;number of clicks",
        "<td>Best&nbsp;bet</td>",
        "group&nbsp;stats",
        "groups&nbsp;reduced",
        "mean&nbsp;value",
        "the&nbsp;best&nbsp;ofa&nbsp;kind",
        "&nbsp;&nbsp;best&#xA0;nonsense",
        "best price",
        "group redundancy",
    ],
)
def test_pattern_does_not_flag_legitimate_names(line, tmp_path):
    (tmp_path / "ok.py").write_text(f"{line}\n", encoding="utf-8")
    _, hits = scan(tmp_path)
    assert hits == []


def test_scan_skips_pycache_and_binary_files(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "x.py").write_text("mean_radius\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00mean_radius")
    scanned, hits = scan(tmp_path)
    assert (scanned, hits) == (0, [])
