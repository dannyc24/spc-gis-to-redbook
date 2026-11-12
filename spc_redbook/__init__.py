"""Convert SPC convective outlook GIS data to AWIPS Redbook graphics.

The package is intentionally dependency free: it only uses the Python
standard library so that it can be run directly on an AWIPS/EDEX host where
installing extra wheels is inconvenient.

Typical use from Python::

    from spc_redbook import load_source, prepare_areas

    source = load_source("day1otlk-shp.zip")
    areas = prepare_areas(source.layer("cat"), "cat")

or from the command line::

    python -m spc_redbook day1otlk-shp.zip --layer all --out out
"""

from __future__ import annotations

__version__ = "1.1.0"

from .gis import DataSource, Feature, load_source  # noqa: E402
from .outlook import OutlookArea, prepare_areas  # noqa: E402
from .projection import GridSpec, NorthPolarStereo  # noqa: E402
from .redbook import (  # noqa: E402
    RedbookDocument,
    build_redbook,
    parse_redbook,
    verify_redbook,
)

__all__ = [
    "DataSource",
    "Feature",
    "GridSpec",
    "NorthPolarStereo",
    "OutlookArea",
    "RedbookDocument",
    "__version__",
    "build_redbook",
    "load_source",
    "parse_redbook",
    "prepare_areas",
    "verify_redbook",
]
