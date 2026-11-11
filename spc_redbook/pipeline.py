"""High-level pipeline: discover layers, build grids and emit Redbook files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .gis import DataSource, Feature
from .outlook import (
    OutlookArea,
    OutlookTimes,
    collect_times,
    hatch_ring,
    is_known_product,
    label_point,
    legend_lines,
    prepare_areas,
    product_id,
)
from .projection import GridSpec, NorthPolarStereo, build_grid
from .redbook import (
    TextItem,
    VectorRing,
    build_redbook,
    verify_redbook,
)

ALL_LAYERS: Dict[int, List[str]] = {
    1: ["cat", "torn", "hail", "wind"],
    2: ["cat", "prob"],
    3: ["cat", "prob"],
}

_LAYER_ALIASES = {
    "severe": "prob",
    "probabilistic": "prob",
    "categorical": "cat",
    "tornado": "torn",
    "significant": "sig",
}


@dataclass
class ProductSummary:
    layer: str
    wmo: str
    path: str
    bytes_written: int
    areas: int
    vector_rings: int
    texts: int
    grid: str
    skipped: bool = False


def normalize_layer(layer: str) -> str:
    lower = layer.strip().lower()
    return _LAYER_ALIASES.get(lower, lower)


def expand_layers(source: DataSource, day: int, spec: str) -> List[str]:
    """Expand ``all`` and aliases into a list of physical/logical layers."""
    requested: List[str] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        lower = token.lower()
        if lower == "all":
            requested.extend(ALL_LAYERS.get(day, ALL_LAYERS[1]))
        else:
            requested.append(normalize_layer(lower))
    # Remove duplicates while preserving order.
    result: List[str] = []
    for layer in requested:
        if layer not in result:
            result.append(layer)
    return result


def _empty_with_attrs(layer: str, attrs: Dict[str, str]) -> Feature:
    return Feature(layer=layer, rings=[], attrs=attrs)


def _first_time_attrs(source: DataSource, preferred: Sequence[str]) -> Dict[str, str]:
    for layer in preferred:
        try:
            features = source.layer(layer)
        except (KeyError, ValueError):
            continue
        for feature in features:
            if feature.attrs:
                return dict(feature.attrs)
    for layer in source.layer_names():
        try:
            features = source.layer(layer)
        except (KeyError, ValueError):
            continue
        for feature in features:
            if feature.attrs:
                return dict(feature.attrs)
    return {}


def _merge_layers(
    source: DataSource, target: str, layers: Sequence[str]
) -> List[Feature]:
    merged: List[Feature] = []
    seen_geometry = set()
    any_loaded = False
    for layer in layers:
        try:
            features = source.layer(layer)
        except (KeyError, ValueError):
            continue
        any_loaded = True
        for feature in features:
            if feature.empty:
                continue
            signature = feature.geometry_signature()
            if signature in seen_geometry:
                continue
            seen_geometry.add(signature)
            feature.layer = target
            merged.append(feature)
    if merged:
        return merged
    if any_loaded:
        # Preserve the timestamps from an empty but valid layer.
        attrs = _first_time_attrs(source, layers)
        return [_empty_with_attrs(target, attrs)]
    raise KeyError(
        f"none of the layers {', '.join(layers)} were found in {source.label}"
    )


def _significant_from(source: DataSource, target: str, layers: Sequence[str]) -> List[Feature]:
    merged: List[Feature] = []
    seen = set()
    for layer in layers:
        try:
            features = source.layer(layer)
        except (KeyError, ValueError):
            continue
        for feature in features:
            if feature.empty or not feature.significant:
                continue
            signature = feature.geometry_signature()
            if signature in seen:
                continue
            seen.add(signature)
            feature.layer = target
            merged.append(feature)
    if merged:
        return merged
    attrs = _first_time_attrs(source, layers)
    return [_empty_with_attrs(target, attrs)]


def load_layer_features(source: DataSource, layer: str) -> List[Feature]:
    """Resolve logical layer aliases and merged products to GIS features."""
    layer = normalize_layer(layer)

    if layer == "prob":
        try:
            return source.layer("prob")
        except (KeyError, ValueError):
            return _merge_layers(source, "prob", ("torn", "hail", "wind"))

    if layer == "sig":
        separate = [
            name
            for name in ("sigtorn", "sighail", "sigwind")
            if name in source.layer_names()
        ]
        if separate:
            return _merge_layers(source, "sig", separate)
        return _significant_from(source, "sig", ("torn", "hail", "wind"))

    if layer in ("sigtorn", "sighail", "sigwind"):
        try:
            return source.layer(layer)
        except (KeyError, ValueError):
            hazard = layer[3:]
            return _significant_from(source, layer, (hazard,))

    return source.layer(layer)


def _infer_times(
    features: Iterable[Feature],
    source: DataSource,
    layer: str,
    day: int,
) -> OutlookTimes:
    features = list(features)
    times = collect_times(features)
    if times.issue is None or times.valid is None or times.expire is None:
        fallback = collect_times([_empty_with_attrs(layer, _first_time_attrs(source, (layer,)))])
        times.issue = times.issue or fallback.issue
        times.valid = times.valid or fallback.valid
        times.expire = times.expire or fallback.expire

    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    if times.issue is None:
        times.issue = times.valid or now
    if times.valid is None:
        times.valid = times.issue
    if times.expire is None:
        times.expire = times.valid + timedelta(hours=24 * max(1, day))
    return times


def _all_lonlats(areas: Sequence[OutlookArea]) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for area in areas:
        for ring in area.rings:
            points.extend(ring)
    return points


def _area_rings(
    areas: Sequence[OutlookArea],
    grid: GridSpec,
    hatch: bool,
    hatch_spacing: float,
) -> List[VectorRing]:
    rings: List[VectorRing] = []
    for area in areas:
        for ring in area.rings:
            stored = [grid.to_stored(lon, lat) for lon, lat in ring]
            if len(stored) >= 2:
                rings.append(VectorRing(stored, closed=True))

        if hatch and area.significant:
            for ring in area.rings:
                for segment in hatch_ring(ring, grid, spacing=hatch_spacing):
                    stored = [
                        grid.stored_from_grid(gx, gy) for gx, gy in segment
                    ]
                    if len(stored) >= 2:
                        rings.append(VectorRing(stored, closed=False))
    return rings


def generate_product(
    source: DataSource,
    layer: str,
    day: int = 1,
    wmo: Optional[str] = None,
    width: int = 1024,
    height: int = 768,
    margin: float = 2.0,
    bounds: Optional[Sequence[float]] = None,
    central_lon: float = -95.0,
    lat_ts: float = 90.0,
    hatch: bool = True,
    hatch_spacing: float = 9.0,
    labels: bool = True,
    legend: bool = True,
    verify: bool = True,
) -> Tuple[bytes, ProductSummary]:
    """Build one Redbook product from a source, returning its bytes."""
    layer = normalize_layer(layer)
    features = load_layer_features(source, layer)
    times = _infer_times(features, source, layer, day)
    areas = prepare_areas(features, layer)

    wmo_id = wmo or product_id(day, layer)
    if wmo is None and not is_known_product(day, layer):
        # Keep going with the day's categorical ID, but make it visible to
        # callers/tests that this is not an official product mapping.
        wmo_id = product_id(day, "cat")

    projection = NorthPolarStereo(lat_ts=lat_ts, central_lon=central_lon)
    points = _all_lonlats(areas)
    grid = build_grid(
        points,
        width=width,
        height=height,
        margin_degrees=margin,
        bounds=bounds,
        projection=projection,
    )

    rings = _area_rings(areas, grid, hatch=hatch, hatch_spacing=hatch_spacing)

    texts: List[TextItem] = []
    if legend:
        lines = legend_lines(
            day=day,
            layer=layer,
            wmo=wmo_id,
            times=times,
            has_areas=bool(areas),
            has_significant=any(area.significant for area in areas),
        )
        for index, line in enumerate(lines):
            texts.append(TextItem(x=2, y=2 + index * 2, text=line))

    if labels:
        for area in areas:
            lon, lat = label_point(area)
            px, py = grid.to_stored(lon, lat)
            texts.append(TextItem(x=px, y=py, text=area.label))

    data = build_redbook(
        grid=grid,
        wmo=wmo_id,
        issue_time=times.issue,
        valid_time=times.valid,
        expire_time=times.expire,
        rings=rings,
        texts=texts,
    )

    if verify:
        verify_redbook(data)

    summary = ProductSummary(
        layer=layer,
        wmo=wmo_id,
        path="",
        bytes_written=len(data),
        areas=len(areas),
        vector_rings=len(rings),
        texts=len(texts),
        grid=f"{grid.m}x{grid.n}",
    )
    return data, summary


def output_filename(wmo: str, layer: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in layer)
    return f"{wmo}_{safe}.rbk"


def write_product(
    data: bytes,
    out: str,
    wmo: str,
    layer: str,
    single: bool,
) -> str:
    """Write one product.  ``out`` ending in .rbk is an explicit filename."""
    low = out.lower()
    if out == "-":
        import sys

        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return "-"

    if low.endswith((".rbk", ".redbook")):
        if not single:
            raise ValueError(
                "--out must be a directory when generating multiple layers"
            )
        path = os.path.expanduser(out)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    else:
        directory = os.path.expanduser(out)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, output_filename(wmo, layer))

    with open(path, "wb") as handle:
        handle.write(data)
    return path
