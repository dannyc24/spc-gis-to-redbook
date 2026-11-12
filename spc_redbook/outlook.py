"""Outlook-specific processing: labels, ordering, hatch generation, legend.

The AWIPS CAVE Redbook renderer draws every vector and text block in the
single colour configured on the resource.  It ignores fill colour and fill
pattern blocks.  The useful way to represent SPC outlooks is therefore a
monochrome map with outlines, short labels and diagonal hatching for
significant-severe areas.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .gis import Feature
from .projection import GridSpec, LonLat

CATEGORY_NAMES: Dict[int, str] = {
    2: "TSTM",
    3: "MRGL",
    4: "SLGT",
    5: "ENH",
    6: "MDT",
    8: "HIGH",
}
CATEGORY_RANK: Dict[str, float] = {
    "TSTM": 2,
    "MRGL": 3,
    "SLGT": 4,
    "ENH": 5,
    "MDT": 6,
    "HIGH": 8,
}

#: AWIPS/WMO IDs for the official Redbook products per outlook day.
PRODUCT_IDS: Dict[int, Dict[str, str]] = {
    1: {
        "cat": "PGWE46",
        "torn": "PGNE00",
        "hail": "PENE00",
        "wind": "PWNE00",
        "sigtorn": "PGNE00",
        "sighail": "PENE00",
        "sigwind": "PWNE00",
        "sig": "PGNE00",
    },
    2: {
        "cat": "PGWI47",
        "torn": "PGNE02",
        "hail": "PENE02",
        "wind": "PWNE02",
        "prob": "PGNI00",
        "severe": "PGNI00",
        "sig": "PGNI00",
    },
    3: {
        "cat": "PGWK48",
        "prob": "PZNK00",
        "severe": "PZNK00",
        "sig": "PZNK00",
    },
    4: {"prob": "PGNW49"},
    5: {"prob": "PGNW50"},
    6: {"prob": "PGNW51"},
    7: {"prob": "PGNW52"},
    8: {"prob": "PGNW53"},
}

LAYER_TITLES: Dict[str, str] = {
    "cat": "CATEGORICAL",
    "torn": "TORNADO",
    "hail": "HAIL",
    "wind": "WIND",
    "prob": "ALL SEVERE",
    "severe": "ALL SEVERE",
    "sig": "SIGNIFICANT SEVERE AREAS",
    "sigtorn": "SIGNIFICANT TORNADO",
    "sighail": "SIGNIFICANT HAIL",
    "sigwind": "SIGNIFICANT WIND",
    "cigprob": "CIG ALL SEVERE",
    "cigtorn": "CIG TORNADO",
    "cighail": "CIG HAIL",
    "cigwind": "CIG WIND",
}

_PROB_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


class OutlookError(ValueError):
    """Raised when outlook data cannot be interpreted."""


@dataclass
class OutlookTimes:
    issue: Optional[datetime] = None
    valid: Optional[datetime] = None
    expire: Optional[datetime] = None


@dataclass
class OutlookArea:
    feature: Feature
    label: str
    rank: float
    significant: bool
    rings: List[List[LonLat]]


def parse_datetime(value: str) -> Optional[datetime]:
    """Parse the date/time strings found in SPC DBF and GeoJSON attributes."""
    if not value:
        return None
    text = value.replace("\x00", "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass

    digits = re.sub(r"\D", "", text)
    formats = {
        14: "%Y%m%d%H%M%S",
        12: "%Y%m%d%H%M",
        10: "%Y%m%d%H",
        8: "%Y%m%d",
    }
    fmt = formats.get(len(digits))
    if fmt:
        try:
            return datetime.strptime(digits, fmt)
        except ValueError:
            return None
    return None


def collect_times(features: Iterable[Feature]) -> OutlookTimes:
    """Extract issue/valid/expire times from the first record that has them."""
    times = OutlookTimes()
    for feature in features:
        if times.issue is None:
            for key in ("ISSUE_ISO", "ISSUE"):
                times.issue = parse_datetime(feature.get(key))
                if times.issue:
                    break
        if times.valid is None:
            for key in ("VALID_ISO", "VALID"):
                times.valid = parse_datetime(feature.get(key))
                if times.valid:
                    break
        if times.expire is None:
            for key in ("EXPIRE_ISO", "EXPIRE"):
                times.expire = parse_datetime(feature.get(key))
                if times.expire:
                    break
    return times


def _dn(feature: Feature) -> Optional[int]:
    value = feature.get("DN")
    try:
        if value and value.strip():
            return int(float(value))
    except ValueError:
        pass
    return None


def _probability(value: str) -> Optional[float]:
    value = value.replace("\x00", "").strip()
    if not value or value.upper() == "SIGN":
        return None
    if value.startswith("Less Than"):
        return None
    match = _PROB_RE.search(value)
    if match:
        return float(match.group(1))
    try:
        number = float(value)
    except ValueError:
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return number


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _is_placeholder(label: str) -> bool:
    low = label.strip().lower()
    return low.startswith("less than") or low.startswith("predictability too low")


def _is_no_area(feature: Feature) -> bool:
    """Return True for SPC's placeholder records on days with no outlook area.

    Day 4-8 shapefiles always contain one tiny placeholder polygon near Cape
    Cod with ``DN=0`` and no label when nothing is forecast.  The GeoJSON form
    uses an empty geometry and ``LABEL=Predictability Too Low``.  Neither
    should be drawn or labelled.
    """
    label = feature.get("LABEL").strip()
    if label.lower().startswith("predictability too low"):
        return True
    return not label and _dn(feature) == 0


def _label_for(feature: Feature, layer: str) -> Tuple[str, float, bool]:
    raw = feature.get("LABEL")
    label2 = feature.get("LABEL2")
    raw_upper = raw.upper()
    dn = _dn(feature)
    layer_lower = layer.lower()
    significant = raw_upper == "SIGN" or layer_lower.startswith("sig")

    if layer_lower == "cat" or raw_upper in CATEGORY_RANK:
        name = raw_upper if raw_upper in CATEGORY_RANK else CATEGORY_NAMES.get(dn or -1)
        if not name:
            name = "TSTM" if dn is not None and dn <= 2 else "?"
        return (name, CATEGORY_RANK.get(name, float(dn or 0)), False)

    if significant:
        return ("SIG", 1000.0, True)

    probability = _probability(raw)
    if probability is None:
        probability = _probability(label2)
    if probability is not None:
        return (_format_percent(probability), probability, False)

    if _is_placeholder(raw):
        return ("LT", 0.0, False)
    if raw:
        return (raw[:12], 0.0, False)
    if dn is not None:
        return (_format_percent(float(dn)), float(dn), False)
    return ("?", 0.0, False)


def prepare_areas(features: Iterable[Feature], layer: str) -> List[OutlookArea]:
    """Normalise, label and order the features of one outlook layer.

    Lower-ranked categorical and probability areas are drawn first so higher
    risk outlines and labels appear on top.  Significant areas are drawn last.
    """
    areas: List[OutlookArea] = []
    seen = set()
    for feature in features:
        if feature.empty or _is_no_area(feature):
            continue
        label, rank, significant = _label_for(feature, layer)
        signature = (label, feature.geometry_signature())
        if signature in seen:
            continue
        seen.add(signature)
        areas.append(
            OutlookArea(
                feature=feature,
                label=label,
                rank=rank,
                significant=significant,
                rings=feature.rings,
            )
        )
    areas.sort(key=lambda area: (area.rank, area.label, area.significant))
    return areas


# ---------------------------------------------------------------------------
# Label placement
# ---------------------------------------------------------------------------


def ring_area(ring: Sequence[LonLat]) -> float:
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def ring_centroid(ring: Sequence[LonLat]) -> LonLat:
    area = ring_area(ring)
    if abs(area) < 1e-12:
        lon = sum(p[0] for p in ring) / len(ring)
        lat = sum(p[1] for p in ring) / len(ring)
        return (lon, lat)
    cx = cy = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * area), cy / (6.0 * area))


def point_in_ring(lon: float, lat: float, ring: Sequence[LonLat]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            denominator = yj - yi
            if denominator == 0:
                denominator = 1e-12
            x_cross = (xj - xi) * (lat - yi) / denominator + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _widest_span_at(ring: Sequence[LonLat], lat: float) -> Optional[LonLat]:
    intersections: List[float] = []
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        if (y1 > lat) == (y2 > lat):
            continue
        if y2 == y1:
            continue
        x = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
        intersections.append(x)
    intersections.sort()
    best: Optional[LonLat] = None
    best_width = -1.0
    for i in range(0, len(intersections) - 1, 2):
        width = intersections[i + 1] - intersections[i]
        if width > best_width:
            best_width = width
            best = ((intersections[i] + intersections[i + 1]) / 2.0, lat)
    return best


def interior_point(ring: Sequence[LonLat]) -> LonLat:
    """Return a point reasonably guaranteed to lie inside a polygon ring."""
    centroid = ring_centroid(ring)
    if point_in_ring(centroid[0], centroid[1], ring):
        return centroid

    min_lat = min(p[1] for p in ring)
    max_lat = max(p[1] for p in ring)
    for fraction in (0.5, 0.35, 0.65, 0.2, 0.8):
        lat = min_lat + (max_lat - min_lat) * fraction
        candidate = _widest_span_at(ring, lat)
        if candidate and point_in_ring(candidate[0], candidate[1], ring):
            return candidate

    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    return (lon, lat)


def label_point(area: OutlookArea) -> LonLat:
    ring = max(area.rings, key=lambda candidate: abs(ring_area(candidate)))
    return interior_point(ring)


# ---------------------------------------------------------------------------
# Hatching
# ---------------------------------------------------------------------------


def hatch_ring(
    ring: Sequence[LonLat], grid: GridSpec, spacing: float = 9.0
) -> List[List[Tuple[float, float]]]:
    """Return diagonal hatch segments for a polygon ring in grid space.

    Lines satisfy ``x - y = c``.  Intersections with polygon edges are sorted
    along each line and paired using the even-odd rule, which also works for
    concave SPC polygons.
    """
    if len(ring) < 3 or spacing <= 0:
        return []
    points = [grid.to_grid(lon, lat) for lon, lat in ring]
    if points[0] != points[-1]:
        points.append(points[0])

    offsets = [x - y for x, y in points]
    c_min = min(offsets)
    c_max = max(offsets)
    if c_max <= c_min:
        return []

    segments: List[List[Tuple[float, float]]] = []
    start = math.ceil(c_min / spacing) * spacing
    c = start
    while c <= c_max:
        intersections: List[Tuple[float, float]] = []
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            denominator = dx - dy
            if abs(denominator) < 1e-12:
                continue
            t = (c - (x1 - y1)) / denominator
            if -1e-9 <= t <= 1.0 + 1e-9:
                x = x1 + t * dx
                y = y1 + t * dy
                intersections.append((x, y))
        intersections.sort(key=lambda p: p[0] + p[1])
        for i in range(0, len(intersections) - 1, 2):
            p1 = intersections[i]
            p2 = intersections[i + 1]
            if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) >= 0.5:
                segments.append([p1, p2])
        c += spacing
    return segments


# ---------------------------------------------------------------------------
# Titles, product IDs and legend
# ---------------------------------------------------------------------------


def outlook_title(day: int, layer: str) -> str:
    if day >= 4:
        return f"SPC DAY {day} SEVERE OUTLOOK"
    title = LAYER_TITLES.get(layer.lower(), layer.upper())
    if title.endswith("AREAS"):
        return f"SPC DAY {day} {title}"
    return f"SPC DAY {day} {title} OUTLOOK"


def product_id(day: int, layer: str) -> str:
    """Return the AWIPS/WMO Redbook ID for a day/layer.

    Unknown layers fall back to the day's categorical product ID, or to the
    probabilistic product on days that only have a probabilistic outlook.
    """
    day_map = PRODUCT_IDS.get(day, PRODUCT_IDS[1])
    key = layer.lower()
    if key in day_map:
        return day_map[key]
    for fallback in ("cat", "prob"):
        if fallback in day_map:
            return day_map[fallback]
    return next(iter(day_map.values()))


def is_known_product(day: int, layer: str) -> bool:
    return layer.lower() in PRODUCT_IDS.get(day, {})


def _format_time(value: Optional[datetime]) -> str:
    if value is None:
        return "UNKNOWN"
    return value.strftime("%H%MZ %d %b %Y").upper()


def legend_lines(
    day: int,
    layer: str,
    wmo: str,
    times: OutlookTimes,
    has_areas: bool,
    has_significant: bool,
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    lines = [outlook_title(day, layer)]
    lines.append(f"ISSUED {_format_time(times.issue)}")
    lines.append(f"VALID {_format_time(times.valid)}")
    if times.expire is not None:
        lines.append(f"THRU {_format_time(times.expire)}")
    lines.append(f"AWIPS ID {wmo}")
    if has_significant:
        lines.append("SIGNIFICANT AREAS HATCHED")
    if not has_areas:
        lines.append("NO AREAS FORECAST")
    if extra:
        lines.extend(extra)
    return lines
