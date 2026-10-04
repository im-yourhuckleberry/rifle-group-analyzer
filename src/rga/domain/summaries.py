"""Plain-text summaries of groups and shooting days, written to stand alone when read back later.

Pure and deterministic: the same inputs always give the same text. Size is reported as extreme
spread only (center to center). A zero check group, or a step 2 / confirm zeroing group with fewer
than 10 shots, reports its size and shot count but no point of impact, offset, or direction words.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from rga.domain.types import GroupPurpose, GroupStats, Position
from rga.domain.zeroing import masks_poi

_POSITION_LABELS = {
    Position.BENCH: "bench",
    Position.PRONE_OFF_PACK: "prone off pack",
    Position.KNEELING_SITTING: "kneeling or sitting",
    Position.STANDING_FIELD: "standing (field)",
}
_ZERO_CHECK_NOTE = (
    "Zero check: this group is judged pass or fail against the dot, so no point of impact is "
    "reported."
)
_ES_LABEL = "smallest target every shot hit, center to center"
_STEP1_OVER_THREE = (
    "Step 1 only corrects from 1 to 3 shots, so this group reports no point of impact."
)
_ZEROING_IN_PROGRESS = (
    "Zeroing in progress: this group has fewer than 10 shots, so no point of impact is reported."
)


@dataclass(frozen=True)
class SummaryGroup:
    """One group as the summary needs it; `shots` counts every shot, estimated included."""

    public_id: str
    load_label: str
    lot_code: str
    shots: int
    stats: GroupStats
    notes: str
    shot_date: str
    distance_yd: int
    position: Position
    suppressor: bool
    shot_time: str | None = None
    legacy_notes: str = ""
    purpose: GroupPurpose = GroupPurpose.GENERAL


def _when(group: SummaryGroup) -> str:
    return (
        group.shot_date if group.shot_time is None else f"{group.shot_date} {group.shot_time[:5]}"
    )


def _context_line(rifle_label: str, group: SummaryGroup) -> str:
    suppressor = "suppressed" if group.suppressor else "unsuppressed"
    return (
        f"Shot on {_when(group)} with {rifle_label} at {group.distance_yd} yd, "
        f"{_POSITION_LABELS[group.position]}, {suppressor}."
    )


def _notes_line(label: str, notes: str) -> str:
    text = " ".join(notes.split())
    return f"{label}: {text if text else 'none'}"


def _poi_text(stats: GroupStats) -> str:
    vertical = _axis_text(stats.mean_dy_mil, stats.mean_dy_in, "high", "low")
    horizontal = _axis_text(stats.mean_dx_mil, stats.mean_dx_in, "right", "left")
    parts = [part for part in (vertical, horizontal) if part]
    return ", ".join(parts) if parts else "centered on the aim point"


def _axis_text(mil: float, inches: float, positive: str, negative: str) -> str:
    if round(mil, 2) == 0.0:
        return ""
    word = positive if mil > 0 else negative
    return f"{abs(mil):.2f} mil {word} ({abs(inches):.2f} in)"


def _poi_note(group: SummaryGroup) -> str | None:
    if not masks_poi(group.purpose, group.shots):
        return None
    if group.purpose == GroupPurpose.ZERO_CHECK:
        return _ZERO_CHECK_NOTE
    if group.purpose == GroupPurpose.ZERO_STEP1:
        return _STEP1_OVER_THREE
    return _ZEROING_IN_PROGRESS


def _group_body(group: SummaryGroup) -> list[str]:
    stats = group.stats
    lines = [
        f"Group {group.public_id}: {group.load_label} (lot {group.lot_code}), {group.shots} shots.",
        f"Extreme spread {stats.es_in:.2f} in ({stats.es_moa:.2f} MOA), {_ES_LABEL}.",
    ]
    note = _poi_note(group)
    if note is not None:
        lines.append(note)
    else:
        lines.append(f"Mean point of impact: {_poi_text(stats)}.")
    if stats.includes_estimated:
        lines.append(
            "Includes estimated shots: best-guess positions inside ragged holes, counted in the "
            "statistics."
        )
    lines.append(_notes_line("Group notes", group.notes))
    if group.legacy_notes.strip():
        lines.append(_notes_line("Legacy notes", group.legacy_notes))
    return lines


def group_summary(rifle_label: str, group: SummaryGroup) -> str:
    """Standalone text for one group: when, where and how it was shot, load and lot, shots, ES (in
    and MOA), mean POI in mil with direction words, an estimated-shots note, and the notes.

    A group the zeroing rules mask (`domain.zeroing.masks_poi`: any ZERO_CHECK group, a ZERO_STEP2
    or ZERO_CONFIRM group under 10 shots, a ZERO_STEP1 group over 3 shots) reports no point of impact.
    Raises ValueError if the group has no shots.
    """
    if group.shots < 1:
        raise ValueError("a group summary needs at least one shot")
    return "\n".join([_context_line(rifle_label, group), *_group_body(group)])


def day_summary(rifle_label: str, day: str, groups: Sequence[SummaryGroup]) -> str:
    """Standalone text for one shooting day: each group in the given order with its own context."""
    lines = [f"{day} with {rifle_label}."]
    if not groups:
        lines.append("No groups recorded.")
    for number, group in enumerate(groups, start=1):
        if group.shots < 1:
            raise ValueError("a group summary needs at least one shot")
        lines.append("")
        lines.append(f"Group {number} of {len(groups)}")
        lines.append(_context_line(rifle_label, group))
        lines.extend(_group_body(group))
    return "\n".join(lines)
