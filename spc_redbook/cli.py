"""Command-line interface for the SPC -> AWIPS Redbook converter."""

from __future__ import annotations

import argparse
import re
import sys
from typing import List, Optional, Sequence

from . import __version__
from .gis import DataSource, GisError, load_source
from .outlook import is_known_product, product_id
from .pipeline import (
    ProductSummary,
    expand_layers,
    generate_product,
    normalize_layer,
    write_product,
)
from .redbook import RedbookError


def _parse_bounds(value: Optional[str]) -> Optional[List[float]]:
    if not value:
        return None
    parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--bounds needs four values: west,south,east,north"
        )
    try:
        bounds = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --bounds: {exc}") from exc
    west, south, east, north = bounds
    if west >= east:
        raise argparse.ArgumentTypeError("--bounds west must be less than east")
    if south >= north:
        raise argparse.ArgumentTypeError("--bounds south must be less than north")
    return bounds


def _detect_day(source: DataSource, explicit: Optional[int]) -> int:
    if explicit in (1, 2, 3):
        return explicit
    haystack = " ".join([source.label] + list(source.files))
    match = re.search(r"day\s*([123])", haystack, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spc-redbook",
        description=(
            "Convert SPC convective outlook shapefiles/GeoJSON into AWIPS "
            "Redbook graphics that EDEX can ingest."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input",
        help=(
            "SPC zip, shapefile, GeoJSON, directory, or HTTP(S) URL. "
            "The zip may contain all outlook layers for one issuance."
        ),
    )
    parser.add_argument(
        "-l",
        "--layer",
        default="cat",
        help=(
            "Layer(s) to render: cat, torn, hail, wind, prob/severe, sig, "
            "sigtorn, sighail, sigwind, cigtorn, ...; comma-separated, or 'all'."
        ),
    )
    parser.add_argument(
        "-o",
        "--out",
        default=".",
        help=(
            "Output directory (created if needed), an explicit .rbk/.redbook "
            "filename for a single layer, or '-' for stdout."
        ),
    )
    parser.add_argument(
        "--day",
        type=int,
        choices=(1, 2, 3),
        help="Outlook day; detected from day1/day2/day3 in the input name.",
    )
    parser.add_argument(
        "--wmo",
        help=(
            "Override the six-character WMO/AWIPS ID. Only valid when "
            "generating one layer."
        ),
    )
    parser.add_argument(
        "--width", type=int, default=1024, help="Redbook plot-space width"
    )
    parser.add_argument(
        "--height", type=int, default=768, help="Redbook plot-space height"
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=2.0,
        help="Extra map margin around the data, in degrees",
    )
    parser.add_argument(
        "--bounds",
        help="Fixed map bounds west,south,east,north (degrees) instead of data bounds",
    )
    parser.add_argument(
        "--central-lon",
        type=float,
        default=-95.0,
        help="Central meridian for north polar stereographic projection",
    )
    parser.add_argument(
        "--lat-ts",
        type=float,
        default=90.0,
        help="Latitude of true scale for the north polar stereographic projection",
    )
    parser.add_argument(
        "--hatch-spacing",
        type=float,
        default=9.0,
        help="Spacing of significant-area hatch lines, in grid pixels",
    )
    parser.add_argument(
        "--no-hatch",
        action="store_true",
        help="Do not hatch significant-severe areas",
    )
    parser.add_argument(
        "--no-labels", action="store_true", help="Do not plot area labels"
    )
    parser.add_argument(
        "--no-legend", action="store_true", help="Do not write the text legend"
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the built-in encoder/decoder round-trip check",
    )
    parser.add_argument(
        "--list-layers",
        action="store_true",
        help="List the physical layers found in the input and exit",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def _print_summary(summary: ProductSummary) -> None:
    print(
        f"{summary.wmo:6s}  {summary.layer:9s} "
        f"{summary.areas:3d} areas  "
        f"{summary.vector_rings:5d} vector rings  "
        f"{summary.texts:3d} text  "
        f"{summary.grid:9s}  "
        f"{summary.bytes_written:7d} bytes  "
        f"{summary.path}"
    )


def _normalize_argv(argv: Optional[Sequence[str]]) -> List[str]:
    """Allow comma-separated bounds whose first value is negative.

    ``argparse`` treats ``--bounds -130,20,-60,55`` as an option because the
    value starts with a dash.  Joining it to ``--bounds=...`` avoids that.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    normalized: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--bounds" and index + 1 < len(tokens):
            value = tokens[index + 1]
            if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[,\s]+-?\d+(?:\.\d+)?){3}", value):
                normalized.append("--bounds=" + value)
                index += 2
                continue
        normalized.append(token)
        index += 1
    return normalized


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(_normalize_argv(argv))
    try:
        bounds = _parse_bounds(args.bounds)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    try:
        source = load_source(args.input)
    except (GisError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_layers:
        for layer in source.layer_names():
            print(layer)
        return 0

    day = _detect_day(source, args.day)
    layers = expand_layers(source, day, args.layer)
    if not layers:
        print("error: no layers selected", file=sys.stderr)
        return 2

    if args.wmo and len(layers) != 1:
        print("error: --wmo can only be used with a single layer", file=sys.stderr)
        return 2

    summaries: List[ProductSummary] = []
    for layer in layers:
        layer = normalize_layer(layer)
        if args.wmo is None and not args.quiet and not is_known_product(day, layer):
            fallback = product_id(day, layer)
            print(
                f"warning: no official product ID for day {day} layer "
                f"{layer!r}; using {fallback}. Use --wmo to override.",
                file=sys.stderr,
            )
        try:
            data, summary = generate_product(
                source=source,
                layer=layer,
                day=day,
                wmo=args.wmo,
                width=args.width,
                height=args.height,
                margin=args.margin,
                bounds=bounds,
                central_lon=args.central_lon,
                lat_ts=args.lat_ts,
                hatch=not args.no_hatch,
                hatch_spacing=args.hatch_spacing,
                labels=not args.no_labels,
                legend=not args.no_legend,
                verify=not args.no_verify,
            )
            summary.path = write_product(
                data, args.out, summary.wmo, layer, single=len(layers) == 1
            )
        except (KeyError, GisError, RedbookError, ValueError, OSError) as exc:
            print(f"error: layer {layer}: {exc}", file=sys.stderr)
            return 2
        if summary.areas == 0 and not args.quiet:
            print(
                f"warning: layer {layer!r} has no polygon geometry; "
                "wrote an empty product with a legend.",
                file=sys.stderr,
            )
        summaries.append(summary)

    if not args.quiet and args.out != "-":
        for summary in summaries:
            _print_summary(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
