"""Readers for SPC GIS input: shapefiles, zipped shapefiles and GeoJSON.

Only the Python standard library is used.  Shapefile support is deliberately
small but covers the pieces SPC publishes: Polygon (and PolygonZ) records with
one or more rings, plus DBF attributes.  Companion ``.shx`` and ``.prj`` files
are not needed.
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LonLat = Tuple[float, float]

_DBF_EXTENSION = ".dbf"

#: Logical layer names SPC uses.  A single file whose inferred layer is one of
#: these is treated as that specific layer; it is not reused for a different
#: requested layer.  This prevents a lone ``day1otlk_cat`` file from being
#: silently rendered as tornado/hail/wind.
_SPC_LAYERS = frozenset(
    {
        "cat",
        "categorical",
        "torn",
        "tornado",
        "hail",
        "wind",
        "prob",
        "probabilistic",
        "severe",
        "sig",
        "significant",
        "sigtorn",
        "sighail",
        "sigwind",
        "cigtorn",
        "cighail",
        "cigwind",
        "cigprob",
    }
)


class GisError(ValueError):
    """Raised when an input file cannot be understood."""


def _decode(raw: bytes) -> str:
    """Decode DBF bytes, preferring UTF-8 and falling back to Latin-1."""
    raw = raw.replace(b"\x00", b"")
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace").strip()


def _is_blank(value: str) -> bool:
    value = value.replace("\x00", "").strip()
    return not value or set(value) == {"*"}


def normalize_ring(
    points: Iterable[Sequence[float]],
) -> List[LonLat]:
    """Convert a sequence of coordinates to a clean lon/lat ring."""
    ring: List[LonLat] = []
    for point in points:
        if len(point) < 2:
            continue
        lon, lat = float(point[0]), float(point[1])
        if ring and abs(ring[-1][0] - lon) < 1e-9 and abs(ring[-1][1] - lat) < 1e-9:
            continue
        ring.append((lon, lat))
    return ring


@dataclass
class Feature:
    """One GIS record: one or more rings plus DBF/GeoJSON attributes."""

    layer: str
    rings: List[List[LonLat]] = field(default_factory=list)
    attrs: Dict[str, str] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        self.attrs = {
            str(k).upper(): "" if v is None else str(v).strip()
            for k, v in self.attrs.items()
        }
        self.rings = [
            ring for ring in (normalize_ring(r) for r in self.rings) if len(ring) >= 3
        ]

    @property
    def empty(self) -> bool:
        return not self.rings

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key.upper(), default)

    def all_points(self) -> Iterable[LonLat]:
        for ring in self.rings:
            for point in ring:
                yield point

    @property
    def label(self) -> str:
        return self.get("LABEL")

    @property
    def label2(self) -> str:
        return self.get("LABEL2")

    @property
    def significant(self) -> bool:
        upper = self.label.upper()
        layer = self.layer.lower()
        return (
            upper == "SIGN"
            or upper.startswith("CIG")
            or layer.startswith("sig")
            or layer.startswith("cig")
        )

    def geometry_signature(self) -> Tuple:
        """Stable signature used to de-duplicate merged layers."""
        return tuple(
            tuple((round(lon, 5), round(lat, 5)) for lon, lat in ring)
            for ring in self.rings
        )


@dataclass
class DataSource:
    """A collection of input files held in memory.

    ``files`` maps names (possibly including subdirectories) to raw bytes.
    ``label`` is only used for messages and error text.
    """

    files: Dict[str, bytes] = field(default_factory=dict)
    label: str = "<memory>"
    #: Virtual layers produced by a text product.  ``layer()`` returns copies
    #: so callers can safely retarget features when merging logical layers.
    parsed_layers: Dict[str, List[Feature]] = field(default_factory=dict)
    #: Optional provenance/metadata such as the outlook day and product times.
    metadata: Dict[str, str] = field(default_factory=dict)

    # -- basic access ----------------------------------------------------------
    def names(self, suffix: str = "") -> List[str]:
        suffix = suffix.lower()
        return sorted(
            name for name in self.files if name.lower().endswith(suffix)
        )

    def read(self, name: str) -> bytes:
        if name in self.files:
            return self.files[name]
        base = os.path.basename(name)
        for key, value in self.files.items():
            if os.path.basename(key) == base:
                return value
        raise KeyError(name)

    def _read_sibling(self, name: str, extension: str) -> Optional[bytes]:
        base = os.path.splitext(name)[0] + extension
        try:
            return self.read(base)
        except KeyError:
            return None

    # -- layer discovery -------------------------------------------------------
    @staticmethod
    def infer_layer(name: str) -> str:
        base = os.path.basename(name)
        low = base.lower()
        for ext in (".shp", ".geojson", ".json", ".dbf"):
            if low.endswith(ext):
                base = base[: -len(ext)]
                low = base.lower()
                break
        base = re.sub(r"\.(lyr|nolyr)$", "", base, flags=re.IGNORECASE)
        # SPC Day 4-8 GeoJSON uses names such as ``day4prob`` with no
        # underscore, so the generic suffix rule below cannot see the layer.
        if re.fullmatch(r"day[4-8]prob", base, flags=re.IGNORECASE):
            return "prob"
        if "_" in base:
            return base.rsplit("_", 1)[-1]
        return base

    @staticmethod
    def _preference(name: str) -> Tuple[int, int]:
        low = name.lower()
        penalty = 0
        if ".lyr." in low:
            penalty = 2
        elif ".nolyr." in low:
            penalty = 1
        return (penalty, len(name))

    def layer_names(self) -> List[str]:
        layers = set(self.parsed_layers)
        for name in self.files:
            low = name.lower()
            if low.endswith(".shp") or low.endswith(".geojson") or low.endswith(".json"):
                layers.add(self.infer_layer(name))
        return sorted(layers)

    @staticmethod
    def _clone_feature(feature: Feature, layer: Optional[str] = None) -> Feature:
        return Feature(
            layer=layer or feature.layer,
            rings=[list(ring) for ring in feature.rings],
            attrs=dict(feature.attrs),
            source=feature.source,
        )

    def _candidates(self, layer: str, extensions: Sequence[str]) -> List[str]:
        result = []
        for name in self.files:
            base = os.path.basename(name).lower()
            # SPC publishes e.g. ``day1otlk_cat.nolyr.geojson``; the
            # .lyr/.nolyr qualifier sits between the layer and the extension.
            normalized = re.sub(r"\.(?:lyr|nolyr)(?=\.[^.]+$)", "", base)
            matched = False
            for ext in extensions:
                if normalized.endswith("_" + layer.lower() + ext):
                    result.append(name)
                    matched = True
                    break
            if not matched and self.infer_layer(name).lower() == layer.lower():
                if any(normalized.endswith(ext) for ext in extensions):
                    result.append(name)
        return sorted(result, key=self._preference)

    def _single_shapefile(self) -> Optional[str]:
        shapes = self.names(".shp")
        if len(shapes) == 1:
            return shapes[0]
        return None

    def _single_geojson(self) -> Optional[str]:
        files = self.names(".geojson") + self.names(".json")
        if len(files) == 1:
            return files[0]
        return None

    def _can_fallback(self, layer: str) -> bool:
        """Whether a lone file may stand in for the requested layer.

        This exists so a single downloaded file with an arbitrary name can be
        converted without knowing its layer.  If the file's inferred name is a
        recognised SPC layer, it is only used for that layer.
        """
        names = {name.lower() for name in self.layer_names()}
        if layer.lower() in names:
            return False
        return not bool(names & _SPC_LAYERS)

    # -- loading ---------------------------------------------------------------
    def layer(self, layer: str) -> List[Feature]:
        """Load all features for ``layer``.

        A missing layer raises :class:`KeyError`.  If the source contains only
        one shapefile or GeoJSON file, that file is used as a fallback so a
        single downloaded layer can be converted without knowing its name.
        """
        if layer in self.parsed_layers:
            return [
                self._clone_feature(feature, layer)
                for feature in self.parsed_layers[layer]
            ]

        shapefiles = self._candidates(layer, (".shp",))
        if not shapefiles:
            single = self._single_shapefile()
            if single is not None and self._can_fallback(layer):
                shapefiles = [single]

        if shapefiles:
            shp_name = shapefiles[0]
            dbf = self._read_sibling(shp_name, _DBF_EXTENSION)
            features = parse_shapefile(
                self.read(shp_name),
                dbf,
                layer=layer,
                source=f"{self.label}:{shp_name}",
            )
            info = self._read_sibling(shp_name, ".info")
            info_times = parse_info_times(info) if info else {}
            if info_times:
                if features:
                    for feature in features:
                        if not (
                            feature.get("VALID") or feature.get("VALID_ISO")
                        ) and info_times.get("VALID_ISO"):
                            feature.attrs["VALID_ISO"] = info_times["VALID_ISO"]
                        if not (
                            feature.get("EXPIRE")
                            or feature.get("EXPIRE_ISO")
                        ) and info_times.get("EXPIRE_ISO"):
                            feature.attrs["EXPIRE_ISO"] = info_times[
                                "EXPIRE_ISO"
                            ]
                else:
                    # SPC's Day 4-8 "no areas" shapefiles contain a null
                    # record; keep the valid/expire times from the .info file.
                    features = [
                        Feature(
                            layer=layer,
                            rings=[],
                            attrs=dict(info_times),
                            source=f"{self.label}:{shp_name}",
                        )
                    ]
            return features

        geojson = self._candidates(layer, (".geojson", ".json"))
        if not geojson:
            single = self._single_geojson()
            if single is not None and self._can_fallback(layer):
                geojson = [single]
        if geojson:
            name = geojson[0]
            return parse_geojson(
                self.read(name), layer=layer, source=f"{self.label}:{name}"
            )

        physical = ", ".join(self.layer_names()) or "none"
        raise KeyError(
            f"layer {layer!r} not found in {self.label} "
            f"(available layers: {physical})"
        )

    def time_attributes(self, layer: str) -> Dict[str, str]:
        """Return attributes from the first record of a layer, even if empty."""
        try:
            features = self.layer(layer)
        except KeyError:
            return {}
        return dict(features[0].attrs) if features else {}


# ---------------------------------------------------------------------------
# Shapefile / DBF parsing
# ---------------------------------------------------------------------------


_INFO_TIME_RE = re.compile(
    r"Product\s+Valid\s+Time\s+(Begin|End)\s*:\s*(.+)", re.IGNORECASE
)


def parse_info_times(data: bytes) -> Dict[str, str]:
    """Extract valid/expire times from an SPC ``.info`` sidecar file.

    Day 4-8 shapefile archives include a small text file such as::

        Product Valid Time Begin: 2024-04-28 12:00:00+00:00
        Product Valid Time End: 2024-04-29 12:00:00+00:00

    These are returned as ``VALID_ISO``/``EXPIRE_ISO`` attributes so the
    normal time collection in :mod:`spc_redbook.outlook` can use them.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")

    attrs: Dict[str, str] = {}
    for line in text.splitlines():
        match = _INFO_TIME_RE.search(line)
        if not match:
            continue
        value = match.group(2).strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        key = "VALID_ISO" if match.group(1).lower() == "begin" else "EXPIRE_ISO"
        attrs[key] = parsed.isoformat()
    return attrs


def parse_dbf(data: bytes) -> List[Dict[str, str]]:
    """Parse a dBASE III/IV table into a list of attribute dictionaries."""
    if len(data) < 32:
        raise GisError("DBF file is too short")
    record_count, header_length, record_length = struct.unpack_from(
        "<IHH", data, 4
    )
    if header_length < 33 or record_length < 1:
        raise GisError("invalid DBF header")
    if header_length > len(data):
        raise GisError("truncated DBF header")

    fields: List[Tuple[str, str, int]] = []
    offset = 32
    while offset + 32 <= header_length and data[offset] != 0x0D:
        raw_name = data[offset : offset + 11]
        name = raw_name.split(b"\x00", 1)[0].decode("latin-1").strip().upper()
        field_type = chr(data[offset + 11])
        length = data[offset + 16]
        # decimal = data[offset + 17]
        fields.append((name, field_type, length))
        offset += 32

    records: List[Dict[str, str]] = []
    for index in range(record_count):
        start = header_length + index * record_length
        if start + record_length > len(data):
            break
        deleted = data[start : start + 1] == b"*"
        cursor = start + 1
        record: Dict[str, str] = {}
        for name, field_type, length in fields:
            raw = data[cursor : cursor + length]
            cursor += length
            value = _decode(raw)
            if field_type in "NF":
                value = value.replace("\x00", "").strip()
                if _is_blank(value):
                    value = ""
            record[name] = value
        if deleted:
            record["_DELETED"] = "1"
        records.append(record)
    return records


def parse_shp(data: bytes) -> List[List[List[LonLat]]]:
    """Parse a shapefile's records into lists of rings.

    Null records produce an empty list, so the result always has one entry per
    shapefile record and can be zipped with DBF records.
    """
    if len(data) < 100:
        raise GisError("SHP file is too short")
    file_code = struct.unpack_from(">i", data, 0)[0]
    if file_code != 9994:
        raise GisError("not a shapefile (bad file code)")

    result: List[List[List[LonLat]]] = []
    offset = 100
    while offset + 8 <= len(data):
        _record_number, content_words = struct.unpack_from(">ii", data, offset)
        offset += 8
        content_length = content_words * 2
        if content_length < 0 or offset + content_length > len(data):
            break
        content = data[offset : offset + content_length]
        offset += content_length
        if len(content) < 4:
            result.append([])
            continue
        shape_type = struct.unpack_from("<i", content, 0)[0]
        if shape_type == 0:  # Null shape
            result.append([])
            continue
        if shape_type not in (3, 5, 13, 15, 23, 25):
            # Point/MultiPoint records are not useful for outlook graphics.
            result.append([])
            continue
        if len(content) < 44:
            result.append([])
            continue
        num_parts, num_points = struct.unpack_from("<ii", content, 36)
        if num_parts < 0 or num_points < 0:
            result.append([])
            continue
        parts_offset = 44
        points_offset = parts_offset + 4 * num_parts
        if points_offset + 16 * num_points > len(content):
            result.append([])
            continue
        parts = list(
            struct.unpack_from("<%di" % num_parts, content, parts_offset)
        )
        coords = struct.unpack_from(
            "<%dd" % (2 * num_points), content, points_offset
        )
        points = [
            (coords[i], coords[i + 1]) for i in range(0, 2 * num_points, 2)
        ]

        rings: List[List[LonLat]] = []
        if not parts:
            if points:
                rings.append(points)
        else:
            parts = sorted(set(p for p in parts if 0 <= p <= num_points))
            for i, start in enumerate(parts):
                end = parts[i + 1] if i + 1 < len(parts) else num_points
                if end - start >= 3:
                    rings.append(points[start:end])
        result.append(rings)
    return result


def parse_shapefile(
    shp_data: bytes,
    dbf_data: Optional[bytes] = None,
    layer: str = "",
    source: str = "",
) -> List[Feature]:
    """Combine a SHP byte string and optional DBF byte string into features."""
    shapes = parse_shp(shp_data)
    records = parse_dbf(dbf_data) if dbf_data else []
    features: List[Feature] = []
    for index, rings in enumerate(shapes):
        attrs = records[index] if index < len(records) else {}
        if attrs.get("_DELETED") == "1":
            continue
        features.append(
            Feature(
                layer=layer or DataSource.infer_layer(source),
                rings=rings,
                attrs=attrs,
                source=source,
            )
        )
    return features


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


def _geojson_geometry_rings(geometry: dict) -> List[List[LonLat]]:
    if not geometry:
        return []
    kind = geometry.get("type", "")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [normalize_ring(ring) for ring in coords]
    if kind == "MultiPolygon":
        rings: List[List[LonLat]] = []
        for polygon in coords:
            for ring in polygon:
                rings.append(normalize_ring(ring))
        return rings
    if kind == "LineString":
        return [normalize_ring(coords)]
    if kind == "MultiLineString":
        return [normalize_ring(line) for line in coords]
    return []


def parse_geojson(
    data: bytes, layer: str = "", source: str = ""
) -> List[Feature]:
    """Parse SPC GeoJSON into :class:`Feature` objects."""
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GisError(f"could not parse GeoJSON: {exc}") from exc

    if document.get("type") == "FeatureCollection":
        raw_features = document.get("features") or []
    elif document.get("type") == "Feature":
        raw_features = [document]
    elif document.get("type") in ("Polygon", "MultiPolygon", "LineString"):
        raw_features = [{"type": "Feature", "geometry": document, "properties": {}}]
    else:
        raise GisError("GeoJSON is not a FeatureCollection or Feature")

    features: List[Feature] = []
    for item in raw_features:
        props = item.get("properties") or {}
        geometry = item.get("geometry") or {}
        rings = _geojson_geometry_rings(geometry)
        features.append(
            Feature(
                layer=layer or DataSource.infer_layer(source),
                rings=rings,
                attrs=props,
                source=source,
            )
        )
    return features


# ---------------------------------------------------------------------------
# Input source construction
# ---------------------------------------------------------------------------


def _fetch(url: str, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "spc-redbook/1.0 (+AWIPS Redbook converter)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _from_zip_bytes(data: bytes, label: str) -> DataSource:
    files: Dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            low = info.filename.lower()
            if low.endswith(
                (".shp", ".dbf", ".shx", ".prj", ".info", ".geojson", ".json")
            ):
                files[info.filename] = archive.read(info)
    if not files:
        raise GisError(f"no GIS files found in {label}")
    return DataSource(files=files, label=label)


def _from_directory(path: str) -> DataSource:
    files: Dict[str, bytes] = {}
    for root, _dirs, names in os.walk(path):
        for name in names:
            low = name.lower()
            if not low.endswith(
                (".shp", ".dbf", ".shx", ".prj", ".info", ".geojson", ".json")
            ):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, path)
            with open(full, "rb") as handle:
                files[rel] = handle.read()
    if not files:
        raise GisError(f"no GIS files found in directory {path}")
    return DataSource(files=files, label=path)


def _from_url(url: str) -> DataSource:
    data = _fetch(url)
    low = url.lower().split("?", 1)[0]
    if data[:4] == b"PK\x03\x04" or low.endswith(".zip"):
        return _from_zip_bytes(data, url)

    # Import lazily: the text parser imports DataSource from this module.
    from .text import is_outlook_text, parse_outlook_text

    if is_outlook_text(data):
        return parse_outlook_text(data, url)

    files: Dict[str, bytes] = {}
    name = os.path.basename(low) or "download"
    if low.endswith(".shp"):
        files[name] = data
        stem = os.path.splitext(name)[0]
        for extension in (".dbf", ".info"):
            try:
                files[stem + extension] = _fetch(url[:-4] + extension)
            except Exception:
                pass
    elif low.endswith((".geojson", ".json")) or data[:1] in (b"{", b"["):
        files[name + (".geojson" if not low.endswith((".geojson", ".json")) else "")] = data
    else:
        raise GisError(f"unsupported file type at {url}")
    return DataSource(files=files, label=url)


def load_source(source: str) -> DataSource:
    """Create a :class:`DataSource` from a path, zip, or HTTP(S) URL."""
    if not source:
        raise GisError("empty input path")
    if source.lower().startswith(("http://", "https://")):
        return _from_url(source)

    path = os.path.expanduser(source)
    if os.path.isdir(path):
        return _from_directory(path)
    if not os.path.exists(path):
        raise GisError(f"input does not exist: {source}")

    with open(path, "rb") as handle:
        data = handle.read()
    if data[:4] == b"PK\x03\x04" or path.lower().endswith(".zip"):
        return _from_zip_bytes(data, path)

    from .text import is_outlook_text, parse_outlook_text

    if is_outlook_text(data):
        return parse_outlook_text(data, path)

    files: Dict[str, bytes] = {}
    name = os.path.basename(path)
    low = path.lower()
    if low.endswith(".shp"):
        files[name] = data
        for extension in (".dbf", ".info"):
            sidecar = os.path.splitext(path)[0] + extension
            if os.path.exists(sidecar):
                with open(sidecar, "rb") as handle:
                    files[os.path.basename(sidecar)] = handle.read()
    elif low.endswith((".geojson", ".json")) or data[:1] in (b"{", b"["):
        files[name] = data
    else:
        raise GisError(f"unsupported input file: {source}")
    return DataSource(files=files, label=path)
