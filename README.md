# spc-redbook

Convert [SPC Storm Prediction Center](https://www.spc.noaa.gov/) convective
outlook GIS data into **AWIPS Redbook graphics** that an EDEX server can
ingest.

The converter is written for Python 3.8+ and uses only the Python standard
library.  It runs directly on an AWIPS/EDEX host without `pip`, GDAL, Fiona,
or Shapely, and reads shapefiles, zipped shapefiles, GeoJSON, directories, and
HTTP(S) URLs.

## What it produces

One Redbook (`.rbk`) graphics file per outlook layer.  CAVE renders the file
using the Redbook display resource configured on the EDEX server.  The
products contain:

* outline rings for categorical, probabilistic, and significant-severe areas;
* short labels such as `TSTM`, `SLGT`, `30%`, or `SIG`;
* diagonal hatching for significant-severe areas;
* a text legend with the outlook day, issue/valid/expire times, and AWIPS ID.

Redbook vectors and text are drawn in a single resource colour and the AWIPS
renderer ignores fill colours and fill patterns, so this is a monochrome,
outline-and-hatch representation rather than a colour-filled map.

## Install

From this directory:

```bash
python3 -m pip install .
```

or run it without installing:

```bash
python3 -m spc_redbook --help
```

The installed command is `spc-redbook`.

## Usage

Convert the current day 1 categorical, tornado, hail, and wind outlooks:

```bash
spc-redbook https://www.spc.noaa.gov/products/outlook/day1otlk-shp.zip \
    --layer all --out redbook
```

Convert an archived issuance to one explicit file:

```bash
spc-redbook day1otlk_20230331_1630-shp.zip \
    --layer cat --out /tmp/PGWE46_cat.rbk
```

Read a single GeoJSON layer:

```bash
spc-redbook day1otlk_cat.nolyr.geojson --day 1 --layer cat --out out/
```

List the physical layers in an input and exit:

```bash
spc-redbook day2otlk-shp.zip --list-layers
```

Write to stdout:

```bash
spc-redbook day1otlk-shp.zip --layer cat --out - > PGWE46_cat.rbk
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--layer`, `-l` | Layer or comma-separated layers, or `all` |
| `--out`, `-o` | Directory, explicit `.rbk`/`.redbook` file, or `-` |
| `--day` | Outlook day (1, 2, or 3); otherwise inferred from the input name |
| `--wmo` | Override the six-character WMO/AWIPS ID (single layer only) |
| `--width`, `--height` | Redbook plot-space size (default 1024x768) |
| `--bounds W,S,E,N` | Fixed map bounds instead of fitting the data |
| `--margin` | Extra margin around the data, in degrees |
| `--central-lon`, `--lat-ts` | Projection parameters (default -95, 90) |
| `--hatch-spacing` | Hatching interval in grid pixels |
| `--no-hatch`, `--no-labels`, `--no-legend` | Disable styling pieces |
| `--no-verify` | Skip the built-in encoder/decoder round-trip check |
| `--quiet`, `-q` | Suppress the per-product summary |

## Layers

Physical SPC layers are discovered from file names (`..._cat.shp`,
`day1otlk_cat.nolyr.geojson`, and so on).  Logical aliases and merged products
are resolved by the converter:

| Layer | Meaning |
| --- | --- |
| `cat` | Categorical (`TSTM`/`MRGL`/`SLGT`/`ENH`/`MDT`/`HIGH`) |
| `torn`, `hail`, `wind` | Individual hazard probability areas (day 1) |
| `prob`, `severe` | All-severe probability; uses a physical `prob` layer when present, otherwise merges `torn`+`hail`+`wind` |
| `sig` | Significant-severe areas; uses separate `sig*` files when present, otherwise `LABEL=SIGN` areas from the hazard layers |
| `sigtorn`, `sighail`, `sigwind` | Significant areas for one hazard |
| `cigprob`, `cigtorn`, `cighail`, `cigwind` | Conditional-intensity-group passthrough layers |

`all` expands to `cat,torn,hail,wind` on day 1 and `cat,prob` on days 2 and 3.

## Product IDs

Official NWSI 10-512 Redbook identifiers are used by default:

| Day | Layer | WMO/AWIPS ID | Forecast hour |
| --- | --- | --- | --- |
| 1 | `cat` | `PGWE46` | none |
| 1 | `torn` | `PGNE00` | 24 |
| 1 | `hail` | `PENE00` | 24 |
| 1 | `wind` | `PWNE00` | 24 |
| 2 | `cat` | `PGWI47` | 48 |
| 2 | `prob` | `PGNI00` | 48 |
| 3 | `cat` | `PGWK48` | 72 |
| 3 | `prob` | `PZNK00` | 72 |

If a layer has no official ID, the day's categorical ID is used and a warning
is printed; override it with `--wmo`.  Output files are named
`<WMO>_<layer>.rbk`.

EDEX determines the forecast hour from the WMO ID (via
`redbookFcstMap.xml`), not from the product-ID block, so changing the WMO ID
with `--wmo` also changes the forecast-hour mapping.

## Ingest into EDEX

1. Generate the products on or near the EDEX host.
2. Copy the `.rbk` files into the EDEX ingest directory:

   ```bash
   cp PGWE46_cat.rbk /awips2/data_store/ingest/
   ```

3. Check the ingest log:

   ```bash
   edex log redbook
   ```

   The Redbook distributor accepts products whose WMO header matches
   `^[PQ]..... .... ......`, which all IDs in the table above do.

4. To give the products friendly names in CAVE, add entries to
   `/awips2/edex/data/utility/common_static/base/redbook/redbookMapping.xml`
   (or the site/configured equivalent) on the EDEX server.

For recurring generation, run the command from cron or a systemd timer.  A
typical schedule is shortly after each SPC issuance (about 06Z, 13Z, 16:30Z,
20Z, and 01Z for day 1; see NWSI 10-512 for the full schedule).

## How the output is built

The file is a WMO header line followed by the binary Redbook blocks:

```
001_001  product identification (originator, WMO ID, valid time)
002_005  version
004_016  plot space (m x n grid, valid/expire times)
004_017  north polar stereographic projection corners
001_004  plot parameters
004_005  vector rings (one or more blocks)
005_001  alphanumeric text (legend and labels)
001_002  end of product
```

Coordinates are stored as `(posX, posY)` on the plot-space grid, with `posY`
counting up from the bottom (GEMPAK device coordinates; CAVE converts it to a
top-down row with `row = n - posY`).  Subsequent points are delta-encoded in the short
(7-bit) or long (13-bit) AWIPS vector format.  A beam flag starts a new line or
ring.  Closed rings are rotated internally when necessary so that two rings
sharing a start/end point survive the beam encoding.

The default map projection is spherical north polar stereographic with
`R = 6371200 m`, `lat_ts = 90`, and `central_lon = -95`.  The projected
envelope is fitted to the data (or `--bounds`) and adjusted to the requested
pixel aspect ratio.

## Python API

```python
from spc_redbook import load_source, prepare_areas
from spc_redbook.pipeline import generate_product, write_product

source = load_source("day1otlk-shp.zip")
areas = prepare_areas(source.layer("cat"), "cat")

data, summary = generate_product(source, "cat", day=1)
path = write_product(data, "out", summary.wmo, summary.layer, single=True)
```

Lower-level helpers are also exported: `build_redbook`, `parse_redbook`,
`verify_redbook`, `GridSpec`, and `NorthPolarStereo`.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The suite covers DBF deletion flags and field parsing, shapefile/GeoJSON
readers, projection and grid round-trips, Redbook block framing, vector
encode/decode geometry (including shared ring endpoints), outlook labels and
hatching, product-ID mapping, output naming, and the CLI.

## Limitations

* Redbook graphics are monochrome in CAVE; categorical probability fills are
  represented by nested outlines and labels rather than colours.
* Only polygon/line shape records are read.  Point/multipoint records are
  ignored.
* The built-in reader expects dBASE III/IV `.dbf` attributes and does not use
  `.prj` files; the projection is fixed to AWIPS' north polar stereographic
  parameters.
* The encoder deliberately writes the 28-byte product-ID payload used by real
  NOAA/GEMPAK files, not the inconsistent length test in AWIPS'
  `ProductIdBlock` parser.
