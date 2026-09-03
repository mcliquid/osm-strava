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

Last measured Mallorca snapshot (fresh OSM extract, no task database):

| Mode | Accepted | GeoJSON |
|---|---:|---:|
| Baseline (no suppression) | 171 | 171 |
| `--suppress-parallel-osm` `--suppress-ferry` | 171 | 51 |
| those plus `--suppress-heat-halo` | 171 | 15 |

`accepted` means the candidate passed the normal detector (size and requested-area
tests). Optional suppression may omit it from GeoJSON afterwards; diagnostics
still record `accepted=true`.

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

`--strava-tiles strava` fetches authenticated Global Heatmap tiles. `-c`
selects the heatmap sport layer (verified against `content-a.strava.com`):

| `-c` | Heatmap layer | Notes |
|---|---|---|
| `ride` | `sport_Ride` | Mallorca production baseline |
| `all` | `all` | Combined heatmap. `sport_All` returns HTTP 400 |
| `run` | `sport_Run` | Public `-c run` mapping. Distinct from the raw `run` layer |

Existing `-c ride --strava-tiles strava` commands are unchanged. Cache paths
include the activity name, so Ride and All never share tiles:

    cache/strava/ride/strava/<z>/<x>/<y>.png
    cache/strava/all/strava/<z>/<x>/<y>.png
    cache/strava/run/strava/<z>/<x>/<y>.png   # -c run → sport_Run

`--strava-heatmap-layer` is experimental and selects the raw path segment
(`sport_Ride`, `all`, `sport_Run`, `run`). `sport_Run` and `run` are different
heatmaps. An override that differs from the `-c` default uses a separate cache
stem (`run__run` for `-c run --strava-heatmap-layer run`).

Authentication:

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

With optional suppressors and diagnostics:

```
python strava.py -c ride --strava-tiles strava -z 14 `
  -a boundaries/mallorca.geojson `
  --osm-file osm-data/mallorca/current.osm `
  -g output.geojson `
  --diagnostics diagnostics.csv `
  --stats-json run-stats.json `
  --suppress-parallel-osm `
  --suppress-ferry `
  --suppress-heat-halo `
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
| `-c` / `--activity` | Activity (`run` default). Direct Strava tiles: `ride`, `all`, or `run` |
| `--strava-tiles` | `freemap` (default), `nakarte`, or `strava` |
| `--strava-heatmap-layer` | Experimental raw direct-heatmap layer (`sport_Ride`, `all`, `sport_Run`, `run`) |
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
| `--suppress-heat-halo` | Opt-in lateral heat-halo suppression (default off) |
| `--suppress-golf` | Opt-in All-layer golf stays-inside suppression (default off) |

Area polygons can be downloaded from [OSM-Boundaries](https://osm-boundaries.com/).
Example files live in `boundaries/`. Candidate output is clipped to the polygon
itself, not its bounding box.

The intensity colour scale is shown in `docs/palette.png`.

## Optional suppression

These flags are **opt-in** and default **off**. They run after normal detection
on accepted in-area candidates only. Flood-fill and task IDs are unchanged. A
candidate can match more than one rule; it is counted once in the GeoJSON union.
Each rule still increments its own counter.

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

### `--suppress-heat-halo`

Omits accepted candidates whose leftover heatmap sits in a warm lateral halo
of a nearby parallel OSM way, rather than in an independent corridor:

    between_heat_ratio >= 1.0

`between_heat_ratio` is the same diagnostic metric: median unmasked heatmap
along the segment from the candidate centroid to the nearest point on the
best parallel OSM way, divided by the candidate component mean. Blank values
do not match. This does **not** change the 35 m mask, thresholds, or
candidate IDs. Diagnostics column: `suppressed_heat_halo`.

Do not use `>= 0.8`: that threshold has no extra Not_An_Issue yield and
removed a Too_Hard control. Cover-only and `heat_ratio_p90`-only rules are
unsafe.

Validated on MapRoulette challenge 56715 (47 leftover Not_An_Issue after the
existing production filters): `>= 1.0` removes 36/47 NAI. The reconstructed
Fixed control `14/8308/6230/389/806` (`between_heat_ratio = 0`) and all four
Too_Hard controls survive.

### `--suppress-golf`

Opt-in, default **off**. Applies **only** to the combined All heatmap layer
(`-c all` / heatmap path `all`). It does **not** run on `sport_Ride`,
`sport_Run`, or `run`.

Omits accepted leftover heat that stays almost entirely inside an OSM
`leisure=golf_course` polygon:

    center inside golf_course
    AND component_inside_frac >= 0.90
    AND the component does not cross the golf-course boundary

This is the validated `stays_inside golf_course` predicate from the complete
Mallorca All-only review (292 tasks). It is **not** a generic golf-course OSM
filter: candidates merely near a course, that only touch it, or that cross
its boundary are kept (useful missing connections).

Mallorca All-only ground truth: 35/67 Not_An_Issue, 1/219 positives
(`14/8319/6228/191/310`, accepted trade-off). Crossing Fixed
`14/8320/6233/446/681` survives.

Diagnostics column: `suppressed_golf`. Stats: `golf_suppressed` (predicate
matches) and `golf_additional_suppressed` (GeoJSON removals not already
caught by a previous enabled rule).

### Combined statistics

With `--suppress-parallel-osm` and `--suppress-ferry` (Mallorca, measured):

- parallel 119
- ferry 1
- overlap 0
- union suppressed 120
- GeoJSON 51

Adding `--suppress-heat-halo` on the same extract:

- heat-halo matches 150 (predicate; many already match parallel)
- heat-halo additional suppressed 36 (would still have reached GeoJSON after parallel/ferry)
- union suppressed 156
- GeoJSON 15

On challenge 56715 labels, those 36 extra GeoJSON omissions are exactly
36/47 leftover Not_An_Issue. Parallel and ferry counts are unchanged.

Summary fields:

- raw detections
- rejected (too small)
- rejected (outside area)
- accepted (passed size and requested-area tests; before optional suppression)
- suppressed parallel to OSM
- suppressed ferry traces
- suppressed heat-halo traces (predicate matches)
- heat-halo additional suppressed (GeoJSON removals not already caught by a previous enabled rule)
- suppression overlap
- total suppressed
- GeoJSON features (after suppression and `-b` exclusions)

Known Mallorca controls that must remain in GeoJSON with the existing
parallel and ferry flags (and with `--suppress-heat-halo` when still detected):

- `14/8305/6233/875/927`
- `14/8306/6225/102/938`

`14/8308/6230/389/806` is mapped in current OSM, so it is no longer detected.
Its reconstructed `between_heat_ratio` is 0 and would not match heat-halo.

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
    sources/*-working.osm.pbf        # unclipped --fresh working copy
    sources/*-working.state          # last replication timestamps / diff count
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

`--force` rebuilds the extract and XML even if the source and boundary have
not changed.

### Geofabrik daily vs `--fresh`

**Default** (`.\update-osm.ps1 mallorca`) uses the Geofabrik `*-latest.osm.pbf`
file as-is. That extract is typically about a day behind OSM.

Geofabrik also publishes regional `.osc.gz` files under the extract's
`osmosis_replication_base_url`. Those follow the Geofabrik generation cycle,
so they are **not** true minutely updates.

**`--fresh`** is opt-in. It keeps an *unclipped* working copy of the Geofabrik
bootstrap (`…-working.osm.pbf`) and advances it with official
[planet.openstreetmap.org](https://planet.openstreetmap.org/replication/minute/)
replication (`osmupdate`, no polygon clip). The detector file is still produced
afterwards with `osmium extract --strategy=complete_ways`. Official planet diffs
must not be applied to an already clipped Mallorca/Bodenseekreis detector
extract.

Planet diffs are global. Applied without clipping they add out-of-region objects
to the working copy; `complete_ways` drops those again at extract time. Do
**not** clip the diffs with `osmupdate -B` — that was the old missing-node-ref
failure. If a `check-refs` run fails, the previous detector files are kept.

The working copy grows over time because it accumulates worldwide creates. A
newer Geofabrik extract automatically re-bootstraps it. `--bootstrap` (with
`--fresh`) forces that rebuild when you want a clean regional base.

```
.\update-osm.ps1 mallorca --fresh
.\update-osm.ps1 mallorca --fresh --bootstrap
```

A second `--fresh` run only downloads newly published planet diffs. osmupdate
keeps those diffs under `osm-data/.update-tmp/osmupdate/`.

The updater:

1. HEAD-checks the Geofabrik URL (ETag / Last-Modified / Content-Length)
2. skips the Geofabrik download when the local source already matches
3. in `--fresh` mode, copies/updates the unclipped working PBF from planet
   replication, then still extracts with `complete_ways`
4. skips extract and XML conversion when the derived files already match
   that input plus the current boundary
5. otherwise extracts and converts from a temp file
6. runs `osmium check-refs` on the extract (fails on missing way→node refs)
7. checks that the GeoJSON boundary lies in the region safety window and
   that the extract overlaps that boundary
8. replaces `current.osm.pbf` / `current.osm` only after validation

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
- suppression status (`suppressed_parallel_osm`, `suppressed_ferry`, `suppressed_heat_halo`, `suppressed_golf`, `written_to_geojson`)

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
