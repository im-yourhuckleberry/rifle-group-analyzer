"""Hardened image loading: magic-byte sniffing, size and pixel caps, EXIF orientation, BGR output.

Accepts JPEG, PNG and HEIC only. Every decoder or limit failure surfaces as `ImageRejected`.
Pillow never parses upload EXIF or XMP: the carriers are cut out of the raw JPEG and PNG bytes
before `Image.open`, the orientation is read by a bounded reader of IFD0 (`_ifd0_orientation`), and
the capture time by a bounded reader of one IFD0 -> Exif sub-IFD hop (`_exif_taken_at`).
"""

import hashlib
import io
import re
import warnings
import zlib
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pillow_heif
from PIL import Image, ImageFile

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_PIXELS = 60_000_000
MAX_ASPECT_RATIO = 20
MAX_METADATA_BYTES = 256 * 1024
MAX_IFD_ENTRIES = 256
MAX_ICC_BYTES = 1024 * 1024
MAX_JPEG_SEGMENTS = 1024
MAX_KEPT_SEGMENTS = 64
MAX_KEPT_HEADER_BYTES = 1024 * 1024
MAX_JUNK_BYTES = 4096
MAX_JPEG_SCANS = 64
MAX_PNG_CHUNKS = 20_000

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False
pillow_heif.register_heif_opener()

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"})
_PILLOW_FORMATS = {"jpg": "JPEG", "png": "PNG", "heic": "HEIF"}
_ORIENTATION_TAG = 0x0112
_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_JPEG_EXIF_PREFIX = b"Exif\0\0"
_JPEG_XMP_PREFIX = b"http://ns.adobe.com/xap/1.0/\0"
_PNG_XMP_KEYWORDS = frozenset({b"XML:com.adobe.xmp", b"xmp"})
_PNG_RAW_PROFILE_KEYWORD = b"Raw profile type exif"
_BOMB_ERRORS = (Image.DecompressionBombError, Image.DecompressionBombWarning)
_XMP_ORIENTATION = re.compile(rb'tiff:Orientation(="|>)([0-9])')
_EXIF_IFD_POINTER_TAG = 0x8769
_DATETIME_TAGS = (0x9003, 0x9004)
_OFFSET_TIME_ORIGINAL_TAG = 0x9011
_TIFF_ASCII, _TIFF_LONG = 2, 4
_TAKEN_AT_TEXT = re.compile(rb"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}")
_UTC_OFFSET_TEXT = re.compile(rb"[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00)")
MIN_TAKEN_YEAR = 1990


class ImageRejected(ValueError):
    """Raised when bytes are not an acceptable image (type, size, pixel count or corrupt data)."""


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def _sniff_extension(data: bytes) -> str | None:
    if data.startswith(_JPEG_MAGIC):
        return "jpg"
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "heic"
    return None


def _to_bgr(img: Image.Image) -> np.ndarray:
    if img.mode.startswith("I;16"):
        gray = (np.asarray(img, dtype=np.uint32) >> 8).astype(np.uint8)
        return np.ascontiguousarray(np.repeat(gray[:, :, None], 3, axis=2))
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    return np.ascontiguousarray(rgb[:, :, ::-1])


@dataclass(frozen=True)
class _Rebuilt:
    """An upload re-emitted from a whitelist of segments or chunks, and the carriers' contents."""

    data: bytes
    exif: bytes | None
    xmp: bytes


def _charge(total: int, size: int) -> int:
    """Add `size` carrier bytes to `total`, rejecting the upload once it passes the cap."""
    total += size
    if total > MAX_METADATA_BYTES:
        raise ImageRejected("EXIF metadata too large")
    return total


_JPEG_SOF_KEPT = frozenset({0xC0, 0xC1, 0xC2})
_JPEG_LENGTH_MARKERS = frozenset([*range(0xC0, 0xC8), *range(0xC9, 0xD0), *range(0xDA, 0xE0)])
_JPEG_LENGTH_MARKERS |= frozenset([*range(0xE0, 0xF0), 0xFE])
_JPEG_KEPT_WHOLE = frozenset({0xC4, 0xDB, 0xDD})
_JPEG_KEPT_APP = {0xE0: b"JFIF", 0xEE: b"Adobe"}
_JPEG_MAX_FRAME_PAYLOAD = 64
_TOO_MANY_SEGMENTS = "too many image segments"
_TOO_MANY_SCANS = "too many progressive scans"
_JPEG_SCAN_DATA_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")
_JPEG_APP2_ICC = b"ICC_PROFILE\0"


def _check_jpeg_scans(data: bytes, pos: int) -> None:
    """Reject a JPEG with more than MAX_JPEG_SCANS scans; `pos` is just past the first SOS header.

    Walks the entropy-coded data at C speed with a regular expression: a stuffed 0xFF (FF 00),
    a restart marker (FF D0 to D7) and fill bytes (FF FF) are not markers. Marker segments between
    scans are skipped by their declared length, so bytes inside them are never mistaken for an
    SOS, and the walk ends at EOI. Anything else lacking a length is skipped as a standalone
    marker. The marker count is bounded by MAX_JPEG_SEGMENTS.
    """
    scans, markers, end = 1, 0, len(data)
    while True:
        found = _JPEG_SCAN_DATA_MARKER.search(data, pos)
        if found is None or data[found.start() + 1] == 0xD9:
            return
        start, marker = found.start(), data[found.start() + 1]
        markers += 1
        if markers > MAX_JPEG_SEGMENTS:
            raise ImageRejected(_TOO_MANY_SEGMENTS)
        if marker not in _JPEG_LENGTH_MARKERS:
            pos = start + 2
            continue
        length = int.from_bytes(data[start + 2 : start + 4], "big")
        if length < 2:
            raise ImageRejected("malformed JPEG segment length")
        if marker == 0xDA:
            scans += 1
            if scans > MAX_JPEG_SCANS:
                raise ImageRejected(_TOO_MANY_SCANS)
        pos = start + 2 + length
        if pos > end:
            return


def _rebuild_jpeg(data: bytes) -> _Rebuilt:
    """Re-emit a JPEG from a whitelist of segments and return its EXIF and XMP payloads.

    Kept: SOI, DQT, DHT, DRI, one SOF0/1/2 frame header, APP0 JFIF, APP14 Adobe, APP2
    ICC_PROFILE (up to MAX_ICC_BYTES), and the scan from SOS to the end. Every other APPn and COM
    segment is dropped, its EXIF and XMP content kept aside for the orientation reader. The walk
    follows Pillow's own marker table, and anything whose framing Pillow and libjpeg could read
    differently is rejected: a length under 2, markers without a length (RSTn, SOI, EOI, JPGn,
    TEM, 0xC8, an escaped 0xFF00), an unknown marker, an unsupported or repeated frame header, a
    missing scan, or more than MAX_JPEG_SCANS scans (`_check_jpeg_scans`). Work and memory are
    bounded whatever the input: at most MAX_JPEG_SEGMENTS segments are walked, MAX_KEPT_SEGMENTS
    kept, MAX_KEPT_HEADER_BYTES of kept header bytes and MAX_JUNK_BYTES of junk or fill between
    segments, otherwise `ImageRejected`. The output is a handful of slices of the input, joined
    once, or the input itself when nothing was dropped.
    """
    view = memoryview(data)
    pieces: list[memoryview] = []
    exif_parts, xmp_parts = [], []
    carrier_bytes = icc_bytes = kept_bytes = junk = walked = kept = frames = 0
    keep_from, drop_from = 0, None
    pos, end = 2, len(data)
    while True:
        if pos + 1 >= end:
            raise ImageRejected("JPEG has no scan")
        if data[pos] != 0xFF:
            next_ff = data.find(b"\xff", pos)
            skipped = (end if next_ff < 0 else next_ff) - pos
            junk += skipped
            if junk > MAX_JUNK_BYTES:
                raise ImageRejected(_TOO_MANY_SEGMENTS)
            drop_from = pos if drop_from is None else drop_from
            pos += skipped
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            junk += 1
            if junk > MAX_JUNK_BYTES:
                raise ImageRejected(_TOO_MANY_SEGMENTS)
            drop_from = pos if drop_from is None else drop_from
            pos += 1
            continue
        if drop_from is not None:
            pieces.append(view[keep_from:drop_from])
            keep_from, drop_from = pos, None
        if marker not in _JPEG_LENGTH_MARKERS:
            raise ImageRejected("unexpected JPEG marker before the scan")
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2:
            raise ImageRejected("malformed JPEG segment length")
        segment_end = pos + 2 + length
        if segment_end > end:
            raise ImageRejected("truncated JPEG segment")
        if marker == 0xDA:
            _check_jpeg_scans(data, segment_end)
            pieces.append(view[keep_from:])
            break
        walked += 1
        if walked > MAX_JPEG_SEGMENTS:
            raise ImageRejected(_TOO_MANY_SEGMENTS)
        payload = data[pos + 4 : segment_end]
        keep, keep_bytes, carrier = False, length + 2, None
        if marker in _JPEG_SOF_KEPT:
            frames += 1
            if frames > 1 or len(payload) > _JPEG_MAX_FRAME_PAYLOAD:
                raise ImageRejected("malformed JPEG frame header")
            keep = True
        elif marker == 0xDD and length != 4:
            raise ImageRejected("malformed JPEG restart interval")
        elif marker in _JPEG_KEPT_WHOLE:
            keep = True
        elif 0xC0 <= marker <= 0xDF:
            raise ImageRejected("unsupported JPEG segment")
        elif marker in _JPEG_KEPT_APP and payload.startswith(_JPEG_KEPT_APP[marker]):
            keep = True
        elif marker == 0xE2 and payload.startswith(_JPEG_APP2_ICC):
            icc_bytes += len(payload)
            if icc_bytes <= MAX_ICC_BYTES:
                keep, keep_bytes = True, 0
        elif marker == 0xE1 and payload.startswith(_JPEG_EXIF_PREFIX):
            carrier = exif_parts, payload[len(_JPEG_EXIF_PREFIX) :]
        elif marker == 0xE1 and payload.startswith(_JPEG_XMP_PREFIX):
            carrier = xmp_parts, payload[len(_JPEG_XMP_PREFIX) :]
        if keep:
            kept += 1
            kept_bytes += keep_bytes
            if kept > MAX_KEPT_SEGMENTS or kept_bytes > MAX_KEPT_HEADER_BYTES:
                raise ImageRejected(_TOO_MANY_SEGMENTS)
        else:
            pieces.append(view[keep_from:pos])
            keep_from = segment_end
        if carrier is not None:
            carrier_bytes = _charge(carrier_bytes, len(carrier[1]))
            carrier[0].append(carrier[1])
        pos = segment_end
    return _Rebuilt(
        data if len(pieces) == 1 else b"".join(pieces),
        b"".join(exif_parts) if exif_parts else None,
        b"".join(xmp_parts),
    )


def _bounded_inflate(compressed: memoryview) -> bytes:
    """Inflate zlib data, stopping just above MAX_METADATA_BYTES; empty if undecodable."""
    try:
        return zlib.decompressobj().decompress(compressed, MAX_METADATA_BYTES + 1)
    except zlib.error:
        return b""


def _png_text_carrier(kind: bytes, body: memoryview) -> tuple[str, bytes] | None:
    """`(role, text)` of a tEXt/zTXt/iTXt chunk that carries EXIF ("exif") or XMP ("xmp")."""
    key_end = bytes(body[:80]).find(b"\0")
    if key_end < 0:
        return None
    keyword = bytes(body[:key_end])
    if keyword == _PNG_RAW_PROFILE_KEYWORD:
        role = "exif"
    elif keyword in _PNG_XMP_KEYWORDS:
        role = "xmp"
    else:
        return None
    rest = body[key_end + 1 :]
    if kind == b"tEXt":
        return role, bytes(rest[: MAX_METADATA_BYTES + 1])
    if kind == b"zTXt":
        return role, _bounded_inflate(rest[1:])
    flag = rest[0] if len(rest) >= 2 else 0
    fields = bytes(rest[2 : 2 + MAX_METADATA_BYTES + 3 * 1024])
    lang_end = fields.find(b"\0")
    translated_end = fields.find(b"\0", lang_end + 1) if lang_end >= 0 else -1
    if translated_end < 0:
        return None
    text = rest[2 + translated_end + 1 :]
    return role, _bounded_inflate(text) if flag == 1 else bytes(text[: MAX_METADATA_BYTES + 1])


def _raw_profile_tiff(text: bytes) -> bytes:
    lines = text.decode("latin-1").split("\n")
    try:
        return bytes.fromhex("".join(lines[3:]))
    except ValueError as exc:
        raise ImageRejected(f"invalid EXIF metadata: {type(exc).__name__}") from exc


_PNG_CHUNK_SIZES = {
    b"IHDR": (13, 13),
    b"PLTE": (3, 768),
    b"tRNS": (0, 256),
    b"gAMA": (4, 4),
    b"cHRM": (32, 32),
    b"sRGB": (1, 1),
    b"pHYs": (9, 9),
    b"sBIT": (1, 4),
}


def _rebuild_png(data: bytes) -> _Rebuilt:
    """Re-emit a PNG keeping only IHDR, PLTE, IDAT, IEND, tRNS, gAMA, cHRM, sRGB, pHYs, sBIT and
    an iCCP of up to MAX_ICC_BYTES, and return the EXIF and XMP carried by the dropped chunks.

    Dropped chunks include eXIf, every text chunk, tIME, bKGD and animation chunks; data after
    IEND is dropped. A chunk that runs past the end of the data, a missing IEND, or a kept chunk
    of an impossible size is rejected. Work and memory are bounded: at most MAX_PNG_CHUNKS chunks
    are walked and MAX_KEPT_SEGMENTS kept chunks other than IDAT, otherwise `ImageRejected`. The
    output is a few slices of the input joined once (runs of kept chunks, IDAT included, and runs of
    dropped chunks each cost one slice, never one object per chunk), or the input itself when nothing was dropped.
    """
    view = memoryview(data)
    pieces: list[memoryview] = []
    xmp_parts: list[bytes] = []
    exif_chunk: bytes | None = None
    raw_profile: bytes | None = None
    carrier_bytes = icc_bytes = walked = kept = keep_from = 0
    dropping = False
    pos = len(_PNG_MAGIC)
    while True:
        if pos + 12 > len(data):
            raise ImageRejected("PNG has no IEND chunk")
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = bytes(view[pos + 4 : pos + 8])
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            raise ImageRejected("truncated PNG chunk")
        walked += 1
        if walked > MAX_PNG_CHUNKS:
            raise ImageRejected(_TOO_MANY_SEGMENTS)
        keep = kind in (b"IDAT", b"IEND")
        if kind in _PNG_CHUNK_SIZES:
            low, high = _PNG_CHUNK_SIZES[kind]
            if not low <= length <= high:
                raise ImageRejected("malformed PNG chunk size")
            keep = True
        elif kind == b"iCCP":
            icc_bytes += length
            keep = icc_bytes <= MAX_ICC_BYTES
        elif kind == b"eXIf":
            carrier_bytes = _charge(carrier_bytes, length)
            exif_chunk = bytes(view[pos + 8 : pos + 8 + length])
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            carrier = _png_text_carrier(kind, view[pos + 8 : pos + 8 + length])
            if carrier is not None:
                role, text = carrier
                carrier_bytes = _charge(carrier_bytes, len(text))
                if role == "xmp":
                    xmp_parts.append(text)
                else:
                    raw_profile = text
        if keep and kind != b"IDAT":
            kept += 1
            if kept > MAX_KEPT_SEGMENTS:
                raise ImageRejected(_TOO_MANY_SEGMENTS)
        if keep and dropping:
            keep_from, dropping = pos, False
        elif not keep and not dropping:
            pieces.append(view[keep_from:pos])
            dropping = True
        if kind == b"IEND":
            pieces.append(view[keep_from:chunk_end])
            break
        pos = chunk_end
    if exif_chunk is None and raw_profile is not None:
        exif_chunk = _raw_profile_tiff(raw_profile)
    unchanged = len(pieces) == 1 and chunk_end == len(data)
    return _Rebuilt(
        data if unchanged else b"".join(pieces), exif_chunk or None, b"".join(xmp_parts)
    )


def _info_metadata_bytes(info: dict) -> int:
    """Payload size of the EXIF and XMP carriers Pillow has already materialised in `info`.

    Pillow stores a PNG XMP packet under both "xmp" and "XML:com.adobe.xmp", so XMP counts once,
    as the larger of the two.
    """

    def size(key: str) -> int:
        value = info.get(key)
        return len(value) if isinstance(value, bytes | bytearray | str) else 0

    exif = size("exif")
    if isinstance(info.get("exif"), bytes) and info["exif"].startswith(_JPEG_EXIF_PREFIX):
        exif -= len(_JPEG_EXIF_PREFIX)
    xmp = max(size("xmp"), size("XML:com.adobe.xmp"))
    return exif + xmp + size("Raw profile type exif")


def _ifd0_orientation(exif: bytes) -> int | None:
    """Orientation SHORT of IFD0 read straight from raw EXIF bytes, never touching other values.

    Returns None for a missing or malformed header, IFD or tag; when several Orientation entries
    exist the last one wins, as in Pillow. At most MAX_IFD_ENTRIES entries are read, so work is
    bounded whatever the declared count, and out-of-line values are never read.
    """
    tiff = exif[len(_JPEG_EXIF_PREFIX) :] if exif.startswith(_JPEG_EXIF_PREFIX) else exif
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    order = "little" if tiff[:2] == b"II" else "big"
    if int.from_bytes(tiff[2:4], order) != 42:
        return None
    offset = int.from_bytes(tiff[4:8], order)
    declared = int.from_bytes(tiff[offset : offset + 2], order)
    orientation = None
    for index in range(min(declared, MAX_IFD_ENTRIES, (len(tiff) - offset - 2) // 12)):
        entry = offset + 2 + 12 * index
        tag = int.from_bytes(tiff[entry : entry + 2], order)
        kind = int.from_bytes(tiff[entry + 2 : entry + 4], order)
        count = int.from_bytes(tiff[entry + 4 : entry + 8], order)
        if tag == _ORIENTATION_TAG and kind == 3 and count == 1:
            orientation = int.from_bytes(tiff[entry + 8 : entry + 10], order)
    return orientation


def _tiff_of(exif: bytes) -> tuple[memoryview, str] | None:
    """`(tiff view, byte order)` of raw EXIF bytes, or None when the TIFF header is not valid."""
    view = memoryview(exif)
    tiff = view[len(_JPEG_EXIF_PREFIX) :] if exif.startswith(_JPEG_EXIF_PREFIX) else view
    if len(tiff) < 8 or bytes(tiff[:2]) not in (b"II", b"MM"):
        return None
    order = "little" if bytes(tiff[:2]) == b"II" else "big"
    return (tiff, order) if int.from_bytes(tiff[2:4], order) == 42 else None


def _ifd_entries(tiff: memoryview, order: str, offset: int) -> list[tuple[int, int, int, int]]:
    """`(tag, type, count, value field position)` of an IFD at `offset`; empty when it is not inside `tiff`.

    At most MAX_IFD_ENTRIES entries are read and only entries that lie wholly inside `tiff`.
    """
    if offset < 8 or offset + 2 > len(tiff):
        return []
    declared = int.from_bytes(tiff[offset : offset + 2], order)
    entries = []
    for index in range(min(declared, MAX_IFD_ENTRIES, (len(tiff) - offset - 2) // 12)):
        entry = offset + 2 + 12 * index
        entries.append(
            (
                int.from_bytes(tiff[entry : entry + 2], order),
                int.from_bytes(tiff[entry + 2 : entry + 4], order),
                int.from_bytes(tiff[entry + 4 : entry + 8], order),
                entry + 8,
            )
        )
    return entries


def _ascii_value(
    tiff: memoryview, order: str, entry: tuple[int, int, int, int], count: int
) -> bytes | None:
    """The NUL-terminated ASCII value (without the NUL) of an out-of-line entry of `count` bytes."""
    _, kind, declared, field = entry
    if kind != _TIFF_ASCII or declared != count:
        return None
    start = int.from_bytes(tiff[field : field + 4], order)
    if start < 8 or start + count > len(tiff) or tiff[start + count - 1] != 0:
        return None
    return bytes(tiff[start : start + count - 1])


def _taken_at_text(value: bytes | None) -> str | None:
    if value is None or not _TAKEN_AT_TEXT.fullmatch(value):
        return None
    try:
        moment = datetime.strptime(value.decode("ascii"), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if not MIN_TAKEN_YEAR <= moment.year <= datetime.now().year + 1:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _exif_taken_at(exif: bytes) -> tuple[str | None, str | None]:
    """`(taken_at, utc_offset)` from raw EXIF bytes: local `YYYY-MM-DDTHH:MM:SS` and `+HH:MM`.

    One bounds-checked hop: IFD0 is scanned (at most MAX_IFD_ENTRIES entries) for the Exif sub-IFD
    pointer 0x8769 (LONG, count 1; the first one counts), then that sub-IFD (also at most
    MAX_IFD_ENTRIES entries) for DateTimeOriginal 0x9003, else DateTimeDigitized 0x9004 (ASCII,
    count 20, 19 characters at an offset inside the data), and OffsetTimeOriginal 0x9011 (ASCII,
    count 7). A pointer outside the data, inside the TIFF header or back at IFD0 is not followed;
    no other pointer is. The date must match `YYYY:MM:DD HH:MM:SS`, be a real moment, and fall
    between MIN_TAKEN_YEAR and next year; the offset must be `+HH:MM` within 14:00. Any failure
    yields None for that value and never raises, so a bad date can never reject an image.
    """
    located = _tiff_of(exif)
    if located is None:
        return None, None
    tiff, order = located
    ifd0 = int.from_bytes(tiff[4:8], order)
    pointer = next(
        (e for e in _ifd_entries(tiff, order, ifd0) if e[0] == _EXIF_IFD_POINTER_TAG), None
    )
    if pointer is None or pointer[1] != _TIFF_LONG or pointer[2] != 1:
        return None, None
    sub_ifd = int.from_bytes(tiff[pointer[3] : pointer[3] + 4], order)
    if sub_ifd == ifd0:
        return None, None
    first: dict[int, tuple[int, int, int, int]] = {}
    for entry in _ifd_entries(tiff, order, sub_ifd):
        first.setdefault(entry[0], entry)
    taken_at = None
    for tag in _DATETIME_TAGS:
        if tag in first and (taken_at := _taken_at_text(_ascii_value(tiff, order, first[tag], 20))):
            break
    offset = None
    if _OFFSET_TIME_ORIGINAL_TAG in first:
        text = _ascii_value(tiff, order, first[_OFFSET_TIME_ORIGINAL_TAG], 7)
        offset = (
            text.decode("ascii") if text is not None and _UTC_OFFSET_TEXT.fullmatch(text) else None
        )
    return taken_at, offset


def _orientation(exif: bytes | None, xmp: bytes) -> int | None:
    """EXIF Orientation from raw carrier bytes, falling back to an XMP `tiff:Orientation`.

    A carrier whose TIFF header is invalid raises `ImageRejected`; a damaged IFD, or an
    Orientation of any other type, count or value, simply yields no orientation.
    """
    if exif:
        tiff = exif[len(_JPEG_EXIF_PREFIX) :] if exif.startswith(_JPEG_EXIF_PREFIX) else exif
        if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
            raise ImageRejected("invalid EXIF metadata: not a TIFF header")
        if int.from_bytes(tiff[2:4], "little" if tiff[:2] == b"II" else "big") != 42:
            raise ImageRejected("invalid EXIF metadata: not a TIFF header")
        orientation = _ifd0_orientation(tiff)
        if orientation is not None:
            return orientation
    match = _XMP_ORIENTATION.search(xmp)
    return int(match[2]) if match else None


def _oriented(img: Image.Image, orientation: int | None) -> Image.Image:
    """Rotate/flip `img` for an EXIF orientation of 2 to 8; anything else leaves it unchanged."""
    if orientation not in _ORIENTATION_TRANSPOSE:
        return img
    return img.transpose(_ORIENTATION_TRANSPOSE[orientation])


def _check_dimensions(width: int, height: int) -> None:
    if width * height > MAX_PIXELS:
        raise ImageRejected(f"image exceeds {MAX_PIXELS} pixels")
    if max(width, height) > MAX_ASPECT_RATIO * min(width, height):
        raise ImageRejected(f"image aspect ratio exceeds {MAX_ASPECT_RATIO}:1")


def _heif_carriers(info: dict) -> tuple[bytes | None, bytes]:
    exif, xmp = info.get("exif"), info.get("xmp")
    return (exif if isinstance(exif, bytes) else None), (xmp if isinstance(xmp, bytes) else b"")


@dataclass(frozen=True)
class LoadedImage:
    """A decoded upload: oriented BGR pixels, the extension, and the capture time when the EXIF has one.

    `taken_at` is local `YYYY-MM-DDTHH:MM:SS` and `taken_at_offset` is `+HH:MM`; either is None
    when absent or invalid.
    """

    bgr: np.ndarray
    ext: str
    taken_at: str | None
    taken_at_offset: str | None


def _check_upload(data: bytes) -> str:
    if not data:
        raise ImageRejected("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
    ext = _sniff_extension(data)
    if ext is None:
        raise ImageRejected("unsupported image type (JPEG, PNG or HEIC only)")
    return ext


def load_image(data: bytes) -> LoadedImage:
    """Decode JPEG/PNG/HEIC bytes to an EXIF-oriented BGR uint8 array and read the capture time.

    Raises `ImageRejected` for empty or oversize (> MAX_UPLOAD_BYTES) input, unsupported or
    non-image bytes, more than MAX_PIXELS decoded pixels, a long/short side ratio above
    MAX_ASPECT_RATIO, more than MAX_METADATA_BYTES of EXIF or XMP metadata, a JPEG segment with a
    declared length under 2, an invalid EXIF header, and any failure of Pillow to decode
    (truncated or corrupt data, unsupported codecs, malformed chunks). Pillow is never asked to
    parse EXIF or XMP; a missing or malformed capture time only leaves `taken_at` None.
    """
    ext = _check_upload(data)

    rebuilt = _rebuild_jpeg(data) if ext == "jpg" else _rebuild_png(data) if ext == "png" else None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(
                io.BytesIO(rebuilt.data if rebuilt else data), formats=[_PILLOW_FORMATS[ext]]
            )
    except _BOMB_ERRORS as exc:
        raise ImageRejected(f"image exceeds {MAX_PIXELS} pixels") from exc
    except Exception as exc:
        raise ImageRejected(f"image could not be decoded: {type(exc).__name__}") from exc

    with img:
        _check_dimensions(img.width, img.height)
        try:
            img.load()
        except Exception as exc:
            raise ImageRejected(f"image could not be decoded: {type(exc).__name__}") from exc
        if rebuilt is None:
            _charge(0, _info_metadata_bytes(img.info))
            exif, xmp = _heif_carriers(img.info)
        else:
            exif, xmp = rebuilt.exif, rebuilt.xmp
        bgr = _to_bgr(_oriented(img, _orientation(exif, xmp)))
    taken_at, taken_at_offset = _exif_taken_at(exif) if exif else (None, None)
    return LoadedImage(bgr, ext, taken_at, taken_at_offset)


def load_oriented(data: bytes) -> tuple[np.ndarray, str]:
    """`load_image` reduced to `(image, ext)` with ext in {"jpg", "png", "heic"}; same errors."""
    loaded = load_image(data)
    return loaded.bgr, loaded.ext


def read_taken_at(data: bytes) -> tuple[str | None, str | None]:
    """`(taken_at, taken_at_offset)` of an upload without decoding its pixels; `(None, None)` on any failure.

    Meant for a duplicate upload whose stored photo has no capture time. It walks the same
    whitelisted carriers as `load_image`, so the same bounds apply, and never raises.
    """
    try:
        ext = _check_upload(data)
        if ext == "heic":
            with Image.open(io.BytesIO(data), formats=[_PILLOW_FORMATS[ext]]) as img:
                exif, _ = _heif_carriers(img.info)
        else:
            rebuilt = _rebuild_jpeg(data) if ext == "jpg" else _rebuild_png(data)
            exif = rebuilt.exif
    except Exception:
        return None, None
    return _exif_taken_at(exif) if exif else (None, None)
