"""Spherical north-polar-stereographic projection used by AWIPS Redbook.

AWIPS renders a Redbook graphic by building a north polar stereographic
projected CRS from the four corners in the ``004_017`` projection block and
then laying an ``m`` by ``n`` grid over the projected envelope.  This module
implements the same projection and the grid/index conventions used by the
CAVE renderer so that generated vector coordinates land in the right place.

All longitudes in this module are *east positive* (the normal GIS
convention).  The Redbook projection block itself stores west-positive
hundredths of a degree; that conversion happens in :mod:`spc_redbook.redbook`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

#: Earth radius used by AWIPS' MapUtil.AWIPS_EARTH_RADIUS (metres).
AWIPS_EARTH_RADIUS = 6371200.0

LonLat = Tuple[float, float]
Point = Tuple[float, float]


def normalize_lon(lon: float) -> float:
    """Normalize a longitude to the range (-180, 180]."""
    lon = math.fmod(lon, 360.0)
    if lon <= -180.0:
        lon += 360.0
    elif lon > 180.0:
        lon -= 360.0
    return lon


def _clamp(value: int, low: int, high: int) -> int:
    return low if value < low else high if value > high else value


class NorthPolarStereo:
    """Spherical north polar stereographic projection.

    Parameters mirror the arguments AWIPS passes to
    ``MapUtil.constructNorthPolarStereo(radius, radius, lat_ts, lon_0)``.
    """

    def __init__(
        self,
        radius: float = AWIPS_EARTH_RADIUS,
        lat_ts: float = 90.0,
        central_lon: float = -95.0,
    ) -> None:
        if radius <= 0:
            raise ValueError("radius must be positive")
        if not -90.0 < lat_ts <= 90.0:
            raise ValueError("lat_ts must be in (-90, 90]")
        self.radius = float(radius)
        self.lat_ts = float(lat_ts)
        self.central_lon = normalize_lon(float(central_lon))
        # Scale factor at the pole for a latitude of true scale.
        self.k0 = (1.0 + math.sin(math.radians(self.lat_ts))) / 2.0

    def forward(self, lon: float, lat: float) -> Point:
        """Project east-positive lon/lat (degrees) to x/y metres."""
        if lat <= -90.0:
            raise ValueError("latitude must be greater than -90 degrees")
        lat = min(lat, 90.0)
        rho = (
            2.0
            * self.radius
            * self.k0
            * math.tan(math.pi / 4.0 - math.radians(lat) / 2.0)
        )
        dlon = math.radians(normalize_lon(lon - self.central_lon))
        return (rho * math.sin(dlon), -rho * math.cos(dlon))

    def inverse(self, x: float, y: float) -> LonLat:
        """Inverse-project x/y metres to east-positive lon/lat (degrees)."""
        rho = math.hypot(x, y)
        if rho == 0.0:
            return (self.central_lon, 90.0)
        lat = math.pi / 2.0 - 2.0 * math.atan(
            rho / (2.0 * self.radius * self.k0)
        )
        lon = math.radians(self.central_lon) + math.atan2(x, -y)
        return (normalize_lon(math.degrees(lon)), math.degrees(lat))


@dataclass
class GridSpec:
    """An ``m`` by ``n`` AWIPS Redbook grid.

    ``minx``/``maxx``/``miny``/``maxy`` describe the projected envelope that
    AWIPS passes to ``GeneralGridEnvelope([0,0],[m,n],false)``.  Grid
    coordinates used by Redbook blocks are cell-centred: the centre of the
    first column is ``gx=0`` and the centre of the last column is
    ``gx=m-1``.

    Stored Redbook ``posY`` values count **upward from the bottom** of the
    image (GEMPAK device coordinates), with ``posY=1`` at the centre of the
    bottom row.  CAVE converts a stored value to its top-down grid row with
    ``row = n - posY`` before applying the projection.  Getting this
    backwards mirrors every product vertically.
    """

    m: int
    n: int
    minx: float
    maxx: float
    miny: float
    maxy: float
    projection: NorthPolarStereo = field(
        default_factory=NorthPolarStereo
    )

    def __post_init__(self) -> None:
        if self.m < 1 or self.n < 1:
            raise ValueError("grid dimensions must be positive")
        if self.maxx <= self.minx or self.maxy <= self.miny:
            raise ValueError("invalid projected envelope")

    # -- coordinate conversion -------------------------------------------------
    def to_grid(self, lon: float, lat: float) -> Point:
        """Return continuous, cell-centred grid coordinates (x right, y up)."""
        x, y = self.projection.forward(lon, lat)
        gx = (x - self.minx) / (self.maxx - self.minx) * self.m - 0.5
        gy = (y - self.miny) / (self.maxy - self.miny) * self.n - 0.5
        return (gx, gy)

    def stored_from_grid(self, gx: float, gy: float) -> Tuple[int, int]:
        """Convert continuous cell-centred grid coordinates to stored ones.

        ``gy`` is the zero-based bottom-up row index used internally.  Redbook
        stores y one higher, so the bottom row centre is ``posY=1``.  This is
        the same convention GEMPAK's RBK driver uses, and CAVE undoes it with
        ``row = n - posY``.
        """
        px = _clamp(int(round(gx)), 0, self.m - 1)
        py = _clamp(int(round(gy)) + 1, 0, self.n)
        return (px, py)

    def to_stored(self, lon: float, lat: float) -> Tuple[int, int]:
        """Return integer Redbook ``(posX, posY)`` (posY is bottom-up)."""
        gx, gy = self.to_grid(lon, lat)
        return self.stored_from_grid(gx, gy)

    def stored_to_continuous(self, px: int, py: int) -> Point:
        """Convert stored Redbook coordinates back to cell-centred x/y-up."""
        return (float(px), float(py - 1))

    def corners(self) -> dict:
        """Return corner lon/lat values for the projection block."""
        ul = self.projection.inverse(self.minx, self.maxy)
        ur = self.projection.inverse(self.maxx, self.maxy)
        lr = self.projection.inverse(self.maxx, self.miny)
        ll = self.projection.inverse(self.minx, self.miny)
        return {"ul": ul, "ur": ur, "lr": lr, "ll": ll}

    def describe(self) -> str:
        c = self.corners()
        return (
            f"{self.m}x{self.n} "
            f"LL={c['ll'][1]:.2f},{c['ll'][0]:.2f} "
            f"UR={c['ur'][1]:.2f},{c['ur'][0]:.2f}"
        )


def _iter_bounds_points(
    west: float, south: float, east: float, north: float, samples: int = 33
) -> Iterable[LonLat]:
    """Sample the edges of a lon/lat bounding box for robust projection."""
    for i in range(samples):
        t = i / float(samples - 1)
        lon = west + (east - west) * t
        yield (lon, south)
        yield (lon, north)
    for i in range(samples):
        t = i / float(samples - 1)
        lat = south + (north - south) * t
        yield (west, lat)
        yield (east, lat)


def build_grid(
    lonlats: Iterable[LonLat],
    width: int = 1024,
    height: int = 768,
    margin_degrees: float = 2.0,
    bounds: Optional[Sequence[float]] = None,
    projection: Optional[NorthPolarStereo] = None,
    min_span_m: float = 250_000.0,
) -> GridSpec:
    """Build a Redbook grid covering all supplied lon/lat points.

    ``bounds`` is an optional ``(west, south, east, north)`` tuple in degrees.
    ``margin_degrees`` expands the projected envelope by roughly that many
    degrees of great-circle distance.  The envelope is then adjusted so its
    aspect ratio matches ``width:height``; this avoids stretching the map when
    the projection block and the plot-space block are combined by CAVE.
    """
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")
    if width > 4096 or height > 4096:
        raise ValueError("width and height must be <= 4096")

    proj = projection or NorthPolarStereo()

    if bounds is not None:
        if len(bounds) != 4:
            raise ValueError("bounds must be west,south,east,north")
        west, south, east, north = (float(v) for v in bounds)
        if south >= north:
            raise ValueError("bounds south must be less than north")
        points: List[LonLat] = list(
            _iter_bounds_points(west, south, east, north)
        )
    else:
        points = list(lonlats)
        if not points:
            # A sensible CONUS default for an empty outlook layer.
            points = [(lon, lat) for lon in (-125.0, -66.5) for lat in (24.0, 50.0)]

    projected: List[Point] = []
    for lon, lat in points:
        # Clamp extreme southern latitudes to keep the spherical formulas
        # well-behaved; SPC outlooks never approach the south pole.
        projected.append(proj.forward(lon, max(lat, -85.0)))

    minx = min(p[0] for p in projected)
    maxx = max(p[0] for p in projected)
    miny = min(p[1] for p in projected)
    maxy = max(p[1] for p in projected)

    margin_m = max(0.0, margin_degrees) * math.radians(1.0) * proj.radius
    minx -= margin_m
    maxx += margin_m
    miny -= margin_m
    maxy += margin_m

    # Avoid an absurdly tiny grid for a point or a very small polygon.
    if maxx - minx < min_span_m:
        cx = (minx + maxx) / 2.0
        minx, maxx = cx - min_span_m / 2.0, cx + min_span_m / 2.0
    if maxy - miny < min_span_m:
        cy = (miny + maxy) / 2.0
        miny, maxy = cy - min_span_m / 2.0, cy + min_span_m / 2.0

    # Match the requested pixel aspect ratio so CAVE does not stretch shapes.
    target_ratio = width / float(height)
    w = maxx - minx
    h = maxy - miny
    if w / h < target_ratio:
        new_w = h * target_ratio
        cx = (minx + maxx) / 2.0
        minx, maxx = cx - new_w / 2.0, cx + new_w / 2.0
    else:
        new_h = w / target_ratio
        cy = (miny + maxy) / 2.0
        miny, maxy = cy - new_h / 2.0, cy + new_h / 2.0

    return GridSpec(
        m=int(width),
        n=int(height),
        minx=minx,
        maxx=maxx,
        miny=miny,
        maxy=maxy,
        projection=proj,
    )
