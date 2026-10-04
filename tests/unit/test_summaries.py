"""T1 tests for rga.domain.summaries: standalone, deterministic group and day text."""

import dataclasses
import random
import re
from pathlib import Path

import pytest

from rga.domain import summaries
from rga.domain.summaries import (
    SummaryGroup,
    day_summary,
    group_summary,
)
from rga.domain.types import GroupPurpose, GroupStats, Position

DIRECTION_WORDS = re.compile(r"\b(?:high|low|left|right|up|down)\b", re.I)


def make_stats(
    n=10,
    es_in=1.234,
    es_moa=1.179,
    dx_mil=0.31,
    dy_mil=-0.21,
    dx_in=1.116,
    dy_in=-0.756,
    estimated=False,
) -> GroupStats:
    return GroupStats(
        n=n,
        es_in=es_in,
        es_moa=es_moa,
        mean_dx_in=dx_in,
        mean_dy_in=dy_in,
        mean_dx_mil=dx_mil,
        mean_dy_mil=dy_mil,
        includes_estimated=estimated,
    )


RIFLE = "Tikka T3x 6.5 Creedmoor"
DAY = "2026-09-30"


def make_group(**overrides) -> SummaryGroup:
    base = {
        "public_id": "grp-2222",
        "load_label": "Hornady 140 ELD-M",
        "lot_code": "L123",
        "shots": 10,
        "stats": make_stats(),
        "notes": "Cold bore first",
        "shot_date": "2026-09-30",
        "distance_yd": 100,
        "position": Position.BENCH,
        "suppressor": True,
        "legacy_notes": "Light mirage",
        "purpose": GroupPurpose.GENERAL,
    }
    return SummaryGroup(**{**base, **overrides})


class TestGroupSummary:
    def test_exact_text_for_a_general_group(self):
        text = group_summary(RIFLE, make_group())

        assert text == (
            "Shot on 2026-09-30 with Tikka T3x 6.5 Creedmoor at 100 yd, bench, suppressed.\n"
            "Group grp-2222: Hornady 140 ELD-M (lot L123), 10 shots.\n"
            "Extreme spread 1.23 in (1.18 MOA), smallest target every shot hit, center to "
            "center.\n"
            "Mean point of impact: 0.21 mil low (0.76 in), 0.31 mil right (1.12 in).\n"
            "Group notes: Cold bore first\n"
            "Legacy notes: Light mirage"
        )

    def test_it_stands_alone_with_every_required_fact(self):
        text = group_summary(RIFLE, make_group(distance_yd=300, shots=12))

        for fact in (
            "2026-09-30",
            "Tikka T3x 6.5 Creedmoor",
            "Hornady 140 ELD-M",
            "lot L123",
            "300 yd",
            "12 shots",
            "1.23 in",
            "1.18 MOA",
            "mil",
            "Cold bore first",
            "Light mirage",
            "grp-2222",
        ):
            assert fact in text

    @pytest.mark.parametrize(
        ("dx_mil", "dy_mil", "expected"),
        [
            (0.31, 0.2, "0.20 mil high (0.50 in), 0.31 mil right (0.50 in)"),
            (-0.31, 0.2, "0.20 mil high (0.50 in), 0.31 mil left (0.50 in)"),
            (0.31, -0.2, "0.20 mil low (0.50 in), 0.31 mil right (0.50 in)"),
            (-0.31, -0.2, "0.20 mil low (0.50 in), 0.31 mil left (0.50 in)"),
            (0.0, 0.2, "0.20 mil high (0.50 in)"),
            (0.31, 0.0, "0.31 mil right (0.50 in)"),
            (0.0, 0.0, "centered on the aim point"),
            (0.004, -0.004, "centered on the aim point"),
        ],
    )
    def test_mean_poi_uses_direction_words_in_mil(self, dx_mil, dy_mil, expected):
        stats = make_stats(dx_mil=dx_mil, dy_mil=dy_mil, dx_in=0.5, dy_in=0.5)

        text = group_summary(RIFLE, make_group(stats=stats))

        assert f"Mean point of impact: {expected}." in text

    def test_a_tiny_negative_offset_never_prints_negative_zero(self):
        stats = make_stats(dx_mil=-0.001, dy_mil=-0.001)

        text = group_summary(RIFLE, make_group(stats=stats))

        assert "-0.00" not in text
        assert "Mean point of impact: centered on the aim point." in text

    def test_estimated_shots_are_noted_only_when_present(self):
        with_estimated = group_summary(RIFLE, make_group(stats=make_stats(estimated=True)))
        without = group_summary(RIFLE, make_group())

        assert "Includes estimated shots" in with_estimated
        assert "estimated" not in without

    def test_blank_notes_read_as_none_and_whitespace_is_collapsed(self):
        text = group_summary(RIFLE, make_group(legacy_notes="  \n ", notes="line one\n  line two"))
        assert "Group notes: line one line two" in text
        assert "Legacy notes" not in text
        assert group_summary(RIFLE, make_group(notes="  ", legacy_notes="")).endswith(
            "Group notes: none"
        )

    @pytest.mark.parametrize(
        ("position", "label"),
        [
            (Position.BENCH, "bench"),
            (Position.PRONE_OFF_PACK, "prone off pack"),
            (Position.KNEELING_SITTING, "kneeling or sitting"),
            (Position.STANDING_FIELD, "standing (field)"),
        ],
    )
    def test_position_and_suppressor_are_spelled_out(self, position, label):
        loud = group_summary(RIFLE, make_group(position=position, suppressor=False))
        quiet = group_summary(RIFLE, make_group(position=position, suppressor=True))

        assert f", {label}, unsuppressed." in loud
        assert f", {label}, suppressed." in quiet

    def test_it_is_deterministic(self):
        assert group_summary(RIFLE, make_group()) == group_summary(RIFLE, make_group())

    def test_extreme_spread_is_the_only_size_metric(self):
        text = group_summary(RIFLE, make_group())

        assert not re.search(r"radius|best[- ]?of|reduction|mean r", text, re.I)
        assert "Extreme spread" in text and "center to center" in text

    def test_a_group_without_shots_is_rejected(self):
        with pytest.raises(ValueError, match="at least one shot"):
            group_summary(RIFLE, make_group(shots=0))


class TestGroupContext:
    def test_the_shot_time_is_shown_to_the_minute_when_known(self):
        timed = group_summary(RIFLE, make_group(shot_time="07:45:59"))
        untimed = group_summary(RIFLE, make_group(shot_time=None))

        assert timed.startswith("Shot on 2026-09-30 07:45 with ")
        assert untimed.startswith("Shot on 2026-09-30 with ")

    def test_the_context_belongs_to_the_group_not_to_a_shared_record(self):
        a = group_summary(RIFLE, make_group(distance_yd=100, shot_date="2026-09-01"))
        b = group_summary(RIFLE, make_group(distance_yd=500, shot_date="2026-10-02"))

        assert "2026-09-01" in a and "100 yd" in a and "500 yd" not in a
        assert "2026-10-02" in b and "500 yd" in b and "100 yd" not in b

    def test_legacy_notes_appear_only_when_there_are_some(self):
        assert "Legacy notes: Light mirage" in group_summary(RIFLE, make_group())
        assert "Legacy notes" not in group_summary(RIFLE, make_group(legacy_notes=""))

    def test_no_text_mentions_a_session(self):
        text = day_summary(RIFLE, DAY, [make_group(), make_group(shot_time="09:00:00")])

        assert not re.search(r"session", text, re.I)


class TestShortZeroingGroups:
    """B-09 / B-14: no mil POI, offsets or direction words for short step 2 / confirm groups."""

    @pytest.mark.parametrize("purpose", [GroupPurpose.ZERO_STEP2, GroupPurpose.ZERO_CONFIRM])
    @pytest.mark.parametrize("shots", range(1, 10))
    def test_only_size_and_a_zeroing_in_progress_note_are_reported(self, purpose, shots):
        stats = make_stats(n=shots, dx_mil=0.9, dy_mil=-0.7, dx_in=3.2, dy_in=-2.5)
        group = make_group(shots=shots, stats=stats, purpose=purpose, notes="steady")

        text = group_summary(RIFLE, group)

        assert f"{shots} shots" in text
        assert "Extreme spread 1.23 in (1.18 MOA)" in text
        assert "Zeroing in progress" in text
        assert "Mean point of impact" not in text
        assert not DIRECTION_WORDS.search(text)
        assert not re.search(r"\bmil\b|0\.90|0\.70|3\.20|2\.50", text)

    @pytest.mark.parametrize("purpose", [GroupPurpose.ZERO_STEP2, GroupPurpose.ZERO_CONFIRM])
    def test_ten_shots_report_the_point_of_impact_again(self, purpose):
        text = group_summary(RIFLE, make_group(shots=10, purpose=purpose))

        assert "Mean point of impact: 0.21 mil low" in text
        assert "Zeroing in progress" not in text

    @pytest.mark.parametrize("shots", [1, 2, 3, 10, 15])
    def test_a_zero_check_group_of_any_size_reports_no_point_of_impact(self, shots):
        stats = make_stats(n=shots, dx_mil=0.9, dy_mil=-0.7, dx_in=3.2, dy_in=-2.5)
        group = make_group(
            shots=shots, stats=stats, purpose=GroupPurpose.ZERO_CHECK, notes="steady"
        )

        text = group_summary(RIFLE, group)

        assert f"{shots} shots" in text
        assert "Extreme spread 1.23 in (1.18 MOA)" in text
        assert "Zero check: this group is judged pass or fail against the dot" in text
        assert "Mean point of impact" not in text
        assert "Zeroing in progress" not in text
        assert not DIRECTION_WORDS.search(text)
        assert not re.search(r"\bmil\b|0\.90|0\.70|3\.20|2\.50", text)

    def test_zero_check_in_a_day_summary_hides_only_its_own_point_of_impact(self):
        check = make_group(
            public_id="grp-check",
            shots=2,
            purpose=GroupPurpose.ZERO_CHECK,
            notes="",
            stats=make_stats(n=2),
        )
        full = make_group(public_id="grp-full")

        text = day_summary(RIFLE, DAY, [check, full])
        check_block, full_block = text.split("Group 2 of 2")

        assert "Mean point of impact" not in check_block and "Zero check:" in check_block
        assert "Mean point of impact: 0.21 mil low" in full_block

    @pytest.mark.parametrize("purpose", [GroupPurpose.GENERAL, GroupPurpose.ZERO_STEP1])
    def test_other_purposes_keep_the_point_of_impact_even_when_short(self, purpose):
        text = group_summary(RIFLE, make_group(shots=3, purpose=purpose))

        assert "Mean point of impact: 0.21 mil low" in text
        assert "Zeroing in progress" not in text

    def test_day_summary_hides_the_poi_of_a_short_zero_group_but_not_its_neighbors(self):
        short = make_group(
            public_id="grp-short",
            shots=5,
            purpose=GroupPurpose.ZERO_STEP2,
            notes="",
            stats=make_stats(n=5),
        )
        full = make_group(public_id="grp-full")

        text = day_summary(RIFLE, DAY, [short, full])
        short_block, full_block = text.split("Group 2 of 2")

        assert "Mean point of impact" not in short_block
        assert "Zeroing in progress" in short_block
        assert "Mean point of impact: 0.21 mil low" in full_block


class TestPurposeGivenAsPlainString:
    """`GroupRow.purpose` is a str; the mapper may pass it through unchanged (G2-W4)."""

    @pytest.mark.parametrize("shots", [1, 2, 3, 10, 15])
    def test_a_str_zero_check_purpose_is_masked_at_any_size(self, shots):
        group = make_group(shots=shots, purpose="zero_check", stats=make_stats(n=shots))

        text = group_summary(RIFLE, group)

        assert "Mean point of impact" not in text
        assert "Zero check: this group is judged pass or fail against the dot" in text
        assert not DIRECTION_WORDS.search(text)
        assert not re.search(r"\bmil\b", text)

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    @pytest.mark.parametrize("shots", [1, 9])
    def test_a_str_step_2_or_confirm_purpose_is_masked_below_ten_shots(self, purpose, shots):
        text = group_summary(RIFLE, make_group(shots=shots, purpose=purpose))

        assert "Mean point of impact" not in text
        assert "Zeroing in progress" in text
        assert not DIRECTION_WORDS.search(text)
        assert not re.search(r"\bmil\b", text)

    @pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
    def test_a_str_step_2_or_confirm_purpose_shows_the_poi_at_ten_shots(self, purpose):
        text = group_summary(RIFLE, make_group(shots=10, purpose=purpose))

        assert "Mean point of impact: 0.21 mil low" in text

    @pytest.mark.parametrize("purpose", ["general", "zero_step1"])
    def test_a_str_general_or_step_1_purpose_keeps_the_poi(self, purpose):
        text = group_summary(RIFLE, make_group(shots=2, purpose=purpose))

        assert "Mean point of impact: 0.21 mil low" in text

    def test_day_summary_masks_str_purposes_and_keeps_the_others(self):
        groups = [
            make_group(public_id="g-check", shots=2, purpose="zero_check", stats=make_stats(n=2)),
            make_group(public_id="g-step2", shots=4, purpose="zero_step2", stats=make_stats(n=4)),
            make_group(
                public_id="g-confirm", shots=3, purpose="zero_confirm", stats=make_stats(n=3)
            ),
            make_group(public_id="g-general", shots=5, purpose="general"),
        ]

        text = day_summary(RIFLE, DAY, groups)

        blocks = text.split("\n\nGroup ")
        assert [("Mean point of impact" in b) for b in blocks[1:]] == [False, False, False, True]
        assert text.count("Mean point of impact") == 1
        assert not DIRECTION_WORDS.search("\n".join(blocks[1:4]))


class TestDaySummary:
    def test_exact_text_with_two_groups_in_the_given_order(self):
        first = make_group(public_id="grp-a", load_label="Load A", notes="")
        second = make_group(
            public_id="grp-b", load_label="Load B", lot_code="L9", stats=make_stats(estimated=True)
        )

        text = day_summary(RIFLE, DAY, [first, second])

        assert text == (
            "2026-09-30 with Tikka T3x 6.5 Creedmoor.\n"
            "\n"
            "Group 1 of 2\n"
            "Shot on 2026-09-30 with Tikka T3x 6.5 Creedmoor at 100 yd, bench, suppressed.\n"
            "Group grp-a: Load A (lot L123), 10 shots.\n"
            "Extreme spread 1.23 in (1.18 MOA), smallest target every shot hit, center to "
            "center.\n"
            "Mean point of impact: 0.21 mil low (0.76 in), 0.31 mil right (1.12 in).\n"
            "Group notes: none\n"
            "Legacy notes: Light mirage\n"
            "\n"
            "Group 2 of 2\n"
            "Shot on 2026-09-30 with Tikka T3x 6.5 Creedmoor at 100 yd, bench, suppressed.\n"
            "Group grp-b: Load B (lot L9), 10 shots.\n"
            "Extreme spread 1.23 in (1.18 MOA), smallest target every shot hit, center to "
            "center.\n"
            "Mean point of impact: 0.21 mil low (0.76 in), 0.31 mil right (1.12 in).\n"
            "Includes estimated shots: best-guess positions inside ragged holes, counted in the "
            "statistics.\n"
            "Group notes: Cold bore first\n"
            "Legacy notes: Light mirage"
        )

    def test_a_day_without_groups_says_so(self):
        text = day_summary(RIFLE, DAY, [])

        assert text.endswith("No groups recorded.") and DAY in text

    def test_each_group_keeps_its_own_time_distance_position_and_suppressor(self):
        early = make_group(public_id="g-early", shot_time="07:45:10", distance_yd=100)
        late = make_group(
            public_id="g-late",
            shot_time="15:05:00",
            distance_yd=300,
            position=Position.PRONE_OFF_PACK,
            suppressor=False,
        )

        text = day_summary(RIFLE, DAY, [early, late])

        assert (
            "Shot on 2026-09-30 07:45 with Tikka T3x 6.5 Creedmoor at 100 yd, bench, suppressed."
            in text
        )
        assert (
            "Shot on 2026-09-30 15:05 with Tikka T3x 6.5 Creedmoor at 300 yd, prone off pack, "
            "unsuppressed." in text
        )

    def test_order_follows_the_input(self):
        a, b = make_group(public_id="grp-a"), make_group(public_id="grp-b")

        forward = day_summary(RIFLE, DAY, [a, b])
        backward = day_summary(RIFLE, DAY, [b, a])

        assert forward.index("grp-a") < forward.index("grp-b")
        assert backward.index("grp-b") < backward.index("grp-a")

    def test_a_group_without_shots_is_rejected(self):
        with pytest.raises(ValueError, match="at least one shot"):
            day_summary(RIFLE, DAY, [make_group(shots=0)])

    def test_random_days_are_deterministic_and_free_of_banned_metrics(self):
        rng = random.Random(4)
        for _ in range(25):
            groups = [
                make_group(
                    public_id=f"g{i}",
                    shots=rng.randint(1, 30),
                    stats=make_stats(
                        dx_mil=rng.uniform(-1, 1),
                        dy_mil=rng.uniform(-1, 1),
                        estimated=rng.random() < 0.3,
                    ),
                    purpose=rng.choice(list(GroupPurpose)),
                )
                for i in range(rng.randint(0, 4))
            ]

            first = day_summary(RIFLE, DAY, groups)

            assert first == day_summary(RIFLE, DAY, groups)
            assert not re.search(r"radius|best[- ]?of|reduction", first, re.I)


class TestTypes:
    def test_summary_inputs_are_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            make_group().shots = 3  # type: ignore[misc]

    def test_purpose_defaults_to_general(self):
        group = SummaryGroup(
            "g", "load", "lot", 5, make_stats(), "", "2026-09-30", 100, Position.BENCH, False
        )

        assert group.purpose is GroupPurpose.GENERAL

    def test_module_imports_only_domain_modules(self):
        source = Path(summaries.__file__).read_text(encoding="utf-8")
        imports = re.findall(r"^(?:from|import)\s+([\w.]+)", source, re.M)

        assert all(m.split(".")[0] in {"rga", "collections", "dataclasses"} for m in imports)
        assert all(m.startswith("rga.domain") for m in imports if m.startswith("rga"))


class TestStepOneSummaries:
    """B-43: a Step 1 group over 3 shots reports no point of impact, like the other masked groups."""

    @pytest.mark.parametrize("shots", [4, 5, 9, 10])
    def test_a_step_1_group_over_three_shots_has_no_mean_poi_or_direction_words(self, shots):
        group = make_group(shots=shots, purpose=GroupPurpose.ZERO_STEP1, stats=make_stats(n=shots))

        text = summaries.group_summary(RIFLE, group)

        assert "Mean point of impact" not in text and "mil" not in text.replace("MOA", "")
        assert "Step 1 only corrects from 1 to 3 shots" in text
        assert not DIRECTION_WORDS.search(text.split("Group g")[1].split("Group notes")[0])
        assert f"{shots} shots" in text and "Extreme spread" in text

    @pytest.mark.parametrize("shots", [1, 2, 3])
    def test_one_to_three_shot_step_1_groups_keep_their_mean_poi(self, shots):
        group = make_group(shots=shots, purpose=GroupPurpose.ZERO_STEP1, stats=make_stats(n=shots))

        assert "Mean point of impact" in summaries.group_summary(RIFLE, group)

    def test_a_str_step_1_purpose_is_masked_too(self):
        group = make_group(shots=5, purpose="zero_step1", stats=make_stats(n=5))

        assert "Mean point of impact" not in summaries.group_summary(RIFLE, group)

    def test_the_day_summary_masks_the_oversize_step_1_group_only(self):
        groups = [
            make_group(public_id="g-big", shots=5, purpose="zero_step1", stats=make_stats(n=5)),
            make_group(public_id="g-ok", shots=3, purpose="zero_step1", stats=make_stats(n=3)),
        ]

        text = summaries.day_summary(RIFLE, DAY, groups)

        assert text.count("Mean point of impact") == 1
        assert text.index("g-big") < text.index("Step 1 only corrects") < text.index("g-ok")
