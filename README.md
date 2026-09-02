# osm-strava

Detect missing OpenStreetMap ways from [Strava](https://www.strava.com/) heatmap
tiles and write a GeoJSON file suitable for a
[MapRoulette](https://maproulette.org/) challenge.

The detector compares heatmap traces against a local OSM extract (or Overpass),
masks pixels that already follow OSM objects, and emits point tasks for leftover
traces that pass size, intensity, and requested-area checks.

The GeoJSON given to `-a` is authoritative for **candidate inclusion**. Tiles that
cross the boundary are still processed, but a candidate point outside the polygon
(including holes) is not accepted and is not written to GeoJSON. Boundary points
are kept.

`highway=construction` is treated as **existing mapped geometry** and is masked at
the normal OSM distance (default 35 m). osm-strava detects missing geometry; a
construction way is already mapped. This is not a suppression rule and does not
include `highway=proposed`.

License: [GNU GPL v3](LICENSE).

## Current Mallorca setup

Validated production usage for Mallorca:

- activity: `ride`
- tiles: direct Strava Global Heatmap (`--strava-tiles strava`)
- zoom: `14`
- OSM mask distance: **35 m** (`-d`, default)
- local OSM XML: `osm-data/mallorca/current.osm`

Optional false-positive filters are **off by default**. They do not change the
35 m mask, threshold, or `min_size`.

Last measured Mallorca snapshot (same OSM extract, no task database):

| Mode | Accepted | GeoJSON |
|---|---:|---:|
| Baseline (no suppression) | 189 | 189 |
| `--suppress-parallel-osm` | 189 | 63 |
| `--suppress-ferry` | 189 | 175 |
| both flags | 189 | 54 |

`accepted` means the candidate passed the normal detector (size and requested-area
tests). Optional suppression may omit it from GeoJSON afterwards; diagnostics
still record `accepted=true`. The Mallorca counts below predate the area-boundary
and `highway=construction` masking fixes and may change on a rerun.

## Requirements

Python 3 with:

- [Pillow](https://pypi.org/project/Pillow/)
- [numpy](https://numpy.org/)
- [shapely](https://pypi.org/project/shapely/)
- [requests](https://pypi.org/project/requests/)

Windows / PowerShell:

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install pillow numpy shapely requests
```

Tiles are cached under `cache/strava/` in this repository (not `/var/cache`).

### Direct Strava tiles

`--strava-tiles strava` currently supports **`-c ride` only**. Authentication:

- environment variable `STRAVA_COOKIE`, or
- file `.strava-cookie` in the repository root

Never commit Strava cookies. `.strava-cookie` is gitignored.

### Local OSM updates (optional, WSL)

`update-osm.ps1` / `update-osm.sh` refresh detector extracts from Geofabrik.
In WSL you need:

- `osmium-tool` (`osmium`)
- `curl`
- `python3`

## Usage

Baseline Mallorca run (no suppression):

```
python strava.py -c ride --strava-tiles strava -z 14 `
  -a boundaries/mallorca.geojson `
  --osm-file osm-data/mallorca/current.osm `
  -g output.geojson
```

With both optional suppressors and diagnostics:

```
python strava.py -c ride --strava-tiles strava -z 14 `
  -a boundaries/mallorca.geojson `
  --osm-file osm-data/mallorca/current.osm `
  -g output.geojson `
  --diagnostics diagnostics.csv `
  --stats-json run-stats.json `
  --suppress-parallel-osm `
  --suppress-ferry `
  -v
```

Single-tile debug (instead of `-a`):

```
python strava.py -c ride --strava-tiles strava -z 14 -x 8305 -y 6233 `
  --osm-file osm-data/mallorca/current.osm -g tile.geojson
```

Without `--osm-file`, OSM is loaded from Overpass (cached under `cache/`).
`--refresh-osm` ignores that cache.

### CLI options

From `strava.py --help` (defaults in parentheses):

| Option | Meaning |
|---|---|
| `-a` / `--area` | Area GeoJSON (polygon/multipolygon). Tiles may overlap the boundary; only in-area candidate points are emitted |
| `-x` / `-y` | Single Strava tile coordinates (debug; both required) |
| `-c` / `--activity` | Activity (`run` default). Direct Strava tiles: `ride` only |
| `--strava-tiles` | `freemap` (default), `nakarte`, or `strava` |
| `-z` / `--zoom` | Tile zoom 10–15 (`15` default). Mallorca uses `14` |
| `-m` / `--minlevel` | Intensity threshold 0–255 (`100`) |
| `-s` / `--size` | Minimum component size in pixels (`20`) |
| `-d` / `--distance` | OSM mask buffer in metres (`35`). Includes `highway=construction` |
| `-g` / `--geojson` | Output GeoJSON (GeoJSONSeq with RS). stdout if omitted |
| `-o` / `--offset` | Tile offset 0–3 (skip contiguous tiles; see workflow) |
| `-b` / `--tasks_db` | Read-only SQLite of already processed MapRoulette tasks |
| `-v` / `--verbose` | Extra progress |
| `-q` / `--quiet` | No progress dots |
| `--debug` | Debug images / extra logs |
| `--osm-file` | Local OSM XML instead of Overpass |
| `--overpass-url` | Overpass interpreter URL |
| `--refresh-osm` | Redownload Overpass cache |
| `--stats-json` | Write run statistics JSON |
| `--diagnostics` | Optional per-candidate CSV (does not change detection) |
| `--suppress-parallel-osm` | Opt-in parallel-OSM suppression (default off) |
| `--suppress-ferry` | Opt-in ferry-candidate suppression (default off) |

Area polygons can be downloaded from [OSM-Boundaries](https://osm-boundaries.com/).
Example files live in `boundaries/`. Candidate output is clipped to the polygon
itself, not its bounding box.

The intensity colour scale is shown in `docs/palette.png`.

## Optional suppression

Both flags are **opt-in** and default **off**. They run after normal detection
on accepted in-area candidates only. Flood-fill and task IDs are unchanged. A
candidate can match both rules; it is counted once in the GeoJSON union.

### `--suppress-parallel-osm`

Omits accepted candidates whose heatmap component follows existing OSM:

    follow100 >= 0.70 AND parallel15 >= 0.70

This targets GPS scatter running parallel to mapped ways. It does **not** change
the 35 m mask. Diagnostics column: `suppressed_parallel_osm`.

Mallorca (measured): 126 parallel matches, GeoJSON 63.

### `--suppress-ferry`

Candidate-level rule on the peak location:

    nearest_ferry_distance_m is populated AND nearest_ferry_distance_m <= 500

This does **not** enlarge the OSM mask around `route=ferry` to 500 m.
Diagnostics column: `suppressed_ferry`.

Mallorca (measured): 14 ferry matches, GeoJSON 175 if used alone.

### Combined statistics

With both flags, Mallorca measured:

- parallel 126
- ferry 14
- overlap 5
- union suppressed 135
- GeoJSON 54

Summary fields:

- raw detections
- rejected (too small)
- rejected (outside area)
- accepted (passed size and requested-area tests; before optional suppression)
- suppressed parallel to OSM
- suppressed ferry traces
- suppression overlap
- total suppressed
- GeoJSON features (after suppression and `-b` exclusions)

Known Mallorca controls that must remain in GeoJSON with both flags:

- `14/8305/6233/875/927`
- `14/8308/6230/389/806`
- `14/8306/6225/102/938`

## Local OSM data (`osm-data/`)

Geofabrik `*-latest.osm.pbf` files are downloaded (with HTTP conditional
requests), then clipped to the detector boundary with:

```
osmium extract --strategy=complete_ways --polygon boundaries/<region>.geojson
```

`complete_ways` keeps every node referenced by a way that intersects the
area, so the detector XML is referentially complete at the boundary. Ferry
routes and other long ways that touch the polygon may extend the *data*
bounding box well beyond the island or district. The configured safety
window therefore applies to the requested GeoJSON boundary, not to every
complete-way node. The old `osmupdate -B` clip left tens of thousands of
missing node references.

Geofabrik extracts are typically about a day behind OSM, not minutely like
Planet replication or Overpass.

```
osm-data/
    sources/                         # Geofabrik *-latest.osm.pbf (gitignored)
    mallorca/current.osm.pbf         # detector extract
    mallorca/current.osm             # XML for --osm-file
    mallorca/backups/                # previous extracts (limited)
    bodenseekreis/current.osm.pbf
    bodenseekreis/current.osm
    islas-baleares.poly              # leftover from the old osmupdate workflow
```

Regions are defined in `osm-regions.conf`. From PowerShell (any working
directory):

```
.\update-osm.ps1 mallorca
.\update-osm.ps1 bodenseekreis
.\update-osm.ps1 --list
.\update-osm.ps1 --show-config mallorca
```

Equivalent in WSL / Git Bash:

```
./update-osm.sh mallorca
./update-osm.sh bodenseekreis
```

`--force` rebuilds the extract and XML even if the Geofabrik source is
unchanged.

The updater:

1. HEAD-checks the Geofabrik URL (ETag / Last-Modified / Content-Length)
2. skips the download when the local source already matches
3. skips extract and XML conversion when the derived files already match
   that source plus the current boundary
4. otherwise downloads to a temp file, validates, then extracts and converts
5. runs `osmium check-refs` on the extract (fails on missing way→node refs)
6. checks that the GeoJSON boundary lies in the region safety window and
   that the extract overlaps that boundary
7. replaces `current.osm.pbf` / `current.osm` only after validation

Use the generated XML with `strava.py`:

```
python strava.py -c ride --strava-tiles strava -z 14 `
  -a boundaries/mallorca.geojson `
  --osm-file osm-data/mallorca/current.osm `
  -g output.geojson
```

```
python strava.py -c ride --strava-tiles strava -z 14 `
  -a boundaries/bodenseekreis.geojson `
  --osm-file osm-data/bodenseekreis/current.osm `
  -g output.geojson
```

**Migration:** previous Mallorca commands used
`osm-data/islas-baleares-current.osm`. That file is not deleted automatically.
Point `--osm-file` at `osm-data/mallorca/current.osm` after the first
`.\update-osm.ps1 mallorca` run. The leftover
`osm-data/bodenseekreis-current.osm` is likewise unused by the new layout.

The first Bodenseekreis Geofabrik source is Regierungsbezirk Tübingen
(~120 MB). The updater will not fetch it until you run that region.

## Diagnostics and research

`--diagnostics FILE` writes a CSV for calibration. It does **not** change
detection unless a suppression flag is also enabled.

`analyze_diagnostics.py` is offline validation. Join MapRoulette exports with
`candidate_id == TaskName`. See [validation/README.md](validation/README.md).

Main diagnostic groups (not a full column list):

- Strava component metrics (pixels, intensity, elongation)
- nearest OSM object and distances
- nearest ferry / construction
- follow / parallel geometry vs local OSM
- area membership (`inside_area`)
- suppression status (`suppressed_parallel_osm`, `suppressed_ferry`, `written_to_geojson`)

Generated analysis CSV/GeoJSON files are gitignored.

## MapRoulette workflow

This is iterative. After a challenge round, rerun with a task database so
already reviewed “Not an Issue” / “Too Hard” tasks are not written again.

### Simple workflow

1. Download an area boundary (or use `boundaries/`).
2. Run `strava.py`.
3. Create a MapRoulette challenge and import the GeoJSON.
4. Wait until the challenge is finished.
5. Export challenge data as CSV
   ([Exporting Challenge Data](https://learn.maproulette.org/documentation/exporting-challenge-data/)).
6. Convert to SQLite:

   `sqlite3 tasks.sqlite ".import --csv project_XXXXX_tasks.csv tasks" "create index tasks_idx on tasks(TaskName);"`

7. Rerun `strava.py` with `-b tasks.sqlite`.
8. Rebuild the challenge to add new tasks.

Repeat until too many remaining tasks are “Not an Issue”.

`-b` is read-only. Statuses `Not_an_Issue` and `Too_Hard` are omitted from
GeoJSON. `Fixed` / `Already_Fixed` that still match the heatmap produce a
warning (the way may not actually be in OSM yet).

### Offset workflow

`-o 0..3` processes every other tile so adjacent heatmap tiles are not handled
in the same round. Run offset 0, then 1, 2, 3, importing each GeoJSON before the
next round. Combine with `-b` on later passes.

## Other files

- `strava.py` — current detector
- `diagnostics.py` — shared geometry / diagnostics / suppression helpers
- `analyze_diagnostics.py` — offline rule analysis
- `strava-ride.py` — older ride-oriented script (Overpass only; not the current detector)
- `update-osm.ps1` / `update-osm.sh` — Geofabrik OSM extract updater
- `osm-regions.conf` — region URLs, boundaries, and output paths
