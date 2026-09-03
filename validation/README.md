# Validation and diagnostics (research only)

This directory is for **offline** review of detector output. Nothing here changes
detection, masking, or GeoJSON geometry.

## MapRoulette join

Export challenge tasks from MapRoulette as CSV. Join on:

    diagnostics.candidate_id == maproulette.TaskName

Place exports here if you want them next to the notes, for example
`challenge_56700_tasks.csv`. Those files are **gitignored**. They are useful
research/validation data, not part of normal runtime source.

## Tools

- `diagnostics.py` — shared component geometry used by optional `--diagnostics`
  and by the opt-in suppression flags.
- `analyze_diagnostics.py` — read-only analysis of a diagnostics CSV plus a
  MapRoulette export. It does not modify `strava.py` or production rules.

Example:

```
python analyze_diagnostics.py diagnostics-mallorca-filtered-v2.csv validation/challenge_56700_tasks.csv
```

Generated `analysis-*`, `remaining-*`, `rule-results.csv`, and similar files are
gitignored.

## Mallorca heatmap-layer review samples

Local review files (gitignored), built from FINAL Mallorca candidates after
parallel, ferry, and heat-halo suppression. Spatial matching is 25 m on
center coordinates, not `candidate_id`.

```
python validation/sample_heatmap_review.py
```

- `run-sample.geojson` / `run-sample.csv` — ~50 `sport_Run` finals with no Ride final within 25 m
- `all-only-sample.geojson` / `all-only-sample.csv` — ~50 All finals with no Ride and no `sport_Run` final within 25 m

Sampling is deterministic: per OSM class, farthest-point from the westernmost
candidate, tie-broken by `candidate_id`. These files are for manual review only.
Do not treat them as a MapRoulette challenge.

## Open-area diagnostics (research)

`--diagnostics` can record OSM **area** context beside the usual way metrics
(`open_area_class`, `open_area_center_inside`, `open_area_component_inside_frac`,
`open_area_component_crosses_boundary`, `open_area_component_stays_inside`, …).
Those objects are not part of the heatmap mask. Optional `--suppress-golf` uses
the same `stays_inside leisure=golf_course` geometry (All layer only).

Offline review of the Mallorca samples:

```
python validation/analyze_open_area.py
```

Writes `validation/open-area-analysis.md`.

## Production Remaining

When the diagnostics CSV comes from a run with suppression flags enabled,
Remaining candidates are:

    accepted == true AND written_to_geojson == true

Do not reconstruct suppression from metric thresholds for that snapshot.
The CSV flags (`suppressed_parallel_osm`, `suppressed_ferry`,
`suppressed_heat_halo`, `suppressed_golf`, `written_to_geojson`) are authoritative.
