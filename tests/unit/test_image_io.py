"""T1 tests for rga.vision.io: formats, EXIF orientation, limits and hostile input."""

import contextlib
import functools
import io
import struct
import time
import tracemalloc
import warnings
import zlib
from pathlib import Path

import numpy as np
import pillow_heif
import pytest
from PIL import Image

from rga.vision import io as rga_io
from rga.vision.io import (
    MAX_PIXELS,
    MAX_UPLOAD_BYTES,
    ImageRejected,
    load_oriented,
    sha256_hex,
)

pillow_heif.register_heif_opener()

REAL_PHOTOS = Path(__file__).resolve().parents[2] / "data" / "real_photos"
RED_BGR = (0, 0, 255)
BLUE_BGR = (255, 0, 0)
GREEN_BGR = (0, 255, 0)
WHITE_BGR = (255, 255, 255)


def _marker_image(width: int = 96, height: int = 48) -> Image.Image:
    """Blue field, red 24x24 block at stored top-left, green 24x24 block at stored bottom-right."""
    img = Image.new("RGB", (width, height), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, 24, 24))
    img.paste((0, 255, 0), (width - 24, height - 24, width, height))
    return img


def _encode(img: Image.Image, fmt: str, orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _px(arr: np.ndarray, x: int, y: int) -> tuple[int, ...]:
    return tuple(int(v) for v in arr[y, x])


def _near(actual: tuple[int, ...], expected: tuple[int, ...], tol: int = 12) -> bool:
    return all(abs(a - e) <= tol for a, e in zip(actual, expected, strict=True))


def _assert_corner_colors(arr: np.ndarray, red_at: str, green_at: str) -> None:
    h, w = arr.shape[:2]
    spots = {
        "tl": (6, 6),
        "tr": (w - 7, 6),
        "bl": (6, h - 7),
        "br": (w - 7, h - 7),
    }
    for name, (x, y) in spots.items():
        expected = RED_BGR if name == red_at else GREEN_BGR if name == green_at else BLUE_BGR
        assert _near(_px(arr, x, y), expected), (name, _px(arr, x, y), expected)


class TestFormats:
    @pytest.mark.parametrize(("fmt", "ext"), [("JPEG", "jpg"), ("PNG", "png"), ("HEIF", "heic")])
    def test_decodes_to_bgr_uint8_with_normalized_extension(self, fmt, ext):
        arr, got_ext = load_oriented(_encode(_marker_image(), fmt))
        assert got_ext == ext
        assert arr.dtype == np.uint8
        assert arr.shape == (48, 96, 3)
        assert arr.flags["C_CONTIGUOUS"]
        _assert_corner_colors(arr, red_at="tl", green_at="br")

    def test_png_is_lossless_and_channel_order_is_bgr(self):
        img = Image.new("RGB", (4, 3), (10, 20, 30))
        arr, _ = load_oriented(_encode(img, "PNG"))
        assert _px(arr, 0, 0) == (30, 20, 10)

    def test_rgba_png_transparency_is_flattened_onto_white(self):
        img = Image.new("RGBA", (8, 8), (255, 0, 0, 0))
        img.putpixel((0, 0), (0, 0, 255, 255))
        arr, _ = load_oriented(_encode(img, "PNG"))
        assert arr.shape == (8, 8, 3)
        assert _px(arr, 0, 0) == (255, 0, 0)
        assert _px(arr, 4, 4) == WHITE_BGR

    def test_grayscale_png_becomes_three_equal_channels(self):
        img = Image.new("L", (5, 5), 77)
        arr, _ = load_oriented(_encode(img, "PNG"))
        assert arr.shape == (5, 5, 3)
        assert _px(arr, 2, 2) == (77, 77, 77)

    def test_sixteen_bit_grayscale_png_is_scaled_to_eight_bit(self):
        raw = np.full((4, 4), 0x8000, dtype=np.uint16)
        buf = io.BytesIO()
        Image.fromarray(raw).save(buf, "PNG")
        arr, ext = load_oriented(buf.getvalue())
        assert ext == "png"
        assert arr.dtype == np.uint8
        assert _px(arr, 1, 1) == (128, 128, 128)

    def test_cmyk_jpeg_decodes(self):
        img = Image.new("CMYK", (8, 8), (0, 0, 0, 0))
        arr, ext = load_oriented(_encode(img, "JPEG"))
        assert ext == "jpg"
        assert arr.shape == (8, 8, 3)


class TestExifOrientation:
    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_orientation_6_rotates_clockwise(self, fmt):
        arr, _ = load_oriented(_encode(_marker_image(), fmt, orientation=6))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_orientation_8_rotates_counter_clockwise(self, fmt):
        arr, _ = load_oriented(_encode(_marker_image(), fmt, orientation=8))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="bl", green_at="tr")

    def test_orientation_3_rotates_half_turn(self):
        arr, _ = load_oriented(_encode(_marker_image(), "JPEG", orientation=3))
        assert arr.shape == (48, 96, 3)
        _assert_corner_colors(arr, red_at="br", green_at="tl")

    @pytest.mark.parametrize(
        ("orientation", "red_at", "green_at"),
        [(2, "tr", "bl"), (4, "bl", "tr"), (5, "tl", "br"), (7, "br", "tl")],
    )
    def test_mirrored_orientations(self, orientation, red_at, green_at):
        arr, _ = load_oriented(_encode(_marker_image(), "PNG", orientation=orientation))
        _assert_corner_colors(arr, red_at=red_at, green_at=green_at)

    def test_orientation_1_and_missing_leave_pixels_unchanged(self):
        for orientation in (1, None):
            arr, _ = load_oriented(_encode(_marker_image(), "JPEG", orientation=orientation))
            assert arr.shape == (48, 96, 3)
            _assert_corner_colors(arr, red_at="tl", green_at="br")

    def test_heic_with_orientation_6_is_upright(self):
        arr, ext = load_oriented(_encode(_marker_image(), "HEIF", orientation=6))
        assert ext == "heic"
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")


class TestUploadSizeLimit:
    @staticmethod
    def _padded_jpeg(total_len: int) -> bytes:
        data = _encode(_marker_image(), "JPEG")
        return data + b"\x00" * (total_len - len(data))

    def test_exactly_the_cap_is_accepted(self):
        arr, ext = load_oriented(self._padded_jpeg(MAX_UPLOAD_BYTES))
        assert ext == "jpg"
        assert arr.shape == (48, 96, 3)

    def test_one_byte_over_the_cap_is_rejected(self):
        with pytest.raises(ImageRejected, match="exceeds"):
            load_oriented(self._padded_jpeg(MAX_UPLOAD_BYTES + 1))

    def test_cap_constants_match_the_design(self):
        assert MAX_UPLOAD_BYTES == 40 * 1024 * 1024
        assert MAX_PIXELS == 60_000_000


class TestPixelLimit:
    def test_pillow_max_image_pixels_is_set_to_the_cap(self):
        assert Image.MAX_IMAGE_PIXELS == MAX_PIXELS

    def test_image_just_over_the_cap_is_rejected_from_the_header(self):
        data = _encode(Image.new("1", (8000, 7501)), "PNG")
        assert MAX_PIXELS < 8000 * 7501
        with pytest.raises(ImageRejected, match="pixels"):
            load_oriented(data)

    def test_image_beyond_pillow_bomb_threshold_is_rejected(self):
        data = _encode(Image.new("1", (12000, 10100)), "PNG")
        with pytest.raises(ImageRejected, match="pixels"):
            load_oriented(data)

    def test_oversize_pixel_rejection_emits_no_warning(self):
        data = _encode(Image.new("1", (8000, 7501)), "PNG")
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with pytest.raises(ImageRejected):
                load_oriented(data)
        assert recorded == []

    def test_boundary_is_inclusive(self, monkeypatch):
        monkeypatch.setattr(rga_io, "MAX_PIXELS", 100 * 100)
        arr, _ = load_oriented(_encode(Image.new("RGB", (100, 100), (1, 2, 3)), "PNG"))
        assert arr.shape == (100, 100, 3)
        with pytest.raises(ImageRejected, match="pixels"):
            load_oriented(_encode(Image.new("RGB", (101, 100), (1, 2, 3)), "PNG"))

    def test_rotation_does_not_change_the_pixel_count_check(self, monkeypatch):
        monkeypatch.setattr(rga_io, "MAX_PIXELS", 100 * 100)
        data = _encode(Image.new("RGB", (101, 100)), "JPEG", orientation=6)
        with pytest.raises(ImageRejected, match="pixels"):
            load_oriented(data)


class TestHostileInput:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"hello world, definitely not an image",
            b"%PDF-1.7\n" + b"0" * 64,
            b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>",
            b"\x00" * 64,
        ],
    )
    def test_non_image_bytes_are_rejected(self, data):
        with pytest.raises(ImageRejected):
            load_oriented(data)

    def test_empty_upload_is_reported_as_empty(self):
        with pytest.raises(ImageRejected, match="empty"):
            load_oriented(b"")

    @pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF", "WEBP"])
    def test_valid_images_in_unsupported_formats_are_rejected(self, fmt):
        data = _encode(Image.new("RGB", (16, 16), (5, 5, 5)), fmt)
        with pytest.raises(ImageRejected, match="unsupported"):
            load_oriented(data)

    @pytest.mark.parametrize("brand", [b"avif", b"qt  ", b"mp42", b"isom"])
    def test_ftyp_container_with_non_heic_brand_is_rejected(self, brand):
        data = bytearray(_encode(_marker_image(), "HEIF"))
        assert data[4:8] == b"ftyp"
        data[8:12] = brand
        with pytest.raises(ImageRejected, match="unsupported"):
            load_oriented(bytes(data))

    @pytest.mark.parametrize(
        "data",
        [
            b"\xff\xd8\xff",
            b"\xff\xd8\xff\xe0" + b"\x00" * 40,
            b"\x89PNG\r\n\x1a\n",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
            b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40,
        ],
    )
    def test_magic_bytes_followed_by_garbage_are_rejected(self, data):
        with pytest.raises(ImageRejected):
            load_oriented(data)

    def test_extension_mismatch_between_magic_and_content_is_rejected(self):
        png = _encode(Image.new("RGB", (16, 16)), "PNG")
        with pytest.raises(ImageRejected):
            load_oriented(b"\xff\xd8\xff" + png[3:])

    @staticmethod
    def _noisy(width: int = 256, height: int = 256) -> Image.Image:
        rng = np.random.default_rng(0)
        return Image.fromarray(rng.integers(0, 256, (height, width, 3), dtype=np.uint8))

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG", "HEIF"])
    def test_truncated_file_is_rejected(self, fmt):
        data = _encode(self._noisy(), fmt)
        for keep in (0.9, 0.5):
            with pytest.raises(ImageRejected):
                load_oriented(data[: int(len(data) * keep)])

    def test_complete_noisy_files_still_load(self):
        for fmt in ("JPEG", "PNG", "HEIF"):
            arr, _ = load_oriented(_encode(self._noisy(), fmt))
            assert arr.shape == (256, 256, 3)

    def test_pillow_truncated_image_tolerance_stays_disabled(self):
        from PIL import ImageFile

        assert ImageFile.LOAD_TRUNCATED_IMAGES is False

    def test_rejection_is_a_value_error(self):
        assert issubclass(ImageRejected, ValueError)

    def test_rejection_message_does_not_echo_input_bytes(self):
        with pytest.raises(ImageRejected) as info:
            load_oriented(b"SECRET-TOKEN-1234")
        assert "SECRET" not in str(info.value)


class TestSha256:
    def test_known_vectors(self):
        assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert (
            sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_is_lowercase_hex_of_64_chars(self):
        digest = sha256_hex(b"\xff\x00" * 10)
        assert len(digest) == 64
        assert digest == digest.lower()
        int(digest, 16)


@pytest.mark.skipif(not REAL_PHOTOS.is_dir(), reason="data/real_photos is gitignored and absent")
class TestRealPhotos:
    def test_real_samples_load_with_exif_orientation_applied(self):
        photos = sorted(REAL_PHOTOS.glob("real_*.jpg"))
        if not photos:
            pytest.skip("no real_*.jpg samples present")
        for path in photos:
            raw = path.read_bytes()
            arr, ext = load_oriented(raw)
            with Image.open(io.BytesIO(raw)) as src:
                swap = src.getexif().get(0x0112, 1) in (5, 6, 7, 8)
                w, h = (src.height, src.width) if swap else src.size
            assert ext == "jpg"
            assert arr.dtype == np.uint8
            assert arr.shape == (h, w, 3), path.name
            assert arr.shape[0] * arr.shape[1] > 1_000_000
            assert arr.std() > 5, path.name


def _raw_exif(entries: list[tuple[int, int, int, bytes]]) -> bytes:
    """Little-endian EXIF block with one IFD of (tag, type, count, inline value) entries."""
    ifd = struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        ifd += struct.pack("<HHI", tag, typ, count) + value.ljust(4, b"\0")
    return b"Exif\0\0II*\0" + struct.pack("<I", 8) + ifd + struct.pack("<I", 0)


ORIENTATION_RESULTS = {
    1: ("tl", "br", (48, 96, 3)),
    2: ("tr", "bl", (48, 96, 3)),
    3: ("br", "tl", (48, 96, 3)),
    4: ("bl", "tr", (48, 96, 3)),
    5: ("tl", "br", (96, 48, 3)),
    6: ("tr", "bl", (96, 48, 3)),
    7: ("br", "tl", (96, 48, 3)),
    8: ("bl", "tr", (96, 48, 3)),
}
EXIF_ORIENTATION_6 = (0x0112, 3, 1, struct.pack("<H", 6))
EXIF_XRESOLUTION_AS_ASCII = (0x011A, 2, 4, b"abc\0")


class TestHostileMetadataAndContainers:
    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    def test_wrongly_typed_unrelated_tag_with_an_orientation_still_loads_oriented(self, fmt):
        exif = _raw_exif([EXIF_ORIENTATION_6, EXIF_XRESOLUTION_AS_ASCII])
        buf = io.BytesIO()
        _marker_image().save(buf, fmt, exif=exif)
        arr, _ = load_oriented(buf.getvalue())
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    @pytest.mark.parametrize("tag", [0x010F, 0x0131], ids=["Make", "Software"])
    def test_float_typed_make_or_software_with_an_orientation_still_loads_oriented(self, fmt, tag):
        float_tag = (tag, 11, 1, struct.pack("<f", 1.0))
        buf = io.BytesIO()
        _marker_image().save(buf, fmt, exif=_raw_exif([EXIF_ORIENTATION_6, float_tag]))
        arr, _ = load_oriented(buf.getvalue())
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    @pytest.mark.filterwarnings("ignore::UserWarning")
    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    @pytest.mark.parametrize("tag", [0x010F, 0x0131, 0x011A], ids=["Make", "Software", "XRes"])
    def test_exif_tag_type_fuzz_loads_correctly_oriented_or_is_rejected(self, fmt, tag):
        loaded = 0
        for tiff_type in [*range(14), 99, 0xFFFF]:
            for count, value in [
                (1, b"\x00\x00\x00\x00"),
                (4, b"abc\0"),
                (1, struct.pack("<f", 1.0)),
                (0xFFFFFFFF, b"\xff\xff\xff\xff"),
            ]:
                for orientation in range(1, 9):
                    entries = [
                        (0x0112, 3, 1, struct.pack("<H", orientation)),
                        (tag, tiff_type, count, value),
                    ]
                    buf = io.BytesIO()
                    _marker_image().save(buf, fmt, exif=_raw_exif(entries))
                    try:
                        arr, _ = load_oriented(buf.getvalue())
                    except ImageRejected:
                        continue
                    red_at, green_at, shape = ORIENTATION_RESULTS[orientation]
                    assert arr.shape == shape, (fmt, tag, tiff_type, count, orientation)
                    _assert_corner_colors(arr, red_at=red_at, green_at=green_at)
                    loaded += 1
        assert loaded >= 16 * 4 * 8 * 3 // 4, "fuzz rejected nearly everything; it proves little"

    @pytest.mark.parametrize("fmt", ["JPEG", "PNG"])
    @pytest.mark.parametrize("orientation", range(1, 9))
    def test_malformed_unrelated_tags_do_not_change_the_orientation_result(self, fmt, orientation):
        entries = [
            (0x0112, 3, 1, struct.pack("<H", orientation)),
            (0x010F, 11, 1, struct.pack("<f", 1.0)),
            (0x0131, 12, 1, b"\0\0\0\0"),
            (0x011A, 2, 4, b"abc\0"),
        ]
        plain, clean = io.BytesIO(), io.BytesIO()
        _marker_image().save(plain, fmt, exif=_raw_exif(entries))
        _marker_image().save(clean, fmt, exif=_raw_exif(entries[:1]))
        np.testing.assert_array_equal(
            load_oriented(plain.getvalue())[0], load_oriented(clean.getvalue())[0]
        )

    @pytest.mark.parametrize(
        "entry",
        [
            (0x0112, 2, 4, b"6\0\0\0"),
            (0x0112, 11, 1, struct.pack("<f", 6.0)),
            (0x0112, 5, 1, struct.pack("<I", 8)),
            (0x0112, 3, 1, struct.pack("<H", 0)),
            (0x0112, 3, 1, struct.pack("<H", 9)),
            (0x0112, 3, 1, struct.pack("<H", 0xFFFF)),
        ],
    )
    def test_a_non_integer_or_out_of_range_orientation_leaves_the_pixels_unrotated(self, entry):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG", exif=_raw_exif([entry]))
        arr, _ = load_oriented(buf.getvalue())
        assert arr.shape == (48, 96, 3)
        _assert_corner_colors(arr, red_at="tl", green_at="br")

    def test_the_same_tag_without_an_orientation_does_not_disturb_loading(self):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG", exif=_raw_exif([EXIF_XRESOLUTION_AS_ASCII]))
        arr, ext = load_oriented(buf.getvalue())
        assert (ext, arr.shape) == ("jpg", (48, 96, 3))

    @pytest.mark.parametrize(
        "orientation_entry",
        [
            (0x0112, 2, 4, b"6\0\0\0"),
            (0x0112, 1, 1, b"\x06"),
            (0x0112, 4, 1, struct.pack("<I", 6)),
            (0x0112, 5, 1, struct.pack("<I", 8)),
        ],
    )
    def test_any_typing_of_the_orientation_tag_either_loads_or_is_rejected(self, orientation_entry):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG", exif=_raw_exif([orientation_entry]))
        try:
            arr, _ = load_oriented(buf.getvalue())
        except ImageRejected:
            return
        assert arr.dtype == np.uint8

    @staticmethod
    def _av1_coded_heic() -> bytes:
        buf = io.BytesIO()
        _marker_image().save(buf, "HEIF")
        data = buf.getvalue().replace(b"hvc1", b"av01").replace(b"hvcC", b"av1C")
        assert data[4:12] == b"ftypheic"
        return data

    def test_an_av1_coded_file_with_a_heic_brand_is_rejected(self):
        with pytest.raises(ImageRejected, match="could not be decoded"):
            load_oriented(self._av1_coded_heic())

    def test_the_crafted_av1_file_passes_the_brand_sniff_and_opens(self):
        data = self._av1_coded_heic()
        assert rga_io._sniff_extension(data) == "heic"
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "HEIF"

    def test_the_unmodified_heic_still_loads(self):
        buf = io.BytesIO()
        _marker_image().save(buf, "HEIF")
        assert load_oriented(buf.getvalue())[1] == "heic"

    @pytest.mark.filterwarnings("ignore::UserWarning")
    @pytest.mark.parametrize("fmt", ["JPEG", "PNG", "HEIF"])
    def test_no_exception_other_than_image_rejected_escapes_for_corrupted_bytes(self, fmt):
        data = bytearray(_encode(_marker_image(), fmt, orientation=6))
        rng = np.random.default_rng(7)
        for _ in range(200):
            mutated = bytearray(data)
            for pos in rng.integers(12, len(mutated), size=3):
                mutated[int(pos)] = int(rng.integers(0, 256))
            with contextlib.suppress(ImageRejected):
                load_oriented(bytes(mutated))


class TestAspectRatioLimit:
    def test_constant_matches_the_design_decision(self):
        assert rga_io.MAX_ASPECT_RATIO == 20

    @pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
    def test_exactly_twenty_to_one_is_accepted_in_both_orientations(self, fmt):
        for size in ((2000, 100), (100, 2000)):
            arr, _ = load_oriented(_encode(Image.new("RGB", size, (9, 9, 9)), fmt))
            assert arr.shape[:2] == (size[1], size[0])

    @pytest.mark.parametrize("size", [(2001, 100), (100, 2001), (4000, 100), (64, 1281)])
    def test_beyond_twenty_to_one_is_rejected(self, size):
        with pytest.raises(ImageRejected, match="aspect ratio"):
            load_oriented(_encode(Image.new("RGB", size), "PNG"))

    def test_the_937500_by_64_png_is_rejected_from_the_header(self):
        data = _encode(Image.new("1", (937_500, 64)), "PNG")
        assert MAX_PIXELS == 937_500 * 64
        with pytest.raises(ImageRejected, match="aspect ratio"):
            load_oriented(data)
        with pytest.raises(ImageRejected, match="aspect ratio"):
            load_oriented(_encode(Image.new("1", (64, 937_500)), "PNG"))

    def test_the_limit_applies_before_the_orientation_swap_and_is_orientation_blind(self):
        with pytest.raises(ImageRejected, match="aspect ratio"):
            load_oriented(_encode(Image.new("RGB", (2001, 100)), "JPEG", orientation=6))

    def test_a_degenerate_image_never_reaches_the_reference_detectors(self):
        from rga.domain.types import ReferenceKind
        from rga.vision.reference_circle import find_circle_candidates
        from rga.vision.reference_grid import find_grid

        for size in ((64, 937_500), (937_500, 64)):
            with pytest.raises(ImageRejected):
                load_oriented(_encode(Image.new("1", size), "PNG"))
        gray = np.full((64, 937_500), 200, dtype=np.uint8)
        with pytest.raises(ValueError):
            find_grid(gray)
        with pytest.raises(ValueError):
            find_circle_candidates(gray, ReferenceKind.CIRCLE_1IN)


# ------------------------------------------------------------------ metadata container paths

BAD_TIFF_HEADERS = [
    b"XX*\0\x08\0\0\0\0\0\0\0",
    b"II*",
    b"\0\0\0\0\x08\0\0\0",
    b"II+\0\x08\0\0\0",
]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """A PNG chunk with a correct CRC, so Pillow actually reaches the payload."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_with_chunks(chunks: list[tuple[bytes, bytes]], *, after_idat: bool = False) -> bytes:
    buf = io.BytesIO()
    _marker_image().save(buf, "PNG")
    base = buf.getvalue()
    at = base.rfind(b"IEND") - 4 if after_idat else 33
    return base[:at] + b"".join(_png_chunk(k, p) for k, p in chunks) + base[at:]


def _raw_profile_text(tiff: bytes, *, hex_text: str | None = None) -> tuple[bytes, bytes]:
    text = tiff.hex() if hex_text is None else hex_text
    return b"tEXt", b"Raw profile type exif\0\nexif\n" + str(
        len(tiff)
    ).encode() + b"\n" + text.encode() + b"\n"


def _jpeg_with_density(exif: bytes) -> bytes:
    buf = io.BytesIO()
    _marker_image().save(buf, "JPEG", dpi=(300, 300), exif=exif)
    return buf.getvalue()


def _heif_with_exif(exif: bytes) -> bytes:
    buf = io.BytesIO()
    _marker_image().save(buf, "HEIF", exif=exif)
    return buf.getvalue()


ORIENTATION_6_TIFF = _raw_exif([EXIF_ORIENTATION_6])[6:]
FLOAT_MAKE = (0x010F, 11, 1, struct.pack("<f", 1.0))


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestMalformedMetadataIsRejectedInEveryContainer:
    """Each trigger found by review-18: malformed EXIF/XMP must surface as ImageRejected."""

    @pytest.mark.parametrize("header", BAD_TIFF_HEADERS)
    def test_jpeg_with_jfif_density_and_a_non_tiff_exif_header(self, header):
        with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
            load_oriented(_jpeg_with_density(b"Exif\0\0" + header))

    @pytest.mark.parametrize("after_idat", [False, True], ids=["before_idat", "after_idat"])
    @pytest.mark.parametrize("header", BAD_TIFF_HEADERS)
    def test_png_exif_chunk_with_a_non_tiff_header(self, header, after_idat):
        data = _png_with_chunks([(b"eXIf", header)], after_idat=after_idat)
        with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
            load_oriented(data)

    def test_png_raw_profile_exif_text_that_is_not_hex(self):
        data = _png_with_chunks([_raw_profile_text(b"x" * 8, hex_text="ZZZZ")])
        with pytest.raises(ImageRejected, match="invalid EXIF metadata: ValueError"):
            load_oriented(data)

    def test_png_raw_profile_exif_hex_that_is_not_a_tiff_file(self):
        data = _png_with_chunks([_raw_profile_text(BAD_TIFF_HEADERS[0])])
        with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
            load_oriented(data)

    def test_png_text_chunk_named_xmp_is_read_as_an_xmp_packet(self):
        data = _png_with_chunks([(b"tEXt", b'xmp\0<x:xmpmeta tiff:Orientation="6"/>')])
        arr, _ = load_oriented(data)
        assert arr.shape == (96, 48, 3)
        plain = load_oriented(_png_with_chunks([(b"tEXt", b"xmp\0<x:xmpmeta/>")]))[0]
        assert plain.shape == (48, 96, 3)

    @pytest.mark.parametrize("header", BAD_TIFF_HEADERS[:2])
    def test_heif_with_bad_exif(self, header):
        with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
            load_oriented(_heif_with_exif(b"Exif\0\0" + header))

    def test_the_rejection_names_the_exception_but_not_the_payload(self):
        data = _png_with_chunks([(b"eXIf", b"SECRET-PAYLOAD")])
        with pytest.raises(ImageRejected) as info:
            load_oriented(data)
        assert "SECRET" not in str(info.value)


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestWellFormedMetadataStillOrientsInEveryContainer:
    def test_png_exif_chunk(self):
        for after_idat in (False, True):
            arr, _ = load_oriented(
                _png_with_chunks([(b"eXIf", ORIENTATION_6_TIFF)], after_idat=after_idat)
            )
            assert arr.shape == (96, 48, 3)
            _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_png_raw_profile_exif(self):
        arr, _ = load_oriented(_png_with_chunks([_raw_profile_text(ORIENTATION_6_TIFF)]))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_jpeg_with_jfif_density(self):
        arr, _ = load_oriented(_jpeg_with_density(b"Exif\0\0" + ORIENTATION_6_TIFF))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_heif(self):
        arr, _ = load_oriented(_heif_with_exif(b"Exif\0\0" + ORIENTATION_6_TIFF))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_png_xmp_orientation_in_an_itxt_chunk(self):
        payload = b"XML:com.adobe.xmp\0\0\0\0\0" + b'<x tiff:Orientation="6"/>'
        arr, _ = load_oriented(_png_with_chunks([(b"iTXt", payload)]))
        assert arr.shape == (96, 48, 3)

    @pytest.mark.parametrize("after_idat", [False, True], ids=["before_idat", "after_idat"])
    def test_a_malformed_unrelated_tag_in_a_png_exif_chunk_still_loads_oriented(self, after_idat):
        tiff = _raw_exif([EXIF_ORIENTATION_6, FLOAT_MAKE, EXIF_XRESOLUTION_AS_ASCII])[6:]
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)], after_idat=after_idat))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_a_malformed_unrelated_tag_with_jfif_density_still_loads_oriented(self):
        exif = _raw_exif([EXIF_ORIENTATION_6, FLOAT_MAKE])
        arr, _ = load_oriented(_jpeg_with_density(exif))
        _assert_corner_colors(arr, red_at="tr", green_at="bl")


_FUZZ_TAGS = [
    0x0112, 0x0112, 0x0112, 0x0128, 0x011A, 0x011B, 0x8769, 0x8825, 0xA005, 0x0131, 0x010F,
    0x0132, 0x0213, 0x9286, 0x927C, 0x0201, 0x0202, 0x0100, 0x0101, 0x02BC, 0x8773, 0x0000, 0xFFFF,
]  # fmt: skip
_FUZZ_COUNTS = [0, 1, 1, 1, 2, 3, 4, 8, 1000, 0x7FFFFFFF, 0xFFFFFFFF]
_FUZZ_KINDS = [
    "jpeg_density",
    "jpeg_plain",
    "png_exif_before",
    "png_exif_after",
    "png_raw_profile",
    "png_xmp_text",
    "png_xmp_itxt",
    "jpeg_xmp",
    "heif",
]


def _fuzz_tiff(rng) -> bytes:
    """A TIFF/EXIF payload with random header, IFD offset, entry types and counts, and framing."""
    endian = rng.choice("<>")
    if rng.random() < 0.08:
        head = rng.choice([b"XX*\0", b"II+\0", b"MM\0+", b"II*", b"", b"\0\0\0\0", b"IIBC"])
        if rng.random() < 0.25:
            return head
    else:
        head = b"II*\0" if endian == "<" else b"MM\0*"
    offset = rng.choice([8, 8, 8, 8, 0, 4, 7, 9, 0xFFFFFFF0, 0x7FFFFFFF, 30])
    entries = []
    for _ in range(rng.randint(0, 6)):
        tag = rng.choice(_FUZZ_TAGS) if rng.random() < 0.9 else rng.randrange(65536)
        tiff_type, count = rng.choice([*range(20), 0xFFFF, 99]), rng.choice(_FUZZ_COUNTS)
        value = bytes(rng.randrange(256) for _ in range(4))
        if tag == 0x0112 and rng.random() < 0.5:
            tiff_type, count = 3, 1
            value = struct.pack(endian + "H", rng.randint(0, 10)) + b"\0\0"
        entries.append((tag, tiff_type, count, value))
    declared = len(entries) if rng.random() < 0.9 else rng.choice([0, 0xFFFF, len(entries) + 3])
    ifd = struct.pack(endian + "H", declared)
    for tag, tiff_type, count, value in entries:
        ifd += struct.pack(endian + "HHI", tag, tiff_type, count) + value
    ifd += struct.pack(endian + "I", rng.choice([0, 0, 8, 0xFFFFFFFF]))
    tail = bytes(rng.randrange(256) for _ in range(rng.choice([0, 0, 16, 64])))
    body = head + struct.pack(endian + "I", offset) + ifd + tail
    return body[: rng.randrange(len(body) + 1)] if rng.random() < 0.05 else body


def _fuzz_container(rng) -> tuple[str, bytes]:
    """One image whose EXIF/XMP payload is mutated while the container framing stays valid."""
    kind = rng.choice(_FUZZ_KINDS)
    tiff = _fuzz_tiff(rng)
    exif = b"Exif\0\0" + tiff
    buf = io.BytesIO()
    if kind in ("jpeg_density", "jpeg_plain", "jpeg_xmp"):
        extra = {"dpi": (300, 300)} if kind == "jpeg_density" else {}
        _marker_image().save(buf, "JPEG", exif=exif, **extra)
        data = buf.getvalue()
        if kind == "jpeg_xmp":
            xmp = rng.choice([b'<x tiff:Orientation="6"/>', b"<x tiff:Orientation>9</x>", tiff])
            segment = b"http://ns.adobe.com/xap/1.0/\0" + xmp
            data = data[:2] + b"\xff\xe1" + struct.pack(">H", len(segment) + 2) + segment + data[2:]
        return kind, data
    if kind == "heif":
        _marker_image().save(buf, "HEIF", exif=exif)
        return kind, buf.getvalue()
    if kind == "png_exif_before":
        return kind, _png_with_chunks([(b"eXIf", tiff)])
    if kind == "png_exif_after":
        return kind, _png_with_chunks([(b"eXIf", tiff)], after_idat=True)
    if kind == "png_raw_profile":
        text = tiff.hex() if rng.random() < 0.8 else "ZZ" + tiff.hex()
        return kind, _png_with_chunks([_raw_profile_text(tiff, hex_text=text)])
    if kind == "png_xmp_text":
        return kind, _png_with_chunks([(b"tEXt", b"xmp\0" + rng.choice([b"<x/>", b"", tiff]))])
    value = rng.choice([b'<x tiff:Orientation="6"/>', b'<x tiff:Orientation="0"/>', b"junk", tiff])
    return kind, _png_with_chunks([(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + value)])


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_metadata_container_fuzz_only_loads_or_raises_image_rejected():
    import collections
    import random

    rng = random.Random(18)
    outcomes: collections.Counter = collections.Counter()
    for _ in range(6000):
        kind, data = _fuzz_container(rng)
        try:
            arr, _ = load_oriented(data)
        except ImageRejected:
            outcomes[kind, "rejected"] += 1
            continue
        assert arr.dtype == np.uint8
        assert arr.ndim == 3
        outcomes[kind, "loaded"] += 1
    kinds_seen = {kind for kind, _ in outcomes}
    assert kinds_seen == set(_FUZZ_KINDS)
    assert all(sum(v for (k, _), v in outcomes.items() if k == kind) >= 300 for kind in _FUZZ_KINDS)
    assert sum(v for (_, r), v in outcomes.items() if r == "rejected") >= 500
    assert sum(v for (_, r), v in outcomes.items() if r == "loaded") >= 2000
    for kind in ("jpeg_density", "png_exif_before", "png_exif_after", "png_raw_profile", "heif"):
        assert outcomes[kind, "rejected"] > 0, f"{kind}: the fuzz never reached the EXIF guard"
    assert outcomes["png_xmp_text", "loaded"] > 0


# ------------------------------------------------------------------ metadata size cap (G2-C1)

CAP = rga_io.MAX_METADATA_BYTES
PEAK_LIMIT_BYTES = 64 * 2**20


def _amplifying_tiff(payload_bytes: int, entries: int) -> bytes:
    """IFD0 with distinct UNDEFINED tags whose in-bounds counts all span the payload."""
    total = max(payload_bytes, 8 + 2 + 12 * entries + 4 + 16)
    count = total - 1 - 8
    tags = [t for t in range(1, 65536) if t != 0x0112][:entries]
    ifd = struct.pack("<H", entries) + b"".join(struct.pack("<HHII", t, 7, count, 1) for t in tags)
    body = b"II*\0" + struct.pack("<I", 8) + ifd + struct.pack("<I", 0)
    return body + bytes(total - len(body))


def _padded_orientation_tiff(total_bytes: int, orientation: int = 6) -> bytes:
    """A valid TIFF with one Orientation entry, zero-padded to exactly `total_bytes`."""
    body = _raw_exif([(0x0112, 3, 1, struct.pack("<H", orientation))])[6:]
    return body + bytes(total_bytes - len(body))


def _jpeg_segments(prefix: bytes, payload: bytes, piece: int = 65000) -> bytes:
    """Consecutive APP1 segments carrying `payload`; every segment starts with `prefix`."""
    out = b""
    for start in range(0, len(payload), piece):
        part = payload[start : start + piece]
        out += b"\xff\xe1" + struct.pack(">H", len(part) + len(prefix) + 2) + prefix + part
    return out


def _jpeg_with(segments: bytes, *, density: bool = False, junk: bytes = b"") -> bytes:
    buf = io.BytesIO()
    _marker_image().save(buf, "JPEG", **({"dpi": (300, 300)} if density else {}))
    base = buf.getvalue()
    app0_end = 4 + int.from_bytes(base[4:6], "big")
    return base[:app0_end] + junk + segments + base[app0_end:]


def _measure(data: bytes):
    """Run load_oriented under tracemalloc; returns (outcome, peak bytes) with the outcome either
    the decoded array or the ImageRejected raised.
    """
    tracemalloc.start()
    try:
        try:
            outcome = load_oriented(data)[0]
        except ImageRejected as exc:
            outcome = exc
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return outcome, peak


def _amplification_inputs() -> dict[str, bytes]:
    big_png, big_jpeg, mid = (
        _amplifying_tiff(4 * 2**20, 300),
        _amplifying_tiff(2 * 2**20, 300),
        _amplifying_tiff(700 * 1024, 500),
    )
    z_payload = _amplifying_tiff(480 * 1024, 700)
    z_text = b"exif\n" + str(len(z_payload)).encode() + b"\n" + z_payload.hex().encode() + b"\n"
    heif_exif = b"Exif\0\0" + _amplifying_tiff(1024 * 1024, 500)
    return {
        "png_exif_before_idat_4mb_300": _png_with_chunks([(b"eXIf", big_png)]),
        "png_exif_after_idat": _png_with_chunks([(b"eXIf", big_jpeg)], after_idat=True),
        "png_raw_profile_text": _png_with_chunks([_raw_profile_text(mid)]),
        "png_raw_profile_ztxt": _png_with_chunks(
            [(b"zTXt", b"Raw profile type exif\0\0" + zlib.compress(z_text))]
        ),
        "png_raw_profile_compressed_itxt": _png_with_chunks(
            [(b"iTXt", b"Raw profile type exif\0\1\0\0\0" + zlib.compress(z_text))]
        ),
        "jpeg_multi_app1": _jpeg_with(_jpeg_segments(b"Exif\0\0", big_jpeg)),
        "jpeg_multi_app1_with_density": _jpeg_with(
            _jpeg_segments(b"Exif\0\0", big_jpeg), density=True
        ),
        "jpeg_junk_before_segments": _jpeg_with(
            _jpeg_segments(b"Exif\0\0", big_jpeg), junk=b"\0\0\x07"
        ),
        "jpeg_ff_padding_before_segments": _jpeg_with(
            _jpeg_segments(b"Exif\0\0", big_jpeg), junk=b"\xff\xff\xff"
        ),
        "heif": _heif_with_exif(heif_exif),
    }


AMPLIFICATION_INPUTS = _amplification_inputs()


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestMetadataSizeCap:
    def test_the_cap_is_256_kb(self):
        assert CAP == 256 * 1024

    @pytest.mark.parametrize("name", list(AMPLIFICATION_INPUTS))
    def test_amplifying_metadata_is_rejected_fast_and_cheaply(self, name):
        started = time.perf_counter()
        outcome, peak = _measure(AMPLIFICATION_INPUTS[name])
        assert isinstance(outcome, ImageRejected), name
        assert str(outcome) == "EXIF metadata too large"
        assert peak < PEAK_LIMIT_BYTES, f"{name}: peak {peak / 2**20:.0f} MB"
        assert time.perf_counter() - started < 2.0

    @pytest.mark.parametrize("name", [n for n in AMPLIFICATION_INPUTS if n != "heif"])
    def test_jpeg_and_png_are_rejected_before_pillow_opens_the_file(self, name, monkeypatch):
        opened = []
        real_open = Image.open
        monkeypatch.setattr(
            Image, "open", lambda *a, **k: (opened.append(1), real_open(*a, **k))[1]
        )
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(AMPLIFICATION_INPUTS[name])
        assert opened == []

    def test_heif_metadata_is_rejected_before_pillow_parses_the_exif(self, monkeypatch):
        parsed = []
        real = Image.Image.getexif
        monkeypatch.setattr(Image.Image, "getexif", lambda self: (parsed.append(1), real(self))[1])
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(AMPLIFICATION_INPUTS["heif"])
        assert parsed == []

    def test_a_png_exif_chunk_of_exactly_the_cap_loads_and_one_byte_more_is_rejected(self):
        for after_idat in (False, True):
            at_cap = _png_with_chunks(
                [(b"eXIf", _padded_orientation_tiff(CAP))], after_idat=after_idat
            )
            arr, _ = load_oriented(at_cap)
            assert arr.shape == (96, 48, 3)
            over = _png_with_chunks(
                [(b"eXIf", _padded_orientation_tiff(CAP + 1))], after_idat=after_idat
            )
            with pytest.raises(ImageRejected, match="too large"):
                load_oriented(over)

    def test_jpeg_exif_segments_of_exactly_the_cap_load_and_one_byte_more_is_rejected(self):
        for density in (False, True):
            for total, ok in ((CAP, True), (CAP + 1, False)):
                payload = _padded_orientation_tiff(total)
                data = _jpeg_with(
                    _jpeg_segments(b"Exif\0\0", payload, piece=len(payload) // 5 + 1),
                    density=density,
                )
                if ok:
                    assert load_oriented(data)[0].shape == (96, 48, 3)
                else:
                    with pytest.raises(ImageRejected, match="too large"):
                        load_oriented(data)

    def test_png_text_carriers_are_measured_by_decoded_size(self):
        key = b"XML:com.adobe.xmp"
        keyword_cases = {
            "tEXt": lambda body: (b"tEXt", key + b"\0" + body),
            "iTXt": lambda body: (b"iTXt", key + b"\0\0\0\0\0" + body),
            "zTXt": lambda body: (b"zTXt", key + b"\0\0" + zlib.compress(body)),
            "iTXt_compressed": lambda body: (b"iTXt", key + b"\0\1\0\0\0" + zlib.compress(body)),
        }
        for name, build in keyword_cases.items():
            at_cap = _png_with_chunks([build(b"x" * CAP)])
            assert load_oriented(at_cap)[0].shape == (48, 96, 3), name
            with pytest.raises(ImageRejected, match="too large"):
                load_oriented(_png_with_chunks([build(b"x" * (CAP + 1))]))

    @pytest.mark.parametrize("kind", [b"tEXt", b"zTXt"])
    def test_an_oversize_chunk_keyworded_xmp_is_rejected_for_size_not_for_its_type(self, kind):
        body = b"xmp\0" + (
            b"x" * (CAP + 1) if kind == b"tEXt" else b"\0" + zlib.compress(b"x" * (CAP + 1))
        )
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(_png_with_chunks([(kind, body)]))

    def test_carriers_are_summed_not_judged_one_by_one(self):
        half = b"x" * (CAP // 2 + 1)
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(
                _jpeg_with(
                    _jpeg_segments(b"Exif\0\0", _padded_orientation_tiff(len(half)))
                    + _jpeg_segments(b"http://ns.adobe.com/xap/1.0/\0", half)
                )
            )
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(
                _png_with_chunks(
                    [(b"eXIf", _padded_orientation_tiff(len(half))), (b"tEXt", b"xmp\0" + half)]
                )
            )
        two = _jpeg_segments(b"Exif\0\0", _padded_orientation_tiff(CAP // 2 + 10))
        with pytest.raises(ImageRejected, match="too large"):
            load_oriented(_jpeg_with(two + two))

    @pytest.mark.parametrize("kind", [b"zTXt", b"iTXt"])
    def test_a_compressed_chunk_that_inflates_hugely_is_rejected_without_inflating_it(self, kind):
        bomb = zlib.compress(bytes(200 * 2**20))
        if kind == b"zTXt":
            chunk = (b"zTXt", b"Raw profile type exif\0\0" + bomb)
        else:
            chunk = (b"iTXt", b"Raw profile type exif\0\1\0\0\0" + bomb)
        data = _png_with_chunks([chunk])
        assert len(data) < 1024 * 1024
        outcome, peak = _measure(data)
        assert isinstance(outcome, ImageRejected)
        assert str(outcome) == "EXIF metadata too large"
        assert peak < PEAK_LIMIT_BYTES

    def test_metadata_chunks_after_iend_are_ignored_like_pillow_ignores_them(self):
        base = _png_with_chunks([])
        data = base + _png_chunk(b"eXIf", _padded_orientation_tiff(2 * CAP))
        rebuilt = rga_io._rebuild_png(data)
        assert (rebuilt.exif, rebuilt.xmp) == (None, b"")
        assert rebuilt.data == base
        assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_text_chunks_with_other_keywords_are_not_counted(self):
        data = _png_with_chunks([(b"tEXt", b"Comment\0" + b"x" * (2 * CAP))])
        assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_non_exif_jpeg_segments_are_not_counted(self):
        icc = b"\xff\xe2" + struct.pack(">H", 60002) + b"ICC_PROFILE\0" + bytes(59989)
        other_app1 = _jpeg_segments(b"NotExif\0", bytes(2 * CAP))
        data = _jpeg_with(icc * 8 + other_app1)
        assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_metadata_after_the_start_of_scan_is_not_counted_by_the_scanner(self):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG")
        appended = buf.getvalue() + _jpeg_segments(b"Exif\0\0", bytes(3 * CAP))
        stripped = rga_io._rebuild_jpeg(appended)
        assert (stripped.exif, stripped.xmp) == (None, b"")
        assert stripped.data == appended

    def test_normal_camera_sized_exif_still_loads_and_orients(self):
        entries = [
            (0x010F, 2, 6, struct.pack("<I", 200)),
            (0x0110, 2, 8, struct.pack("<I", 210)),
            EXIF_ORIENTATION_6,
        ]
        tiff = _raw_exif(entries)[6:]
        tiff = tiff + b"\0" * (260 - len(tiff)) + b"Canon\0" + bytes(4) + b"PowerShot\0".ljust(8)
        tiff += bytes(60 * 1024)
        for data in (
            _png_with_chunks([(b"eXIf", tiff)]),
            _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff, piece=30000), density=True),
            _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff, piece=30000)),
            _heif_with_exif(b"Exif\0\0" + tiff),
        ):
            arr, _ = load_oriented(data)
            assert arr.shape == (96, 48, 3)

    def test_info_carrier_size_counts_exif_xmp_and_raw_profile(self):
        info = {
            "exif": b"Exif\0\0" + b"a" * 10,
            "xmp": b"b" * 20,
            "XML:com.adobe.xmp": "c" * 30,
            "Raw profile type exif": "d" * 40,
            "comment": b"e" * 1000,
        }
        assert rga_io._info_metadata_bytes(info) == 10 + 30 + 40
        assert rga_io._info_metadata_bytes({"exif": 5, "xmp": None}) == 0

    def test_jpeg_strip_walks_markers_like_pillow(self):
        exif = _jpeg_segments(b"Exif\0\0", bytes(1000))
        for variant in (
            _jpeg_with(exif),
            _jpeg_with(exif, junk=b"\0\x07"),
            _jpeg_with(exif, junk=b"\xff\xff"),
        ):
            stripped = rga_io._rebuild_jpeg(variant)
            assert stripped.exif == bytes(1000)
            assert b"Exif" not in stripped.data
        assert rga_io._rebuild_jpeg(_jpeg_with(b"")).exif is None
        for broken in (b"\xff\xd8\xff", b"\xff\xd8\xff\xe1\x00", b"\xff\xd8\xff\xe1\x00\x10"):
            with pytest.raises(ImageRejected):
                rga_io._rebuild_jpeg(broken)

    def test_png_rebuild_takes_each_carrier_kind_once_and_drops_every_non_whitelisted_chunk(self):
        tiff = _padded_orientation_tiff(1000)
        other = (b"tEXt", b"Comment\0" + b"z" * 5000)
        data = _png_with_chunks(
            [
                (b"eXIf", tiff),
                _raw_profile_text(bytes(8), hex_text="ab" * 10),
                (b"tEXt", b"xmp\0" + b"x" * 7),
                (b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + b"y" * 5),
                other,
            ]
        )
        stripped = rga_io._rebuild_png(data)
        assert stripped.exif == tiff
        assert stripped.xmp == b"x" * 7 + b"y" * 5
        assert stripped.data == _png_with_chunks([])

    def test_png_strip_prefers_the_exif_chunk_over_a_raw_profile(self):
        raw = _raw_profile_text(_padded_orientation_tiff(40, orientation=3))
        both = _png_with_chunks([raw, (b"eXIf", _padded_orientation_tiff(40, orientation=6))])
        assert rga_io._rebuild_png(both).exif == _padded_orientation_tiff(40, orientation=6)
        assert rga_io._rebuild_png(_png_with_chunks([raw])).exif == _padded_orientation_tiff(
            40, orientation=3
        )

    @pytest.mark.parametrize("seed", range(3))
    def test_strippers_only_return_or_raise_image_rejected_on_corrupted_containers(self, seed):
        import random

        rng = random.Random(seed)
        samples = [
            AMPLIFICATION_INPUTS["jpeg_multi_app1"][:70000],
            _png_with_chunks([(b"eXIf", bytes(500))]),
            _jpeg_with(_jpeg_segments(b"Exif\0\0", bytes(500))),
        ]
        for _ in range(500):
            data = bytearray(rng.choice(samples))
            for _ in range(rng.randint(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            data = bytes(data[: rng.randrange(len(data) // 2, len(data) + 1)])
            with contextlib.suppress(ImageRejected):
                rga_io._rebuild_jpeg(b"\xff\xd8\xff" + data)
            with contextlib.suppress(ImageRejected):
                rga_io._rebuild_png(rga_io._PNG_MAGIC + data)


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestMalformedTagBeforeOrientation:
    """review-19 note 1: Pillow stops reading IFD0 at an entry whose value offset is out of range,
    which dropped an Orientation sorted after it. The rotation is now recovered from IFD0 directly.
    """

    BAD_MAKE = (0x010F, 2, 64, struct.pack("<I", 0xFFFFFF00))

    def _tiff(self, *entries) -> bytes:
        return _raw_exif(list(entries))[6:]

    @pytest.mark.parametrize(
        "variant", ["png_exif", "jpeg_density", "jpeg_plain", "png_exif_after"]
    )
    def test_a_bad_tag_sorted_before_orientation_still_rotates(self, variant):
        tiff = self._tiff(self.BAD_MAKE, EXIF_ORIENTATION_6)
        data = {
            "png_exif": _png_with_chunks([(b"eXIf", tiff)]),
            "png_exif_after": _png_with_chunks([(b"eXIf", tiff)], after_idat=True),
            "jpeg_density": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff), density=True),
            "jpeg_plain": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff)),
        }[variant]
        arr, _ = load_oriented(data)
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    @pytest.mark.parametrize("orientation", range(1, 9))
    def test_every_orientation_is_recovered_after_a_bad_leading_tag(self, orientation):
        tiff = self._tiff(self.BAD_MAKE, (0x0112, 3, 1, struct.pack("<H", orientation)))
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)]))
        red_at, green_at, shape = ORIENTATION_RESULTS[orientation]
        assert arr.shape == shape
        _assert_corner_colors(arr, red_at=red_at, green_at=green_at)

    def test_the_same_tags_in_the_other_order_give_the_same_pixels(self):
        before = self._tiff(self.BAD_MAKE, EXIF_ORIENTATION_6)
        after = self._tiff(EXIF_ORIENTATION_6, self.BAD_MAKE)
        a = load_oriented(_png_with_chunks([(b"eXIf", before)]))[0]
        b = load_oriented(_png_with_chunks([(b"eXIf", after)]))[0]
        np.testing.assert_array_equal(a, b)

    def test_a_bad_leading_tag_without_an_orientation_leaves_the_pixels_unrotated(self):
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", self._tiff(self.BAD_MAKE))]))
        assert arr.shape == (48, 96, 3)

    @pytest.mark.parametrize("value", [0, 9, 0xFFFF])
    def test_an_out_of_range_orientation_after_a_bad_tag_is_ignored(self, value):
        tiff = self._tiff(self.BAD_MAKE, (0x0112, 3, 1, struct.pack("<H", value)))
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)]))
        assert arr.shape == (48, 96, 3)

    def test_pillows_own_orientation_wins_over_the_fallback_when_it_finds_one(self):
        tiff = self._tiff(
            (0x0112, 3, 1, struct.pack("<H", 3)), (0x0112, 3, 1, struct.pack("<H", 6))
        )
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)]))
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="tr", green_at="bl")

    def test_the_fallback_reads_ifd0_only_and_never_out_of_line_values(self):
        for tiff in (
            b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 0xFFFF),
            b"II*\0" + struct.pack("<I", 0xFFFFFFF0),
            b"II*\0" + struct.pack("<I", 8) + struct.pack("<H", 3) + bytes(5),
            b"MM\0*" + struct.pack(">I", 8),
        ):
            arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)]))
            assert arr.shape == (48, 96, 3)


class TestIfd0OrientationReader:
    @staticmethod
    def _be(entries) -> bytes:
        ifd = struct.pack(">H", len(entries))
        for tag, typ, count, value in entries:
            ifd += struct.pack(">HHI", tag, typ, count) + value.ljust(4, b"\0")
        return b"MM\0*" + struct.pack(">I", 8) + ifd + struct.pack(">I", 0)

    def test_reads_little_and_big_endian(self):
        assert rga_io._ifd0_orientation(_raw_exif([EXIF_ORIENTATION_6])[6:]) == 6
        assert rga_io._ifd0_orientation(self._be([(0x0112, 3, 1, struct.pack(">H", 8))])) == 8

    def test_strips_the_exif_prefix(self):
        assert rga_io._ifd0_orientation(_raw_exif([EXIF_ORIENTATION_6])) == 6

    def test_finds_the_tag_among_other_entries(self):
        entries = [(0x010F, 2, 64, bytes(4)), (0x0110, 2, 64, bytes(4)), EXIF_ORIENTATION_6]
        assert rga_io._ifd0_orientation(_raw_exif(entries)[6:]) == 6

    @pytest.mark.parametrize(
        "entry",
        [
            (0x0112, 4, 1, struct.pack("<I", 6)),
            (0x0112, 3, 2, struct.pack("<HH", 6, 1)),
            (0x0112, 1, 1, b"\x06"),
            (0x0113, 3, 1, struct.pack("<H", 6)),
        ],
    )
    def test_only_a_single_short_orientation_entry_counts(self, entry):
        assert rga_io._ifd0_orientation(_raw_exif([entry])[6:]) is None

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"II",
            b"II*\0",
            b"XX*\0\x08\0\0\0\0\0",
            b"II+\0\x08\0\0\0\0\0",
            b"II*\0" + struct.pack("<I", 0xFFFFFFFF),
            b"II*\0" + struct.pack("<I", 8) + b"\x01",
        ],
    )
    def test_malformed_input_gives_none_not_an_exception(self, raw):
        assert rga_io._ifd0_orientation(raw) is None

    def test_the_declared_count_is_clamped_to_the_bytes_present(self):
        raw = (
            b"II*\0"
            + struct.pack("<I", 8)
            + struct.pack("<H", 60000)
            + struct.pack("<HHI", 0x0112, 3, 1)
            + struct.pack("<H", 5)
            + b"\0\0"
        )
        assert rga_io._ifd0_orientation(raw) == 5

    def test_a_wrong_tiff_magic_gives_none_even_with_a_valid_looking_ifd(self):
        valid = _raw_exif([EXIF_ORIENTATION_6])[6:]
        for magic in (b"+\0", b"\0\0", b"\0*"):
            assert rga_io._ifd0_orientation(valid[:2] + magic + valid[4:]) is None
        assert rga_io._ifd0_orientation(valid) == 6

    def test_an_ifd_offset_past_the_end_gives_none(self):
        for offset in (len(_raw_exif([EXIF_ORIENTATION_6])[6:]), 0x7FFFFFFF, 0xFFFFFFFF):
            raw = b"II*\0" + struct.pack("<I", offset) + bytes(20)
            assert rga_io._ifd0_orientation(raw) is None

    def test_the_last_orientation_entry_wins_as_in_pillow(self):
        two = [EXIF_ORIENTATION_6, (0x0112, 3, 1, struct.pack("<H", 8))]
        assert rga_io._ifd0_orientation(_raw_exif(two)[6:]) == 8


class TestShortAncillaryPngChunks:
    """review-19 note 4. A short fixed-size chunk used to raise struct.error inside Pillow's loader;
    the whitelist now rejects it by size first, and struct.error from the decoder is still mapped.
    """

    @pytest.mark.parametrize(
        ("kind", "payload"), [(b"gAMA", b"\0\1"), (b"cHRM", b"\0\1\2"), (b"pHYs", b"\0\1\2\3\4")]
    )
    def test_a_short_post_idat_chunk_is_rejected(self, kind, payload):
        data = _png_with_chunks([(kind, payload)], after_idat=True)
        with pytest.raises(ImageRejected, match="malformed PNG chunk size"):
            load_oriented(data)

    def test_struct_error_from_the_decoder_is_still_mapped(self, monkeypatch):
        from PIL import PngImagePlugin

        def explode(self):
            raise struct.error("unpack requires a buffer of 4 bytes")

        monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", explode)
        with pytest.raises(ImageRejected, match="could not be decoded: error"):
            load_oriented(PLAIN_PNG)


# ------------------------------------------------------------------ Pillow never parses upload EXIF (G3-C1, G3-W3)

UNDER_CAP_PAYLOAD = 250 * 1024
RAW_PROFILE_PAYLOAD = 120 * 1024
ENTRY_COUNTS = [4000, 10000]
XMP_ORIENTATION_6 = b'<x:xmpmeta tiff:Orientation="6"/>'


def _entry_flood_inputs(entries: int) -> dict[str, bytes]:
    """Carriers under the 256 KB cap whose IFD declares `entries` entries, in every container."""
    tiff = _amplifying_tiff(UNDER_CAP_PAYLOAD, entries)
    raw_tiff = _amplifying_tiff(RAW_PROFILE_PAYLOAD, entries)
    assert len(tiff) <= CAP and len(raw_tiff.hex()) < CAP
    return {
        "png_exif": _png_with_chunks([(b"eXIf", tiff)]),
        "png_exif_after_idat": _png_with_chunks([(b"eXIf", tiff)], after_idat=True),
        "png_raw_profile": _png_with_chunks([_raw_profile_text(raw_tiff)]),
        "jpeg_multi_app1": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff)),
        "jpeg_multi_app1_density": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff), density=True),
        "heif": _heif_with_exif(b"Exif\0\0" + tiff),
    }


ENTRY_FLOODS = {
    (name, entries): data
    for entries in ENTRY_COUNTS
    for name, data in _entry_flood_inputs(entries).items()
}


@pytest.fixture
def exif_parse_spy(monkeypatch):
    """Record every call into Pillow's EXIF/IFD parsing, delegating to the real function."""
    from PIL import JpegImagePlugin, TiffImagePlugin

    calls: list[str] = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            calls.append(f"{owner.__name__}.{name}")
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    spy(Image.Image, "getexif")
    spy(Image.Image, "_reload_exif")
    spy(Image.Exif, "load")
    spy(Image.Exif, "load_from_fp")
    spy(JpegImagePlugin.JpegImageFile, "_getexif")
    spy(TiffImagePlugin.ImageFileDirectory_v2, "load")
    return calls


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestPillowNeverParsesUploadExif:
    @pytest.mark.parametrize(("name", "entries"), list(ENTRY_FLOODS))
    def test_a_cap_compliant_carrier_with_thousands_of_entries_is_cheap(self, name, entries):
        started = time.perf_counter()
        outcome, peak = _measure(ENTRY_FLOODS[name, entries])
        assert isinstance(outcome, np.ndarray), (name, outcome)
        assert peak < PEAK_LIMIT_BYTES, f"{name} x {entries}: peak {peak / 2**20:.0f} MB"
        assert time.perf_counter() - started < 2.0

    @pytest.mark.parametrize(("name", "entries"), list(ENTRY_FLOODS))
    def test_no_pillow_exif_or_ifd_parsing_is_reached(self, name, entries, exif_parse_spy):
        load_oriented(ENTRY_FLOODS[name, entries])
        assert exif_parse_spy == []

    def test_no_pillow_exif_parsing_for_ordinary_and_hostile_uploads_in_any_container(
        self, exif_parse_spy
    ):
        tiff = _raw_exif([EXIF_ORIENTATION_6])[6:]
        bad = BAD_TIFF_HEADERS[0]
        inputs = {
            "jpeg_plain": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff)),
            "jpeg_density": _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff), density=True),
            "jpeg_save_exif": _jpeg_with(b"", density=False),
            "jpeg_bad_header": _jpeg_with(_jpeg_segments(b"Exif\0\0", bad), density=True),
            "jpeg_xmp": _jpeg_with(
                _jpeg_segments(b"http://ns.adobe.com/xap/1.0/\0", XMP_ORIENTATION_6)
            ),
            "png_exif": _png_with_chunks([(b"eXIf", tiff)]),
            "png_bad_header": _png_with_chunks([(b"eXIf", bad)]),
            "png_raw_profile": _png_with_chunks([_raw_profile_text(tiff)]),
            "png_xmp": _png_with_chunks([(b"tEXt", b"xmp\0" + XMP_ORIENTATION_6)]),
            "png_plain": _png_with_chunks([]),
            "heif_exif": _heif_with_exif(b"Exif\0\0" + tiff),
            "heif_bad_exif": _heif_with_exif(b"Exif\0\0" + bad),
            "heif_plain": _encode(_marker_image(), "HEIF"),
        }
        for name, data in inputs.items():
            exif_parse_spy.clear()
            with contextlib.suppress(ImageRejected):
                load_oriented(data)
            assert exif_parse_spy == [], name

    def test_the_spy_does_detect_a_pillow_exif_parse(self, exif_parse_spy):
        img = Image.open(
            io.BytesIO(_png_with_chunks([(b"eXIf", _raw_exif([EXIF_ORIENTATION_6])[6:])]))
        )
        img.getexif()
        assert "Image.getexif" in exif_parse_spy

    @pytest.mark.parametrize("container", ["jpeg", "png"])
    def test_carriers_are_removed_from_the_bytes_pillow_opens(self, container):
        tiff = _raw_exif([EXIF_ORIENTATION_6])[6:]
        if container == "jpeg":
            data = _jpeg_with(
                _jpeg_segments(b"Exif\0\0", tiff)
                + _jpeg_segments(b"http://ns.adobe.com/xap/1.0/\0", XMP_ORIENTATION_6),
                density=True,
            )
            stripped = rga_io._rebuild_jpeg(data).data
        else:
            data = _png_with_chunks(
                [
                    (b"eXIf", tiff),
                    _raw_profile_text(tiff),
                    (b"tEXt", b"xmp\0" + XMP_ORIENTATION_6),
                    (b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + XMP_ORIENTATION_6),
                    (b"tEXt", b"Comment\0kept"),
                ],
                after_idat=False,
            )
            stripped = rga_io._rebuild_png(data).data
        with Image.open(io.BytesIO(stripped)) as img:
            for key in ("exif", "xmp", "XML:com.adobe.xmp", "Raw profile type exif"):
                assert key not in img.info, key
            if container == "jpeg":
                assert img.info["dpi"] == (300, 300)
            else:
                assert "Comment" not in img.info
        assert len(stripped) < len(data)

    def test_a_truncated_exif_chunk_is_rejected(self):
        data = _png_with_chunks([(b"eXIf", _padded_orientation_tiff(1000))])
        cut = data[: data.index(b"eXIf") + 300]
        with pytest.raises(ImageRejected, match="truncated PNG chunk"):
            rga_io._rebuild_png(cut)
        with pytest.raises(ImageRejected):
            load_oriented(cut)

    def test_a_file_with_only_whitelisted_parts_is_passed_to_pillow_byte_for_byte(self):
        png = _png_with_chunks([(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))])
        assert rga_io._rebuild_png(png).data == png
        jpeg = _jpeg_with(b"", density=True)
        assert rga_io._rebuild_jpeg(jpeg).data == jpeg
        assert rga_io._rebuild_png(_png_with_chunks([(b"tEXt", b"Comment\0x")])).data == (
            _png_with_chunks([])
        )


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestMalformedJpegSegments:
    @pytest.mark.parametrize(
        "segment",
        [b"\xff\xe2\x00\x00", b"\xff\xe2\x00\x01", b"\xff\xfe\x00\x00", b"\xff\xe0\x00\x01"],
        ids=["app2_len0", "app2_len1", "com_len0", "app0_len1"],
    )
    def test_a_segment_with_a_declared_length_under_two_is_rejected(self, segment):
        tiff = _amplifying_tiff(4 * 2**20, 300)
        hiding = _jpeg_with(segment + _jpeg_segments(b"Exif\0\0", tiff))
        started = time.perf_counter()
        outcome, peak = _measure(hiding)
        assert isinstance(outcome, ImageRejected)
        assert "malformed JPEG segment" in str(outcome)
        assert peak < PEAK_LIMIT_BYTES
        assert time.perf_counter() - started < 2.0

    def test_the_malformed_segment_is_rejected_even_without_any_carrier(self):
        with pytest.raises(ImageRejected, match="malformed JPEG segment"):
            load_oriented(_jpeg_with(b"\xff\xe2\x00\x00"))

    def test_a_segment_with_length_two_is_valid_and_empty(self):
        assert load_oriented(_jpeg_with(b"\xff\xe2\x00\x02"))[0].shape == (48, 96, 3)

    def test_a_malformed_segment_after_the_start_of_scan_is_not_walked(self):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG")
        assert rga_io._rebuild_jpeg(buf.getvalue() + b"\xff\xe2\x00\x00").exif is None

    def test_a_truncated_exif_segment_is_rejected(self):
        exif = _jpeg_segments(b"Exif\0\0", bytes(1000))
        data = _jpeg_with(exif)
        cut = data[: data.index(b"Exif\0\0") + 300]
        with pytest.raises(ImageRejected, match="truncated JPEG segment"):
            rga_io._rebuild_jpeg(cut)
        with pytest.raises(ImageRejected):
            load_oriented(cut)

    def test_a_truncated_final_segment_is_left_to_pillow_to_reject(self):
        buf = io.BytesIO()
        _marker_image().save(buf, "JPEG")
        truncated = buf.getvalue()[:60]
        with pytest.raises(ImageRejected):
            load_oriented(truncated)


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestAnyPillowDecodeFailureIsImageRejected:
    @pytest.mark.parametrize(
        ("kind", "payload"),
        [(b"iCCP", b""), (b"iCCP", b"\0")],
    )
    def test_a_malformed_ancillary_chunk_after_idat_is_rejected(self, kind, payload):
        data = _png_with_chunks([(kind, payload)], after_idat=True)
        with pytest.raises(ImageRejected, match="could not be decoded"):
            load_oriented(data)

    def test_an_empty_iccp_before_idat_is_rejected_too(self):
        with pytest.raises(ImageRejected, match="could not be decoded"):
            load_oriented(_png_with_chunks([(b"iCCP", b"")]))

    def test_the_reported_exception_type_is_named_without_echoing_data(self):
        data = _png_with_chunks([(b"iCCP", b"")], after_idat=True)
        with pytest.raises(ImageRejected) as info:
            load_oriented(data)
        assert str(info.value) == "image could not be decoded: IndexError"

    @pytest.mark.parametrize(
        "exception", [IndexError, KeyError, AttributeError, TypeError, ZeroDivisionError]
    )
    def test_every_exception_class_from_the_decoder_is_mapped(self, exception, monkeypatch):
        def explode(self):
            raise exception("boom")

        from PIL import PngImagePlugin

        monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", explode)
        with pytest.raises(ImageRejected, match="could not be decoded"):
            load_oriented(_png_with_chunks([]))

    @pytest.mark.parametrize("exception", [IndexError, KeyError, AttributeError, RuntimeError])
    def test_every_exception_class_from_opening_is_mapped(self, exception, monkeypatch):
        def explode(*args, **kwargs):
            raise exception("boom")

        monkeypatch.setattr(Image, "open", explode)
        with pytest.raises(ImageRejected, match="could not be decoded"):
            load_oriented(_png_with_chunks([]))


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestOrientationThroughOurOwnReader:
    CONTAINERS = [
        "png_exif",
        "png_exif_after_idat",
        "png_raw_profile",
        "jpeg",
        "jpeg_density",
        "heif",
    ]

    @staticmethod
    def _build(container: str, tiff: bytes) -> bytes:
        return {
            "png_exif": lambda: _png_with_chunks([(b"eXIf", tiff)]),
            "png_exif_after_idat": lambda: _png_with_chunks([(b"eXIf", tiff)], after_idat=True),
            "png_raw_profile": lambda: _png_with_chunks([_raw_profile_text(tiff)]),
            "jpeg": lambda: _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff)),
            "jpeg_density": lambda: _jpeg_with(_jpeg_segments(b"Exif\0\0", tiff), density=True),
            "heif": lambda: _heif_with_exif(b"Exif\0\0" + tiff),
        }[container]()

    @pytest.mark.parametrize("container", CONTAINERS)
    @pytest.mark.parametrize("orientation", range(1, 9))
    def test_all_eight_orientations_are_applied(self, container, orientation):
        data = self._build(container, _padded_orientation_tiff(64, orientation))
        arr, _ = load_oriented(data)
        red_at, green_at, shape = ORIENTATION_RESULTS[orientation]
        assert arr.shape == shape
        _assert_corner_colors(arr, red_at=red_at, green_at=green_at)

    @pytest.mark.parametrize("container", CONTAINERS[:5])
    @pytest.mark.parametrize("orientation", [3, 6, 8])
    def test_a_bad_tag_before_orientation_still_recovers_the_rotation(self, container, orientation):
        bad_make = (0x010F, 2, 64, struct.pack("<I", 0xFFFFFF00))
        tiff = _raw_exif([bad_make, (0x0112, 3, 1, struct.pack("<H", orientation))])[6:]
        arr, _ = load_oriented(self._build(container, tiff))
        red_at, green_at, shape = ORIENTATION_RESULTS[orientation]
        assert arr.shape == shape
        _assert_corner_colors(arr, red_at=red_at, green_at=green_at)

    def test_an_xmp_orientation_in_a_jpeg_is_used_when_exif_has_none(self):
        data = _jpeg_with(_jpeg_segments(b"http://ns.adobe.com/xap/1.0/\0", XMP_ORIENTATION_6))
        assert load_oriented(data)[0].shape == (96, 48, 3)

    def test_exif_orientation_wins_over_xmp(self):
        tiff = _padded_orientation_tiff(40, orientation=3)
        data = _jpeg_with(
            _jpeg_segments(b"Exif\0\0", tiff)
            + _jpeg_segments(b"http://ns.adobe.com/xap/1.0/\0", XMP_ORIENTATION_6)
        )
        arr, _ = load_oriented(data)
        assert arr.shape == (48, 96, 3)
        _assert_corner_colors(arr, red_at="br", green_at="tl")

    def test_an_xmp_orientation_form_with_an_element_is_read(self):
        packet = b"<x><tiff:Orientation>8</tiff:Orientation></x>"
        arr, _ = load_oriented(
            _png_with_chunks([(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + packet)])
        )
        assert arr.shape == (96, 48, 3)
        _assert_corner_colors(arr, red_at="bl", green_at="tr")

    def test_the_orientation_flood_still_finds_orientation_6_first_in_ifd0(self):
        entries = [EXIF_ORIENTATION_6] + [(0x2000 + i, 1, 1, b"\0") for i in range(10_000)]
        tiff = _raw_exif(entries)[6:]
        arr, _ = load_oriented(_png_with_chunks([(b"eXIf", tiff)]))
        assert arr.shape == (96, 48, 3)

    def test_an_orientation_entry_beyond_the_ifd_entry_cap_is_ignored(self):
        junk = [(0x2000 + i, 1, 1, b"\0") for i in range(rga_io.MAX_IFD_ENTRIES)]
        beyond = _raw_exif([*junk, EXIF_ORIENTATION_6])[6:]
        within = _raw_exif([*junk[:-1], EXIF_ORIENTATION_6])[6:]
        assert rga_io._ifd0_orientation(beyond) is None
        assert rga_io._ifd0_orientation(within) == 6
        assert load_oriented(_png_with_chunks([(b"eXIf", beyond)]))[0].shape == (48, 96, 3)
        assert load_oriented(_png_with_chunks([(b"eXIf", within)]))[0].shape == (96, 48, 3)

    def test_the_ifd_entry_cap_is_a_few_hundred(self):
        assert rga_io.MAX_IFD_ENTRIES == 256

    def test_an_ifd_with_thousands_of_entries_is_read_in_bounded_time(self):
        tiff = _amplifying_tiff(UNDER_CAP_PAYLOAD, 10_000)
        started = time.perf_counter()
        for _ in range(50):
            assert rga_io._ifd0_orientation(tiff) is None
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("container", ["png_exif", "jpeg", "heif"])
    def test_a_non_tiff_header_is_still_rejected(self, container):
        with pytest.raises(ImageRejected, match="invalid EXIF metadata"):
            load_oriented(self._build(container, BAD_TIFF_HEADERS[0]))

    def test_a_raw_profile_that_is_not_hex_is_still_rejected_as_a_value_error(self):
        data = _png_with_chunks([_raw_profile_text(b"x" * 8, hex_text="ZZZZ")])
        with pytest.raises(ImageRejected, match="invalid EXIF metadata: ValueError"):
            load_oriented(data)


# ------------------------------------------------------------------ whitelist rebuilding (G3c2-C1, G3c2-W1)


def _segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def _mpf_tiff(payload_bytes: int, entries: int) -> bytes:
    return b"MPF\0" + _amplifying_tiff(payload_bytes, entries)


def _jpeg_boundaries(data: bytes) -> list[int]:
    """Offsets between the header segments of a JPEG (after SOI, after each segment, before SOS)."""
    points, pos = [2], 2
    while data[pos + 1] != 0xDA:
        pos += 2 + int.from_bytes(data[pos + 2 : pos + 4], "big")
        points.append(pos)
    return points


def _insert_at(data: bytes, offset: int, extra: bytes) -> bytes:
    return data[:offset] + extra + data[offset:]


def _plain_jpeg(**save_kwargs) -> bytes:
    buf = io.BytesIO()
    _marker_image().save(buf, "JPEG", **save_kwargs)
    return buf.getvalue()


PLAIN_JPEG = _plain_jpeg(dpi=(300, 300))
PLAIN_PNG = _png_with_chunks([])
IFD_LOAD_SPY_TARGETS = (
    (Image.Image, "getexif"),
    (Image.Exif, "load"),
    (Image.Exif, "load_from_fp"),
)


@pytest.fixture
def ifd_load_spy(monkeypatch):
    """Record any call into Pillow's EXIF, MPF or TIFF-IFD loaders, delegating to the real one."""
    from PIL import JpegImagePlugin, TiffImagePlugin

    calls: list[str] = []

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            calls.append(f"{owner.__name__}.{name}")
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    for owner, name in IFD_LOAD_SPY_TARGETS:
        spy(owner, name)
    spy(JpegImagePlugin.JpegImageFile, "_getexif")
    spy(TiffImagePlugin.ImageFileDirectory_v2, "load")
    return calls


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestJpegWhitelist:
    DROPPED_SEGMENTS = {
        "com": _segment(0xFE, b"a comment"),
        "app3": _segment(0xE3, b"Meta\0" + bytes(50)),
        "app5": _segment(0xE5, bytes(50)),
        "app13": _segment(0xED, b"Photoshop 3.0\0" + bytes(50)),
        "app15": _segment(0xEF, bytes(50)),
        "app1_other": _segment(0xE1, b"Other\0" + bytes(50)),
        "app0_jfxx": _segment(0xE0, b"JFXX\0" + bytes(50)),
        "app2_other": _segment(0xE2, b"FPXR\0" + bytes(50)),
        "app2_mpf": _segment(0xE2, _mpf_tiff(2000, 20)),
        "app14_other": _segment(0xEE, b"NotAdobe" + bytes(20)),
        "exif": _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(60)),
        "xmp": _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0" + XMP_ORIENTATION_6),
    }

    @pytest.mark.parametrize("name", list(DROPPED_SEGMENTS))
    def test_non_whitelisted_segments_are_dropped_and_the_pixels_are_unchanged(self, name):
        data = _insert_at(PLAIN_JPEG, 2 + 18, self.DROPPED_SEGMENTS[name])
        rebuilt = rga_io._rebuild_jpeg(data)
        assert rebuilt.data == PLAIN_JPEG
        original = np.asarray(Image.open(io.BytesIO(PLAIN_JPEG)).convert("RGB"))
        again = np.asarray(Image.open(io.BytesIO(rebuilt.data)).convert("RGB"))
        np.testing.assert_array_equal(original, again)

    def test_the_whitelisted_parts_survive_byte_for_byte(self):
        icc = _segment(0xE2, b"ICC_PROFILE\0\x01\x01" + bytes(100))
        adobe = _segment(0xEE, b"Adobe\0\x64\x00\x00\x00\x00\x01")
        data = _insert_at(PLAIN_JPEG, 2 + 18, icc + adobe)
        rebuilt = rga_io._rebuild_jpeg(data).data
        assert icc in rebuilt and adobe in rebuilt
        assert rebuilt == data
        assert PLAIN_JPEG[2:4] == b"\xff\xe0" and PLAIN_JPEG[6:10] == b"JFIF"

    @pytest.mark.parametrize(
        "save_kwargs", [{}, {"progressive": True}, {"quality": 40, "optimize": True}]
    )
    def test_baseline_progressive_and_optimized_files_rebuild_identically(self, save_kwargs):
        data = _plain_jpeg(**save_kwargs)
        assert rga_io._rebuild_jpeg(data).data == data
        arr, ext = load_oriented(data)
        assert (ext, arr.shape) == ("jpg", (48, 96, 3))

    def test_grayscale_and_cmyk_files_still_load(self):
        for mode, channels in (("L", 3), ("CMYK", 3)):
            buf = io.BytesIO()
            Image.new(mode, (40, 30)).save(buf, "JPEG")
            arr, _ = load_oriented(buf.getvalue())
            assert arr.shape == (30, 40, channels)

    def test_junk_bytes_and_fill_bytes_between_segments_are_dropped(self):
        for filler in (b"\x00\x07", b"\xff\xff\xff", b"junk"):
            data = _insert_at(PLAIN_JPEG, 2 + 18, filler)
            assert rga_io._rebuild_jpeg(data).data == PLAIN_JPEG
            assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_icc_segments_beyond_the_size_cap_are_dropped_not_rejected(self):
        piece = _segment(0xE2, b"ICC_PROFILE\0\x01\x01" + bytes(65000))
        many = piece * (rga_io.MAX_ICC_BYTES // 65000 + 2)
        rebuilt = rga_io._rebuild_jpeg(_insert_at(PLAIN_JPEG, 2 + 18, many)).data
        assert len(rebuilt) < rga_io.MAX_ICC_BYTES + len(PLAIN_JPEG)
        assert rebuilt.count(b"ICC_PROFILE") == rga_io.MAX_ICC_BYTES // 65000
        assert load_oriented(rebuilt)[0].shape == (48, 96, 3)

    @pytest.mark.parametrize(
        "marker",
        [
            0x00, 0x01, 0x02, 0x7F, 0xBF,
            0xC3, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF,
            *range(0xD0, 0xDA),
            0xDC, 0xDE, 0xDF,
            *range(0xF0, 0xFE),
        ],
    )  # fmt: skip
    def test_markers_with_ambiguous_framing_or_unsupported_frames_are_rejected(self, marker):
        for extra in (
            bytes([0xFF, marker]),
            _segment(marker, b"x" * 20),
            bytes([0xFF, marker]) + b"\x00\x04abcd",
        ):
            with pytest.raises(ImageRejected):
                load_oriented(_insert_at(PLAIN_JPEG, 2 + 18, extra))

    @pytest.mark.parametrize(
        "marker",
        [0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF, 0xDC, 0xDE, 0xDF],
    )
    def test_unsupported_frame_and_table_segments_are_rejected_by_the_rebuild_itself(self, marker):
        frame = b"\x08\x00\x30\x00\x60\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        with pytest.raises(ImageRejected, match="unsupported JPEG segment"):
            rga_io._rebuild_jpeg(_insert_at(PLAIN_JPEG, 2 + 18, _segment(marker, frame)))

    @pytest.mark.parametrize("tail", [b"", b"\xff", b"\xff\xe1", b"\xff\xe1\x00", b"\xff\xe1\xff"])
    def test_a_header_that_ends_inside_a_segment_marker_or_length_is_rejected(self, tail):
        with pytest.raises(ImageRejected):
            rga_io._rebuild_jpeg(PLAIN_JPEG[:20] + tail)

    def test_jpg_extension_marker_wrapping_an_exif_segment_cannot_hide_it(self):
        """review-24: FF F0 <len> over an APP1 Exif segment (and every other length-less marker)."""
        tiff = _amplifying_tiff(UNDER_CAP_PAYLOAD, 4000)
        exif = _segment(0xE1, b"Exif\0\0" + tiff[:60000])
        for marker in (0xC8, *range(0xF0, 0xFE)):
            wrapper = bytes([0xFF, marker]) + struct.pack(">H", len(exif) + 2)
            data = _insert_at(PLAIN_JPEG, 2 + 18, wrapper + exif)
            outcome, peak = _measure(data)
            assert isinstance(outcome, ImageRejected), hex(marker)
            assert peak < PEAK_LIMIT_BYTES

    def test_the_wrapped_exif_never_reaches_a_pillow_ifd_loader(self, ifd_load_spy):
        exif = _segment(0xE1, b"Exif\0\0" + _amplifying_tiff(60000, 4000))
        wrapper = b"\xff\xf0" + struct.pack(">H", len(exif) + 2)
        with pytest.raises(ImageRejected):
            load_oriented(_insert_at(PLAIN_JPEG, 2 + 18, wrapper + exif))
        assert ifd_load_spy == []

    def test_an_mpf_segment_is_dropped_and_never_reaches_the_ifd_loader(self, ifd_load_spy):
        """review-25: a 64 KB APP2 MPF segment with thousands of entries."""
        mpf = _segment(0xE2, _mpf_tiff(65000, 5000))
        data = _insert_at(PLAIN_JPEG, 2 + 18, mpf)
        ifd_load_spy.clear()
        outcome, peak = _measure(data)
        assert isinstance(outcome, np.ndarray)
        assert peak < PEAK_LIMIT_BYTES
        assert ifd_load_spy == []

    def test_several_mpf_segments_and_mpf_beside_exif_are_dropped(self, ifd_load_spy):
        mpf = _segment(0xE2, _mpf_tiff(65000, 5000))
        exif = _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(300, orientation=6))
        arr, _ = load_oriented(_insert_at(PLAIN_JPEG, 2 + 18, mpf * 5 + exif + mpf))
        assert arr.shape == (96, 48, 3)
        assert ifd_load_spy == []

    def test_a_file_with_no_scan_or_only_a_soi_is_rejected(self):
        with pytest.raises(ImageRejected, match="no scan"):
            load_oriented(PLAIN_JPEG[: PLAIN_JPEG.index(b"\xff\xda")])
        with pytest.raises(ImageRejected):
            load_oriented(b"\xff\xd8\xff\xd9")

    def test_data_after_the_scan_starts_is_passed_through_unparsed(self):
        sos = PLAIN_JPEG.index(b"\xff\xda")
        tail = _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(30)) + b"\xff\xf0\xff\xc8"
        rebuilt = rga_io._rebuild_jpeg(PLAIN_JPEG[:sos] + PLAIN_JPEG[sos:] + tail)
        assert rebuilt.exif is None
        assert rebuilt.data.endswith(tail)

    def test_rebuilding_does_not_change_an_ordinary_jpegs_decoded_pixels(self):
        arr, _ = load_oriented(PLAIN_JPEG)
        plain = np.asarray(Image.open(io.BytesIO(PLAIN_JPEG)).convert("RGB"))[:, :, ::-1]
        np.testing.assert_array_equal(arr, plain)


def _random_jpeg_extras(rng) -> bytes:
    """A random sequence of segments, markers, padding and junk to place in a JPEG header."""
    pieces = []
    for _ in range(rng.randint(1, 6)):
        kind = rng.choice(
            [
                "exif", "exif_flood", "xmp", "mpf", "mpf_flood", "icc", "app", "com", "jpgn",
                "jpgn_wrapper", "rst", "padding", "junk", "short_length", "dnl", "adobe",
            ]
        )  # fmt: skip
        if kind == "exif":
            pieces.append(
                _segment(
                    0xE1,
                    b"Exif\0\0" + _padded_orientation_tiff(rng.randint(30, 400), rng.randint(1, 8)),
                )
            )
        elif kind == "exif_flood":
            tiff = _flood_tiff(rng.choice([5000, 20000, 60000]), rng.choice([100, 1000, 4000]))
            pieces.append(_segment(0xE1, b"Exif\0\0" + tiff[:65000]))
        elif kind == "xmp":
            pieces.append(_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0" + XMP_ORIENTATION_6))
        elif kind == "mpf":
            pieces.append(
                _segment(0xE2, b"MPF\0" + _flood_tiff(rng.choice([100, 2000]), rng.choice([0, 40])))
            )
        elif kind == "mpf_flood":
            pieces.append(_segment(0xE2, b"MPF\0" + _flood_tiff(65000, rng.choice([1000, 5000]))))
        elif kind == "icc":
            pieces.append(_segment(0xE2, b"ICC_PROFILE\0\x01\x01" + bytes(rng.randint(0, 3000))))
        elif kind == "app":
            pieces.append(
                _segment(
                    0xE0 + rng.randint(0, 15),
                    bytes(rng.randrange(256) for _ in range(rng.randint(0, 60))),
                )
            )
        elif kind == "com":
            pieces.append(_segment(0xFE, b"hello"))
        elif kind == "jpgn":
            pieces.append(
                bytes([0xFF, rng.choice([0xC8, *range(0xF0, 0xFE)])])
                + bytes(rng.randrange(256) for _ in range(rng.randint(0, 4)))
            )
        elif kind == "jpgn_wrapper":
            inner = _segment(
                0xE1,
                b"Exif\0\0" + _amplifying_tiff(rng.randint(1000, 30000), rng.randint(10, 3000)),
            )
            marker = rng.choice([0xC8, *range(0xF0, 0xFE)])
            pieces.append(
                bytes([0xFF, marker])
                + struct.pack(">H", rng.choice([len(inner) + 2, len(inner), 2, 0, 1]))
                + inner
            )
        elif kind == "rst":
            pieces.append(bytes([0xFF, rng.randint(0xD0, 0xD9)]))
        elif kind == "padding":
            pieces.append(b"\xff" * rng.randint(1, 5))
        elif kind == "junk":
            pieces.append(bytes(rng.randrange(256) for _ in range(rng.randint(1, 8))))
        elif kind == "short_length":
            pieces.append(
                bytes([0xFF, rng.choice([0xE1, 0xE2, 0xFE, 0xE0])])
                + struct.pack(">H", rng.randint(0, 1))
            )
        elif kind == "dnl":
            pieces.append(_segment(rng.choice([0xDC, 0xDE, 0xDF, 0xCC]), b"\0\0"))
        else:
            pieces.append(_segment(0xEE, b"Adobe\0\x64\x00\x00\x00\x00\x01"))
    return b"".join(pieces)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_jpeg_marker_sequence_fuzz_never_reaches_a_pillow_ifd_loader_or_a_big_allocation(
    ifd_load_spy,
):
    import random

    rng = random.Random(50)
    boundaries = _jpeg_boundaries(PLAIN_JPEG)
    cases = [
        _insert_at(PLAIN_JPEG, rng.choice(boundaries), _random_jpeg_extras(rng))
        for _ in range(3000)
    ]
    loaded = rejected = 0
    worst_peak = 0
    tracemalloc.start()
    try:
        for data in cases:
            ifd_load_spy.clear()
            tracemalloc.reset_peak()
            try:
                arr, _ = load_oriented(data)
            except ImageRejected:
                rejected += 1
            else:
                loaded += 1
                assert arr.dtype == np.uint8
            worst_peak = max(worst_peak, tracemalloc.get_traced_memory()[1])
            assert ifd_load_spy == []
    finally:
        tracemalloc.stop()
    assert worst_peak < PEAK_LIMIT_BYTES, f"{worst_peak / 2**20:.0f} MB"
    assert loaded >= 300 and rejected >= 300, (loaded, rejected)
    control_hits = 0
    for data in [case for case in cases if b"Exif\0\0" in case][:60]:
        ifd_load_spy.clear()
        with contextlib.suppress(Exception), Image.open(io.BytesIO(data)) as img:
            img.getexif()
        control_hits += bool(ifd_load_spy)
    assert control_hits >= 20, (
        "the spy never fired on unrebuilt Exif inputs; the guard proves nothing"
    )


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestPngWhitelist:
    DROPPED = {
        "text": (b"tEXt", b"Comment\0hello"),
        "ztxt": (b"zTXt", b"Title\0\0" + zlib.compress(b"hello")),
        "itxt": (b"iTXt", b"Title\0\0\0\0\0hello"),
        "time": (b"tIME", bytes(7)),
        "bkgd": (b"bKGD", bytes(6)),
        "splt": (b"sPLT", b"pal\0\x08" + bytes(10)),
        "private": (b"abCd", bytes(40)),
        "exif": (b"eXIf", _padded_orientation_tiff(60)),
        "animation": (b"acTL", struct.pack(">II", 1, 0)),
        "raw_profile": _raw_profile_text(_padded_orientation_tiff(40)),
    }
    KEPT = {
        "gamma": (b"gAMA", struct.pack(">I", 45455)),
        "chroma": (b"cHRM", bytes(32)),
        "srgb": (b"sRGB", b"\0"),
        "phys": (b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
        "sbit": (b"sBIT", bytes([8, 8, 8])),
    }

    @pytest.mark.parametrize("name", list(DROPPED))
    @pytest.mark.parametrize("after_idat", [False, True])
    def test_non_whitelisted_chunks_are_dropped(self, name, after_idat):
        data = _png_with_chunks([self.DROPPED[name]], after_idat=after_idat)
        assert rga_io._rebuild_png(data).data == PLAIN_PNG

    @pytest.mark.parametrize("name", list(KEPT))
    def test_whitelisted_chunks_are_kept_byte_for_byte(self, name):
        data = _png_with_chunks([self.KEPT[name]])
        assert rga_io._rebuild_png(data).data == data
        assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_a_palette_png_with_transparency_keeps_its_pixels(self):
        img = Image.new("P", (20, 10))
        img.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
        img.paste(1, (0, 0, 10, 10))
        buf = io.BytesIO()
        img.save(buf, "PNG", transparency=0)
        data = buf.getvalue()
        assert rga_io._rebuild_png(data).data == data
        np.testing.assert_array_equal(load_oriented(data)[0], load_oriented(buf.getvalue())[0])

    def test_an_icc_profile_chunk_is_kept_up_to_the_size_cap_and_dropped_beyond_it(self):
        small = (b"iCCP", b"sRGB\0\0" + zlib.compress(bytes(300)))
        big = (b"iCCP", b"big\0\0" + bytes(rga_io.MAX_ICC_BYTES + 1))
        assert rga_io._rebuild_png(_png_with_chunks([small])).data == _png_with_chunks([small])
        assert rga_io._rebuild_png(_png_with_chunks([big])).data == PLAIN_PNG

    def test_data_after_iend_is_dropped(self):
        assert rga_io._rebuild_png(PLAIN_PNG + b"trailing garbage" * 100).data == PLAIN_PNG

    def test_a_png_without_iend_or_with_a_truncated_chunk_is_rejected(self):
        with pytest.raises(ImageRejected, match="no IEND"):
            rga_io._rebuild_png(PLAIN_PNG[:-12])
        with pytest.raises(ImageRejected, match="truncated PNG chunk"):
            rga_io._rebuild_png(PLAIN_PNG[:-20])

    def test_a_dropped_chunk_with_a_bad_crc_does_not_reach_pillow_at_all(self):
        bad = bytearray(_png_chunk(b"tEXt", b"Comment\0x"))
        bad[-1] ^= 0xFF
        data = PLAIN_PNG[:33] + bytes(bad) + PLAIN_PNG[33:]
        assert rga_io._rebuild_png(data).data == PLAIN_PNG


def _splice_png(chunks: list[tuple[bytes, bytes]], *, after_idat: bool) -> bytes:
    at = PLAIN_PNG.rfind(b"IEND") - 4 if after_idat else 33
    return PLAIN_PNG[:at] + b"".join(_png_chunk(k, p) for k, p in chunks) + PLAIN_PNG[at:]


@functools.cache
def _flood_tiff(payload_bytes: int, entries: int) -> bytes:
    return _amplifying_tiff(payload_bytes, entries)


def _random_png_chunks(rng) -> list[tuple[bytes, bytes]]:
    tiff = _flood_tiff(rng.choice([500, 5000, 40000]), rng.choice([10, 300, 4000]))
    pool = [
        (b"eXIf", tiff),
        _raw_profile_text(_flood_tiff(rng.choice([500, 5000, 20000]), rng.choice([10, 300, 3000]))),
        (b"tEXt", b"xmp\0" + XMP_ORIENTATION_6),
        (b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + XMP_ORIENTATION_6),
        (b"zTXt", b"Raw profile type exif\0\0" + zlib.compress(b"\nexif\n8\n" + b"00" * 8 + b"\n")),
        (b"tEXt", b"Comment\0hello"),
        (b"tIME", bytes(rng.randrange(256) for _ in range(rng.randint(0, 9)))),
        (b"abCd", bytes(rng.randrange(256) for _ in range(rng.randint(0, 50)))),
        (b"iCCP", bytes(rng.randrange(256) for _ in range(rng.randint(0, 50)))),
        (b"gAMA", bytes(rng.randrange(256) for _ in range(rng.choice([0, 2, 4])))),
        (b"pHYs", struct.pack(">IIB", 2835, 2835, 1)),
    ]
    return [rng.choice(pool) for _ in range(rng.randint(1, 6))]


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_png_chunk_sequence_fuzz_never_reaches_a_pillow_ifd_loader_or_a_big_allocation(
    ifd_load_spy,
):
    import random

    rng = random.Random(51)
    cases = [
        _png_with_chunks(_random_png_chunks(rng), after_idat=rng.random() < 0.5)
        for _ in range(1500)
    ]
    loaded = rejected = worst_peak = 0
    tracemalloc.start()
    try:
        for data in cases:
            ifd_load_spy.clear()
            tracemalloc.reset_peak()
            try:
                load_oriented(data)
            except ImageRejected:
                rejected += 1
            else:
                loaded += 1
            worst_peak = max(worst_peak, tracemalloc.get_traced_memory()[1])
            assert ifd_load_spy == []
    finally:
        tracemalloc.stop()
    assert worst_peak < PEAK_LIMIT_BYTES, f"{worst_peak / 2**20:.0f} MB"
    assert loaded >= 150 and rejected >= 150, (loaded, rejected)
    control_hits = 0
    for data in [case for case in cases if b"eXIf" in case][:60]:
        ifd_load_spy.clear()
        with contextlib.suppress(Exception), Image.open(io.BytesIO(data)) as img:
            img.getexif()
        control_hits += bool(ifd_load_spy)
    assert control_hits >= 20, (
        "the spy never fired on unrebuilt eXIf inputs; the guard proves nothing"
    )


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_the_ifd_spy_fires_when_pillow_is_allowed_to_parse_the_carrier(ifd_load_spy):
    """The guard is only meaningful if the spy can see Pillow's loaders being used."""
    mpf = _segment(0xE2, _mpf_tiff(2000, 20))
    Image.open(io.BytesIO(_insert_at(PLAIN_JPEG, 2 + 18, mpf)))
    assert "ImageFileDirectory_v2.load" in ifd_load_spy
    ifd_load_spy.clear()
    exif = _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(300))
    Image.open(io.BytesIO(_insert_at(_plain_jpeg(), 2, exif)))
    assert ifd_load_spy != []


# ------------------------------------------------------------------ bounded rebuild (G3c3-C1) and load-path proof (G3c3-W5)

UPLOAD_CAP = rga_io.MAX_UPLOAD_BYTES
TOO_MANY = "too many image segments"
JPEG_HEADER_SEGMENTS = len(_jpeg_boundaries(PLAIN_JPEG)) - 1
REAL_DHT_PAYLOADS = []
_pos = 2
while PLAIN_JPEG[_pos + 1] != 0xDA:
    _length = int.from_bytes(PLAIN_JPEG[_pos + 2 : _pos + 4], "big")
    if PLAIN_JPEG[_pos + 1] == 0xC4:
        REAL_DHT_PAYLOADS.append(PLAIN_JPEG[_pos + 4 : _pos + 2 + _length])
    _pos += 2 + _length
VALID_DHT = _segment(0xC4, REAL_DHT_PAYLOADS[0])


def _jpeg_flooded(unit: bytes, total: int = UPLOAD_CAP) -> bytes:
    """A decodable JPEG whose header is padded with `unit` up to `total` bytes."""
    sos = PLAIN_JPEG.index(b"\xff\xda")
    count = (total - len(PLAIN_JPEG)) // len(unit)
    return PLAIN_JPEG[:sos] + unit * count + PLAIN_JPEG[sos:]


def _png_flooded(unit: bytes, total: int = UPLOAD_CAP) -> bytes:
    count = (total - len(PLAIN_PNG)) // len(unit)
    return PLAIN_PNG[:33] + unit * count + PLAIN_PNG[33:]


def _timed_peak(fn):
    """Run `fn()` under tracemalloc; returns (result or exception, seconds, peak bytes)."""
    tracemalloc.start()
    started = time.perf_counter()
    try:
        try:
            outcome = fn()
        except ImageRejected as exc:
            outcome = exc
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return outcome, elapsed, peak


JPEG_FLOODS = {
    "empty_dht": _segment(0xC4, b""),
    "empty_dqt": _segment(0xDB, b""),
    "dri": _segment(0xDD, b"\x00\x00"),
    "jfif": _segment(0xE0, b"JFIF"),
    "adobe": _segment(0xEE, b"Adobe"),
    "tiny_icc": _segment(0xE2, b"ICC_PROFILE\0\x01\x01"),
    "empty_com": _segment(0xFE, b""),
    "empty_exif": _segment(0xE1, b"Exif\0\0"),
    "empty_xmp": _segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0"),
    "other_app1": _segment(0xE1, b"x"),
    "empty_app15": _segment(0xEF, b""),
    "junk_bytes": b"\x00",
    "fill_bytes": b"\xff",
}
PNG_FLOODS = {
    "sbit": _png_chunk(b"sBIT", b"\x08"),
    "phys": _png_chunk(b"pHYs", struct.pack(">IIB", 1, 1, 1)),
    "srgb": _png_chunk(b"sRGB", b"\0"),
    "time": _png_chunk(b"tIME", bytes(7)),
    "text": _png_chunk(b"tEXt", b"a\0"),
    "xmp_text": _png_chunk(b"tEXt", b"xmp\0"),
    "private": _png_chunk(b"abCd", b""),
    "empty_idat": _png_chunk(b"IDAT", b""),
    "tiny_iccp": _png_chunk(b"iCCP", b"a\0\0"),
}


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestHeaderFloodsAreBounded:
    @pytest.mark.parametrize("name", list(JPEG_FLOODS))
    def test_a_jpeg_filled_to_the_upload_cap_with_one_segment_kind_is_rejected_cheaply(self, name):
        data = _jpeg_flooded(JPEG_FLOODS[name])
        assert len(data) > UPLOAD_CAP - 64
        outcome, seconds, peak = _timed_peak(lambda: load_oriented(data))
        assert isinstance(outcome, ImageRejected), name
        assert str(outcome) == TOO_MANY
        assert seconds < 2.0
        assert peak < PEAK_LIMIT_BYTES, f"{peak / 2**20:.0f} MB"

    @pytest.mark.parametrize("name", list(PNG_FLOODS))
    def test_a_png_filled_to_the_upload_cap_with_one_chunk_kind_is_rejected_cheaply(self, name):
        data = _png_flooded(PNG_FLOODS[name])
        assert len(data) > UPLOAD_CAP - 64
        outcome, seconds, peak = _timed_peak(lambda: load_oriented(data))
        assert isinstance(outcome, ImageRejected), name
        assert str(outcome) == TOO_MANY
        assert seconds < 2.0
        assert peak < PEAK_LIMIT_BYTES, f"{peak / 2**20:.0f} MB"

    @pytest.mark.parametrize("flood", ["jpeg_empty_dht", "png_tiny_chunks"])
    def test_the_resident_set_stays_within_the_input_plus_a_small_margin(self, flood):
        """The review measured +1.5 GB RSS and 20 s for a 40 MB empty-DHT JPEG; run it in a child."""
        import os
        import subprocess
        import sys

        code = f"""
import io, resource, struct, sys, time, zlib
from PIL import Image
from rga.vision import io as vio
cap = vio.MAX_UPLOAD_BYTES
def rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2**20 if sys.platform == "darwin" else peak / 1024
buf = io.BytesIO()
if {flood!r} == "jpeg_empty_dht":
    Image.new("RGB", (96, 48)).save(buf, "JPEG")
    base = buf.getvalue(); sos = base.index(b"\\xff\\xda"); unit = b"\\xff\\xc4\\x00\\x02"
    data = base[:sos] + unit * ((cap - len(base)) // 4) + base[sos:]
else:
    Image.new("RGB", (96, 48)).save(buf, "PNG")
    base = buf.getvalue()
    unit = struct.pack(">I", 1) + b"sBIT" + b"\\x08" + struct.pack(">I", zlib.crc32(b"sBIT\\x08") & 0xFFFFFFFF)
    data = base[:33] + unit * ((cap - len(base)) // len(unit)) + base[33:]
before = rss_mb(); started = time.perf_counter()
try:
    vio.load_oriented(data); outcome = "loaded"
except vio.ImageRejected as exc:
    outcome = str(exc)
print(outcome, round(rss_mb() - before, 1), round(time.perf_counter() - started, 2))
"""
        src = str(Path(rga_io.__file__).resolve().parents[2])
        env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, timeout=120
        )
        assert result.returncode == 0, result.stderr
        outcome, grown_mb, seconds = result.stdout.rsplit(" ", 2)
        assert outcome == TOO_MANY
        assert float(grown_mb) < 64
        assert float(seconds) < 2.0

    def test_a_many_idat_png_within_the_chunk_cap_loads_without_a_per_chunk_cost(self):
        noisy = Image.fromarray(
            np.random.default_rng(3).integers(0, 256, (60, 80, 3), dtype=np.uint8)
        )
        buf = io.BytesIO()
        noisy.save(buf, "PNG")
        png = buf.getvalue()
        idat_at = png.index(b"IDAT") - 4
        idat_len = int.from_bytes(png[idat_at : idat_at + 4], "big")
        stream = png[idat_at + 8 : idat_at + 8 + idat_len]
        end = idat_at + 12 + idat_len
        pieces = [stream[i : i + 3] for i in range(0, len(stream), 3)]
        assert 3000 < len(pieces) < rga_io.MAX_PNG_CHUNKS
        many = png[:idat_at] + b"".join(_png_chunk(b"IDAT", p) for p in pieces) + png[end:]
        outcome, seconds, peak = _timed_peak(lambda: load_oriented(many))
        assert not isinstance(outcome, ImageRejected)
        assert seconds < 2.0 and peak < PEAK_LIMIT_BYTES
        np.testing.assert_array_equal(outcome[0], load_oriented(png)[0])

    def test_an_idat_chunk_count_over_the_cap_is_rejected(self):
        chunks = _png_chunk(b"IDAT", b"") * (rga_io.MAX_PNG_CHUNKS + 1)
        with pytest.raises(ImageRejected, match=TOO_MANY):
            rga_io._rebuild_png(PLAIN_PNG[:33] + chunks + PLAIN_PNG[33:])


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestRebuildCaps:
    def test_exactly_the_kept_segment_cap_loads_and_one_more_is_rejected(self):
        room = rga_io.MAX_KEPT_SEGMENTS - JPEG_HEADER_SEGMENTS
        sos = PLAIN_JPEG.index(b"\xff\xda")
        at_cap = PLAIN_JPEG[:sos] + VALID_DHT * room + PLAIN_JPEG[sos:]
        assert load_oriented(at_cap)[0].shape == (48, 96, 3)
        with pytest.raises(ImageRejected, match=TOO_MANY):
            load_oriented(PLAIN_JPEG[:sos] + VALID_DHT * (room + 1) + PLAIN_JPEG[sos:])

    def test_the_kept_segment_cap_is_64(self):
        assert rga_io.MAX_KEPT_SEGMENTS == 64

    def test_exactly_the_walked_segment_cap_loads_and_one_more_is_rejected(self):
        room = rga_io.MAX_JPEG_SEGMENTS - JPEG_HEADER_SEGMENTS
        com = _segment(0xFE, b"x")
        sos = PLAIN_JPEG.index(b"\xff\xda")
        assert load_oriented(PLAIN_JPEG[:sos] + com * room + PLAIN_JPEG[sos:])[0].shape == (
            48,
            96,
            3,
        )
        with pytest.raises(ImageRejected, match=TOO_MANY):
            load_oriented(PLAIN_JPEG[:sos] + com * (room + 1) + PLAIN_JPEG[sos:])

    def test_kept_header_bytes_beyond_one_megabyte_are_rejected_even_with_few_segments(self):
        big = _segment(0xC4, bytes(60000))
        sos = PLAIN_JPEG.index(b"\xff\xda")
        under = PLAIN_JPEG[:sos] + big * 15 + PLAIN_JPEG[sos:]
        assert len(rga_io._rebuild_jpeg(under).data) == len(under)
        with pytest.raises(ImageRejected, match=TOO_MANY):
            rga_io._rebuild_jpeg(PLAIN_JPEG[:sos] + big * 18 + PLAIN_JPEG[sos:])
        assert rga_io.MAX_KEPT_HEADER_BYTES == 1024 * 1024

    def test_icc_bytes_are_not_part_of_the_kept_header_byte_budget(self):
        icc = _segment(0xE2, b"ICC_PROFILE\0\x01\x01" + bytes(61_600 - 14))
        assert 17 * (len(icc) - 4) <= rga_io.MAX_ICC_BYTES
        extra = _segment(0xC4, bytes(4000))
        sos = PLAIN_JPEG.index(b"\xff\xda")
        data = PLAIN_JPEG[:sos] + icc * 17 + extra + PLAIN_JPEG[sos:]
        assert sum(len(icc) for _ in range(17)) + len(extra) > rga_io.MAX_KEPT_HEADER_BYTES
        assert len(rga_io._rebuild_jpeg(data).data) == len(data)

    @pytest.mark.parametrize("junk", [rga_io.MAX_JUNK_BYTES, rga_io.MAX_JUNK_BYTES + 1])
    def test_the_junk_and_fill_budget_between_segments(self, junk):
        sos = PLAIN_JPEG.index(b"\xff\xda")
        for filler in (b"\x00" * junk, b"\xff" * junk):
            data = PLAIN_JPEG[:sos] + filler + PLAIN_JPEG[sos:]
            if junk == rga_io.MAX_JUNK_BYTES:
                assert load_oriented(data)[0].shape == (48, 96, 3)
            else:
                with pytest.raises(ImageRejected, match=TOO_MANY):
                    load_oriented(data)

    def test_a_second_frame_header_or_an_oversize_one_is_rejected(self):
        sos = PLAIN_JPEG.index(b"\xff\xda")
        sof = PLAIN_JPEG.index(b"\xff\xc0")
        sof_len = int.from_bytes(PLAIN_JPEG[sof + 2 : sof + 4], "big")
        frame = PLAIN_JPEG[sof : sof + 2 + sof_len]
        with pytest.raises(ImageRejected, match="malformed JPEG frame header"):
            rga_io._rebuild_jpeg(PLAIN_JPEG[:sos] + frame + PLAIN_JPEG[sos:])
        big = _segment(0xC0, PLAIN_JPEG[sof + 4 : sof + 2 + sof_len] + bytes(3000))
        with pytest.raises(ImageRejected, match="malformed JPEG frame header"):
            rga_io._rebuild_jpeg(PLAIN_JPEG[:sof] + big + PLAIN_JPEG[sof + 2 + sof_len :])

    @pytest.mark.parametrize("payload", [b"", b"\x00", b"\x00\x00\x00", b"\x00" * 8])
    def test_a_restart_interval_must_be_exactly_two_bytes(self, payload):
        sos = PLAIN_JPEG.index(b"\xff\xda")
        with pytest.raises(ImageRejected, match="restart interval"):
            rga_io._rebuild_jpeg(PLAIN_JPEG[:sos] + _segment(0xDD, payload) + PLAIN_JPEG[sos:])
        ok = PLAIN_JPEG[:sos] + _segment(0xDD, b"\x00\x08") + PLAIN_JPEG[sos:]
        assert rga_io._rebuild_jpeg(ok).data == ok

    def test_the_png_walked_chunk_cap_is_exact(self):
        private = _png_chunk(b"abCd", b"")
        room = rga_io.MAX_PNG_CHUNKS - 3
        at_cap = PLAIN_PNG[:33] + private * room + PLAIN_PNG[33:]
        assert load_oriented(at_cap)[0].shape == (48, 96, 3)
        with pytest.raises(ImageRejected, match=TOO_MANY):
            load_oriented(PLAIN_PNG[:33] + private * (room + 1) + PLAIN_PNG[33:])
        assert rga_io.MAX_PNG_CHUNKS == 20_000

    def test_a_run_of_dropped_png_chunks_costs_one_slice_not_one_per_chunk(self):
        private = _png_chunk(b"abCd", b"")
        data = PLAIN_PNG[:33] + private * (rga_io.MAX_PNG_CHUNKS - 5) + PLAIN_PNG[33:]
        rebuilt, _, peak = _timed_peak(lambda: rga_io._rebuild_png(data))
        assert rebuilt.data == PLAIN_PNG
        assert peak < 256 * 1024, f"{peak / 1024:.0f} KB"

    def test_the_png_kept_non_idat_chunk_cap_is_exact(self):
        phys = _png_chunk(b"pHYs", struct.pack(">IIB", 1, 1, 1))
        room = rga_io.MAX_KEPT_SEGMENTS - 2
        assert len(rga_io._rebuild_png(PLAIN_PNG[:33] + phys * room + PLAIN_PNG[33:]).data) > 0
        with pytest.raises(ImageRejected, match=TOO_MANY):
            rga_io._rebuild_png(PLAIN_PNG[:33] + phys * (room + 1) + PLAIN_PNG[33:])

    @pytest.mark.parametrize(
        ("kind", "ok_sizes", "bad_sizes"),
        [
            (b"IHDR", [13], [12, 14]),
            (b"gAMA", [4], [3, 5, 0]),
            (b"cHRM", [32], [31, 33, 1_000_000]),
            (b"sRGB", [1], [0, 2]),
            (b"pHYs", [9], [8, 10]),
            (b"sBIT", [1, 4], [0, 5]),
            (b"PLTE", [3, 768], [0, 2, 771]),
            (b"tRNS", [0, 256], [257, 10_000]),
        ],
    )
    def test_kept_png_chunks_must_have_a_possible_size(self, kind, ok_sizes, bad_sizes):
        def rebuilt(size):
            return rga_io._rebuild_png(_png_with_chunks([(kind, bytes(size))]))

        for size in ok_sizes:
            assert rebuilt(size).data  # a kept chunk of a possible size is accepted
        for size in bad_sizes:
            with pytest.raises(ImageRejected, match="malformed PNG chunk size"):
                rebuilt(size)

    def test_iccp_chunks_share_one_size_budget(self):
        first = (b"iCCP", b"a\0\0" + bytes(600_000))
        second = (b"iCCP", b"b\0\0" + bytes(600_000))
        rebuilt = rga_io._rebuild_png(_png_with_chunks([first, second])).data
        assert rebuilt.count(b"iCCP") == 1

    def test_rebuilding_a_forty_megabyte_jpeg_with_a_dropped_segment_costs_one_copy(self):
        sos = PLAIN_JPEG.index(b"\xff\xda")
        exif = _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(300))
        big = (
            PLAIN_JPEG[:sos]
            + exif
            + PLAIN_JPEG[sos:]
            + b"\x00" * (UPLOAD_CAP - len(PLAIN_JPEG) - len(exif))
        )
        rebuilt, seconds, peak = _timed_peak(lambda: rga_io._rebuild_jpeg(big))
        assert rebuilt.exif is not None
        assert len(rebuilt.data) == len(big) - len(exif)
        assert seconds < 2.0 and peak < len(big) + PEAK_LIMIT_BYTES

    def test_rebuilding_a_forty_megabyte_png_with_a_dropped_chunk_costs_one_copy(self):
        idat = _png_chunk(b"IDAT", bytes(UPLOAD_CAP - 4096))
        dropped = _png_chunk(b"tEXt", b"Comment\0x")
        big = PLAIN_PNG[:33] + dropped + idat + PLAIN_PNG[-12:]
        rebuilt, seconds, peak = _timed_peak(lambda: rga_io._rebuild_png(big))
        assert len(rebuilt.data) == len(big) - len(dropped)
        assert seconds < 2.0 and peak < len(big) + PEAK_LIMIT_BYTES

    def test_rebuilding_input_with_nothing_to_drop_copies_nothing(self):
        padded_jpeg = PLAIN_JPEG + b"\x00" * (UPLOAD_CAP - len(PLAIN_JPEG))
        padded_png = PLAIN_PNG[:-12] + _png_chunk(b"IDAT", b"") + PLAIN_PNG[-12:]
        for rebuild, data in (
            (rga_io._rebuild_jpeg, padded_jpeg),
            (rga_io._rebuild_png, padded_png),
        ):
            rebuilt, _, peak = _timed_peak(lambda rebuild=rebuild, data=data: rebuild(data))
            assert rebuilt.data is data
            assert peak < 1024 * 1024


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestTheLoadPathUsesTheWhitelist:
    """G3c3-W5: what Pillow is handed is the rebuilt file, for JPEG and for PNG."""

    @pytest.fixture
    def opened(self, monkeypatch):
        seen: list[bytes] = []
        real = Image.open

        def spy(fp, *args, **kwargs):
            seen.append(fp.getvalue())
            return real(fp, *args, **kwargs)

        monkeypatch.setattr(Image, "open", spy)
        return seen

    def test_png_pillow_is_handed_the_rebuilt_bytes(self, opened):
        extras = [
            (b"tEXt", b"Comment\0dropped text"),
            (b"eXIf", _padded_orientation_tiff(80)),
            (b"tIME", bytes(7)),
            (b"abCd", b"private"),
            (b"zTXt", b"Title\0\0" + zlib.compress(b"x")),
        ]
        data = _png_with_chunks(extras) + b"trailing garbage"
        load_oriented(data)
        (handed,) = opened
        assert handed == rga_io._rebuild_png(data).data
        assert handed != data
        for kind in (b"tEXt", b"eXIf", b"tIME", b"abCd", b"zTXt"):
            assert kind not in handed

    def test_jpeg_pillow_is_handed_the_rebuilt_bytes(self, opened):
        extras = (
            _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(80))
            + _segment(0xE2, _mpf_tiff(200, 5))
            + _segment(0xFE, b"comment")
        )
        data = _insert_at(PLAIN_JPEG, 2 + 18, extras)
        load_oriented(data)
        (handed,) = opened
        assert handed == PLAIN_JPEG
        assert b"Exif" not in handed and b"MPF" not in handed and b"comment" not in handed

    def test_a_dropped_png_chunk_with_a_bad_crc_never_reaches_pillow_through_load_oriented(self):
        bad = bytearray(_png_chunk(b"tEXt", b"Comment\0x"))
        bad[-1] ^= 0xFF
        data = PLAIN_PNG[:33] + bytes(bad) + PLAIN_PNG[33:]
        with pytest.raises(OSError):
            Image.open(io.BytesIO(data)).load()
        assert load_oriented(data)[0].shape == (48, 96, 3)

    @pytest.mark.parametrize("kind", [b"acTL", b"fcTL", b"fdAT"])
    def test_a_png_that_pillow_alone_rejects_loads_once_its_dropped_chunk_is_gone(self, kind):
        data = _png_with_chunks([(kind, b"\0")])
        with pytest.raises(ValueError):
            Image.open(io.BytesIO(data)).load()
        assert load_oriented(data)[0].shape == (48, 96, 3)

    def test_heif_is_opened_unmodified(self, opened):
        data = _encode(_marker_image(), "HEIF")
        load_oriented(data)
        assert opened == [data]


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestTheFuzzSpyCanFire:
    """G3c3-W5: the spy used by the fuzz tests must be able to see an unrebuilt control."""

    def test_pillow_parsing_an_unrebuilt_png_exif_chunk_fires_the_spy(self, ifd_load_spy):
        import random

        rng = random.Random(51)
        fired = 0
        for _ in range(60):
            chunks = [(b"eXIf", _padded_orientation_tiff(rng.randint(40, 400)))]
            raw = _splice_png(chunks, after_idat=rng.random() < 0.5)
            ifd_load_spy.clear()
            with Image.open(io.BytesIO(raw)) as img:
                img.load()
                img.getexif()
            fired += bool(ifd_load_spy)
        assert fired == 60

    def test_pillow_parsing_an_unrebuilt_jpeg_exif_segment_fires_the_spy(self, ifd_load_spy):
        import random

        rng = random.Random(50)
        fired = 0
        for _ in range(60):
            exif = _segment(0xE1, b"Exif\0\0" + _padded_orientation_tiff(rng.randint(40, 400)))
            raw = _insert_at(PLAIN_JPEG, rng.choice(_jpeg_boundaries(PLAIN_JPEG)), exif)
            ifd_load_spy.clear()
            with Image.open(io.BytesIO(raw)) as img:
                img.getexif()
            fired += bool(ifd_load_spy)
        assert fired == 60

    def test_the_same_inputs_through_load_oriented_never_fire_it(self, ifd_load_spy):
        import random

        rng = random.Random(52)
        for _ in range(40):
            tiff = _padded_orientation_tiff(rng.randint(40, 400), rng.randint(1, 8))
            png = _splice_png([(b"eXIf", tiff)], after_idat=rng.random() < 0.5)
            jpeg = _insert_at(
                PLAIN_JPEG,
                rng.choice(_jpeg_boundaries(PLAIN_JPEG)),
                _segment(0xE1, b"Exif\0\0" + tiff),
            )
            for data in (png, jpeg):
                ifd_load_spy.clear()
                load_oriented(data)
                assert ifd_load_spy == []


# ------------------------------------------------------------------ progressive scan cap (SE-3, T4.0b)

EXTRA_SCAN = bytes.fromhex("ffda0008010100003f00") + b"\x00" * 5
EOI = b"\xff\xd9"


def _progressive_jpeg(size=(640, 480), mode="RGB", seed=0, **save_kwargs) -> bytes:
    rng = np.random.default_rng(seed)
    shape = (size[1], size[0], 3) if mode == "RGB" else (size[1], size[0])
    img = Image.fromarray(rng.integers(0, 256, shape, dtype=np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", progressive=True, **save_kwargs)
    return buf.getvalue()


PROGRESSIVE_JPEG = _progressive_jpeg()
PROGRESSIVE_SCANS = PROGRESSIVE_JPEG.count(b"\xff\xda")


def _flat_progressive_jpeg() -> bytes:
    """review-32's carrier: a flat 2000x1500 progressive file whose last scan is 15 bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1500), (120, 120, 120)).save(buf, "JPEG", progressive=True)
    return buf.getvalue()


FLAT_PROGRESSIVE = _flat_progressive_jpeg()
FLAT_SCANS = FLAT_PROGRESSIVE.count(b"\xff\xda")
LAST_SCAN = FLAT_PROGRESSIVE[FLAT_PROGRESSIVE.rindex(b"\xff\xda") : -2]


def _with_extra_scans(data: bytes, extra: int) -> bytes:
    assert data.endswith(EOI)
    return data[:-2] + EXTRA_SCAN * extra + EOI


def _with_repeated_last_scan(extra: int) -> bytes:
    """A decodable file: the final scan of FLAT_PROGRESSIVE repeated `extra` more times."""
    return FLAT_PROGRESSIVE[:-2] + LAST_SCAN * extra + EOI


def _first_sos_end(data: bytes) -> int:
    sos = data.index(b"\xff\xda")
    return sos + 2 + int.from_bytes(data[sos + 2 : sos + 4], "big")


@pytest.mark.filterwarnings("ignore::UserWarning")
class TestProgressiveScanCap:
    def test_the_cap_is_64_and_typical_progressive_files_are_far_below_it(self):
        assert rga_io.MAX_JPEG_SCANS == 64
        assert 10 <= PROGRESSIVE_SCANS <= 12

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"mode": "L"},
            {"quality": 40},
            {"quality": 95, "size": (1000, 800)},
            {"restart_marker_rows": 1},
            {"restart_marker_blocks": 5, "size": (300, 200)},
            {"subsampling": 0},
            {"optimize": True},
        ],
        ids=["rgb", "gray", "q40", "big_q95", "restart_rows", "restart_blocks", "444", "optimized"],
    )
    def test_pillow_generated_progressive_files_load_with_unchanged_pixels(self, kwargs):
        data = _progressive_jpeg(**kwargs)
        assert 1 <= data.count(b"\xff\xda") <= 12
        arr, ext = load_oriented(data)
        assert ext == "jpg"
        expected = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))[:, :, ::-1]
        np.testing.assert_array_equal(arr, expected)
        assert rga_io._rebuild_jpeg(data).data == data

    def test_a_progressive_cmyk_file_loads(self):
        buf = io.BytesIO()
        Image.new("CMYK", (80, 60), (10, 20, 30, 40)).save(buf, "JPEG", progressive=True)
        assert load_oriented(buf.getvalue())[0].shape == (60, 80, 3)

    def test_review_32_ten_thousand_extra_scans_are_rejected_fast_and_cheaply(self):
        flood = _with_repeated_last_scan(10_000)
        assert 150_000 < len(flood) < 200_000
        outcome, seconds, peak = _timed_peak(lambda: load_oriented(flood))
        assert isinstance(outcome, ImageRejected)
        assert str(outcome) == "too many progressive scans"
        assert seconds < 0.5
        assert peak < PEAK_LIMIT_BYTES

    def test_the_repeated_scans_are_valid_for_pillow_so_the_cap_is_what_stops_them(self):
        assert LAST_SCAN.startswith(b"\xff\xda") and len(LAST_SCAN) == 15
        modest = _with_repeated_last_scan(40)
        Image.open(io.BytesIO(modest)).load()
        assert load_oriented(modest)[0].shape == (1500, 2000, 3)

    def test_the_flood_is_rejected_before_pillow_decodes_anything(self, monkeypatch):
        opened = []
        real = Image.open
        monkeypatch.setattr(Image, "open", lambda *a, **k: (opened.append(1), real(*a, **k))[1])
        with pytest.raises(ImageRejected, match="too many progressive scans"):
            load_oriented(_with_repeated_last_scan(5000))
        assert opened == []

    def test_a_forty_megabyte_file_of_extra_scans_is_rejected_at_once(self):
        flood = _with_repeated_last_scan((UPLOAD_CAP - len(FLAT_PROGRESSIVE)) // len(LAST_SCAN))
        assert len(flood) > UPLOAD_CAP - 64
        outcome, seconds, peak = _timed_peak(lambda: load_oriented(flood))
        assert isinstance(outcome, ImageRejected)
        assert seconds < 0.5 and peak < PEAK_LIMIT_BYTES

    def test_exactly_the_scan_cap_loads_and_one_more_is_rejected(self):
        at_cap = _with_repeated_last_scan(rga_io.MAX_JPEG_SCANS - FLAT_SCANS)
        assert at_cap.count(b"\xff\xda") == rga_io.MAX_JPEG_SCANS
        assert load_oriented(at_cap)[0].shape == (1500, 2000, 3)
        over = _with_repeated_last_scan(rga_io.MAX_JPEG_SCANS - FLAT_SCANS + 1)
        with pytest.raises(ImageRejected, match="too many progressive scans"):
            load_oriented(over)

    def test_the_rebuild_alone_accepts_the_cap_and_rejects_one_more_on_noisy_files_too(self):
        at_cap = _with_extra_scans(PROGRESSIVE_JPEG, rga_io.MAX_JPEG_SCANS - PROGRESSIVE_SCANS)
        assert rga_io._rebuild_jpeg(at_cap).data == at_cap
        over = _with_extra_scans(PROGRESSIVE_JPEG, rga_io.MAX_JPEG_SCANS - PROGRESSIVE_SCANS + 1)
        with pytest.raises(ImageRejected, match="too many progressive scans"):
            rga_io._rebuild_jpeg(over)

    def test_a_baseline_file_counts_as_one_scan(self):
        assert rga_io._rebuild_jpeg(PLAIN_JPEG).data == PLAIN_JPEG
        rga_io._check_jpeg_scans(PLAIN_JPEG, _first_sos_end(PLAIN_JPEG))

    def test_scans_after_eoi_are_not_counted(self):
        data = PROGRESSIVE_JPEG + EXTRA_SCAN * 500
        assert rga_io._rebuild_jpeg(data).data == data

    def test_a_truncated_file_is_left_to_the_decoder_to_reject(self):
        cut = PROGRESSIVE_JPEG[: len(PROGRESSIVE_JPEG) // 2]
        rga_io._check_jpeg_scans(cut, _first_sos_end(cut))
        with pytest.raises(ImageRejected):
            load_oriented(cut)


class TestScanCountingFollowsTheEntropyCodedDataRules:
    HEADER = bytes.fromhex("ffda0008010100003f00")

    @staticmethod
    def check(tail: bytes) -> None:
        rga_io._check_jpeg_scans(
            b"\xff\xd8" + TestScanCountingFollowsTheEntropyCodedDataRules.HEADER + tail, 12
        )

    def sos(self, count: int, between: bytes = b"") -> bytes:
        return (b"\x12\x34" + self.HEADER + between) * count

    def test_a_stuffed_ff_followed_by_da_is_data_not_a_marker(self):
        self.check(b"\xff\x00\xda\x00\x08\x01\x01\x00\x00\x3f\x00" * 5000 + EOI)

    def test_restart_markers_are_not_scan_markers_and_cost_nothing_to_walk(self):
        rst = b"".join(bytes([0xFF, 0xD0 + i]) + b"\x12" for i in range(8))
        self.check(rst * 20_000 + EOI)

    def test_fill_bytes_before_a_marker_do_not_hide_it(self):
        self.check(self.sos(rga_io.MAX_JPEG_SCANS - 1) + EOI)
        with pytest.raises(ImageRejected, match="progressive scans"):
            self.check((b"\xff\xff\xff" + self.HEADER) * rga_io.MAX_JPEG_SCANS + EOI)

    def test_scan_headers_are_found_inside_arbitrary_entropy_data(self):
        noisy = bytes(range(1, 255)) * 3
        self.check((noisy + self.HEADER) * (rga_io.MAX_JPEG_SCANS - 1) + noisy + EOI)
        with pytest.raises(ImageRejected, match="progressive scans"):
            self.check((noisy + self.HEADER) * rga_io.MAX_JPEG_SCANS + noisy + EOI)

    def test_ff_da_bytes_inside_another_segments_payload_are_not_counted(self):
        dht = b"\xff\xc4" + struct.pack(">H", 2 + 12) + b"\xff\xda" * 6
        self.check((b"\x01\x02" + dht) * 200 + EOI)

    def test_a_segment_between_scans_is_skipped_by_its_length(self):
        self.check(
            self.sos(rga_io.MAX_JPEG_SCANS - 1, between=_segment(0xC4, b"\xff\xda" * 20)) + EOI
        )

    def test_the_walk_ends_at_eoi(self):
        self.check(EOI + self.sos(1000))

    def test_the_count_includes_the_first_scan(self):
        self.check(self.sos(rga_io.MAX_JPEG_SCANS - 1) + EOI)
        with pytest.raises(ImageRejected, match="progressive scans"):
            self.check(self.sos(rga_io.MAX_JPEG_SCANS) + EOI)

    def test_a_marker_flood_after_the_first_scan_is_bounded(self):
        with pytest.raises(ImageRejected, match="too many image segments"):
            self.check(_segment(0xC4, b"") * (rga_io.MAX_JPEG_SEGMENTS + 1) + EOI)
        self.check(_segment(0xC4, b"") * rga_io.MAX_JPEG_SEGMENTS + EOI)

    def test_a_segment_with_a_length_under_two_in_the_scan_data_is_rejected(self):
        for length in (0, 1):
            with pytest.raises(ImageRejected, match="malformed JPEG segment length"):
                self.check(b"\x01\xff\xe1" + struct.pack(">H", length))

    def test_standalone_markers_without_a_length_are_skipped(self):
        self.check((b"\xff\x01" + b"\xff\xc8" + b"\xff\xf0") * 100 + EOI)

    @pytest.mark.parametrize("marker", [0x01, 0x02, 0xC8, 0xF0, 0xFD, 0xD8])
    def test_a_standalone_marker_does_not_swallow_the_scans_that_follow_it(self, marker):
        flood = bytes([0xFF, marker]) + self.sos(rga_io.MAX_JPEG_SCANS) + EOI
        with pytest.raises(ImageRejected, match="progressive scans"):
            self.check(flood)

    def test_a_segment_running_past_the_end_ends_the_walk(self):
        self.check(b"\x01" + b"\xff\xe1\xff\xff" + b"xx")

    def test_forty_megabytes_of_stuffed_bytes_are_walked_at_c_speed(self):
        data = b"\xff\x00" * (UPLOAD_CAP // 2)
        started = time.perf_counter()
        rga_io._check_jpeg_scans(data, 0)
        assert time.perf_counter() - started < 1.0
