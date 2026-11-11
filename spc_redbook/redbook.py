"""AWIPS Redbook (graphics) encoder, decoder and validator.

The Redbook container is a WMO header line followed by a sequence of binary
blocks.  Each block starts with a big-endian 16-bit header word, a mode byte
and a submode byte::

    bit 15 : 0 = length field is present
    bit 14 : 1 = no checksum word follows the block
    bits 0-13 : total block length in 16-bit words, including the 4-byte
                block header but excluding an optional checksum word

Operational NOAA Redbook samples commonly set bit 14 and therefore carry no
checksum.  This module follows that convention, which is also what AWIPS'
decoder expects in practice (it does not verify the checksum).

The encoder deliberately uses the real-world block layout rather than the
length check bug in AWIPS' ``ProductIdBlock`` (the parser's ``remaining()``
value after the 4-byte header is 28, while it tests for 32/34).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .projection import GridSpec, normalize_lon

# Block identifiers (mode, submode).
BLOCK_PRODUCT_ID = (1, 1)
BLOCK_VERSION = (2, 5)
BLOCK_PLOT_SPACE = (4, 16)
BLOCK_PROJECTION = (4, 17)
BLOCK_PLOT_PARAMETERS = (1, 4)
BLOCK_VECTORS = (4, 5)
BLOCK_TEXT = (5, 1)
BLOCK_PLOT_DATA = (5, 2)
BLOCK_END = (1, 2)

#: Matches the WMO header regex used by AWIPS' WMOHeader class.
WMO_HEADER_RE = re.compile(
    rb"([A-Z]{3}[A-Z0-9](?:\d{0,2}|[A-Z]{0,2}) [A-Z0-9 ]{4} "
    rb"(\d{6})(?: [A-Z]{3})? *[\r]*\n)"
)

_PROJECTION_NAME = b"PNHE01"
_MAX_SHORT = 63
_MIN_SHORT = -64
_MAX_LONG = 4095
_MIN_LONG = -4096
_MAX_BLOCK_WORDS = 0x3FFF
_VECTOR_CHUNK_WORDS = 12000


class RedbookError(ValueError):
    """Raised when a Redbook file is malformed."""


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


def _validate_ascii_id(value: str, length: int, what: str) -> str:
    value = value.strip().upper()
    if len(value) != length or not re.fullmatch(r"[A-Z0-9]{%d}" % length, value):
        raise RedbookError(f"{what} must be exactly {length} uppercase alphanumeric characters")
    return value


def _block(mode: int, submode: int, payload: bytes) -> bytes:
    total = 4 + len(payload)
    if total % 2:
        raise RedbookError("block payload must have an even total length")
    length = total // 2
    if length > _MAX_BLOCK_WORDS:
        raise RedbookError("block is too large for the 14-bit length field")
    # bit 14 set => no checksum, bits 0-13 => length in words
    header = length | 0x4000
    return struct.pack(">HBB", header, mode, submode) + payload


def _encode_short_delta(dx: int, dy: int, beam: bool) -> int:
    if not (_MIN_SHORT <= dx <= _MAX_SHORT and _MIN_SHORT <= dy <= _MAX_SHORT):
        raise RedbookError("delta does not fit the short vector format")
    return (
        0x8000
        | ((dx & 0x7F) << 8)
        | (0x0080 if beam else 0)
        | (dy & 0x7F)
    )


def _encode_long_delta(dx: int, dy: int, beam: bool) -> Tuple[int, int]:
    if not (_MIN_LONG <= dx <= _MAX_LONG and _MIN_LONG <= dy <= _MAX_LONG):
        raise RedbookError("delta does not fit the long vector format")
    word1 = dx & 0x1FFF
    word2 = (dy & 0x1FFF) | (0x2000 if beam else 0)
    return (word1, word2)


def _encode_delta(dx: int, dy: int, beam: bool) -> List[int]:
    if _MIN_SHORT <= dx <= _MAX_SHORT and _MIN_SHORT <= dy <= _MAX_SHORT:
        return [_encode_short_delta(dx, dy, beam)]
    return list(_encode_long_delta(dx, dy, beam))


@dataclass
class VectorRing:
    """A line or polygon ring in stored Redbook coordinates.

    ``points`` are ``(posX, posY)`` pairs where ``posY`` counts up from the
    bottom of the grid, matching GEMPAK's device coordinates in ``004_005``
    blocks.  AWIPS converts them to top-down rows with ``row = n - posY``.
    Set ``closed=False`` for hatching or other open line segments.
    """

    points: List[Tuple[int, int]]
    closed: bool = True

    def normalized(self) -> List[Tuple[int, int]]:
        points: List[Tuple[int, int]] = []
        for point in self.points:
            px, py = int(point[0]), int(point[1])
            if points and points[-1] == (px, py):
                continue
            points.append((px, py))
        if self.closed and len(points) >= 2 and points[0] != points[-1]:
            points.append(points[0])
        return points


@dataclass
class TextItem:
    """A single ``005_001`` alphanumeric text block."""

    x: int
    y: int
    text: str
    offset_x: int = 0
    offset_y: int = 0
    mode: int = 0


def _rotate_closed_ring(
    points: List[Tuple[int, int]], avoid: Tuple[int, int]
) -> Optional[List[Tuple[int, int]]]:
    """Rotate a closed ring so it does not start at ``avoid``.

    A Redbook beam marks the first point of a new line.  If that point is the
    same as the previous line's last point the reader cannot append it, so the
    opening segment of the new ring would be lost.  Closed rings can be
    rotated without changing their geometry.
    """
    base = points[:-1] if points and points[0] == points[-1] else list(points)
    for index, point in enumerate(base):
        if point != avoid:
            rotated = base[index:] + base[:index]
            rotated.append(rotated[0])
            return rotated
    return None


def _encode_vector_payload(rings: Sequence[VectorRing], n: int) -> bytes:
    """Encode one or more rings as a single 004_005 block payload."""
    prepared: List[List[Tuple[int, int]]] = []
    for ring in rings:
        points = ring.normalized()
        if len(points) < 2:
            continue
        if prepared and ring.closed and points[0] == prepared[-1][-1]:
            rotated = _rotate_closed_ring(points, prepared[-1][-1])
            if rotated:
                points = rotated
        prepared.append(points)
    if not prepared:
        raise RedbookError("cannot encode an empty vector block")

    first = prepared[0][0]
    words: List[int] = [first[0] & 0xFFFF, first[1] & 0xFFFF]
    prev_x, prev_y = first[0], n - first[1]
    first_delta = True
    pending_beam = False
    for ring_index, points in enumerate(prepared):
        start = 1 if ring_index == 0 else 0
        for point_index in range(start, len(points)):
            px, py = points[point_index]
            cur_x, cur_y = px, n - py
            beam = (point_index == 0 and ring_index > 0) or pending_beam
            pending_beam = False
            dx_total = cur_x - prev_x
            # Encoded dy is applied by the reader as ``curY = lastY - dy``.
            dy_total = prev_y - cur_y
            if dx_total == 0 and dy_total == 0:
                if beam:
                    pending_beam = True
                continue
            steps = max(
                (abs(dx_total) + _MAX_LONG - 1) // _MAX_LONG,
                (abs(dy_total) + _MAX_LONG - 1) // _MAX_LONG,
                1,
            )
            for step in range(1, steps + 1):
                if step == steps:
                    nx, ny = cur_x, cur_y
                else:
                    nx = round(prev_x + dx_total * step / steps)
                    ny = round(prev_y - dy_total * step / steps)
                dx = nx - prev_x
                dy = prev_y - ny
                step_beam = beam and step == 1 and not first_delta
                if dx == 0 and dy == 0:
                    if step_beam:
                        pending_beam = True
                    continue
                words.extend(_encode_delta(dx, dy, step_beam))
                prev_x, prev_y = nx, ny
                first_delta = False
    return struct.pack(">%dH" % len(words), *words)


def _split_ring_chunks(rings: Sequence[VectorRing]) -> List[List[VectorRing]]:
    """Partition rings into vector blocks that fit the length field."""
    chunks: List[List[VectorRing]] = []
    current: List[VectorRing] = []
    current_words = 4  # header + absolute position
    for ring in rings:
        points = ring.normalized()
        if len(points) < 2:
            continue
        estimated = 2 * len(points) + 4
        if current and current_words + estimated > _VECTOR_CHUNK_WORDS:
            chunks.append(current)
            current = []
            current_words = 4
        current.append(VectorRing(points, closed=ring.closed))
        current_words += estimated
    if current:
        chunks.append(current)
    return chunks


class RedbookWriter:
    """Build a Redbook graphics file block by block."""

    def __init__(
        self,
        grid: GridSpec,
        wmo: str,
        issue_time: Optional[datetime] = None,
        valid_time: Optional[datetime] = None,
        expire_time: Optional[datetime] = None,
        cccc: str = "KWBC",
    ) -> None:
        self.grid = grid
        self.wmo = _validate_ascii_id(wmo, 6, "WMO/AWIPS ID")
        self.cccc = _validate_ascii_id(cccc, 4, "WMO CCCC")
        self.issue_time = _utc(issue_time)
        self.valid_time = _utc(valid_time or issue_time)
        self.expire_time = _utc(expire_time or valid_time or issue_time)
        self.rings: List[VectorRing] = []
        self.texts: List[TextItem] = []

    # -- content ---------------------------------------------------------------
    def add_ring(
        self, points: Iterable[Tuple[int, int]], closed: bool = True
    ) -> None:
        ring = VectorRing([(int(p[0]), int(p[1])) for p in points], closed=closed)
        if len(ring.normalized()) >= 2:
            self.rings.append(ring)

    def add_text(
        self,
        x: int,
        y: int,
        text: str,
        offset_x: int = 0,
        offset_y: int = 0,
        mode: int = 0,
    ) -> None:
        clean = "".join(
            ch if 32 <= ord(ch) <= 126 else " " for ch in str(text)
        ).strip()
        if not clean:
            return
        self.texts.append(
            TextItem(
                x=int(x),
                y=int(y),
                text=clean,
                offset_x=max(-128, min(127, int(offset_x))),
                offset_y=max(-128, min(127, int(offset_y))),
                mode=int(mode) & 0xFF,
            )
        )

    # -- block payloads --------------------------------------------------------
    def _wmo_header(self) -> bytes:
        stamp = self.valid_time.strftime("%d%H%M")
        return f"{self.wmo} {self.cccc} {stamp}\r\r\n".encode("ascii")

    def _product_id_block(self) -> bytes:
        tm = self.valid_time
        payload = (
            self.cccc.encode("ascii")
            + bytes((0, 0, 0))  # classification, retention hours, file indicator
            + (self.wmo + "001").encode("ascii")
            + struct.pack(">HBBBB", tm.year, tm.month, tm.day, tm.hour, tm.minute)
            + self.wmo.encode("ascii")
        )
        if len(payload) != 28:
            raise RedbookError("internal error: bad product ID payload")
        return _block(*BLOCK_PRODUCT_ID, payload)

    def _version_block(self) -> bytes:
        return _block(*BLOCK_VERSION, b"V2.0\x00\x00")

    def _plot_space_block(self) -> bytes:
        m, n = self.grid.m, self.grid.n
        vt = self.valid_time
        evt = self.expire_time
        payload = (
            struct.pack(">BBhBB", 0, 0, 0, 0, 0)
            + struct.pack(">6h", 0, 0, m, n, m, n)
            + bytes((vt.month, vt.day, vt.hour, vt.minute))
            + bytes((evt.month, evt.day, evt.hour, evt.minute))
        )
        if len(payload) != 26:
            raise RedbookError("internal error: bad plot-space payload")
        return _block(*BLOCK_PLOT_SPACE, payload)

    def _projection_block(self) -> bytes:
        corners = self.grid.corners()
        projection = self.grid.projection
        payload = bytearray()
        payload.append(0)  # ignored byte
        payload.append(4)  # selection: UL, UR, LR, LL
        for key in ("ul", "ur", "lr", "ll"):
            lon, lat = corners[key]
            payload.extend(
                struct.pack(
                    ">hh",
                    int(round(lat * 100)),
                    int(round(-normalize_lon(lon) * 100)),
                )
            )
        payload.extend(
            struct.pack(
                ">hhh",
                int(round(-projection.central_lon * 100)),
                int(round(projection.lat_ts * 100)),
                0,
            )
        )
        payload.extend(_PROJECTION_NAME)
        payload.extend(b"\x00\x00")
        if len(payload) != 32:
            raise RedbookError("internal error: bad projection payload")
        return _block(*BLOCK_PROJECTION, bytes(payload))

    def _plot_parameters_block(self) -> bytes:
        # Length 3 (zoom only) is the common early-terminated form.
        return _block(*BLOCK_PLOT_PARAMETERS, struct.pack(">h", 0))

    def _vector_blocks(self) -> List[bytes]:
        blocks: List[bytes] = []
        for chunk in _split_ring_chunks(self.rings):
            payload = _encode_vector_payload(chunk, self.grid.n)
            blocks.append(_block(*BLOCK_VECTORS, payload))
        return blocks

    def _text_blocks(self) -> List[bytes]:
        blocks: List[bytes] = []
        for item in self.texts:
            text = item.text
            if len(text) % 2 == 0:
                text += " "
            payload = (
                struct.pack(
                    ">hhbbB",
                    item.x,
                    item.y,
                    item.offset_x,
                    item.offset_y,
                    item.mode,
                )
                + text.encode("ascii", errors="replace")
            )
            if (4 + len(payload)) % 2:
                raise RedbookError("internal error: odd text block size")
            blocks.append(_block(*BLOCK_TEXT, payload))
        return blocks

    def _end_block(self) -> bytes:
        # Real NOAA/GEMPAK files use length 4 with a four-byte payload.
        return _block(*BLOCK_END, b"\x00\x00\x00\x00")

    # -- output ----------------------------------------------------------------
    def build(self) -> bytes:
        parts: List[bytes] = [
            self._wmo_header(),
            self._product_id_block(),
            self._version_block(),
            self._plot_space_block(),
            self._projection_block(),
            self._plot_parameters_block(),
        ]
        parts.extend(self._vector_blocks())
        parts.extend(self._text_blocks())
        parts.append(self._end_block())
        return b"".join(parts)


# ---------------------------------------------------------------------------
# Decoder / validator
# ---------------------------------------------------------------------------


@dataclass
class Block:
    offset: int
    hdr: int
    mode: int
    submode: int
    length: int
    has_checksum: bool
    payload: bytes

    @property
    def key(self) -> Tuple[int, int]:
        return (self.mode, self.submode)


@dataclass
class RedbookDocument:
    wmo_header: str
    wmo_id: str
    cccc: str
    day_hour_minute: str
    data_offset: int
    blocks: List[Block] = field(default_factory=list)
    product_id: Dict[str, object] = field(default_factory=dict)
    plot_space: Dict[str, object] = field(default_factory=dict)
    projection: Dict[str, object] = field(default_factory=dict)
    vectors: List[List[Tuple[int, int]]] = field(default_factory=list)
    texts: List[Dict[str, object]] = field(default_factory=list)
    end_found: bool = False

    @property
    def m(self) -> int:
        return int(self.plot_space.get("m", 0))

    @property
    def n(self) -> int:
        return int(self.plot_space.get("n", 0))

    @property
    def block_keys(self) -> List[Tuple[int, int]]:
        return [block.key for block in self.blocks]

    @property
    def product_file_time(self) -> Optional[datetime]:
        info = self.product_id
        if not info:
            return None
        try:
            return datetime(
                int(info["year"]),
                int(info["month"]),
                int(info["day"]),
                int(info["hour"]),
                int(info["minute"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _iter_blocks(data: bytes, start: int) -> List[Block]:
    blocks: List[Block] = []
    offset = start
    while offset + 4 <= len(data):
        hdr, mode, submode = struct.unpack_from(">HBB", data, offset)
        if hdr & 0x8000:
            raise RedbookError(
                f"block at offset {offset} has no length field; "
                "this encoder only writes length-prefixed blocks"
            )
        length = hdr & 0x3FFF
        has_checksum = (hdr & 0x4000) == 0
        total = length * 2
        if total < 4 or offset + total > len(data):
            raise RedbookError(f"block at offset {offset} has an invalid length")
        payload_end = offset + total - (2 if has_checksum else 0)
        payload = data[offset + 4 : payload_end]
        blocks.append(
            Block(
                offset=offset,
                hdr=hdr,
                mode=mode,
                submode=submode,
                length=length,
                has_checksum=has_checksum,
                payload=payload,
            )
        )
        offset += total + (2 if has_checksum else 0)
        if (mode, submode) == BLOCK_END:
            break
    return blocks


def _decode_vectors(payload: bytes, n: int) -> List[List[Tuple[int, int]]]:
    if len(payload) < 4:
        return []
    pos_x, pos_y = struct.unpack_from(">hh", payload, 0)
    x, y = pos_x, n - pos_y
    rings: List[List[Tuple[int, int]]] = []
    coords: List[Tuple[int, int]] = [(x, y)]
    offset = 4
    first_point = True
    while offset + 2 <= len(payload):
        word1 = struct.unpack_from(">H", payload, offset)[0]
        if word1 & 0x8000:
            dx = (word1 >> 8) & 0x7F
            if dx & 0x40:
                dx -= 0x80
            dy = word1 & 0x7F
            if dy & 0x40:
                dy -= 0x80
            beam = bool(word1 & 0x0080)
            offset += 2
        else:
            if offset + 4 > len(payload):
                break
            word2 = struct.unpack_from(">H", payload, offset + 2)[0]
            dx = word1 & 0x1FFF
            if dx & 0x1000:
                dx -= 0x2000
            dy = word2 & 0x1FFF
            if dy & 0x1000:
                dy -= 0x2000
            beam = bool(word2 & 0x2000)
            offset += 4
        nx, ny = x + dx, y - dy
        if beam and not first_point:
            if len(coords) > 1:
                rings.append(coords)
            coords = []
        elif beam:
            coords = []
        if (nx, ny) != (x, y):
            coords.append((nx, ny))
        x, y = nx, ny
        first_point = False
    if len(coords) > 1:
        rings.append(coords)
    return rings


def _decode_text(payload: bytes, plot_data: bool = False) -> Dict[str, object]:
    if plot_data:
        if len(payload) < 6:
            return {}
        mode, code, pos_x, pos_y = struct.unpack_from(">BBhh", payload, 0)
        text = payload[6:].decode("latin-1", errors="replace")
        return {
            "mode": mode,
            "code": code,
            "x": pos_x,
            "y": pos_y,
            "text": text,
        }
    if len(payload) < 7:
        return {}
    pos_x, pos_y, off_x, off_y, mode = struct.unpack_from(">hhbbB", payload, 0)
    text = payload[7:].decode("latin-1", errors="replace")
    return {
        "mode": mode,
        "x": pos_x,
        "y": pos_y,
        "offset_x": off_x,
        "offset_y": off_y,
        "text": text,
    }


def parse_redbook(data: bytes) -> RedbookDocument:
    """Parse a Redbook byte string into a :class:`RedbookDocument`."""
    match = WMO_HEADER_RE.search(data)
    if not match:
        raise RedbookError("no WMO header found")
    header_bytes = match.group(1)
    day_hour_minute = match.group(2).decode("ascii")
    header_text = header_bytes.decode("latin-1").strip()
    parts = header_text.split()
    if len(parts) < 3:
        raise RedbookError("malformed WMO header")
    wmo_id = parts[0]
    cccc = parts[1]

    blocks = _iter_blocks(data, match.end())
    doc = RedbookDocument(
        wmo_header=header_text,
        wmo_id=wmo_id,
        cccc=cccc,
        day_hour_minute=day_hour_minute,
        data_offset=match.end(),
        blocks=blocks,
    )

    for block in blocks:
        if block.key == BLOCK_PRODUCT_ID and len(block.payload) >= 28:
            payload = block.payload
            doc.product_id = {
                "originator": payload[0:4].decode("latin-1", errors="replace"),
                "classification": payload[4],
                "retention_hours": payload[5],
                "file_indicator": payload[6],
                "product_id": payload[7:16].decode("latin-1", errors="replace"),
                "year": struct.unpack_from(">H", payload, 16)[0],
                "month": payload[18],
                "day": payload[19],
                "hour": payload[20],
                "minute": payload[21],
                "product_code": payload[22:28].decode(
                    "latin-1", errors="replace"
                ),
            }
        elif block.key == BLOCK_PLOT_SPACE and len(block.payload) >= 26:
            payload = block.payload
            m1, n1, m2, n2, m3, n3 = struct.unpack_from(">6h", payload, 6)
            doc.plot_space = {
                "pi_set": payload[0],
                "coordinate_flag": payload[1],
                "scale_factor": struct.unpack_from(">h", payload, 2)[0],
                "area_code": payload[4],
                "label_code": payload[5],
                "ref_m1": m1,
                "ref_n1": n1,
                "ref_m2": m2,
                "ref_n2": n2,
                "ref_m3": m3,
                "ref_n3": n3,
                "m": m2,
                "n": n2,
                "vt": tuple(payload[18:22]),
                "evt": tuple(payload[22:26]),
            }
        elif block.key == BLOCK_PROJECTION and len(block.payload) >= 32:
            payload = block.payload
            selection = payload[1]
            corners: Dict[str, Tuple[float, float]] = {}
            offset = 2
            for name in ("ul", "ur", "lr", "ll"):
                if offset + 4 > len(payload):
                    break
                lat_raw, lon_raw = struct.unpack_from(">hh", payload, offset)
                offset += 4
                corners[name] = (lon_raw / -100.0, lat_raw / 100.0)
            if offset + 6 <= len(payload):
                lon1, lat1, lat2 = struct.unpack_from(">hhh", payload, offset)
                offset += 6
            else:
                lon1 = lat1 = lat2 = 0
            name = payload[offset : offset + 6].decode(
                "latin-1", errors="replace"
            )
            doc.projection = {
                "selection": selection,
                "corners": corners,
                "central_lon": lon1 / -100.0,
                "lat_ts": lat1 / 100.0,
                "lat2": lat2 / 100.0,
                "name": name,
            }
        elif block.key == BLOCK_VECTORS and doc.n:
            doc.vectors.extend(_decode_vectors(block.payload, doc.n))
        elif block.key == BLOCK_TEXT:
            item = _decode_text(block.payload, plot_data=False)
            if item:
                doc.texts.append(item)
        elif block.key == BLOCK_PLOT_DATA:
            item = _decode_text(block.payload, plot_data=True)
            if item:
                doc.texts.append(item)
        elif block.key == BLOCK_END:
            doc.end_found = True

    return doc


def build_redbook(
    grid: GridSpec,
    wmo: str,
    issue_time: Optional[datetime] = None,
    valid_time: Optional[datetime] = None,
    expire_time: Optional[datetime] = None,
    rings: Optional[Iterable[VectorRing]] = None,
    texts: Optional[Iterable[TextItem]] = None,
    cccc: str = "KWBC",
) -> bytes:
    """Convenience function for creating a complete Redbook byte string."""
    writer = RedbookWriter(
        grid=grid,
        wmo=wmo,
        issue_time=issue_time,
        valid_time=valid_time,
        expire_time=expire_time,
        cccc=cccc,
    )
    for ring in rings or ():
        writer.add_ring(ring.points, closed=ring.closed)
    for item in texts or ():
        writer.add_text(
            item.x,
            item.y,
            item.text,
            offset_x=item.offset_x,
            offset_y=item.offset_y,
            mode=item.mode,
        )
    return writer.build()


def verify_redbook(
    data: bytes, expected_vectors: Optional[int] = None
) -> RedbookDocument:
    """Parse and sanity-check a Redbook byte string.

    Raises :class:`RedbookError` on structural problems.  Returns the decoded
    document for callers that want to inspect it.
    """
    doc = parse_redbook(data)
    if not doc.end_found:
        raise RedbookError("end-of-product block (001_002) was not found")
    if doc.m <= 0 or doc.n <= 0:
        raise RedbookError("plot-space block is missing or has invalid dimensions")
    if doc.product_id.get("product_code") != doc.wmo_id:
        raise RedbookError("product code does not match the WMO header")
    if doc.product_id.get("product_id", "")[:6] != doc.wmo_id:
        raise RedbookError("product ID does not match the WMO header")
    if expected_vectors is not None and len(doc.vectors) != expected_vectors:
        raise RedbookError(
            f"expected {expected_vectors} vector rings, decoded {len(doc.vectors)}"
        )
    return doc
