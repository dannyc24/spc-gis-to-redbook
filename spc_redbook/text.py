"""Parser for SPC AWIPS areal-outline text products (``PTSDY1``-``PTSDY3``).

These products are the human-readable WMO text form of the same outlook
geometry published as shapefiles and GeoJSON.  Points are encoded as eight
digits: the first four are latitude hundredths and the last four are longitude
hundredths west.  For longitudes west of 100 degrees SPC omits the leading
``1``, so ``0224`` means ``-102.24`` rather than ``-2.24``.  ``99999999``
separates rings within one area record.

Only the Python standard library is used.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from .gis import DataSource, Feature, GisError

LonLat = Tuple[float, float]

_DAY_RE = re.compile(
    r"\bDAY\s*([123])\s+CONVECTIVE\s+OUTLOOK\s+AREAL\s+OUTLINE\b", re.IGNORECASE
)
_PTSDY_RE = re.compile(r"\bPTSDY([123])\b", re.IGNORECASE)
_WMO_RE = re.compile(r"\bW[A-Z]{3}\d{2}\s+KWNS\s+(\d{6})\b", re.IGNORECASE)
_VALID_RE = re.compile(
    r"\bVALID\s+TIME\s+(\d{6})Z\s*-\s*(\d{6})Z\b", re.IGNORECASE
)
_SECTION_RE = re.compile(r"^\.\.\.\s*(.*?)\s*\.\.\.$")
_POINTS_HEADER_RE = re.compile(r"OUTLOOK\s+POINTS", re.IGNORECASE)

# 0810 PM CDT WED JUN 17 2026
_ISSUE_RE = re.compile(
    r"\b(\d{4}|\d{1,2}:\d{2})\s*([AP]M)\s+"
    r"([A-Z]{2,5})\s+"
    r"(?:(?:MON|TUE|WED|THU|FRI|SAT|SUN)\s+)?"
    r"([A-Z]{3})\s+(\d{1,2})\s+(\d{4})\b",
    re.IGNORECASE,
)
_FILENAME_ISO_RE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})"
)
_FILENAME_DTG_RE = re.compile(
    r"(?<!\d)(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)"
)

_COORD_RE = re.compile(r"^\d{8}$")
_PROB_LABEL_RE = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)%?$")
_CIG_LABEL_RE = re.compile(r"^CIG\d+$", re.IGNORECASE)
_SEPARATOR = "99999999"

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

# US/Canada zones that appear in SPC issuance lines.  Values are whole hours
# west of UTC.
_TZ_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    "Z": 0,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
    "AKST": -9,
    "AKDT": -8,
    "HST": -10,
}

_CATEGORIES = {"TSTM", "MRGL", "SLGT", "ENH", "MDT", "HIGH"}
_DN_BY_LABEL = {
    "TSTM": 2,
    "MRGL": 3,
    "SLGT": 4,
    "ENH": 5,
    "MDT": 6,
    "HIGH": 8,
    "SIGN": 8,
}


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def is_outlook_text(data: bytes) -> bool:
    """Return True when ``data`` looks like a PTSDY1-3 areal-outline product."""
    if not data:
        return False
    head = _decode(data[:8192]).upper()
    has_product = bool(_DAY_RE.search(head) or _PTSDY_RE.search(head))
    return has_product and "CONVECTIVE OUTLOOK" in head


def _iso(value: Optional[datetime]) -> str:
    return value.isoformat() + "Z" if value is not None else ""


def _parse_filename_time(label: str) -> Optional[datetime]:
    name = os.path.basename(label.split("?", 1)[0])
    match = _FILENAME_ISO_RE.search(name)
    if match:
        year, month, day, hour, minute = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError:
            return None
    match = _FILENAME_DTG_RE.search(name)
    if match:
        year, month, day, hour, minute = (int(part) for part in match.groups())
        try:
            return datetime(year, month, day, hour, minute)
        except ValueError:
            return None
    return None


def _parse_narrative_issue(text: str) -> Optional[datetime]:
    for line in text.splitlines():
        match = _ISSUE_RE.search(line)
        if not match:
            continue
        time_text, am_pm, zone, month_text, day_text, year_text = match.groups()
        month = _MONTHS.get(month_text.upper())
        if month is None:
            continue
        offset = _TZ_OFFSETS.get(zone.upper())
        if offset is None:
            continue
        if ":" in time_text:
            hour_text, minute_text = time_text.split(":", 1)
        else:
            hour_text, minute_text = time_text[:2], time_text[2:]
        hour = int(hour_text)
        minute = int(minute_text)
        if am_pm.upper() == "AM" and hour == 12:
            hour = 0
        elif am_pm.upper() == "PM" and hour < 12:
            hour += 12
        try:
            local = datetime(int(year_text), month, int(day_text), hour, minute)
        except ValueError:
            continue
        return local - timedelta(hours=offset)
    return None


def _month_candidates(ddhhmm: str, reference: datetime, hour: int, minute: int):
    day = int(ddhhmm[:2])
    for shift in (0, -1, 1, -2, 2):
        index = reference.month - 1 + shift
        year = reference.year + index // 12
        month = index % 12 + 1
        try:
            yield datetime(year, month, day, hour, minute)
        except ValueError:
            continue


def _closest_time(ddhhmm: str, reference: datetime) -> Optional[datetime]:
    hour, minute = int(ddhhmm[2:4]), int(ddhhmm[4:6])
    candidates = list(_month_candidates(ddhhmm, reference, hour, minute))
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs(value - reference))


def _following_time(
    ddhhmm: str, reference: datetime, begin: Optional[datetime]
) -> Optional[datetime]:
    hour, minute = int(ddhhmm[2:4]), int(ddhhmm[4:6])
    candidates = list(_month_candidates(ddhhmm, reference, hour, minute))
    if not candidates:
        return None
    if begin is not None:
        after = [value for value in candidates if value > begin]
        if after:
            return min(after)
    return min(candidates, key=lambda value: abs(value - reference))


def _parse_valid_times(
    text: str, reference: datetime
) -> Tuple[Optional[datetime], Optional[datetime]]:
    match = _VALID_RE.search(text)
    if not match:
        return None, None
    begin = _closest_time(match.group(1), reference)
    expire = _following_time(match.group(2), reference, begin)
    return begin, expire


def _parse_day(text: str) -> int:
    match = _DAY_RE.search(text) or _PTSDY_RE.search(text)
    if not match:
        raise GisError("not an SPC PTSDY1-3 areal-outline product")
    day = int(match.group(1))
    if day not in (1, 2, 3):
        raise GisError(f"unsupported outlook day in text product: {day}")
    return day


def _decode_point(token: str) -> Optional[LonLat]:
    if not _COORD_RE.fullmatch(token):
        return None
    lat = int(token[:4]) / 100.0
    lon_hundredths = int(token[4:])
    # SPC drops the leading 1 for longitudes from 100W through 130W.
    if 0 <= lon_hundredths < 1000:
        lon_hundredths += 10000
    lon = -lon_hundredths / 100.0
    if not (0.0 <= lat <= 90.0 and 180.0 >= -lon >= 0.0):
        return None
    return (lon, lat)


def _map_section(name: str) -> Optional[str]:
    upper = name.upper()
    if "TORNADO" in upper:
        return "torn"
    if "HAIL" in upper:
        return "hail"
    if "WIND" in upper:
        return "wind"
    if "CATEGORICAL" in upper:
        return "cat"
    if "SEVERE" in upper:
        return "prob"
    return None


def _is_record_label(token: str) -> bool:
    upper = token.upper()
    return bool(
        upper in _CATEGORIES
        or upper == "SIGN"
        or _PROB_LABEL_RE.fullmatch(token)
        or _CIG_LABEL_RE.fullmatch(token)
    )


def _target_layer(base: str, label: str) -> str:
    if _CIG_LABEL_RE.fullmatch(label):
        return "cigprob" if base == "prob" else f"cig{base}"
    return base


def parse_outlook_text(data: bytes, label: str = "<memory>") -> DataSource:
    """Parse a PTSDY1-3 text product into a :class:`DataSource`.

    The returned source exposes the product's sections as virtual layers named
    ``cat``, ``torn``, ``hail``, ``wind``, and/or ``prob``.  Sections that are
    present but empty yield an empty feature carrying the product times, so the
    normal pipeline can still write a legend-only Redbook product.
    """
    text = _decode(data)
    day = _parse_day(text)

    narrative_issue = _parse_narrative_issue(text)
    filename_time = _parse_filename_time(label)
    wmo_match = _WMO_RE.search(text)

    # The narrative issuance line is for human readers and is only approximate.
    # Use it for month/year context, but prefer the WMO DTG when present.
    reference = narrative_issue or filename_time or datetime.now().replace(
        second=0, microsecond=0
    )
    if wmo_match is not None:
        issue = _closest_time(wmo_match.group(1), reference)
    else:
        issue = narrative_issue or filename_time

    valid, expire = _parse_valid_times(text, issue or reference)

    common_attrs = {
        "ISSUE": _iso(issue),
        "VALID": _iso(valid),
        "EXPIRE": _iso(expire),
        "LABEL2": "",
        "DN": "",
    }
    source_name = f"{label}:text" if label else "text"
    features_by_layer: Dict[str, List[Feature]] = {}
    section_order: List[str] = []

    current_layer: Optional[str] = None
    current_label = ""
    current_rings: List[List[LonLat]] = []
    current_ring: List[LonLat] = []

    def finish_ring() -> None:
        nonlocal current_ring
        if len(current_ring) >= 3:
            current_rings.append(current_ring)
        current_ring = []

    def finish_record() -> None:
        nonlocal current_label, current_rings
        finish_ring()
        if current_label and current_rings and current_layer:
            upper = current_label.upper()
            dn = str(_DN_BY_LABEL.get(upper, ""))
            attrs = dict(common_attrs)
            attrs["LABEL"] = upper
            attrs["DN"] = dn
            layer = _target_layer(current_layer, upper)
            features_by_layer.setdefault(layer, []).append(
                Feature(
                    layer=layer,
                    rings=current_rings,
                    attrs=attrs,
                    source=source_name,
                )
            )
        current_label = ""
        current_rings = []

    def start_section(name: str) -> None:
        nonlocal current_layer
        finish_record()
        current_layer = _map_section(name)
        if current_layer:
            features_by_layer.setdefault(current_layer, [])
            if current_layer not in section_order:
                section_order.append(current_layer)

    def process_tokens(tokens: Sequence[str]) -> None:
        for token in tokens:
            if token == _SEPARATOR:
                finish_ring()
                continue
            point = _decode_point(token)
            if point is not None:
                current_ring.append(point)

    for raw_line in text.splitlines():
        line = raw_line.strip().upper()
        if not line:
            continue

        section = _SECTION_RE.fullmatch(line)
        if section:
            start_section(section.group(1))
            continue

        if line == "&&" or _POINTS_HEADER_RE.search(line):
            finish_record()
            continue

        if not current_layer:
            continue

        tokens = line.split()
        if not tokens:
            continue
        first = tokens[0]
        if _COORD_RE.fullmatch(first):
            process_tokens(tokens)
            continue
        if first == _SEPARATOR:
            process_tokens(tokens)
            continue
        if _is_record_label(first):
            finish_record()
            current_label = first
            process_tokens(tokens[1:])
            continue

        # Narrative text after the point list ends this section.
        finish_record()
        current_layer = None

    finish_record()

    # Keep empty sections as time-carrying features.  This matters for products
    # with no outlined areas, where a legend-only output is still required.
    for layer in section_order:
        if not any(not feature.empty for feature in features_by_layer.get(layer, [])):
            features_by_layer[layer].append(
                Feature(layer=layer, attrs=common_attrs, source=source_name)
            )

    name = os.path.basename(label.split("?", 1)[0]) or f"PTSDY{day}.txt"
    if not name.lower().endswith((".txt", ".text")):
        name += ".txt"

    metadata = {
        "day": str(day),
        "product": f"PTSDY{day}",
    }
    if wmo_match:
        metadata["wmo_header"] = wmo_match.group(0)
    metadata.update({key: value for key, value in common_attrs.items() if value})

    return DataSource(
        files={name: data},
        label=label,
        parsed_layers={key: value for key, value in features_by_layer.items() if value},
        metadata=metadata,
    )


__all__ = ["is_outlook_text", "parse_outlook_text"]
