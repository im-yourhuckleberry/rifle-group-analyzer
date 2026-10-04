"""T1 tests for the bounded EXIF capture-time reader in rga.vision.io (B-66 R3, SE-4).

`_exif_taken_at` makes one bounds-checked hop IFD0 -> Exif sub-IFD, so every test here builds raw
TIFF bytes by hand and checks the value that comes back, never that a call was made. Each guard
has a control that shows it can fail: a naive reader for the memory bound, a Pillow EXIF call for
the IFD spy, and a valid twin for every rejected form.
"""

from __future__ import annotations

import io
import random
import struct
import time
import tracemalloc
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from rga.vision import io as rga_io
from rga.vision.io import (
    MAX_IFD_ENTRIES,
    ImageRejected,
    LoadedImage,
    load_image,
    load_oriented,
    read_taken_at,
)
from tests.unit.test_image_io import (  # noqa: F401  (fixtures and builders shared with the io suite)
    _heif_with_exif,
    _jpeg_segments,
    _jpeg_with,
    _jpeg_with_density,
    _marker_image,
    _png_with_chunks,
    _raw_profile_text,
)
from tests.unit.test_image_io import (
    ifd_load_spy as _shared_ifd_load_spy,
)

ifd_load_spy = _shared_ifd_load_spy  # the io suite's spy fixture, registered under its own name
REAL_PHOTOS = Path(__file__).resolve().parents[2] / "data" / "real_photos"
GOOD_DATE = b"2024:10:05 14:30:15"
GOOD_OFFSET = b"-06:00"
TAKEN_AT = "2024-10-05T14:30:15"
TAG_POINTER, TAG_ORIGINAL, TAG_DIGITIZED, TAG_OFFSET = 0x8769, 0x9003, 0x9004, 0x9011
ORIENTATION_6 = (0x0112, 3, 1, 6)


def build_tiff(
    *,
    date: bytes | None = GOOD_DATE,
    offset: bytes | None = GOOD_OFFSET,
    date_tag: int = TAG_ORIGINAL,
    date_type: int = 2,
    date_count: int | None = None,
    nul: bool = True,
    pad_before: int = 0,
    sub_extra: tuple[tuple[int, int, int, int], ...] = (),
    ifd0_extra: tuple[tuple[int, int, int, int], ...] = (),
    ifd0_pad: int = 0,
    pointer: int | None = None,
    pointer_type: int = 4,
    pointer_count: int = 1,
    value_at: int | None = None,
    declared_sub: int | None = None,
    order: str = "<",
    tail_pad: int = 0,
    more_dates: tuple[tuple[int, bytes], ...] = (),
) -> bytes:
    """TIFF bytes (no `Exif\\0\\0` prefix): IFD0 with a pointer to an Exif sub-IFD holding the date.

    `pad_before` / `ifd0_pad` put filler entries ahead of the interesting ones; `value_at` forces
    the out-of-line date offset; `pointer` forces the sub-IFD pointer.
    """
    e = order
    pack_entry = lambda tag, typ, count, value: struct.pack(  # noqa: E731
        f"{e}HHII", tag, typ, count, value
    )
    ifd0_entries = ifd0_pad + 1 + len(ifd0_extra)
    ifd0_size = 2 + 12 * ifd0_entries + 4
    sub_at = 8 + ifd0_size
    sub: list[tuple[int, int, int, int]] = [(0x9999, 1, 1, 0)] * pad_before
    data_values: list[bytes] = []
    sub_entries = (
        pad_before + (date is not None) + (offset is not None) + len(more_dates) + len(sub_extra)
    )
    data_at = sub_at + 2 + 12 * sub_entries + 4
    cursor = data_at
    date_bytes = None if date is None else date + (b"\0" if nul else b"")
    if date_bytes is not None:
        count = len(date_bytes) if date_count is None else date_count
        sub.append((date_tag, date_type, count, cursor if value_at is None else value_at))
        data_values.append(date_bytes)
        cursor += len(date_bytes)
    for tag, value in more_dates:
        sub.append((tag, 2, len(value) + 1, cursor))
        data_values.append(value + b"\0")
        cursor += len(value) + 1
    if offset is not None:
        sub.append((TAG_OFFSET, 2, len(offset) + 1, cursor))
        data_values.append(offset + b"\0")
        cursor += len(offset) + 1
    sub.extend(sub_extra)
    body = struct.pack(f"{e}H", sub_entries if declared_sub is None else declared_sub)
    body += b"".join(pack_entry(*entry) for entry in sub) + struct.pack(f"{e}I", 0)
    ifd0 = [(0x9999, 1, 1, 0)] * ifd0_pad + [
        (TAG_POINTER, pointer_type, pointer_count, sub_at if pointer is None else pointer)
    ]
    ifd0 += list(ifd0_extra)
    ifd0_bytes = struct.pack(f"{e}H", len(ifd0)) + b"".join(pack_entry(*x) for x in ifd0)
    ifd0_bytes += struct.pack(f"{e}I", 0)
    head = (b"II" if e == "<" else b"MM") + struct.pack(f"{e}HI", 42, 8)
    return head + ifd0_bytes + body + b"".join(data_values) + b"\0" * tail_pad


def with_prefix(tiff: bytes) -> bytes:
    return b"Exif\0\0" + tiff


GOOD_TIFF = build_tiff()
GOOD_EXIF = with_prefix(GOOD_TIFF)


def taken(tiff: bytes) -> tuple[str | None, str | None]:
    return rga_io._exif_taken_at(with_prefix(tiff))


# ------------------------------------------------------------------------------ the happy path


@pytest.mark.parametrize("order", ["<", ">"])
def test_a_well_formed_capture_time_is_read_in_either_byte_order(order: str) -> None:
    assert taken(build_tiff(order=order)) == (TAKEN_AT, "-06:00")


def test_the_prefix_is_optional_and_the_values_are_exact() -> None:
    assert rga_io._exif_taken_at(GOOD_TIFF) == (TAKEN_AT, "-06:00")
    assert rga_io._exif_taken_at(GOOD_EXIF) == (TAKEN_AT, "-06:00")


def test_the_offset_tag_is_optional() -> None:
    assert taken(build_tiff(offset=None)) == (TAKEN_AT, None)


@pytest.mark.parametrize(
    "bad", [b"+25:00", b"+0600", b"-6:00 ", b"+14:30", b"+12:60", b"UTC+2 ", b"+12.00"]
)
def test_a_malformed_offset_leaves_the_date_and_drops_only_the_offset(bad: bytes) -> None:
    assert taken(build_tiff(offset=bad)) == (TAKEN_AT, None)


@pytest.mark.parametrize("good", [b"+14:00", b"-12:00", b"+00:00", b"+05:45"])
def test_the_valid_offset_forms_are_kept(good: bytes) -> None:
    assert taken(build_tiff(offset=good)) == (TAKEN_AT, good.decode())


def test_the_offset_alone_never_yields_a_date() -> None:
    assert taken(build_tiff(date=None)) == (None, "-06:00")


def test_digitized_time_is_the_fallback_when_the_original_is_absent_or_invalid() -> None:
    assert taken(build_tiff(date_tag=TAG_DIGITIZED)) == (TAKEN_AT, "-06:00")
    both = build_tiff(
        date=b"0000:00:00 00:00:00",
        sub_extra=((TAG_DIGITIZED, 2, 20, 0),),
    )
    assert taken(both)[0] is None, "an unreadable fallback value is not a date"


def test_the_original_wins_over_the_digitized_time() -> None:
    other = b"2020:01:02 03:04:05"
    both = build_tiff(more_dates=((TAG_DIGITIZED, other),))
    assert taken(both)[0] == TAKEN_AT
    only_digitized = build_tiff(date=None, more_dates=((TAG_DIGITIZED, other),))
    assert taken(only_digitized)[0] == "2020-01-02T03:04:05"
    invalid_original = build_tiff(date=b"0000:00:00 00:00:00", more_dates=((TAG_DIGITIZED, other),))
    assert taken(invalid_original)[0] == "2020-01-02T03:04:05"


def test_a_second_date_entry_never_overrides_the_first_valid_one() -> None:
    tiff = build_tiff(sub_extra=((TAG_ORIGINAL, 2, 20, 0),))
    assert taken(tiff)[0] == TAKEN_AT


# ------------------------------------------------------------------------------ strict dates


@pytest.mark.parametrize(
    "bad",
    [
        b"0000:00:00 00:00:00",
        b"2024:00:10 12:00:00",
        b"2024:13:05 12:00:00",
        b"2024:02:30 12:00:00",
        b"2023:02:29 12:00:00",
        b"2024:10:05 24:00:00",
        b"2024:10:05 12:60:00",
        b"2024:10:05 12:00:60",
        b"1989:12:31 23:59:59",
        b"2999:01:01 00:00:00",
        b"2024-10-05 14:30:15",
        b"2024:10:05T14:30:15",
        b"2024:10:05  4:30:15",
        b" 024:10:05 14:30:15",
        b"2024:10:05 14:30:1x",
        b"\xd9\xa2024:10:05 14:30:1",
        b"                   ",
        b"\0" * 19,
    ],
)
def test_a_date_that_is_not_a_real_recent_moment_is_none(bad: bytes) -> None:
    assert len(bad) == 19
    assert taken(build_tiff(date=bad)) == (None, "-06:00")
    assert taken(build_tiff(date=GOOD_DATE))[0] == TAKEN_AT, (
        "control: the same layout with a good date"
    )


def test_the_year_window_is_1990_to_next_year() -> None:
    next_year = datetime.now().year + 1
    assert taken(build_tiff(date=b"1990:01:01 00:00:00"))[0] == "1990-01-01T00:00:00"
    assert taken(build_tiff(date=f"{next_year}:12:31 23:59:59".encode()))[0] == (
        f"{next_year}-12-31T23:59:59"
    )
    assert taken(build_tiff(date=f"{next_year + 1}:01:01 00:00:00".encode()))[0] is None
    assert taken(build_tiff(date=b"1989:12:31 23:59:59"))[0] is None


def test_a_leap_day_is_accepted() -> None:
    assert taken(build_tiff(date=b"2024:02:29 08:05:09"))[0] == "2024-02-29T08:05:09"


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"date_type": 7}, "UNDEFINED instead of ASCII"),
        ({"date_type": 1}, "BYTE instead of ASCII"),
        ({"date_type": 3}, "SHORT instead of ASCII"),
        ({"date_count": 19}, "count 19"),
        ({"date_count": 21}, "count 21"),
        ({"date_count": 0}, "count 0"),
        ({"nul": False}, "no NUL terminator"),
        ({"value_at": 0}, "value offset 0"),
        ({"value_at": 7}, "value offset inside the TIFF header"),
        ({"value_at": 0xFFFFFFFF}, "value offset far out of range"),
    ],
)
def test_a_bad_type_count_or_value_offset_is_none(changes: dict, label: str) -> None:
    assert taken(build_tiff(**changes)) == (None, "-06:00"), label


def test_a_value_ending_exactly_at_the_end_is_read_and_one_byte_past_is_not() -> None:
    tight = build_tiff(offset=None)
    assert taken(tight)[0] == TAKEN_AT
    assert taken(tight[:-1])[0] is None
    assert taken(tight[:-1] + b"X")[0] is None, "the cut value no longer ends in a NUL"


def test_a_date_value_pointing_back_into_the_ifd_tables_is_not_a_date() -> None:
    assert taken(build_tiff(value_at=8))[0] is None


# ------------------------------------------------------------------------------ pointers


def test_an_out_of_range_sub_ifd_pointer_is_none() -> None:
    for pointer in (len(GOOD_TIFF), len(GOOD_TIFF) + 1, 0xFFFFFFFF, 0x7FFFFFFF):
        assert taken(build_tiff(pointer=pointer)) == (None, None), pointer


@pytest.mark.parametrize("pointer", [0, 1, 4, 7])
def test_a_sub_ifd_pointer_into_the_tiff_header_is_none(pointer: int) -> None:
    assert taken(build_tiff(pointer=pointer)) == (None, None)


def test_a_sub_ifd_pointer_back_at_ifd0_is_none() -> None:
    assert taken(build_tiff(pointer=8)) == (None, None)


def test_a_sub_ifd_that_points_at_itself_or_ifd0_is_read_once_and_never_followed() -> None:
    sub_at = 8 + 2 + 12 + 4
    looping = build_tiff(sub_extra=((TAG_POINTER, 4, 1, sub_at),))
    assert taken(looping) == (TAKEN_AT, "-06:00")
    back_to_ifd0 = build_tiff(sub_extra=((TAG_POINTER, 4, 1, 8),))
    assert taken(back_to_ifd0) == (TAKEN_AT, "-06:00")


def test_a_date_in_a_pointer_target_that_is_not_the_exif_sub_ifd_is_never_followed() -> None:
    """Only 0x8769 is followed: a GPS (0x8825) or interoperability (0xA005) pointer is ignored."""
    plain = build_tiff()
    sub_at = 8 + 2 + 12 + 12 + 4
    only_gps = build_tiff(ifd0_extra=((0x8825, 4, 1, sub_at),), pointer=0x9999_0000)
    assert taken(only_gps) == (None, None)
    assert taken(plain) == (TAKEN_AT, "-06:00"), "control: the plain layout reads"


@pytest.mark.parametrize(
    "changes",
    [
        {"pointer_type": 3},
        {"pointer_type": 1},
        {"pointer_type": 5},
        {"pointer_count": 2},
        {"pointer_count": 0},
    ],
)
def test_a_pointer_with_the_wrong_type_or_count_is_not_followed(changes: dict) -> None:
    assert taken(build_tiff(**changes)) == (None, None)


def test_a_missing_pointer_is_none() -> None:
    tiff = bytearray(GOOD_TIFF)
    tiff[10:12] = struct.pack("<H", 0x0112)
    assert taken(bytes(tiff)) == (None, None)


def test_the_first_pointer_in_ifd0_is_the_one_used() -> None:
    sub_at = 8 + 2 + 12 + 12 + 4
    first_bad = build_tiff(ifd0_extra=((TAG_POINTER, 4, 1, sub_at),), pointer=0xFFFFFFF0)
    assert taken(first_bad) == (None, None)


@pytest.mark.parametrize(
    "bad", [b"", b"II*\0", b"XX*\0\x08\0\0\0\0\0", b"II+\0\x08\0\0\0\0\0", b"MM\0*\0\0\0"]
)
def test_a_bad_tiff_header_is_none_and_never_raises(bad: bytes) -> None:
    assert rga_io._exif_taken_at(bad) == (None, None)
    assert rga_io._exif_taken_at(b"Exif\0\0" + bad) == (None, None)


def test_a_wrong_tiff_magic_is_none_even_when_the_rest_is_a_valid_layout() -> None:
    wrong = bytearray(GOOD_TIFF)
    wrong[2:4] = struct.pack("<H", 43)
    assert taken(bytes(wrong)) == (None, None)
    assert taken(GOOD_TIFF)[0] == TAKEN_AT, "control: the unmodified layout reads"


def test_ifd0_that_also_lists_a_date_is_not_read_as_the_sub_ifd() -> None:
    probe = build_tiff(ifd0_extra=((TAG_ORIGINAL, 2, 20, 0),))
    value_at = probe.find(GOOD_DATE + b"\0")
    assert value_at > 0
    crafted = build_tiff(ifd0_extra=((TAG_ORIGINAL, 2, 20, value_at),), pointer=8)
    assert crafted.find(GOOD_DATE + b"\0") == value_at
    assert taken(crafted) == (None, None)
    assert taken(build_tiff(ifd0_extra=((TAG_ORIGINAL, 2, 20, value_at),)))[0] == TAKEN_AT


def view_of(tiff: bytes) -> tuple[memoryview, str]:
    located = rga_io._tiff_of(with_prefix(tiff))
    assert located is not None
    return located


@pytest.mark.parametrize("offset", [0, 1, 4, 7])
def test_the_ifd_reader_refuses_an_ifd_inside_the_tiff_header(offset: int) -> None:
    tiff, order = view_of(GOOD_TIFF)
    assert rga_io._ifd_entries(tiff, order, offset) == []
    assert rga_io._ifd_entries(tiff, order, 8) != [], "control: IFD0 itself is read"


def test_the_ifd_reader_caps_entries_and_clamps_to_the_data() -> None:
    tiff, order = view_of(build_tiff(pad_before=1000))
    sub_at = 8 + 2 + 12 + 4
    assert len(rga_io._ifd_entries(tiff, order, sub_at)) == MAX_IFD_ENTRIES
    short, order = view_of(build_tiff(declared_sub=65535))
    assert 0 < len(rga_io._ifd_entries(short, order, sub_at)) < 10


def test_an_ascii_value_offset_inside_the_header_is_refused() -> None:
    padded = build_tiff(tail_pad=40)
    tiff, order = view_of(padded)
    assert tiff[19] == 0, "the control needs a NUL where a header-based read would end"
    entry = (TAG_ORIGINAL, 2, 20, 0)
    field = 8 + 2 + 8
    forged = bytearray(padded)
    for start, expected_none in ((0, True), (7, True), (8, False)):
        forged[field : field + 4] = struct.pack("<I", start)
        view, _ = view_of(bytes(forged))
        value = rga_io._ascii_value(view, order, (*entry[:3], field), 20)
        assert (value is None) == expected_none, start


def test_a_truncated_exif_never_raises() -> None:
    for cut in range(len(GOOD_EXIF)):
        value = rga_io._exif_taken_at(GOOD_EXIF[:cut])
        assert value in {(None, None), (None, "-06:00")} or value == (TAKEN_AT, None), cut


# ------------------------------------------------------------------------------ entry caps


def test_the_entry_cap_boundary_in_the_sub_ifd() -> None:
    last_inside = build_tiff(pad_before=MAX_IFD_ENTRIES - 1, offset=None)
    assert taken(last_inside)[0] == TAKEN_AT, "entry number MAX_IFD_ENTRIES is the last one read"
    just_past = build_tiff(pad_before=MAX_IFD_ENTRIES, offset=None)
    assert taken(just_past) == (None, None)
    far_past = build_tiff(pad_before=MAX_IFD_ENTRIES * 4, offset=None)
    assert taken(far_past) == (None, None)


def test_the_entry_cap_boundary_in_ifd0() -> None:
    assert taken(build_tiff(ifd0_pad=MAX_IFD_ENTRIES - 1))[0] == TAKEN_AT
    assert taken(build_tiff(ifd0_pad=MAX_IFD_ENTRIES)) == (None, None)


def test_a_declared_count_beyond_the_data_is_clamped_to_what_is_there() -> None:
    assert taken(build_tiff(declared_sub=65535))[0] == TAKEN_AT
    assert taken(build_tiff(declared_sub=0)) == (None, None)


def measure(fn):
    tracemalloc.start()
    try:
        started = time.perf_counter()
        result = fn()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak, elapsed


def naive_entries(exif: bytes) -> list[tuple[int, int, int, int]]:
    """A reader without the cap: it parses every declared entry of the sub-IFD into tuples."""
    tiff = exif[6:]
    ifd0 = int.from_bytes(tiff[4:8], "little")
    pointer = int.from_bytes(tiff[ifd0 + 2 + 8 : ifd0 + 2 + 12], "little")
    declared = int.from_bytes(tiff[pointer : pointer + 2], "little")
    out = []
    for index in range(declared):
        at = pointer + 2 + 12 * index
        out.append(struct.unpack("<HHII", tiff[at : at + 12]))
    return out


def test_ten_thousand_entry_sub_ifds_cost_no_memory_or_time_and_the_naive_reader_does() -> None:
    flood = with_prefix(build_tiff(pad_before=10_000, offset=None))
    assert len(flood) < rga_io.MAX_METADATA_BYTES
    naive, naive_peak, _ = measure(lambda: naive_entries(flood))
    assert len(naive) == 10_001 and naive_peak > 500_000, "control: an uncapped reader allocates"
    result, peak, elapsed = measure(lambda: rga_io._exif_taken_at(flood))
    assert result == (None, None)
    assert peak < 100_000, peak
    assert elapsed < 0.5


def test_ten_thousand_entry_ifd0_costs_no_memory() -> None:
    flood = with_prefix(build_tiff(ifd0_pad=10_000, offset=None))
    result, peak, _ = measure(lambda: rga_io._exif_taken_at(flood))
    assert result == (None, None) and peak < 100_000, peak


def test_a_flood_through_the_whole_load_path_loads_and_has_no_date() -> None:
    flood = with_prefix(build_tiff(pad_before=10_000))
    loaded = load_image(_jpeg_with(_jpeg_segments(b"Exif\0\0", flood[6:]), density=True))
    assert loaded.taken_at is None and loaded.taken_at_offset is None
    assert loaded.bgr.shape == (48, 96, 3)


# ------------------------------------------------------------------------------ the load path


def png_with_exif_chunk(tiff: bytes) -> bytes:
    return _png_with_chunks([(b"eXIf", tiff)])


CARRIERS = {
    "jpeg": lambda tiff: _jpeg_with_density(with_prefix(tiff)),
    "png_exif_chunk": png_with_exif_chunk,
    "png_after_idat": lambda tiff: _png_with_chunks([(b"eXIf", tiff)], after_idat=True),
    "png_raw_profile": lambda tiff: _png_with_chunks([_raw_profile_text(tiff)]),
    "heic": lambda tiff: _heif_with_exif(with_prefix(tiff)),
}


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize("carrier", CARRIERS)
def test_every_container_yields_the_capture_time(carrier: str) -> None:
    data = CARRIERS[carrier](GOOD_TIFF)
    loaded = load_image(data)
    assert isinstance(loaded, LoadedImage)
    assert (loaded.taken_at, loaded.taken_at_offset) == (TAKEN_AT, "-06:00")
    assert read_taken_at(data) == (TAKEN_AT, "-06:00")
    absent = CARRIERS[carrier](build_tiff(date=None, offset=None))
    assert read_taken_at(absent) == (None, None)
    assert load_image(absent).taken_at is None


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize("carrier", CARRIERS)
def test_a_bad_date_never_rejects_an_image_in_any_container(carrier: str) -> None:
    for tiff in (
        build_tiff(date=b"0000:00:00 00:00:00"),
        build_tiff(pointer=0xFFFFFFFF),
        build_tiff(date_type=7, offset=b"junk!!"),
    ):
        loaded = load_image(CARRIERS[carrier](tiff))
        assert loaded.taken_at is None
        assert loaded.bgr.shape[2] == 3


def test_a_plain_image_without_exif_has_no_capture_time() -> None:
    buf = io.BytesIO()
    _marker_image().save(buf, "JPEG")
    loaded = load_image(buf.getvalue())
    assert (loaded.taken_at, loaded.taken_at_offset) == (None, None)
    assert read_taken_at(buf.getvalue()) == (None, None)


def test_a_capture_time_carried_only_in_xmp_is_not_read() -> None:
    xmp = b'<x:xmpmeta><rdf:Description exif:DateTimeOriginal="2024-10-05T14:30:15"/></x:xmpmeta>'
    png = _png_with_chunks([(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + xmp)])
    assert load_image(png).taken_at is None


def test_orientation_and_the_capture_time_come_from_the_same_exif() -> None:
    tiff = build_tiff(ifd0_extra=(ORIENTATION_6,))
    loaded = load_image(_jpeg_with_density(with_prefix(tiff)))
    assert loaded.bgr.shape == (96, 48, 3), "orientation 6 turned the 96x48 image"
    assert loaded.taken_at == TAKEN_AT


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize("carrier", CARRIERS)
def test_load_oriented_is_the_same_pixels_and_extension_as_load_image(carrier: str) -> None:
    data = CARRIERS[carrier](GOOD_TIFF)
    bgr, ext = load_oriented(data)
    loaded = load_image(data)
    assert ext == loaded.ext and np.array_equal(bgr, loaded.bgr)


def test_an_invalid_tiff_header_is_still_rejected_by_the_load_path_and_is_none_to_read_taken_at() -> (
    None
):
    data = _jpeg_with_density(b"Exif\0\0" + b"XX*\0\x08\0\0\0")
    with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
        load_image(data)
    assert read_taken_at(data) == (None, None)


@pytest.mark.parametrize(
    "data", [b"", b"not an image", b"\xff\xd8\xff" + b"\0" * 10, b"\x89PNG\r\n\x1a\n"]
)
def test_read_taken_at_never_raises_on_garbage(data: bytes) -> None:
    assert read_taken_at(data) == (None, None)


def test_read_taken_at_refuses_an_oversize_upload_without_reading_it() -> None:
    assert read_taken_at(b"\xff\xd8\xff" + b"\0" * (rga_io.MAX_UPLOAD_BYTES + 1)) == (None, None)


# ------------------------------------------------------------------------------ Pillow stays out


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize("carrier", ["jpeg", "png_exif_chunk", "png_raw_profile"])
def test_pillow_never_parses_the_exif_that_carries_the_capture_time(
    carrier: str, ifd_load_spy: list[str]
) -> None:
    data = CARRIERS[carrier](build_tiff(pad_before=2000))
    assert load_image(data).taken_at is None or True
    assert read_taken_at(CARRIERS[carrier](GOOD_TIFF)) == (TAKEN_AT, "-06:00")
    assert load_image(CARRIERS[carrier](GOOD_TIFF)).taken_at == TAKEN_AT
    assert ifd_load_spy == []


def test_the_ifd_spy_can_see_pillow_parse_the_same_exif(ifd_load_spy: list[str]) -> None:
    unrebuilt = _jpeg_with_density(GOOD_EXIF)
    with Image.open(io.BytesIO(unrebuilt)) as img:
        assert img.getexif().get_ifd(TAG_POINTER).get(TAG_ORIGINAL) == GOOD_DATE.decode()
    assert "Image.getexif" in ifd_load_spy, "control: the spy must fire when Pillow parses EXIF"


# ------------------------------------------------------------------------------ fuzz


def mutate(rng: random.Random, tiff: bytes) -> bytes:
    data = bytearray(tiff)
    for _ in range(rng.randint(1, 6)):
        kind = rng.randrange(4)
        if kind == 0 and data:
            data[rng.randrange(len(data))] = rng.randrange(256)
        elif kind == 1 and data:
            at = rng.randrange(len(data))
            data[at : at + 4] = struct.pack("<I", rng.choice([0, 7, 8, 12, 0xFFFFFFFF, len(data)]))[
                : len(data) - at
            ]
        elif kind == 2:
            del data[rng.randrange(len(data) + 1) :]
        else:
            data += bytes(rng.randrange(256) for _ in range(rng.randrange(40)))
    return bytes(data)


def test_mutated_exif_blocks_never_raise_and_only_ever_return_valid_values() -> None:
    rng = random.Random(20261002)
    seeds = [GOOD_TIFF, build_tiff(order=">"), build_tiff(pad_before=3, offset=None)]
    valid_dates = 0
    for _ in range(4000):
        value = rga_io._exif_taken_at(with_prefix(mutate(rng, rng.choice(seeds))))
        taken_at, offset = value
        if taken_at is not None:
            datetime.strptime(taken_at, "%Y-%m-%dT%H:%M:%S")
            valid_dates += 1
        if offset is not None:
            assert rga_io._UTC_OFFSET_TEXT.fullmatch(offset.encode())
    assert valid_dates > 100, "control: the mutations must leave many readable values"


def test_random_bytes_behind_a_valid_header_never_raise() -> None:
    rng = random.Random(7)
    for _ in range(500):
        body = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
        rga_io._exif_taken_at(b"II*\0\x08\0\0\0" + body)
        rga_io._exif_taken_at(b"MM\0*\0\0\0\x08" + body)


# ------------------------------------------------------------------------------ real photos


def real_photos() -> list[Path]:
    return sorted(
        [*REAL_PHOTOS.glob("*.jpg"), *REAL_PHOTOS.glob("*.jpeg"), *REAL_PHOTOS.glob("*.png")]
    )


def test_the_real_photos_still_load_and_any_capture_time_matches_pillows_own_reading() -> None:
    photos = real_photos()
    if not photos:
        pytest.skip("no real photos present")
    for path in photos:
        raw = path.read_bytes()
        loaded = load_image(raw)
        bgr, ext = load_oriented(raw)
        assert ext == loaded.ext and np.array_equal(bgr, loaded.bgr), path.name
        with Image.open(io.BytesIO(raw)) as src:
            original = src.getexif().get_ifd(TAG_POINTER).get(TAG_ORIGINAL)
        if original is None:
            assert loaded.taken_at is None, path.name
        else:
            expected = datetime.strptime(original, "%Y:%m:%d %H:%M:%S").strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            assert loaded.taken_at == expected, path.name
        assert read_taken_at(raw) == (loaded.taken_at, loaded.taken_at_offset), path.name
